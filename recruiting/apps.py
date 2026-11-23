from django.apps import AppConfig
from django.db.models.signals import post_migrate


class RecruitingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "recruiting"
    verbose_name = "CRM рекрутингу"

    def ready(self):
        # Після кожного migrate (коли дозволи Django вже створені) — оновити права ролі «Рекрутер».
        post_migrate.connect(_sync_roles, sender=self, dispatch_uid="recruiting_sync_roles")


def _sync_roles(**kwargs):
    from .roles import ensure_roles

    ensure_roles()
