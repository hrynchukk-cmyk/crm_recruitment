import re
from django.utils import timezone

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
    latitude = models.DecimalField("широта", max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField("довгота", max_digits=9, decimal_places=6, null=True, blank=True)
    notes = models.TextField("нотатки", blank=True)
    created_at = models.DateTimeField("створено", auto_now_add=True)

    class Meta:
        verbose_name = "підприємство"
        verbose_name_plural = "підприємства"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def get_absolute_url(self) -> str:
        return reverse("recruiting:company_edit", args=[self.pk])

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class Vacancy(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Відкрита"
        PAUSED = "paused", "На паузі"
        CLOSED = "closed", "Закрита"

    company = models.ForeignKey(
        Company,
        verbose_name="підприємство",
        on_delete=models.PROTECT,
        related_name="vacancies",
        null=True,
        blank=True,
        help_text="Порожнє лише для вакансій, щойно імпортованих з Work.ua, — призначте підприємство.",
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
    workua_active = models.BooleanField("активна на Work.ua", null=True, blank=True)
    workua_expires_on = models.DateField("активна на Work.ua до", null=True, blank=True)
    workua_street_address = models.CharField("адреса з вакансії Work.ua", max_length=255, blank=True)
    workua_last_response_id = models.BigIntegerField(
        "останній отриманий відгук Work.ua", null=True, blank=True, editable=False
    )
    opened_on = models.DateField(
        "дата публікації",
        default=timezone.localdate,
        help_text="Коли вакансію опублікували (на Work.ua). Від неї рахуються «дні активності».",
    )
    closed_on = models.DateField("дата закриття", null=True, blank=True)
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
        return f"{self.title} — {self.company or 'підприємство не призначено'}"

    def get_absolute_url(self) -> str:
        return reverse("recruiting:vacancy_detail", args=[self.pk])

    @property
    def workua_url(self) -> str:
        return f"https://www.work.ua/jobs/{self.workua_job_id}/" if self.workua_job_id else ""

    def save(self, *args, **kwargs):
        # Дата закриття ставиться сама, коли вакансію закривають, і знімається, якщо відкрили знову.
        if self.status == self.Status.CLOSED and not self.closed_on:
            self.closed_on = timezone.localdate()
        elif self.status != self.Status.CLOSED:
            self.closed_on = None
        super().save(*args, **kwargs)

    @property
    def days_active(self) -> int:
        """Скільки днів вакансія активна (для закритої — скільки днів була відкрита)."""
        end = self.closed_on or timezone.localdate()
        return max((end - self.opened_on).days, 0)


class Candidate(models.Model):
    """Людина. Один кандидат може відгукуватися на кілька вакансій."""

    class Source(models.TextChoices):
        WORKUA = "workua", "Work.ua"
        MANUAL = "manual", "Вручну"
        REFERRAL = "referral", "Рекомендація"
        OTHER = "other", "Інше"

    class District(models.TextChoices):
        HALYTSKYI = "halytskyi", "Галицький"
        ZALIZNYCHNYI = "zaliznychnyi", "Залізничний"
        LYCHAKIVSKYI = "lychakivskyi", "Личаківський"
        SYKHIVSKYI = "sykhivskyi", "Сихівський"
        FRANKIVSKYI = "frankivskyi", "Франківський"
        SHEVCHENKIVSKYI = "shevchenkivskyi", "Шевченківський"
        REGION = "region", "Область / передмістя"

    full_name = models.CharField("ім'я", max_length=255)
    phone = models.CharField("телефон", max_length=50, blank=True, db_index=True)
    age = models.PositiveSmallIntegerField("вік", null=True, blank=True)
    district = models.CharField("район", max_length=20, choices=District, blank=True)
    email = models.EmailField("email", blank=True)
    city = models.CharField("місто", max_length=100, blank=True)
    source = models.CharField("джерело", max_length=20, choices=Source, default=Source.MANUAL)
    workua_resume_id = models.CharField("ID резюме на Work.ua", max_length=32, blank=True)
    workua_candidate_id = models.CharField("ID кандидата на Work.ua", max_length=32, blank=True)
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
            ),
            models.UniqueConstraint(
                fields=["workua_candidate_id"],
                condition=~models.Q(workua_candidate_id=""),
                name="unique_candidate_workua_candidate_id",
            ),
        ]

    def __str__(self) -> str:
        return self.full_name

    def save(self, *args, **kwargs):
        self.phone = normalize_phone(self.phone)
        if not self.workua_resume_id:
            self.workua_resume_id = parse_workua_resume_id(self.resume_url)
        super().save(*args, **kwargs)

    @property
    def phone_display(self) -> str:
        """+380931818875 → 093 181-88-75 (як звикли записувати в таблиці)."""
        p = self.phone
        if len(p) == 13 and p.startswith("+380"):
            local = "0" + p[4:]
            return f"{local[:3]} {local[3:6]}-{local[6:8]}-{local[8:]}"
        return p

    @property
    def viber_url(self) -> str:
        digits = "".join(ch for ch in self.phone if ch.isdigit())
        return f"viber://chat?number=%2B{digits}" if digits else ""


_WORKUA_RESUME_RE = re.compile(r"work\.ua/(?:[a-z]{2}/)?resumes/(\d+)")
_WORKUA_APPLICANT_RE = re.compile(r"/my/applicants/(\d+)")
_WORKUA_RECOMMENDED_RE = re.compile(r"/my/recommended/(\d+)")


def parse_workua_resume_id(value: str) -> str:
    """Дістає ID резюме з посилання виду https://www.work.ua/resumes/1234567/."""
    if not value:
        return ""
    match = _WORKUA_RESUME_RE.search(value)
    return match.group(1) if match else ""


def parse_workua_link(value: str) -> tuple[str, str]:
    """Розпізнає посилання з Work.ua і повертає (розділ, ID).

    * …/my/applicants/<ID>  — відгук на вакансію        → ("response", ID)
    * …/my/recommended/<ID> — рекомендований кандидат   → ("recommended", ID)
    * …/resumes/<ID>        — резюме з бази (підібраний) → ("sourced", ID)
    Інакше — ("", "").
    """
    if not value:
        return "", ""
    for channel, regex in (
        ("response", _WORKUA_APPLICANT_RE),
        ("recommended", _WORKUA_RECOMMENDED_RE),
        ("sourced", _WORKUA_RESUME_RE),
    ):
        match = regex.search(value)
        if match:
            return channel, match.group(1)
    return "", ""


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
    """Кандидат на конкретній вакансії — саме тут живуть розділ і статус."""

    class Channel(models.TextChoices):
        """Розділ вакансії: як кандидат потрапив на цю посаду."""

        RESPONSE = "response", "Відгуки"
        RECOMMENDED = "recommended", "Рекомендовані Work.ua"
        SOURCED = "sourced", "Підібрані рекрутером"

    class Stage(models.TextChoices):
        NEW = "new", "Зателефонувати"
        NO_ANSWER = "no_answer", "Не додзвонилися"
        VIBER = "viber", "Написали у Viber"
        CALLBACK = "callback", "Передзвонити"
        THINKING = "thinking", "Думає"
        INTERVIEW = "interview", "Запрошено на співбесіду"
        NO_SHOW = "no_show", "Не прийшов на співбесіду"
        PASSED = "passed", "Пройшов співбесіду"
        HIRED = "hired", "Вийшов на роботу"
        RESERVE = "reserve", "Резерв"
        REJECTED_BY_US = "rejected_by_us", "Не підходить"
        REJECTED_BY_CANDIDATE = "rejected_by_candidate", "Не актуально"

    ACTIVE_STAGES = [
        Stage.NEW,
        Stage.NO_ANSWER,
        Stage.VIBER,
        Stage.CALLBACK,
        Stage.THINKING,
        Stage.INTERVIEW,
        Stage.NO_SHOW,
        Stage.PASSED,
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
    channel = models.CharField(
        "розділ", max_length=20, choices=Channel, default=Channel.SOURCED, db_index=True
    )
    stage = models.CharField("статус", max_length=30, choices=Stage, default=Stage.NEW, db_index=True)
    source = models.CharField(
        "джерело", max_length=20, choices=Candidate.Source, default=Candidate.Source.MANUAL
    )
    workua_response_id = models.CharField(
        "ID відгуку/рекомендації на Work.ua", max_length=64, blank=True
    )
    note = models.TextField(
        "примітка", blank=True, help_text="Коротко про кандидата: досвід, що уточнити (як у колонці «Примітка»)."
    )
    class WorkUaFrom(models.TextChoices):
        SEND = "send", "Відгук з резюме"
        PHONECALL = "phonecall", "Подзвонив за номером з вакансії"
        MESSENGER = "messenger", "Написав у месенджер"
        SAVED = "saved", "Збережений у кабінеті Work.ua"

    workua_from = models.CharField("як прийшов з Work.ua", max_length=12, choices=WorkUaFrom, blank=True)
    workua_has_file = models.BooleanField("є файл резюме на Work.ua", default=False)
    cover_letter = models.TextField("супровідний лист", blank=True)
    interview_at = models.DateTimeField("дата співбесіди", null=True, blank=True)
    applied_at = models.DateTimeField("дата відгуку / додавання", null=True, blank=True)
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
                fields=["vacancy", "workua_response_id"],
                condition=~models.Q(workua_response_id=""),
                name="unique_application_vacancy_workua_ref",
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


class WorkUaSyncState(models.Model):
    """Стан синхронізації з Work.ua (один запис)."""

    last_run_at = models.DateTimeField("останній запуск", null=True, blank=True)
    last_success_at = models.DateTimeField("остання успішна синхронізація", null=True, blank=True)
    last_summary = models.TextField("підсумок", blank=True)
    last_error = models.TextField("остання помилка", blank=True)
    # Відбиток логіна/пароля, з якими Work.ua відповів 401. Поки дані ті самі, автоматична
    # синхронізація не повторює спроби: після 10 невдалих входів Work.ua блокує доступ на добу.
    auth_failed_fingerprint = models.CharField(max_length=64, blank=True)

    class Meta:
        verbose_name = "стан синхронізації з Work.ua"
        verbose_name_plural = "стан синхронізації з Work.ua"

    def __str__(self) -> str:
        return "Синхронізація з Work.ua"

    @classmethod
    def get(cls) -> "WorkUaSyncState":
        state, _ = cls.objects.get_or_create(pk=1)
        return state
