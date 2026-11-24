"""Перенесення даних між копіями CRM (наприклад, з комп'ютера в хмару) одним файлом.

Файл — JSON з користувачами, групами і всіма даними CRM. Користувачів і групи CRM зіставляє за логіном і назвою,
тому їхні номери в різних базах можуть відрізнятися: користувача з тим самим логіном буде оновлено (разом
з паролем), нового — створено, а тих, кого у файлі немає, не чіпаємо. Підприємства, вакансії, кандидати, відгуки
та історію в цільовій базі замінюємо повністю, щоб вийшла точна копія.
"""
import json
import logging
import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import serializers
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from .models import Application, ApplicationEvent, Candidate, Company, Vacancy
from .roles import ensure_roles

logger = logging.getLogger(__name__)

FORMAT = "crm-recruitment-transfer"
VERSION = 1
MAX_SIZE = 50 * 1024 * 1024

# Дані CRM у порядку, в якому їх можна видаляти (спершу ті, що посилаються на інші).
CRM_MODELS = [ApplicationEvent, Application, Candidate, Vacancy, Company]


class TransferError(ValueError):
    """Файл не підходить або дані не вдалося завантажити. Повідомлення — для людини."""


def _exported_models():
    # Порядок: спершу те, на що посилаються інші записи.
    return [Group, get_user_model(), *reversed(CRM_MODELS)]


def _label(model) -> str:
    return model._meta.label_lower


def current_counts() -> dict[str, int]:
    return {
        "companies": Company.objects.count(),
        "vacancies": Vacancy.objects.count(),
        "candidates": Candidate.objects.count(),
        "applications": Application.objects.count(),
        "users": get_user_model().objects.count(),
    }


def export_data() -> bytes:
    objects = []
    for model in _exported_models():
        text = serializers.serialize(
            "json",
            model._default_manager.order_by("pk"),
            use_natural_foreign_keys=True,
            use_natural_primary_keys=True,  # користувачі й групи — за логіном / назвою, без номерів
        )
        objects.extend(json.loads(text))
    payload = {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": timezone.now().isoformat(),
        "counts": dict(Counter(item["model"] for item in objects)),
        "objects": objects,
    }
    return json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")


def read_payload(raw: bytes) -> list[dict]:
    """Перевіряє файл і повертає список записів. Нічого не змінює в базі."""
    not_ours = "Це не файл перенесення CRM. Потрібен файл, вивантажений кнопкою «Вивантажити дані» в іншій копії CRM."
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TransferError(not_ours)
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise TransferError(not_ours)
    if payload.get("version") != VERSION:
        raise TransferError("Файл вивантажено з іншої версії CRM. Оновіть обидві копії CRM і вивантажте файл ще раз.")
    objects = payload.get("objects")
    if not isinstance(objects, list) or not all(isinstance(item, dict) for item in objects):
        raise TransferError("Файл пошкоджено: немає списку записів.")
    allowed = {_label(model) for model in _exported_models()}
    unexpected = {str(item.get("model")) for item in objects} - allowed
    if unexpected:
        raise TransferError(f"У файлі є дані, які CRM не переносить: {', '.join(sorted(unexpected))}.")
    return objects


@dataclass
class ImportResult:
    counts: Counter
    weak_password_users: list[str] = field(default_factory=list)

    def summary(self) -> str:
        c = self.counts
        return (
            "Дані перенесено: "
            f"підприємств — {c[_label(Company)]}, вакансій — {c[_label(Vacancy)]}, "
            f"кандидатів — {c[_label(Candidate)]}, відгуків і підборів — {c[_label(Application)]}, "
            f"користувачів — {c[_label(get_user_model())]}."
        )


def import_data(raw: bytes) -> ImportResult:
    """Замінює дані CRM вмістом файлу. Або все, або нічого: при помилці база лишається як була."""
    objects = read_payload(raw)
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(objects, fh, ensure_ascii=False)
        with transaction.atomic():
            for model in CRM_MODELS:
                model._default_manager.all().delete()
            # loaddata зберігає записи як є (без автозаповнень моделі) і сам підводить лічильники номерів у PostgreSQL.
            call_command("loaddata", path, verbosity=0)
            ensure_roles()
    except Exception as exc:
        logger.exception("Не вдалося завантажити файл перенесення")
        raise TransferError(f"Не вдалося завантажити дані, у CRM нічого не змінено. Деталі: {exc}") from exc
    finally:
        os.unlink(path)

    usernames = [item["fields"].get("username") for item in objects if item["model"] == _label(get_user_model())]
    imported_users = get_user_model().objects.filter(username__in=[u for u in usernames if u], is_active=True)
    weak = [user.username for user in imported_users if user.check_password(user.username)]
    return ImportResult(counts=Counter(item["model"] for item in objects), weak_password_users=sorted(weak))
