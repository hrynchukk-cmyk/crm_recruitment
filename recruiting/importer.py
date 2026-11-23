"""Імпорт кандидатів з Google Таблиці (CSV) у вакансію.

Очікувані колонки (як у робочій таблиці команди, порядок не важливий):
Лінк | Ім'я | Вік | Контакт | Район | Статус | Примітка

* Посилання …/my/applicants/… потрапляють у «Відгуки», …/my/recommended/… — у
  «Рекомендовані Work.ua», …/resumes/… — у «Підібрані рекрутером». Рядки без посилання
  йдуть у розділ, вибраний у формі імпорту.
* Статуси з таблиці перетворюються на статуси CRM (див. `map_status`). Невідомий статус
  стає «Зателефонувати», а оригінальний текст дописується в примітку.
* Порожні рядки (лише статус без імені й телефону) пропускаються.
* Повторний імпорт тієї самої таблиці не створює дублікатів.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import Application, Candidate, Vacancy, parse_workua_link
from .services import attach_candidate, change_stage, find_existing_candidate

S = Application.Stage

COLUMN_ALIASES = {
    "link": ["лінк", "посилання", "link", "url", "резюме"],
    "name": ["ім'я", "імя", "піб", "name", "кандидат"],
    "age": ["вік", "age"],
    "phone": ["контакт", "телефон", "phone", "тел"],
    "district": ["район", "district"],
    "status": ["статус", "status"],
    "note": ["примітка", "примітки", "коментар", "note"],
}

DISTRICT_PREFIXES = {
    "галиц": Candidate.District.HALYTSKYI,
    "залізн": Candidate.District.ZALIZNYCHNYI,
    "личак": Candidate.District.LYCHAKIVSKYI,
    "сихів": Candidate.District.SYKHIVSKYI,
    "франк": Candidate.District.FRANKIVSKYI,
    "шевч": Candidate.District.SHEVCHENKIVSKYI,
    "облас": Candidate.District.REGION,
}


def _norm(text: str) -> str:
    return " ".join((text or "").replace("’", "'").replace("ʼ", "'").replace("`", "'").lower().split())


def map_status(text: str) -> str | None:
    """Статус з таблиці → статус CRM. None — якщо не вдалося розпізнати."""
    t = _norm(text)
    if not t:
        return S.NEW
    rules = [
        (("зателефон",), S.NEW),
        (("вайбер", "viber", "написал", "написав"), S.VIBER),
        (("не додзвон", "не бере", "недодзвон"), S.NO_ANSWER),
        (("передзвон",), S.CALLBACK),
        (("думає", "дума"), S.THINKING),
        (("не прийш",), S.NO_SHOW),
        (("співбесід",), S.INTERVIEW),
        (("пройш",), S.PASSED),
        (("вийш", "працевлашт", "працює"), S.HIRED),
        (("резерв",), S.RESERVE),
        (("не підход",), S.REJECTED_BY_US),
        (("не актуал", "відмов"), S.REJECTED_BY_CANDIDATE),
    ]
    for needles, stage in rules:
        if any(n in t for n in needles):
            return stage
    return None


def map_district(text: str) -> str:
    t = _norm(text)
    for prefix, district in DISTRICT_PREFIXES.items():
        if t.startswith(prefix):
            return district
    return ""


def _detect_columns(header: list[str]) -> dict[str, int]:
    columns: dict[str, int] = {}
    for index, title in enumerate(header):
        t = _norm(title)
        for key, aliases in COLUMN_ALIASES.items():
            if key not in columns and t in aliases:
                columns[key] = index
    return columns


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("Не вдалося прочитати файл: збережіть його як CSV у кодуванні UTF-8.")


@dataclass
class ImportResult:
    created: int = 0
    already_present: int = 0
    skipped_empty: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"Імпортовано кандидатів: {self.created}"
        if self.already_present:
            text += f". Вже були на вакансії: {self.already_present}"
        if self.errors:
            text += f". Рядків з помилками: {len(self.errors)}"
        return text


def import_csv(vacancy: Vacancy, data: bytes, *, default_channel: str, user=None) -> ImportResult:
    rows = list(csv.reader(io.StringIO(_decode(data))))
    if not rows:
        raise ValueError("Файл порожній.")
    columns = _detect_columns(rows[0])
    if "name" not in columns and "phone" not in columns:
        raise ValueError(
            "Не знайдено колонок «Ім'я» або «Контакт». Перший рядок файлу має містити назви колонок, "
            "як у таблиці: Лінк, Ім'я, Вік, Контакт, Район, Статус, Примітка."
        )

    result = ImportResult()
    # Щоб у CRM рядки йшли в тому ж порядку, що й у таблиці (список сортується від новіших).
    base_time = timezone.now()
    for line_no, row in enumerate(rows[1:], start=2):
        def cell(key: str) -> str:
            index = columns.get(key)
            return row[index].strip() if index is not None and index < len(row) else ""

        name, phone = cell("name"), cell("phone")
        if not name and not phone:
            result.skipped_empty += 1
            continue
        try:
            with transaction.atomic():
                created = _import_row(
                    vacancy, cell, default_channel, user, applied_at=base_time - timedelta(seconds=line_no)
                )
        except Exception as exc:
            result.errors.append(f"Рядок {line_no}: {exc}")
            continue
        if created:
            result.created += 1
        else:
            result.already_present += 1
    return result


def _import_row(vacancy: Vacancy, cell, default_channel: str, user, applied_at=None) -> bool:
    link = cell("link")
    if link.startswith("/"):
        link = "https://www.work.ua" + link
    channel, workua_id = parse_workua_link(link)
    channel = channel or default_channel

    extra_notes = []
    status_text = cell("status")
    stage = map_status(status_text)
    if stage is None:
        stage = S.NEW
        extra_notes.append(f"Статус у таблиці: {status_text}")
    district_text = cell("district")
    district = map_district(district_text)
    if district_text and not district and _norm(district_text) not in {"?", "-", "—"}:
        extra_notes.append(f"Район: {district_text}")
    age_text = cell("age")
    age = int(age_text) if age_text.isdigit() and 14 <= int(age_text) <= 90 else None

    resume_id = workua_id if channel == Application.Channel.SOURCED else ""
    response_id = workua_id if channel != Application.Channel.SOURCED else ""

    if response_id and vacancy.applications.filter(workua_response_id=response_id).exists():
        return False

    candidate = find_existing_candidate(phone=cell("phone"), workua_resume_id=resume_id)
    if candidate is None:
        candidate = Candidate.objects.create(
            full_name=cell("name") or "Без імені",
            phone=cell("phone"),
            age=age,
            district=district,
            city=vacancy.city,
            source=Candidate.Source.WORKUA if "work.ua" in link else Candidate.Source.MANUAL,
            resume_url=link if link.startswith("http") else "",
            workua_resume_id=resume_id,
        )
    application, created = attach_candidate(
        vacancy,
        candidate,
        channel=channel,
        source=candidate.source,
        user=user,
        workua_response_id=response_id,
        applied_at=applied_at,
    )
    if not created:
        return False

    note = "\n".join(filter(None, [cell("note"), *extra_notes]))
    if note:
        application.note = note
        application.save(update_fields=["note", "updated_at"])
    if stage != S.NEW:
        change_stage(application, stage, user=user, comment="Імпорт з таблиці")
    return True
