from django.utils.functional import SimpleLazyObject


def workua(request):
    """Стан інтеграції з Work.ua для шаблонів (запит до БД — лише якщо шаблон його використає)."""
    from django.conf import settings

    from .integrations.workua import is_enabled
    from .models import WorkUaSyncState

    return {
        "workua_enabled": is_enabled(),
        "workua_state": SimpleLazyObject(WorkUaSyncState.get),
        "workua_sync_minutes": settings.WORKUA.get("SYNC_MINUTES", 15),
    }
