"""Інтеграція з Work.ua: підтягує відгуки на вакансію і створює кандидатів у CRM.

Публічного документованого API для роботодавців у Work.ua немає; ATS-системи
(PeopleForce, HURMA тощо) працюють через партнерський доступ, який видає Work.ua.
Тому модуль поділено на дві частини:

* `WorkUaClient` — «транспорт», який вміє повернути список відгуків на вакансію.
  Конкретну реалізацію (`WorkUaApiClient`) треба дописати, коли Work.ua надасть
  документацію та доступ до API. До того часу використовується `DisabledClient`.
* `sync_vacancy()` — незалежна від транспорту логіка: дедуплікація кандидатів,
  створення записів у CRM, історія. Вона вже готова і покрита тестами.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Protocol

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import Application, Candidate, Vacancy
from ..services import attach_candidate, find_existing_candidate

logger = logging.getLogger(__name__)


class WorkUaError(Exception):
    """Загальна помилка інтеграції."""


class WorkUaNotConfigured(WorkUaError):
    """Інтеграцію не налаштовано (немає доступу до API)."""


@dataclass(frozen=True)
class WorkUaResponse:
    """Один відгук кандидата на вакансію, у форматі, незалежному від API."""

    response_id: str
    full_name: str
    phone: str = ""
    email: str = ""
    city: str = ""
    birth_year: int | None = None
    resume_id: str = ""
    resume_url: str = ""
    resume_text: str = ""
    cover_letter: str = ""
    applied_at: datetime | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)


class WorkUaClient(Protocol):
    def list_responses(self, job_id: str, since: datetime | None = None) -> Iterable[WorkUaResponse]:
        """Повертає відгуки на вакансію `job_id` (опційно — лише новіші за `since`)."""


class DisabledClient:
    def list_responses(self, job_id: str, since: datetime | None = None) -> Iterable[WorkUaResponse]:
        raise WorkUaNotConfigured(
            "Інтеграцію з Work.ua ще не підключено. Потрібен партнерський доступ до API Work.ua "
            "(див. README, розділ «Інтеграція з Work.ua»)."
        )


class WorkUaApiClient:
    """Клієнт партнерського API Work.ua.

    TODO: реалізувати після отримання документації від Work.ua. Метод має
    перетворювати відповідь API на список `WorkUaResponse` (див. поля вище).
    Облікові дані беруться з settings.WORKUA (змінні WORKUA_*).
    """

    def __init__(self, base_url: str, login: str, password: str, timeout: int = 20):
        if not (base_url and login and password):
            raise WorkUaNotConfigured(
                "Не задано WORKUA_API_BASE_URL / WORKUA_LOGIN / WORKUA_PASSWORD."
            )
        self.base_url = base_url.rstrip("/")
        self.login = login
        self.password = password
        self.timeout = timeout

    def list_responses(self, job_id: str, since: datetime | None = None) -> Iterable[WorkUaResponse]:
        raise WorkUaNotConfigured(
            "Клієнт API Work.ua ще не реалізовано: чекаємо документацію та доступ від Work.ua."
        )


def get_client() -> WorkUaClient:
    config = settings.WORKUA
    backend = config.get("BACKEND", "disabled")
    if backend == "api":
        return WorkUaApiClient(
            base_url=config["API_BASE_URL"],
            login=config["LOGIN"],
            password=config["PASSWORD"],
            timeout=config["TIMEOUT"],
        )
    return DisabledClient()


@dataclass
class SyncResult:
    total: int = 0
    created: int = 0
    already_present: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"Отримано відгуків: {self.total}, нових кандидатів на вакансії: {self.created}"
        if self.already_present:
            text += f", вже були: {self.already_present}"
        if self.errors:
            text += f", помилок: {len(self.errors)}"
        return text


def _upsert_candidate(response: WorkUaResponse) -> Candidate:
    candidate = find_existing_candidate(
        phone=response.phone, email=response.email, workua_resume_id=response.resume_id
    )
    if candidate is None:
        return Candidate.objects.create(
            full_name=response.full_name or "Без імені",
            phone=response.phone,
            email=response.email,
            city=response.city,
            birth_year=response.birth_year,
            source=Candidate.Source.WORKUA,
            workua_resume_id=response.resume_id,
            resume_url=response.resume_url,
            resume_text=response.resume_text,
        )
    # Доповнюємо порожні поля, але не перезаписуємо те, що рекрутер уже виправив руками.
    changed = []
    for attr in ("phone", "email", "city", "birth_year", "resume_url", "resume_text"):
        new_value = getattr(response, attr)
        if new_value and not getattr(candidate, attr):
            setattr(candidate, attr, new_value)
            changed.append(attr)
    if response.resume_id and not candidate.workua_resume_id:
        candidate.workua_resume_id = response.resume_id
        changed.append("workua_resume_id")
    if changed:
        candidate.save()
    return candidate


def sync_vacancy(vacancy: Vacancy, client: WorkUaClient | None = None, user=None) -> SyncResult:
    """Підтягує відгуки з Work.ua для однієї вакансії."""
    if not vacancy.workua_job_id:
        raise WorkUaError("У вакансії не вказано ID на Work.ua.")
    client = client or get_client()
    result = SyncResult()
    started_at = timezone.now()

    for response in client.list_responses(vacancy.workua_job_id, since=vacancy.workua_synced_at):
        result.total += 1
        if response.response_id and Application.objects.filter(
            workua_response_id=response.response_id
        ).exists():
            result.already_present += 1
            continue
        try:
            with transaction.atomic():
                candidate = _upsert_candidate(response)
                _, created = attach_candidate(
                    vacancy,
                    candidate,
                    source=Candidate.Source.WORKUA,
                    user=user,
                    workua_response_id=response.response_id,
                    cover_letter=response.cover_letter,
                    applied_at=response.applied_at,
                )
        except Exception as exc:  # одна погана анкета не має зупиняти всю синхронізацію
            logger.exception("Work.ua: не вдалося обробити відгук %s", response.response_id)
            result.errors.append(f"{response.response_id}: {exc}")
            continue
        if created:
            result.created += 1
        else:
            result.already_present += 1

    vacancy.workua_synced_at = started_at
    vacancy.save(update_fields=["workua_synced_at", "updated_at"])
    return result
