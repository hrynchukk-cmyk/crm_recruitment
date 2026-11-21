from django.core.management.base import BaseCommand, CommandError

from recruiting.integrations.workua import WorkUaError, sync_vacancy
from recruiting.models import Vacancy


class Command(BaseCommand):
    help = "Підтягує нові відгуки з Work.ua для відкритих вакансій (зручно запускати за розкладом)."

    def add_arguments(self, parser):
        parser.add_argument("--vacancy", type=int, help="ID вакансії в CRM (за замовчуванням — усі відкриті)")

    def handle(self, *args, **options):
        vacancies = Vacancy.objects.exclude(workua_job_id="")
        if options["vacancy"]:
            vacancies = vacancies.filter(pk=options["vacancy"])
        else:
            vacancies = vacancies.filter(status=Vacancy.Status.OPEN)

        failed = False
        for vacancy in vacancies:
            try:
                result = sync_vacancy(vacancy)
            except WorkUaError as exc:
                failed = True
                self.stderr.write(f"{vacancy}: {exc}")
                continue
            self.stdout.write(f"{vacancy}: {result.summary()}")
        if failed:
            raise CommandError("Синхронізація завершилася з помилками.")
