"""Інтеграція з Work.ua: підтягує кандидатів на вакансію в CRM.

З Work.ua на кожну вакансію надходять дві групи людей, і в CRM вони потрапляють
у різні розділи вакансії:

* **Відгуки**: люди, які самі відгукнулися на вакансію → `Application.Channel.RESPONSE`;
* **Рекомендовані**: кандидати, яких запропонувала платформа → `Application.Channel.RECOMMENDED`.

Третій розділ, «Підібрані рекрутером», рекрутер заповнює сам: вставляє в CRM
посилання на резюме з бази Work.ua (див. `views.vacancy_add_candidate`).

Публічного документованого API для роботодавців у Work.ua немає. ATS-системи
(PeopleForce, HURMA тощо) працюють через партнерський доступ, який видає Work.ua.
Тому модуль поділено на дві частини:

* `WorkUaClient` — «транспорт», який вміє повернути відгуки та рекомендованих
  кандидатів для вакансії. Конкретну реалізацію (`WorkUaApiClient`) треба дописати,
  коли Work.ua надасть документацію та доступ до API. До того часу використовується
  `DisabledClient`.
* `sync_vacancy()` — логіка, що не залежить від транспорту: дедуплікація кандидатів,
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
class WorkUaCandidate:
    """Кандидат із Work.ua (відгук або рекомендація) у форматі, що не залежить від API."""

    external_id: str  # ID відгуку або рекомендації; унікальний у межах Work.ua
    full_name: str
    channel: str = Application.Channel.RESPONSE
    phone: str = ""
    email: str = ""
    city: str = ""
    age: int | None = None
    district: str = ""
    resume_id: str = ""
    resume_url: str = ""
    resume_text: str = ""
    cover_letter: str = ""
    applied_at: datetime | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)


class WorkUaClient(Protocol):
    def list_responses(self, job_id: str, since: datetime | None = None) -> Iterable[WorkUaCandidate]:
        """Відгуки на вакансію `job_id` (опційно — лише новіші за `since`)."""

    def list_recommended(self, job_id: str) -> Iterable[WorkUaCandidate]:
        """Кандидати, яких Work.ua рекомендує для вакансії `job_id`."""


_NOT_CONNECTED = (
    "Інтеграцію з Work.ua ще не підключено. Потрібен партнерський доступ до API Work.ua "
    "(див. README, розділ «Інтеграція з Work.ua»)."
)


class DisabledClient:
    def list_responses(self, job_id: str, since: datetime | None = None) -> Iterable[WorkUaCandidate]:
        raise WorkUaNotConfigured(_NOT_CONNECTED)

    def list_recommended(self, job_id: str) -> Iterable[WorkUaCandidate]:
        raise WorkUaNotConfigured(_NOT_CONNECTED)


class WorkUaApiClient:
    """Клієнт партнерського API Work.ua.

    TODO: реалізувати після отримання документації від Work.ua. Обидва методи мають
    перетворювати відповідь API на `WorkUaCandidate` з відповідним `channel`.
    Облікові дані беруться з settings.WORKUA (змінні WORKUA_*).
    """

    def __init__(self, base_url: str, login: str, password: str, timeout: int = 20):
        if not (base_url and login and password):
            raise WorkUaNotConfigured("Не задано WORKUA_API_BASE_URL / WORKUA_LOGIN / WORKUA_PASSWORD.")
        self.base_url = base_url.rstrip("/")
        self.login = login
        self.password = password
        self.timeout = timeout

    def list_responses(self, job_id: str, since: datetime | None = None) -> Iterable[WorkUaCandidate]:
        raise WorkUaNotConfigured("Клієнт API Work.ua ще не реалізовано: чекаємо документацію та доступ.")

    def list_recommended(self, job_id: str) -> Iterable[WorkUaCandidate]:
        raise WorkUaNotConfigured("Клієнт API Work.ua ще не реалізовано: чекаємо документацію та доступ.")


def get_client() -> WorkUaClient:
    config = settings.WORKUA
    if config.get("BACKEND", "disabled") == "api":
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
    created_by_channel: dict[str, int] = field(default_factory=dict)
    already_present: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def created(self) -> int:
        return sum(self.created_by_channel.values())

    def summary(self) -> str:
        labels = dict(Application.Channel.choices)
        if self.created_by_channel:
            parts = ", ".join(f"{labels[ch]}: {n}" for ch, n in self.created_by_channel.items())
            text = f"Додано нових кандидатів: {self.created} ({parts})"
        else:
            text = "Нових кандидатів немає"
        if self.already_present:
            text += f". Вже були в CRM: {self.already_present}"
        if self.errors:
            text += f". Помилок: {len(self.errors)}"
        return text


def _upsert_candidate(item: WorkUaCandidate) -> Candidate:
    candidate = find_existing_candidate(phone=item.phone, email=item.email, workua_resume_id=item.resume_id)
    if candidate is None:
        return Candidate.objects.create(
            full_name=item.full_name or "Без імені",
            phone=item.phone,
            email=item.email,
            city=item.city,
            age=item.age,
            district=item.district,
            source=Candidate.Source.WORKUA,
            workua_resume_id=item.resume_id,
            resume_url=item.resume_url,
            resume_text=item.resume_text,
        )
    # Доповнюємо порожні поля, але не перезаписуємо те, що рекрутер уже виправив руками.
    changed = False
    for attr in ("phone", "email", "city", "age", "district", "resume_url", "resume_text"):
        new_value = getattr(item, attr)
        if new_value and not getattr(candidate, attr):
            setattr(candidate, attr, new_value)
            changed = True
    if item.resume_id and not candidate.workua_resume_id:
        candidate.workua_resume_id = item.resume_id
        changed = True
    if changed:
        candidate.save()
    return candidate


def _import(vacancy: Vacancy, items: Iterable[WorkUaCandidate], result: SyncResult, user) -> None:
    for item in items:
        result.total += 1
        if item.external_id and Application.objects.filter(
            vacancy=vacancy, workua_response_id=item.external_id
        ).exists():
            result.already_present += 1
            continue
        try:
            with transaction.atomic():
                candidate = _upsert_candidate(item)
                _, created = attach_candidate(
                    vacancy,
                    candidate,
                    channel=item.channel,
                    source=Candidate.Source.WORKUA,
                    user=user,
                    workua_response_id=item.external_id,
                    cover_letter=item.cover_letter,
                    applied_at=item.applied_at,
                )
        except Exception as exc:  # одна погана анкета не має зупиняти всю синхронізацію
            logger.exception("Work.ua: не вдалося обробити кандидата %s", item.external_id)
            result.errors.append(f"{item.external_id}: {exc}")
            continue
        if created:
            result.created_by_channel[item.channel] = result.created_by_channel.get(item.channel, 0) + 1
        else:
            result.already_present += 1


def sync_vacancy(vacancy: Vacancy, client: WorkUaClient | None = None, user=None) -> SyncResult:
    """Підтягує з Work.ua відгуки та рекомендованих кандидатів для однієї вакансії."""
    if not vacancy.workua_job_id:
        raise WorkUaError("У вакансії не вказано ID на Work.ua.")
    client = client or get_client()
    result = SyncResult()
    started_at = timezone.now()

    _import(vacancy, client.list_responses(vacancy.workua_job_id, since=vacancy.workua_synced_at), result, user)
    _import(vacancy, client.list_recommended(vacancy.workua_job_id), result, user)

    vacancy.workua_synced_at = started_at
    vacancy.save(update_fields=["workua_synced_at", "updated_at"])
    return result
