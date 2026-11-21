from datetime import datetime, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .integrations.workua import DisabledClient, WorkUaNotConfigured, WorkUaResponse, sync_vacancy
from .models import Application, ApplicationEvent, Candidate, Company, Vacancy, normalize_phone
from .services import attach_candidate, change_stage


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def list_responses(self, job_id, since=None):
        self.calls.append((job_id, since))
        return list(self.responses)


class BaseTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("recruiter", password="pass-12345-word")
        cls.company = Company.objects.create(name="Львівський завод")
        cls.vacancy = Vacancy.objects.create(
            company=cls.company, title="Оператор лінії", workua_job_id="1234567", recruiter=cls.user
        )


class PhoneNormalizationTests(TestCase):
    def test_formats(self):
        self.assertEqual(normalize_phone("067 123 45 67"), "+380671234567")
        self.assertEqual(normalize_phone("+38 (067) 123-45-67"), "+380671234567")
        self.assertEqual(normalize_phone("380671234567"), "+380671234567")
        self.assertEqual(normalize_phone(""), "")


class StageTests(BaseTestCase):
    def test_change_stage_writes_history(self):
        candidate = Candidate.objects.create(full_name="Іван Петренко", phone="0671234567")
        application, created = attach_candidate(self.vacancy, candidate, user=self.user)
        self.assertTrue(created)

        self.assertTrue(change_stage(application, Application.Stage.INTERVIEW, user=self.user, comment="на завтра"))
        self.assertFalse(change_stage(application, Application.Stage.INTERVIEW, user=self.user))

        application.refresh_from_db()
        self.assertEqual(application.stage, Application.Stage.INTERVIEW)
        kinds = list(application.events.values_list("kind", flat=True))
        self.assertEqual(sorted(kinds), sorted([ApplicationEvent.Kind.CREATED, ApplicationEvent.Kind.STAGE]))

    def test_unknown_stage_rejected(self):
        candidate = Candidate.objects.create(full_name="Хтось")
        application, _ = attach_candidate(self.vacancy, candidate)
        with self.assertRaises(ValueError):
            change_stage(application, "bogus")


class WorkUaSyncTests(BaseTestCase):
    def response(self, rid, name, phone="", resume_id=""):
        return WorkUaResponse(
            response_id=rid,
            full_name=name,
            phone=phone,
            resume_id=resume_id,
            applied_at=datetime(2026, 11, 20, 10, 0, tzinfo=dt_timezone.utc),
        )

    def test_creates_candidates_and_is_idempotent(self):
        client = FakeClient([self.response("r1", "Олена", "0501112233", "res1"), self.response("r2", "Петро")])
        result = sync_vacancy(self.vacancy, client=client)
        self.assertEqual((result.total, result.created), (2, 2))
        self.assertEqual(self.vacancy.applications.count(), 2)
        self.assertTrue(all(a.source == Candidate.Source.WORKUA for a in self.vacancy.applications.all()))

        result = sync_vacancy(self.vacancy, client=client)
        self.assertEqual((result.created, result.already_present), (0, 2))
        self.assertEqual(Candidate.objects.count(), 2)
        self.vacancy.refresh_from_db()
        self.assertIsNotNone(self.vacancy.workua_synced_at)
        # Перша синхронізація — без обмеження, друга — лише нові відгуки після першої.
        self.assertIsNone(client.calls[0][1])
        self.assertIsNotNone(client.calls[1][1])

    def test_matches_existing_candidate_by_phone(self):
        existing = Candidate.objects.create(full_name="Олена Коваль", phone="+380501112233")
        sync_vacancy(self.vacancy, client=FakeClient([self.response("r1", "Олена", "050 111 22 33", "res1")]))
        self.assertEqual(Candidate.objects.count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.full_name, "Олена Коваль")  # не перезаписуємо ручні правки
        self.assertEqual(existing.workua_resume_id, "res1")

    def test_disabled_client_raises(self):
        with self.assertRaises(WorkUaNotConfigured):
            sync_vacancy(self.vacancy, client=DisabledClient())


class ViewTests(BaseTestCase):
    def setUp(self):
        self.client.force_login(self.user)

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(reverse("recruiting:vacancy_list"))
        self.assertEqual(response.status_code, 302)

    def test_vacancy_pages_and_stage_change(self):
        candidate = Candidate.objects.create(full_name="Іван Петренко", phone="0671234567")
        application, _ = attach_candidate(self.vacancy, candidate)

        self.assertContains(self.client.get(reverse("recruiting:vacancy_list")), "Оператор лінії")
        self.assertContains(
            self.client.get(reverse("recruiting:vacancy_detail", args=[self.vacancy.pk])), "Іван Петренко"
        )
        self.assertContains(self.client.get(application.get_absolute_url()), "Іван Петренко")

        response = self.client.post(
            reverse("recruiting:application_stage", args=[application.pk]),
            {"stage": Application.Stage.HIRED, "next": "https://evil.example/"},
        )
        self.assertRedirects(response, application.get_absolute_url())
        application.refresh_from_db()
        self.assertEqual(application.stage, Application.Stage.HIRED)

        # На вкладці «в роботі» працевлаштованого вже немає, на вкладці «Вийшов на роботу» — є.
        detail = reverse("recruiting:vacancy_detail", args=[self.vacancy.pk])
        self.assertNotContains(self.client.get(detail), "Іван Петренко")
        self.assertContains(self.client.get(detail, {"stage": "hired"}), "Іван Петренко")

    def test_add_candidate_deduplicates_by_phone(self):
        Candidate.objects.create(full_name="Іван Петренко", phone="+380671234567")
        url = reverse("recruiting:vacancy_add_candidate", args=[self.vacancy.pk])
        response = self.client.post(url, {"full_name": "Іван", "phone": "067-123-45-67", "source": "manual"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Candidate.objects.count(), 1)
        self.assertEqual(self.vacancy.applications.count(), 1)

    def test_vacancy_form_accepts_workua_link(self):
        response = self.client.post(
            reverse("recruiting:vacancy_create"),
            {
                "company": self.company.pk,
                "title": "Комплектувальник",
                "city": "Львів",
                "headcount": 3,
                "status": "open",
                "workua_job_id": "https://www.work.ua/jobs/7654321/",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Vacancy.objects.get(title="Комплектувальник").workua_job_id, "7654321")

    @override_settings(WORKUA={"BACKEND": "disabled"})
    def test_sync_button_shows_error_when_not_configured(self):
        response = self.client.post(
            reverse("recruiting:vacancy_sync_workua", args=[self.vacancy.pk]), follow=True
        )
        self.assertContains(response, "ще не підключено")
