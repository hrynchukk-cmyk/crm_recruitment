"""Бізнес-логіка CRM: зміна статусів, нотатки, додавання кандидатів.

Вʼюхи та синхронізація з Work.ua викликають ці функції, щоб історія
(ApplicationEvent) завжди писалася однаково.
"""

from django.db import transaction
from django.utils import timezone

from .models import Application, ApplicationEvent, Candidate, Vacancy, normalize_phone


@transaction.atomic
def change_stage(application: Application, new_stage: str, user=None, comment: str = "") -> bool:
    """Змінює статус і записує подію в історію. Повертає False, якщо статус той самий."""
    if new_stage not in Application.Stage.values:
        raise ValueError(f"Невідомий статус: {new_stage}")
    if application.stage == new_stage:
        if comment:
            add_note(application, comment, user)
        return False
    old_stage = application.stage
    application.stage = new_stage
    application.stage_changed_at = timezone.now()
    application.save(update_fields=["stage", "stage_changed_at", "updated_at"])
    ApplicationEvent.objects.create(
        application=application,
        kind=ApplicationEvent.Kind.STAGE,
        author=user,
        from_stage=old_stage,
        to_stage=new_stage,
        text=comment,
    )
    return True


def add_note(application: Application, text: str, user=None) -> ApplicationEvent:
    return ApplicationEvent.objects.create(
        application=application, kind=ApplicationEvent.Kind.NOTE, author=user, text=text.strip()
    )


def find_existing_candidate(*, phone: str = "", email: str = "", workua_resume_id: str = ""):
    """Шукає вже наявного кандидата, щоб не плодити дублікати."""
    if workua_resume_id:
        found = Candidate.objects.filter(workua_resume_id=workua_resume_id).first()
        if found:
            return found
    phone = normalize_phone(phone)
    if phone:
        found = Candidate.objects.filter(phone=phone).first()
        if found:
            return found
    if email:
        found = Candidate.objects.filter(email__iexact=email).first()
        if found:
            return found
    return None


@transaction.atomic
def attach_candidate(
    vacancy: Vacancy,
    candidate: Candidate,
    *,
    source: str = Candidate.Source.MANUAL,
    user=None,
    workua_response_id: str = "",
    cover_letter: str = "",
    applied_at=None,
) -> tuple[Application, bool]:
    """Додає кандидата на вакансію (якщо його там ще немає). Повертає (application, created)."""
    application, created = Application.objects.get_or_create(
        vacancy=vacancy,
        candidate=candidate,
        defaults={
            "source": source,
            "workua_response_id": workua_response_id,
            "cover_letter": cover_letter,
            "applied_at": applied_at or timezone.now(),
        },
    )
    if created:
        ApplicationEvent.objects.create(
            application=application,
            kind=ApplicationEvent.Kind.CREATED,
            author=user,
            to_stage=application.stage,
            text=f"Джерело: {application.get_source_display()}",
        )
    elif workua_response_id and not application.workua_response_id:
        application.workua_response_id = workua_response_id
        application.save(update_fields=["workua_response_id", "updated_at"])
    return application, created
