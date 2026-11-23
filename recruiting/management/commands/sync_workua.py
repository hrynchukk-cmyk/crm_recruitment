import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, ProgrammingError, close_old_connections

from recruiting.integrations.workua import (
    WorkUaError,
    auth_blocked_for_auto_sync,
    is_enabled,
    sync_all,
    sync_one,
)
from recruiting.models import Vacancy

logger = logging.getLogger("recruiting.workua.sync")


class Command(BaseCommand):
    help = (
        "Синхронізація з Work.ua: вакансії, нові відгуки (з резюме, дзвінки, месенджер) і кандидати, "
        "збережені на вакансію в кабінеті Work.ua. З --loop працює постійно (сервіс worker)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--vacancy", type=int, help="Лише одна вакансія (ID в CRM)")
        parser.add_argument("--loop", action="store_true", help="Повторювати кожні WORKUA_SYNC_MINUTES хвилин")

    def handle(self, *args, **options):
        if options["loop"]:
            return self.loop()
        if not is_enabled():
            raise CommandError("Інтеграцію з Work.ua не ввімкнено (WORKUA_BACKEND=api, WORKUA_LOGIN, WORKUA_PASSWORD).")
        try:
            if options["vacancy"]:
                result = sync_one(Vacancy.objects.get(pk=options["vacancy"]))
            else:
                result = sync_all()
        except Vacancy.DoesNotExist:
            raise CommandError("Вакансію не знайдено.")
        except WorkUaError as exc:
            raise CommandError(str(exc))
        self.stdout.write(result.summary())
        for error in result.errors:
            self.stderr.write(f"  ! {error}")

    def loop(self):
        minutes = settings.WORKUA["SYNC_MINUTES"]
        self.stdout.write(f"Фонова синхронізація з Work.ua: кожні {minutes} хв.")
        time.sleep(20)  # дати веб-сервісу застосувати міграції
        warned_disabled = False
        while True:
            close_old_connections()
            try:
                if not is_enabled():
                    if not warned_disabled:
                        self.stdout.write("Інтеграцію з Work.ua не ввімкнено (WORKUA_* у .env або Variables на Railway) — чекаю.")
                        warned_disabled = True
                elif auth_blocked_for_auto_sync():
                    self.stdout.write(
                        "Пропускаю: Work.ua не прийняв поточні логін/пароль. Виправте WORKUA_LOGIN/WORKUA_PASSWORD "
                        "(у .env або Variables на Railway) і перезапустіть CRM."
                    )
                else:
                    result = sync_all()
                    self.stdout.write(result.summary())
            except (OperationalError, ProgrammingError) as exc:
                self.stdout.write(f"База ще не готова ({exc.__class__.__name__}), спробую пізніше.")
            except WorkUaError as exc:
                self.stdout.write(f"Work.ua: {exc}")
            except Exception:  # фоновий процес не має падати через одну помилку
                logger.exception("Неочікувана помилка синхронізації з Work.ua")
            time.sleep(minutes * 60)
