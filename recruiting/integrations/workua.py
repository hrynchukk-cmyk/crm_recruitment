"""Інтеграція з Work.ua API (https://api.work.ua) — за документацією «Work: API».

Що синхронізується:

* **Вакансії** нашого акаунта (`GET /jobs/my?full=1`): нові активні з'являються в CRM
  (підприємство призначає рекрутер), у наявних оновлюється стан на Work.ua.
* **Відгуки** на кожну вакансію (`GET /jobs/<id>/responses`) — розділ «Відгуки».
  Беруться всі три типи: відгук з резюме (send), дзвінок за номером з вакансії (phonecall),
  повідомлення в месенджер (messenger). Щоразу — лише нові (після останнього отриманого id).
* **Кандидати, збережені на вакансію в кабінеті Work.ua** (`GET /candidates?job_id=`,
  деталі — `GET /candidates/<id>`) — розділ «Підібрані рекрутером».
* **Файл резюме з відгуку** (`GET /response_files/<job>/<response>`) — на запит, за кнопкою.

Чого НЕ робимо автоматично: `GET /resumes` і `GET /resume` витрачають оплачені відкриття
контактів, тому інтеграція їх не викликає.

Вхід — HTTP Basic з логіном (email) і паролем кабінету Work.ua (змінні WORKUA_* у .env).
Після 10 невдалих входів Work.ua блокує доступ на добу, тож після відповіді 401 автоматична
синхронізація зупиняється, доки логін/пароль не змінять (див. WorkUaSyncState).
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Protocol

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from ..models import Application, ApplicationEvent, Candidate, Vacancy, WorkUaSyncState
from ..services import attach_candidate, find_existing_candidate

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.work.ua"


# --------------------------------------------------------------------------- помилки


class WorkUaError(Exception):
    """Загальна помилка інтеграції (повідомлення — для людини, українською)."""


class WorkUaNotConfigured(WorkUaError):
    pass


class WorkUaAuthError(WorkUaError):
    """401: Work.ua не прийняв логін або пароль."""


class WorkUaBlocked(WorkUaError):
    """403: акаунт компанії заблоковано для API."""


class WorkUaRateLimited(WorkUaError):
    """429: забагато запитів або невдалих входів."""


# Ці помилки стосуються всього акаунта — після них немає сенсу продовжувати синхронізацію.
FATAL_ERRORS = (WorkUaAuthError, WorkUaBlocked, WorkUaRateLimited, WorkUaNotConfigured)


# --------------------------------------------------------------------------- дані


@dataclass(frozen=True)
class WorkUaJob:
    id: str
    name: str
    active: bool
    published_on: date | None = None
    expires_on: date | None = None
    region_id: int | None = None
    street_address: str = ""
    description: str = ""
    salary_text: str = ""
    contact_email: str = ""


@dataclass(frozen=True)
class WorkUaCandidate:
    """Кандидат із Work.ua (відгук або збережений кандидат) у форматі, незалежному від API."""

    external_id: str  # id відгуку, або «cand-<id>» для збереженого кандидата
    full_name: str
    channel: str = Application.Channel.RESPONSE
    from_type: str = Application.WorkUaFrom.SEND
    phone: str = ""
    email: str = ""
    city: str = ""
    age: int | None = None
    workua_candidate_id: str = ""
    resume_id: str = ""
    resume_url: str = ""
    resume_text: str = ""
    cover_letter: str = ""
    has_file: bool = False
    applied_at: datetime | None = None
    note: str = ""


# --------------------------------------------------------------------------- допоміжне


def html_to_text(value: str | None) -> str:
    """HTML з Work.ua (опис вакансії, текст резюме) → звичайний текст із переносами рядків."""
    if not value:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)<li[^>]*>", "• ", text)
    text = re.sub(r"(?i)</(p|li|h[1-6]|div|ul|ol)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def age_from_birth_date(birth: date | None, today: date | None = None) -> int | None:
    if not birth:
        return None
    today = today or timezone.localdate()
    years = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    return years if 14 <= years <= 90 else None


def parse_age_text(value) -> int | None:
    """«28 роки» → 28."""
    match = re.search(r"\d+", str(value or ""))
    if not match:
        return None
    years = int(match.group())
    return years if 14 <= years <= 90 else None


def _parse_dt(value) -> datetime | None:
    """Дата-час з Work.ua; некоректні значення (напр. «0000-00-00 00:00:00») → None."""
    if not value:
        return None
    try:
        dt = parse_datetime(str(value).strip().replace(" ", "T", 1))
        if dt and timezone.is_naive(dt):
            dt = timezone.make_aware(dt)
        return dt
    except (ValueError, TypeError, OverflowError):
        return None


def _safe_date(value) -> date | None:
    try:
        return parse_date(str(value).strip()[:10]) if value else None
    except (ValueError, TypeError):
        return None


def _parse_d(value) -> date | None:
    if not value:
        return None
    dt = _parse_dt(value)
    return timezone.localtime(dt).date() if dt else _safe_date(value)


def _s(value) -> str:
    """Рядок без пробілів по краях; None/числа/інше — безпечно."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def _items(data) -> list:
    """Список записів з відповіді: {"items": [...]} або просто [...]."""
    if isinstance(data, list):
        return [i for i in data if isinstance(i, dict)]
    if isinstance(data, dict):
        items = data.get("items")
        if items is None:
            items = data.get("result")
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    return []


def _last_page(data, page: int) -> int:
    try:
        return int(((data.get("pagination") or {}) if isinstance(data, dict) else {}).get("last_page") or page)
    except (TypeError, ValueError):
        return page


def _salary_text(salary) -> str:
    if not isinstance(salary, dict):
        return ""

    def fmt(n):
        try:
            return f"{int(float(n)):,}".replace(",", " ")
        except (TypeError, ValueError):
            return ""

    low, high = fmt(salary.get("value")), fmt(salary.get("value_max"))
    text = f"{low} – {high} грн" if low and high else (f"від {low} грн" if low else "")
    comment = (salary.get("comment") or "").strip()
    return f"{text} ({comment})" if text and comment else (text or comment)


def _flag(value) -> bool:
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


# --------------------------------------------------------------------------- клієнти


class WorkUaClient(Protocol):
    def my_jobs(self) -> list[WorkUaJob]: ...
    def towns(self) -> dict[int, str]: ...
    def job_responses(self, job_id: str, after_id: int | None = None) -> list[WorkUaCandidate]: ...
    def saved_candidates(self, job_id: str) -> list[dict]: ...
    def saved_candidate(self, item: dict) -> WorkUaCandidate: ...
    def response_file_url(self, job_id: str, response_id: str) -> str: ...


_NOT_CONNECTED = (
    "Інтеграцію з Work.ua не ввімкнено. Задайте WORKUA_BACKEND=api, WORKUA_LOGIN і WORKUA_PASSWORD "
    "(логін і пароль кабінету Work.ua) — у файлі .env або у Variables на Railway — і перезапустіть CRM."
)


class DisabledClient:
    def _fail(self, *args, **kwargs):
        raise WorkUaNotConfigured(_NOT_CONNECTED)

    my_jobs = towns = job_responses = saved_candidates = saved_candidate = response_file_url = _fail


class WorkUaApiClient:
    """HTTP-клієнт Work.ua API."""

    JOBS_PAGE_LIMIT = 100  # /jobs/my: limit 10…100, page і limit обов'язкові з 2026-09-01
    RESPONSES_LIMIT = 50  # /jobs/<id>/responses: максимум 50
    FROM_TYPES = ("send", "phonecall", "messenger")
    MAX_PAGES = 200  # запобіжник від нескінченного гортання

    def __init__(self, login: str, password: str, base_url: str = DEFAULT_BASE_URL, timeout: int = 20,
                 session=None, pause: float = 0.05):
        if not (login and password):
            raise WorkUaNotConfigured(_NOT_CONNECTED)
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.auth = (login, password)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.pause = pause  # ліміт Work.ua — 50 запитів/с; ми робимо значно менше
        contact = login.encode("ascii", "ignore").decode() or "unknown"
        self.headers = {
            # Work.ua просить вказувати назву програми й email, щоб зв'язатися у разі проблем.
            "User-Agent": f"RecruitmentCRM/1.0 ({contact})",
            "X-Locale": "uk_UA",
            "Accept": "application/json",
        }
        self._towns: dict[int, str] | None = None

    # ---- транспорт

    def _request(self, method: str, path: str, params=None, allow_redirects: bool = True):
        url = f"{self.base_url}{path}"
        try:
            response = self.session.request(
                method, url, params=params, auth=self.auth, headers=self.headers,
                timeout=self.timeout, allow_redirects=allow_redirects,
            )
        except requests.RequestException as exc:
            raise WorkUaError(f"Немає зв'язку з Work.ua ({exc.__class__.__name__}). Перевірте інтернет.") from exc
        if self.pause:
            time.sleep(self.pause)
        status = response.status_code
        if status == 401:
            raise WorkUaAuthError(
                "Work.ua не прийняв логін або пароль (WORKUA_LOGIN / WORKUA_PASSWORD у .env). "
                "Перевірте їх: після 10 невдалих спроб Work.ua блокує вхід на добу."
            )
        if status == 403:
            raise WorkUaBlocked("Work.ua закрив доступ до API для цього акаунта. Зверніться до свого менеджера Work.ua.")
        if status == 429:
            raise WorkUaRateLimited("Work.ua тимчасово обмежив доступ (забагато запитів або невдалих входів). Спробуйте пізніше.")
        if status == 501:
            raise WorkUaError(f"Work.ua не підтримує запит {method} {path} (501).")
        if status >= 500:
            raise WorkUaError("Work.ua тимчасово не відповідає (помилка на їхньому боці). Спробуємо пізніше.")
        return response

    def _json(self, path: str, params=None, not_accessible: str = ""):
        """JSON-відповідь, або None для 404 («нічого не знайдено»)."""
        response = self._request("GET", path, params=params)
        if response.status_code == 404:
            return None
        if response.status_code == 400:
            if not_accessible:
                raise WorkUaError(not_accessible)
            raise WorkUaError(f"Work.ua відхилив запит {path}: {self._error_text(response)}")
        try:
            return response.json()
        except ValueError as exc:
            raise WorkUaError(f"Work.ua повернув не JSON на {path}.") from exc

    @staticmethod
    def _error_text(response) -> str:
        try:
            data = response.json()
        except ValueError:
            return response.text[:200]
        errors = data.get("errors") if isinstance(data, dict) else None
        if isinstance(errors, list) and errors:
            return "; ".join(str(e.get("message") or e.get("id")) for e in errors if isinstance(e, dict))
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            return str(error.get("text") or error)
        return str(data)[:200]

    # ---- довідники

    def towns(self) -> dict[int, str]:
        if self._towns is None:
            data = self._json("/dictionaries/town") or []
            if isinstance(data, dict):
                data = data.get("town") or data.get("items") or data.get("result") or []
            towns = {}
            for item in data if isinstance(data, list) else []:
                if isinstance(item, dict) and str(item.get("id") or "").isdigit():
                    towns[int(item["id"])] = _s(item.get("name_ua")) or _s(item.get("name"))
            self._towns = towns
        return self._towns

    # ---- вакансії

    def my_jobs(self) -> list[WorkUaJob]:
        jobs: list[WorkUaJob] = []
        page = 1
        while page <= self.MAX_PAGES:
            data = self._json("/jobs/my", {"full": 1, "all": 1, "page": page, "limit": self.JOBS_PAGE_LIMIT})
            if not data:
                break
            for item in _items(data):
                if item.get("id") is None:
                    continue
                try:
                    jobs.append(self._parse_job(item))
                except Exception:
                    logger.exception("Work.ua: не вдалося розібрати вакансію %r", item.get("id"))
            last_page = _last_page(data, page)
            if page >= last_page:
                break
            page += 1
        return jobs

    @staticmethod
    def _parse_job(item: dict) -> WorkUaJob:
        region = item.get("region")
        return WorkUaJob(
            id=str(item["id"]),
            name=_s(item.get("name")) or f"Вакансія Work.ua {item['id']}",
            active=_flag(item.get("active")),
            published_on=_parse_d(item.get("date")),
            expires_on=_parse_d(item.get("date_expire")),
            region_id=int(region) if str(region or "").isdigit() else None,
            street_address=_s(item.get("streetAddress")),
            description=html_to_text(_s(item.get("description"))),
            salary_text=_salary_text(item.get("salary")),
            contact_email=_s(item.get("contact", {}).get("email")) if isinstance(item.get("contact"), dict) else "",
        )

    # ---- відгуки

    def job_responses(self, job_id: str, after_id: int | None = None) -> list[WorkUaCandidate]:
        """Відгуки на вакансію, новіші за after_id (або всі, якщо after_id не задано).

        Work.ua віддає відгуки від новіших до старіших пачками до 50; гортаємо вниз через
        before_id, поки не дійдемо до after_id.
        """
        raw: dict[int, dict] = {}
        before_id = None
        for _ in range(self.MAX_PAGES):
            params = {"limit": self.RESPONSES_LIMIT, "from_type[]": list(self.FROM_TYPES)}
            if after_id:
                params["last_id"] = after_id
            if before_id:
                params["before_id"] = before_id
            data = self._json(
                f"/jobs/{job_id}/responses", params,
                not_accessible=f"Вакансія {job_id} недоступна в цьому акаунті Work.ua (належить іншій компанії, "
                "видалена або номер вказано неправильно).",
            )
            batch = [r for r in _items(data) if str(r.get("id") or "").isdigit()]
            batch = [r for r in batch if not after_id or int(r["id"]) > after_id]
            if not batch:
                break
            for r in batch:
                raw[int(r["id"])] = r
            if len(batch) < self.RESPONSES_LIMIT:
                break
            lowest = min(int(r["id"]) for r in batch)
            if before_id is not None and lowest >= before_id:
                break  # немає просування — не зациклюємося
            before_id = lowest
        return [self._parse_response(raw[i]) for i in sorted(raw)]

    @staticmethod
    def _parse_response(r: dict) -> WorkUaCandidate:
        from_type = r.get("from_type") or "send"
        if from_type not in Application.WorkUaFrom.values:
            from_type = "send"
        fio = " ".join(_s(r.get("fio")).split())
        return WorkUaCandidate(
            external_id=str(r["id"]),
            full_name=fio or "Кандидат з Work.ua (ім'я приховане)",
            channel=Application.Channel.RESPONSE,
            from_type=from_type,
            phone=_s(r.get("phone"))[:50],
            email=_s(r.get("email"))[:254],
            age=age_from_birth_date(_safe_date(r.get("birth_date"))),
            workua_candidate_id=str(r.get("candidate_id") or ""),
            resume_text=html_to_text(_s(r.get("text"))),
            cover_letter=html_to_text(_s(r.get("cover"))),
            has_file=_flag(r.get("with_file")),
            applied_at=_parse_dt(r.get("date")),
        )

    def response_file_url(self, job_id: str, response_id: str) -> str:
        """Тимчасове (≈5 хв) посилання на файл резюме з відгуку. Не зберігати й не кешувати."""
        response = self._request("GET", f"/response_files/{job_id}/{response_id}/", allow_redirects=False)
        if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location"):
            return response.headers["Location"]
        if response.status_code == 404:
            raise WorkUaError("До цього відгуку не прикріплено файл (або він уже недоступний).")
        raise WorkUaError(f"Не вдалося отримати файл резюме: {self._error_text(response)}")

    # ---- кандидати, збережені на вакансію в кабінеті Work.ua (СУН)

    def saved_candidates(self, job_id: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        while page <= self.MAX_PAGES:
            data = self._json("/candidates", {"job_id": job_id, "page": page})
            if not data:
                break
            items.extend(i for i in _items(data) if i.get("candidate_id"))
            last_page = _last_page(data, page)
            if page >= last_page:
                break
            page += 1
        return items

    def saved_candidate(self, item: dict) -> WorkUaCandidate:
        """Деталі збереженого кандидата (GET /candidates/<id>); якщо недоступні — з даних списку."""
        candidate_id = str(item["candidate_id"])
        data = self._json(f"/candidates/{candidate_id}") or {}
        data = data if isinstance(data, dict) else {}
        info = data.get("candidate") if isinstance(data.get("candidate"), dict) else {}
        brief = item.get("info") if isinstance(item.get("info"), dict) else {}
        first = _s(info.get("first_name")) or _s(brief.get("first_name"))
        last = _s(info.get("last_name")) or _s(brief.get("last_name"))
        middle = _s(info.get("middle_name")) or _s(brief.get("middle_name"))
        name = " ".join(p for p in (first, middle, last) if p).strip()
        resumes = data.get("resumes") or []
        resume_id = str(resumes[0].get("id") or "") if resumes and isinstance(resumes[0], dict) else ""
        contacts_open = data.get("are_contacts_open", item.get("are_contacts_open"))
        return WorkUaCandidate(
            external_id=f"cand-{candidate_id}",
            full_name=name or "Кандидат з Work.ua (ім'я приховане)",
            channel=Application.Channel.SOURCED,
            from_type=Application.WorkUaFrom.SAVED,
            phone=_s(info.get("phone"))[:50],
            email=_s(info.get("email"))[:254],
            age=parse_age_text(info.get("age")),
            workua_candidate_id=candidate_id,
            resume_id=resume_id,
            resume_url=f"https://www.work.ua/resumes/{resume_id}/" if resume_id else "",
            applied_at=_parse_dt(item.get("created_datetime")),
            note="" if contacts_open else "Контакти цього кандидата на Work.ua ще не відкриті.",
        )


def get_client() -> WorkUaClient:
    config = settings.WORKUA
    if config.get("BACKEND", "disabled") != "api":
        return DisabledClient()
    return WorkUaApiClient(
        login=config.get("LOGIN", ""),
        password=config.get("PASSWORD", ""),
        base_url=config.get("API_BASE_URL") or DEFAULT_BASE_URL,
        timeout=config.get("TIMEOUT", 20),
    )


def is_enabled() -> bool:
    config = settings.WORKUA
    return config.get("BACKEND") == "api" and bool(config.get("LOGIN")) and bool(config.get("PASSWORD"))


def credentials_fingerprint() -> str:
    config = settings.WORKUA
    raw = f"{config.get('LOGIN', '')}\0{config.get('PASSWORD', '')}"
    return hashlib.sha256(raw.encode()).hexdigest()


# --------------------------------------------------------------------------- синхронізація


@dataclass
class SyncResult:
    jobs_created: int = 0
    jobs_updated: int = 0
    created_by_channel: dict[str, int] = field(default_factory=dict)
    already_present: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def created(self) -> int:
        return sum(self.created_by_channel.values())

    def summary(self) -> str:
        labels = dict(Application.Channel.choices)
        parts = []
        if self.jobs_created:
            parts.append(f"нових вакансій: {self.jobs_created}")
        if self.created_by_channel:
            detail = ", ".join(f"{labels[ch]} — {n}" for ch, n in self.created_by_channel.items())
            parts.append(f"нових кандидатів: {self.created} ({detail})")
        if not parts:
            parts.append("нового немає")
        text = "Work.ua: " + "; ".join(parts)
        if self.errors:
            text += f". Помилок: {len(self.errors)}"
        return text


def _upsert_candidate(item: WorkUaCandidate) -> Candidate:
    candidate = None
    if item.workua_candidate_id:
        candidate = Candidate.objects.filter(workua_candidate_id=item.workua_candidate_id).first()
    if candidate is None:
        candidate = find_existing_candidate(phone=item.phone, email=item.email, workua_resume_id=item.resume_id)
    if candidate is None:
        return Candidate.objects.create(
            full_name=item.full_name,
            phone=item.phone,
            email=item.email,
            city=item.city,
            age=item.age,
            source=Candidate.Source.WORKUA,
            workua_candidate_id=item.workua_candidate_id,
            workua_resume_id=item.resume_id,
            resume_url=item.resume_url,
            resume_text=item.resume_text,
        )
    # Доповнюємо лише порожні поля — те, що рекрутер виправив руками, не перезаписуємо.
    changed = False
    for attr in ("phone", "email", "city", "age", "resume_url", "resume_text", "workua_candidate_id"):
        value = getattr(item, attr)
        if value and not getattr(candidate, attr):
            if attr == "workua_candidate_id" and Candidate.objects.filter(workua_candidate_id=value).exists():
                continue
            setattr(candidate, attr, value)
            changed = True
    if item.resume_id and not candidate.workua_resume_id and not Candidate.objects.filter(workua_resume_id=item.resume_id).exists():
        candidate.workua_resume_id = item.resume_id
        changed = True
    if changed:
        candidate.save()
    return candidate


def _import(vacancy: Vacancy, items: Iterable[WorkUaCandidate], result: SyncResult, user=None) -> None:
    labels = dict(Application.WorkUaFrom.choices)
    for item in items:
        if item.external_id and vacancy.applications.filter(workua_response_id=item.external_id).exists():
            result.already_present += 1
            continue
        try:
            with transaction.atomic():
                candidate = _upsert_candidate(item)
                application, created = attach_candidate(
                    vacancy,
                    candidate,
                    channel=item.channel,
                    source=Candidate.Source.WORKUA,
                    user=user,
                    workua_response_id=item.external_id,
                    cover_letter=item.cover_letter,
                    applied_at=item.applied_at,
                )
                fields = []
                if not application.workua_from or item.channel == Application.Channel.RESPONSE:
                    if application.workua_from != item.from_type:
                        application.workua_from = item.from_type
                        fields.append("workua_from")
                if item.has_file and not application.workua_has_file:
                    application.workua_has_file = True
                    fields.append("workua_has_file")
                if fields:
                    application.save(update_fields=[*fields, "updated_at"])
                if created:
                    note = f"З Work.ua: {labels.get(item.from_type, item.from_type).lower()}."
                    if item.note:
                        note += f" {item.note}"
                    ApplicationEvent.objects.create(
                        application=application, kind=ApplicationEvent.Kind.NOTE, author=user, text=note
                    )
        except Exception as exc:  # одна погана анкета не має зупиняти всю синхронізацію
            logger.exception("Work.ua: не вдалося обробити %s", item.external_id)
            result.errors.append(f"{vacancy.title}, {item.external_id}: {exc}")
            continue
        if created:
            result.created_by_channel[item.channel] = result.created_by_channel.get(item.channel, 0) + 1
        else:
            result.already_present += 1


def import_jobs(client: WorkUaClient, result: SyncResult) -> None:
    """Вакансії нашого акаунта на Work.ua → CRM."""
    jobs = client.my_jobs()
    users_by_email = {u.email.lower(): u for u in get_user_model().objects.exclude(email="")}
    for job in jobs:
        try:
            with transaction.atomic():
                _import_job(client, job, users_by_email, result)
        except Exception as exc:
            logger.exception("Work.ua: не вдалося імпортувати вакансію %s", job.id)
            result.errors.append(f"Вакансія Work.ua {job.id} «{job.name}»: {exc.__class__.__name__}: {exc}")


def _import_job(client: WorkUaClient, job: WorkUaJob, users_by_email: dict, result: SyncResult) -> None:
    vacancy = Vacancy.objects.filter(workua_job_id=job.id).first()
    if vacancy:
        vacancy.workua_active = job.active
        vacancy.workua_expires_on = job.expires_on
        vacancy.workua_street_address = job.street_address
        fields = ["workua_active", "workua_expires_on", "workua_street_address"]
        if job.description and not vacancy.description:
            vacancy.description = job.description
            fields.append("description")
        if job.salary_text and not vacancy.salary:
            vacancy.salary = job.salary_text[:100]
            fields.append("salary")
        vacancy.save(update_fields=[*fields, "updated_at"])
        result.jobs_updated += 1
    elif job.active:
        city = client.towns().get(job.region_id, "") if job.region_id else ""
        Vacancy.objects.create(
            company=None,  # підприємство призначає рекрутер
            title=job.name[:255],
            city=(city or "Львів")[:100],
            salary=job.salary_text[:100],
            description=job.description,
            status=Vacancy.Status.OPEN,
            opened_on=job.published_on or timezone.localdate(),
            recruiter=users_by_email.get(job.contact_email.lower()) if job.contact_email else None,
            workua_job_id=job.id,
            workua_active=True,
            workua_expires_on=job.expires_on,
            workua_street_address=job.street_address[:255],
        )
        result.jobs_created += 1


def sync_vacancy(vacancy: Vacancy, client: WorkUaClient | None = None, user=None,
                 result: SyncResult | None = None) -> SyncResult:
    """Нові відгуки й збережені кандидати для однієї вакансії."""
    if not vacancy.workua_job_id:
        raise WorkUaError("У вакансії не вказано ID на Work.ua.")
    client = client or get_client()
    result = result if result is not None else SyncResult()
    started_at = timezone.now()

    # 1. Відгуки (send + phonecall + messenger) — лише нові.
    responses = client.job_responses(vacancy.workua_job_id, after_id=vacancy.workua_last_response_id)
    _import(vacancy, responses, result, user)
    numeric_ids = [int(r.external_id) for r in responses if r.external_id.isdigit()]
    if numeric_ids and max(numeric_ids) > (vacancy.workua_last_response_id or 0):
        vacancy.workua_last_response_id = max(numeric_ids)

    # 2. Кандидати, збережені на вакансію в кабінеті Work.ua.
    known = set(
        vacancy.applications.exclude(candidate__workua_candidate_id="").values_list(
            "candidate__workua_candidate_id", flat=True
        )
    )
    for item in client.saved_candidates(vacancy.workua_job_id):
        candidate_id = str(item.get("candidate_id") or "")
        if not candidate_id or candidate_id in known:
            continue
        try:
            detail = client.saved_candidate(item)
        except FATAL_ERRORS:
            raise
        except Exception as exc:
            logger.exception("Work.ua: кандидат %s", candidate_id)
            result.errors.append(f"{vacancy.title}, кандидат {candidate_id}: {exc}")
            continue
        _import(vacancy, [detail], result, user)
        known.add(candidate_id)

    vacancy.workua_synced_at = started_at
    vacancy.save(update_fields=["workua_synced_at", "workua_last_response_id", "updated_at"])
    return result


def _record(state: WorkUaSyncState, *, error: Exception | None = None, result: SyncResult | None = None) -> None:
    now = timezone.now()
    state.last_run_at = now
    if error is not None:
        state.last_error = str(error)
        if isinstance(error, WorkUaAuthError):
            state.auth_failed_fingerprint = credentials_fingerprint()
    else:
        state.auth_failed_fingerprint = ""
        state.last_success_at = now
        state.last_summary = result.summary() if result else ""
        state.last_error = "; ".join(result.errors[:3]) if result and result.errors else ""
    state.save()


def guarded(func):
    """Записує результат/помилку в WorkUaSyncState (зокрема 401 — щоб не повторювати вхід)."""

    def wrapper(*args, **kwargs):
        state = WorkUaSyncState.get()
        try:
            result = func(*args, **kwargs)
        except WorkUaNotConfigured:
            raise
        except WorkUaError as exc:
            _record(state, error=exc)
            raise
        except Exception as exc:
            logger.exception("Work.ua: неочікувана помилка синхронізації")
            error = WorkUaError(
                f"Неочікувана помилка під час синхронізації з Work.ua ({exc.__class__.__name__}: {exc}). "
                "Повний опис — у журналі: docker compose logs web --tail 150"
            )
            _record(state, error=error)
            raise error from exc
        _record(state, result=result)
        return result

    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


def auth_blocked_for_auto_sync() -> bool:
    """True, якщо з поточними логіном/паролем Work.ua уже відповів 401 — авто-спроби не робимо."""
    state = WorkUaSyncState.get()
    return bool(state.auth_failed_fingerprint) and state.auth_failed_fingerprint == credentials_fingerprint()


@guarded
def sync_all(client: WorkUaClient | None = None, user=None) -> SyncResult:
    """Повна синхронізація: вакансії, потім відгуки й кандидати для відкритих і призупинених."""
    client = client or get_client()
    result = SyncResult()
    import_jobs(client, result)
    vacancies = Vacancy.objects.exclude(workua_job_id="").filter(
        status__in=[Vacancy.Status.OPEN, Vacancy.Status.PAUSED]
    )
    for vacancy in vacancies:
        try:
            sync_vacancy(vacancy, client, user, result)
        except FATAL_ERRORS:
            raise
        except WorkUaError as exc:
            result.errors.append(f"{vacancy.title}: {exc}")
        except Exception as exc:
            logger.exception("Work.ua: помилка синхронізації вакансії %s", vacancy.pk)
            result.errors.append(f"{vacancy.title}: {exc.__class__.__name__}: {exc}")
    return result


@guarded
def sync_one(vacancy: Vacancy, client: WorkUaClient | None = None, user=None) -> SyncResult:
    return sync_vacancy(vacancy, client, user)
