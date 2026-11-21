from django.conf import settings
from django.db import models
from django.urls import reverse


class Company(models.Model):
    """Підприємство-замовник, для якого ми наймаємо працівників."""

    name = models.CharField("назва", max_length=255, unique=True)
    contact_person = models.CharField("контактна особа", max_length=255, blank=True)
    phone = models.CharField("телефон", max_length=50, blank=True)
    email = models.EmailField("email", blank=True)
    address = models.CharField("адреса", max_length=255, blank=True)
    notes = models.TextField("нотатки", blank=True)
    created_at = models.DateTimeField("створено", auto_now_add=True)

    class Meta:
        verbose_name = "підприємство"
        verbose_name_plural = "підприємства"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Vacancy(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Відкрита"
        PAUSED = "paused", "На паузі"
        CLOSED = "closed", "Закрита"

    company = models.ForeignKey(
        Company, verbose_name="підприємство", on_delete=models.PROTECT, related_name="vacancies"
    )
    title = models.CharField("посада", max_length=255)
    city = models.CharField("місто", max_length=100, default="Львів")
    salary = models.CharField("зарплата", max_length=100, blank=True)
    headcount = models.PositiveIntegerField("скільки людей потрібно", default=1)
    description = models.TextField("опис / вимоги", blank=True)
    status = models.CharField("статус", max_length=10, choices=Status, default=Status.OPEN)
    recruiter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="рекрутер",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vacancies",
    )
    workua_job_id = models.CharField(
        "ID вакансії на Work.ua",
        max_length=32,
        blank=True,
        help_text="Номер з посилання work.ua/jobs/<ID>/ — потрібен для синхронізації відгуків.",
    )
    workua_synced_at = models.DateTimeField("остання синхронізація з Work.ua", null=True, blank=True)
    created_at = models.DateTimeField("створено", auto_now_add=True)
    updated_at = models.DateTimeField("оновлено", auto_now=True)

    class Meta:
        verbose_name = "вакансія"
        verbose_name_plural = "вакансії"
        ordering = ["status", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workua_job_id"],
                condition=~models.Q(workua_job_id=""),
                name="unique_vacancy_workua_job_id",
            )
        ]

    def __str__(self) -> str:
        return f"{self.title} — {self.company}"

    def get_absolute_url(self) -> str:
        return reverse("recruiting:vacancy_detail", args=[self.pk])

    @property
    def workua_url(self) -> str:
        return f"https://www.work.ua/jobs/{self.workua_job_id}/" if self.workua_job_id else ""


class Candidate(models.Model):
    """Людина. Один кандидат може відгукуватися на кілька вакансій."""

    class Source(models.TextChoices):
        WORKUA = "workua", "Work.ua"
        MANUAL = "manual", "Вручну"
        REFERRAL = "referral", "Рекомендація"
        OTHER = "other", "Інше"

    full_name = models.CharField("ПІБ", max_length=255)
    phone = models.CharField("телефон", max_length=50, blank=True, db_index=True)
    email = models.EmailField("email", blank=True)
    city = models.CharField("місто", max_length=100, blank=True)
    birth_year = models.PositiveSmallIntegerField("рік народження", null=True, blank=True)
    source = models.CharField("джерело", max_length=20, choices=Source, default=Source.MANUAL)
    workua_resume_id = models.CharField("ID резюме на Work.ua", max_length=32, blank=True)
    resume_url = models.URLField("посилання на резюме", blank=True)
    resume_text = models.TextField("текст резюме", blank=True)
    notes = models.TextField("нотатки", blank=True)
    created_at = models.DateTimeField("створено", auto_now_add=True)
    updated_at = models.DateTimeField("оновлено", auto_now=True)

    class Meta:
        verbose_name = "кандидат"
        verbose_name_plural = "кандидати"
        ordering = ["full_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["workua_resume_id"],
                condition=~models.Q(workua_resume_id=""),
                name="unique_candidate_workua_resume_id",
            )
        ]

    def __str__(self) -> str:
        return self.full_name

    def save(self, *args, **kwargs):
        self.phone = normalize_phone(self.phone)
        super().save(*args, **kwargs)


def normalize_phone(phone: str) -> str:
    """Зводить український номер до вигляду +380XXXXXXXXX, щоб знаходити дублікати."""
    if not phone:
        return ""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    elif len(digits) == 9:
        digits = "380" + digits
    if len(digits) == 12 and digits.startswith("380"):
        return "+" + digits
    return phone.strip()


class Application(models.Model):
    """Кандидат на конкретній вакансії — саме тут живе статус."""

    class Stage(models.TextChoices):
        NEW = "new", "Новий"
        NO_ANSWER = "no_answer", "Не додзвонилися"
        CONTACTED = "contacted", "Зв'язалися"
        INTERVIEW = "interview", "Співбесіда призначена"
        SENT_TO_CLIENT = "sent_to_client", "Передано підприємству"
        OFFER = "offer", "Пропозиція / погодили"
        HIRED = "hired", "Вийшов на роботу"
        RESERVE = "reserve", "Резерв"
        REJECTED_BY_US = "rejected_by_us", "Відмова: не підходить"
        REJECTED_BY_CANDIDATE = "rejected_by_candidate", "Відмова: кандидат відмовився"

    ACTIVE_STAGES = [
        Stage.NEW,
        Stage.NO_ANSWER,
        Stage.CONTACTED,
        Stage.INTERVIEW,
        Stage.SENT_TO_CLIENT,
        Stage.OFFER,
    ]
    CLOSED_STAGES = [
        Stage.HIRED,
        Stage.RESERVE,
        Stage.REJECTED_BY_US,
        Stage.REJECTED_BY_CANDIDATE,
    ]

    vacancy = models.ForeignKey(
        Vacancy, verbose_name="вакансія", on_delete=models.CASCADE, related_name="applications"
    )
    candidate = models.ForeignKey(
        Candidate, verbose_name="кандидат", on_delete=models.CASCADE, related_name="applications"
    )
    stage = models.CharField("статус", max_length=30, choices=Stage, default=Stage.NEW, db_index=True)
    source = models.CharField(
        "джерело", max_length=20, choices=Candidate.Source, default=Candidate.Source.MANUAL
    )
    workua_response_id = models.CharField("ID відгуку на Work.ua", max_length=32, blank=True)
    cover_letter = models.TextField("супровідний лист", blank=True)
    interview_at = models.DateTimeField("дата співбесіди", null=True, blank=True)
    applied_at = models.DateTimeField("дата відгуку", null=True, blank=True)
    stage_changed_at = models.DateTimeField("статус змінено", auto_now_add=True)
    created_at = models.DateTimeField("додано", auto_now_add=True)
    updated_at = models.DateTimeField("оновлено", auto_now=True)

    class Meta:
        verbose_name = "кандидат на вакансії"
        verbose_name_plural = "кандидати на вакансіях"
        ordering = ["-applied_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["vacancy", "candidate"], name="unique_application"),
            models.UniqueConstraint(
                fields=["workua_response_id"],
                condition=~models.Q(workua_response_id=""),
                name="unique_application_workua_response_id",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.candidate} → {self.vacancy.title}"

    def get_absolute_url(self) -> str:
        return reverse("recruiting:application_detail", args=[self.pk])

    @property
    def is_active(self) -> bool:
        return self.stage in self.ACTIVE_STAGES


class ApplicationEvent(models.Model):
    """Історія: зміни статусу, нотатки, дзвінки."""

    class Kind(models.TextChoices):
        CREATED = "created", "Додано"
        STAGE = "stage", "Зміна статусу"
        NOTE = "note", "Нотатка"

    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name="events", verbose_name="кандидат на вакансії"
    )
    kind = models.CharField("тип", max_length=10, choices=Kind)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="автор", on_delete=models.SET_NULL, null=True, blank=True
    )
    from_stage = models.CharField("з", max_length=30, choices=Application.Stage, blank=True)
    to_stage = models.CharField("на", max_length=30, choices=Application.Stage, blank=True)
    text = models.TextField("текст", blank=True)
    created_at = models.DateTimeField("коли", auto_now_add=True)

    class Meta:
        verbose_name = "подія"
        verbose_name_plural = "історія"
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} ({self.created_at:%d.%m.%Y %H:%M})"
