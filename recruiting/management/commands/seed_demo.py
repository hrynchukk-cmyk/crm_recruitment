import random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from recruiting.models import Application, Candidate, Company, Vacancy
from recruiting.services import attach_candidate, change_stage

NAMES = [
    "Андрій Коваль", "Оксана Мельник", "Тарас Бондар", "Наталія Шевчук", "Василь Ткачук",
    "Ірина Кравець", "Богдан Олійник", "Марія Гнатюк", "Юрій Пасічник", "Галина Савчук",
    "Олег Романюк", "Світлана Дячук", "Роман Іваницький", "Христина Лозинська",
]


class Command(BaseCommand):
    help = "Заповнює базу демонстраційними даними (лише для локальної розробки)."

    def handle(self, *args, **options):
        User = get_user_model()
        admin, created = User.objects.get_or_create(
            username="admin", defaults={"is_staff": True, "is_superuser": True, "first_name": "Адмін"}
        )
        if created:
            admin.set_password("admin")
            admin.save()
            self.stdout.write("Створено користувача admin / admin")

        rng = random.Random(42)
        plant = Company.objects.get_or_create(name="ТОВ «Львівський хлібозавод»", defaults={"phone": "+380322000000"})[0]
        logistics = Company.objects.get_or_create(name="Склад «Захід-Логістик»")[0]

        vacancies = [
            Vacancy.objects.get_or_create(
                company=plant, title="Пекар", defaults={"salary": "від 22 000 грн", "headcount": 3, "recruiter": admin}
            )[0],
            Vacancy.objects.get_or_create(
                company=logistics, title="Комплектувальник на склад",
                defaults={"salary": "25 000 – 30 000 грн", "headcount": 5, "recruiter": admin},
            )[0],
            Vacancy.objects.get_or_create(
                company=plant, title="Водій категорії C", defaults={"salary": "30 000 грн", "headcount": 1}
            )[0],
        ]

        stages = Application.Stage.values
        for i, name in enumerate(NAMES):
            candidate, _ = Candidate.objects.get_or_create(
                full_name=name,
                defaults={
                    "phone": f"+38067{1000000 + i * 7919:07d}",
                    "city": "Львів",
                    "birth_year": rng.randint(1970, 2004),
                    "source": Candidate.Source.WORKUA if i % 3 else Candidate.Source.MANUAL,
                    "resume_text": "Досвід роботи на виробництві 2 роки.\nГрафік: позмінний.",
                },
            )
            vacancy = vacancies[i % len(vacancies)]
            application, created = attach_candidate(
                vacancy, candidate, source=candidate.source, applied_at=timezone.now() - timezone.timedelta(days=i)
            )
            if created:
                change_stage(application, rng.choice(stages), user=admin)
        self.stdout.write(self.style.SUCCESS("Демо-дані готові."))
