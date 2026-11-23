import random

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from recruiting.models import Application, Candidate, Company, Vacancy
from recruiting.roles import RECRUITER, set_role
from recruiting.services import attach_candidate, change_stage

NAMES = [
    "Андрій Коваль", "Оксана Мельник", "Тарас Бондар", "Наталія Шевчук", "Василь Ткачук",
    "Ірина Кравець", "Богдан Олійник", "Марія Гнатюк", "Юрій Пасічник", "Галина Савчук",
    "Олег Романюк", "Світлана Дячук", "Роман Іваницький", "Христина Лозинська",
]

EXTRA_NAMES = [
    "Віктор Гаврилюк", "Леся Стасюк", "Степан Федорович", "Ольга Мартинюк", "Ігор Сидор",
    "Анна Кузьмич", "Микола Возняк", "Тетяна Демків", "Павло Яремко", "Софія Гуменюк",
]


class Command(BaseCommand):
    help = "Заповнює базу демонстраційними даними (лише для локальної розробки)."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Запустити навіть без DJANGO_DEBUG=1")

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "Демо-дані створюють користувача admin/admin — не запускайте це на робочому сервері. "
                "Для локальної спроби додайте --force."
            )
        User = get_user_model()
        admin, created = User.objects.get_or_create(
            username="admin", defaults={"is_staff": True, "is_superuser": True, "first_name": "Адмін"}
        )
        if created:
            admin.set_password("admin")
            admin.save()
            self.stdout.write("Створено користувача admin / admin")
        recruiter, created = User.objects.get_or_create(username="recruiter", defaults={"first_name": "Оксана"})
        if created:
            recruiter.set_password("recruiter")
            recruiter.save()
            set_role(recruiter, RECRUITER)
            self.stdout.write("Створено рекрутера recruiter / recruiter")

        rng = random.Random(42)
        today = timezone.localdate()
        # Демо-підприємства з приблизними точками у Львові (вигадані назви).
        plant = Company.objects.get_or_create(
            name="ТОВ «Львівський хлібозавод»",
            defaults={"phone": "+380322000000", "address": "вул. Городоцька, 172", "latitude": "49.833400", "longitude": "23.963500"},
        )[0]
        logistics = Company.objects.get_or_create(
            name="Склад «Захід-Логістик»",
            defaults={"address": "вул. Персенківка, 5", "latitude": "49.807900", "longitude": "24.048600"},
        )[0]
        shop = Company.objects.get_or_create(name="Магазин «Без точки на карті»")[0]
        Vacancy.objects.get_or_create(company=shop, title="Продавець-касир", defaults={"headcount": 2})

        vacancies = [
            Vacancy.objects.get_or_create(
                company=logistics, title="Комірник",
                defaults={"salary": "від 24 000 грн", "headcount": 2, "recruiter": admin, "workua_job_id": "1000001",
                          "opened_on": today - timezone.timedelta(days=12)},
            )[0],
            Vacancy.objects.get_or_create(
                company=plant, title="Пекар",
                defaults={"salary": "від 22 000 грн", "headcount": 3, "recruiter": admin,
                          "opened_on": today - timezone.timedelta(days=34)},
            )[0],
            Vacancy.objects.get_or_create(
                company=logistics, title="Комплектувальник на склад",
                defaults={"salary": "25 000 – 30 000 грн", "headcount": 5, "recruiter": admin,
                          "opened_on": today - timezone.timedelta(days=5)},
            )[0],
            Vacancy.objects.get_or_create(
                company=plant, title="Водій категорії C",
                defaults={"salary": "30 000 грн", "headcount": 1, "opened_on": today - timezone.timedelta(days=1)},
            )[0],
        ]

        stages = Application.Stage.values
        channels = Application.Channel.values
        for i, name in enumerate(NAMES + EXTRA_NAMES):
            candidate, _ = Candidate.objects.get_or_create(
                full_name=name,
                defaults={
                    "phone": f"+38067{1000000 + i * 7919:07d}",
                    "city": "Львів",
                    "age": rng.randint(19, 55),
                    "district": rng.choice(Candidate.District.values + [""]),
                    "source": Candidate.Source.WORKUA,
                    "resume_url": f"https://www.work.ua/resumes/{7000000 + i}/",
                    "resume_text": "Досвід роботи на виробництві 2 роки.\nГрафік: позмінний.",
                },
            )
            # Половина людей — на «Комірника», щоб було видно всі три розділи.
            vacancy = vacancies[0] if i % 2 == 0 else vacancies[1 + (i // 2) % (len(vacancies) - 1)]
            application, created = attach_candidate(
                vacancy,
                candidate,
                channel=channels[i % len(channels)],
                source=candidate.source,
                applied_at=timezone.now() - timezone.timedelta(hours=7 * i),
            )
            if created and i % 4:
                change_stage(application, rng.choice(stages), user=admin)
        self.stdout.write(self.style.SUCCESS("Демо-дані готові."))
