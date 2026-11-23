"""Ролі користувачів CRM.

* **Адміністратор** — суперкористувач Django: бачить і робить усе, має доступ до «Адмінки»
  та сторінки «Команда».
* **Рекрутер** — член групи «Рекрутер»: повний доступ до «Дашборду» й «Вакансій» (вакансії,
  кандидати, статуси, імпорт, синхронізація з Work.ua), «Підприємства» — лише перегляд,
  без «Адмінки» та «Команди».

Права рекрутера — звичайні дозволи Django на групі; синхронізуються після кожного `migrate`
(див. apps.py), тож список нижче — єдине джерело правди.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission

RECRUITER_GROUP = "Рекрутер"

ADMIN = "admin"
RECRUITER = "recruiter"
ROLE_CHOICES = [(ADMIN, "Адміністратор"), (RECRUITER, "Рекрутер")]
ROLE_LABELS = dict(ROLE_CHOICES)

# model → дії (view/add/change/delete) для рекрутера
RECRUITER_PERMISSIONS = {
    "vacancy": ("view", "add", "change"),
    "candidate": ("view", "add", "change"),
    "application": ("view", "add", "change"),
    "applicationevent": ("view", "add"),
    "company": ("view",),  # «Підприємства» — лише перегляд
}


def recruiter_codenames() -> list[str]:
    return [f"{action}_{model}" for model, actions in RECRUITER_PERMISSIONS.items() for action in actions]


def ensure_roles(**kwargs) -> Group:
    """Створює/оновлює групу «Рекрутер» з потрібними правами (ідемпотентно)."""
    group, _ = Group.objects.get_or_create(name=RECRUITER_GROUP)
    perms = Permission.objects.filter(content_type__app_label="recruiting", codename__in=recruiter_codenames())
    group.permissions.set(perms)
    return group


def role_of(user) -> str:
    if user.is_superuser:
        return ADMIN
    if user.groups.filter(name=RECRUITER_GROUP).exists():
        return RECRUITER
    return ""


def role_label(user) -> str:
    return ROLE_LABELS.get(role_of(user), "Без ролі")


def set_role(user, role: str) -> None:
    """Призначає роль (користувач має бути збережений)."""
    group = ensure_roles()
    if role == ADMIN:
        user.is_superuser = True
        user.is_staff = True  # доступ до «Адмінки»
        user.groups.remove(group)
    elif role == RECRUITER:
        user.is_superuser = False
        user.is_staff = False  # без «Адмінки»
        user.groups.add(group)
    else:
        raise ValueError(f"Невідома роль: {role}")
    user.save(update_fields=["is_superuser", "is_staff"])


def is_admin(user) -> bool:
    return bool(user and user.is_authenticated and user.is_active and user.is_superuser)


def active_admins_count() -> int:
    return get_user_model().objects.filter(is_active=True, is_superuser=True).count()
