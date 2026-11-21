from datetime import datetime, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .integrations.workua import DisabledClient, WorkUaCandidate, WorkUaNotConfigured, sync_vacancy
from .models import (
    Application,
    ApplicationEvent,
    Candidate,
    Company,
    Vacancy,
    normalize_phone,
    parse_workua_link,
    parse_workua_resume_id,
)
from .importer import import_csv, map_status
from .services import attach_candidate, change_stage


class FakeClient:
    def __init__(self, responses, recommended=()):
        self.responses = responses
        self.recommended = recommended
        self.calls = []

    def list_responses(self, job_id, since=None):
        self.calls.append((job_id, since))
        return list(self.responses)

    def list_recommended(self, job_id):
        return list(self.recommended)


class BaseTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("recruiter", password="pass-12345-word")
        cls.company = Company.objects.create(name="Львівський завод")
        cls.vacancy = Vacancy.objects.create(
            company=cls.company, title="Оператор лінії", workua_job_id="1234567", recruiter=cls.user
        )


class ParsingTests(TestCase):
    def test_resume_link(self):
        self.assertEqual(parse_workua_resume_id("https://www.work.ua/resumes/7351234/"), "7351234")
        self.assertEqual(parse_workua_resume_id("https://www.work.ua/ru/resumes/12/?utm=x"), "12")
        self.assertEqual(parse_workua_resume_id("https://example.com/cv.pdf"), "")

    def test_workua_links(self):
        self.assertEqual(parse_workua_link("https://www.work.ua/employer/my/applicants/41468274/"), ("response", "41468274"))
        self.assertEqual(parse_workua_link("https://www.work.ua/my/recommended/4157701/"), ("recommended", "4157701"))
        self.assertEqual(parse_workua_link("https://www.work.ua/resumes/7351234/"), ("sourced", "7351234"))
        self.assertEqual(parse_workua_link("https://example.com"), ("", ""))

    def test_sheet_statuses(self):
        S = Application.Stage
        self.assertEqual(map_status("Написала у вайбер"), S.VIBER)
        self.assertEqual(map_status("Не підходить"), S.REJECTED_BY_US)
        self.assertEqual(map_status("Не актуальна"), S.REJECTED_BY_CANDIDATE)
        self.assertEqual(map_status("Думає"), S.THINKING)
        self.assertEqual(map_status("Зателефонувати"), S.NEW)
        self.assertIsNone(map_status("щось дивне"))

    def test_phone_formats(self):
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
    def response(self, rid, name, phone="", resume_id="", channel=Application.Channel.RESPONSE):
        return WorkUaCandidate(
            external_id=rid,
            full_name=name,
            channel=channel,
            phone=phone,
            resume_id=resume_id,
            applied_at=datetime(2026, 11, 20, 10, 0, tzinfo=dt_timezone.utc),
        )

    def test_responses_and_recommended_go_to_separate_sections(self):
        client = FakeClient(
            [self.response("r1", "Олена", "0501112233")],
            [self.response("rec1", "Марко", "0632223344", channel=Application.Channel.RECOMMENDED)],
        )
        result = sync_vacancy(self.vacancy, client=client)
        self.assertEqual(result.created_by_channel, {"response": 1, "recommended": 1})
        channels = dict(self.vacancy.applications.values_list("candidate__full_name", "channel"))
        self.assertEqual(channels, {"Олена": "response", "Марко": "recommended"})

    def test_recommended_candidate_who_responds_moves_to_responses(self):
        rec = self.response("rec1", "Марко", "0632223344", "res9", channel=Application.Channel.RECOMMENDED)
        sync_vacancy(self.vacancy, client=FakeClient([], [rec]))
        application = self.vacancy.applications.get()
        change_stage(application, Application.Stage.THINKING)

        sync_vacancy(self.vacancy, client=FakeClient([self.response("r5", "Марко", "063 222 33 44")], [rec]))
        application.refresh_from_db()
        self.assertEqual(self.vacancy.applications.count(), 1)
        self.assertEqual(application.channel, Application.Channel.RESPONSE)
        self.assertEqual(application.stage, Application.Stage.THINKING)  # статус не скидається

    def test_same_recommendation_for_two_vacancies(self):
        other = Vacancy.objects.create(company=self.company, title="Комірник", workua_job_id="999")
        rec = self.response("res1", "Марко", "0632223344", "res1", channel=Application.Channel.RECOMMENDED)
        sync_vacancy(self.vacancy, client=FakeClient([], [rec]))
        sync_vacancy(other, client=FakeClient([], [rec]))
        self.assertEqual(Candidate.objects.count(), 1)
        self.assertEqual(Application.objects.count(), 2)

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
        response = self.client.post(
            url, {"full_name": "Іван", "phone": "067-123-45-67", "source": "manual", "channel": "sourced"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Candidate.objects.count(), 1)
        self.assertEqual(self.vacancy.applications.get().channel, Application.Channel.SOURCED)

    def test_add_candidate_by_workua_resume_link(self):
        url = reverse("recruiting:vacancy_add_candidate", args=[self.vacancy.pk])
        data = {
            "full_name": "Марта Левицька",
            "phone": "0931112233",
            "resume_url": "https://www.work.ua/resumes/7351234/",
            "source": "manual",
            "channel": "sourced",
        }
        self.client.post(url, data)
        candidate = Candidate.objects.get()
        self.assertEqual(candidate.workua_resume_id, "7351234")
        self.assertEqual(candidate.source, Candidate.Source.WORKUA)

        # Те саме резюме, інший телефон — це все одно та сама людина.
        other = Vacancy.objects.create(company=self.company, title="Комірник")
        self.client.post(
            reverse("recruiting:vacancy_add_candidate", args=[other.pk]), {**data, "phone": "0500000000"}
        )
        self.assertEqual(Candidate.objects.count(), 1)
        self.assertEqual(candidate.applications.count(), 2)

    def test_vacancy_page_sections(self):
        for i, channel in enumerate(Application.Channel.values):
            c = Candidate.objects.create(full_name=f"Кандидат {channel}", phone=f"06700000{i}0")
            attach_candidate(self.vacancy, c, channel=channel)
        url = reverse("recruiting:vacancy_detail", args=[self.vacancy.pk])
        page = self.client.get(url)  # за замовчуванням — «Відгуки»
        self.assertContains(page, "Кандидат response")
        self.assertNotContains(page, "Кандидат recommended")
        page = self.client.get(url, {"channel": "sourced"})
        self.assertContains(page, "Кандидат sourced")
        self.assertNotContains(page, "Кандидат response")
        self.assertContains(self.client.get(url, {"channel": "all"}), "Кандидат recommended")

    def test_interview_requires_date(self):
        candidate = Candidate.objects.create(full_name="Іван Петренко", phone="0671234567")
        application, _ = attach_candidate(self.vacancy, candidate)
        url = reverse("recruiting:application_stage", args=[application.pk])

        response = self.client.post(url, {"stage": "interview"})
        self.assertIn("stage=interview", response["Location"])
        application.refresh_from_db()
        self.assertEqual(application.stage, Application.Stage.NEW)

        self.client.post(url, {"stage": "interview", "interview_at": "2026-11-25T10:30"})
        application.refresh_from_db()
        self.assertEqual(application.stage, Application.Stage.INTERVIEW)
        self.assertIsNotNone(application.interview_at)
        self.assertIn("Співбесіда: 25.11.2026 10:30", application.events.filter(kind="stage").get().text)

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


SHEET_CSV = """Лінк,Ім’я,Вік,Контакт,Район,Статус,Примітка
https://www.work.ua/employer/my/applicants/41468274/,Володимир,20,093 181-88-75,Залізничний,Написала у вайбер,"досвіду нема, студент"
https://www.work.ua/employer/my/applicants/41627698/,Марко,29,099 611-75-63,Галицький,Не підходить,досвід хореки
https://www.work.ua/employer/my/recommended/4157701/,Руслан,23,050 147-01-38,Личаківський,Думає,експедитор більше року
,Андрій,37,063 211-49-97,?,Щось інше,
,,,,,Зателефонувати,
"""


class ImportTests(BaseTestCase):
    def test_import_sheet(self):
        result = import_csv(self.vacancy, SHEET_CSV.encode("utf-8"), default_channel="sourced", user=self.user)
        self.assertEqual((result.created, result.skipped_empty, result.errors), (4, 1, []))

        volodymyr = self.vacancy.applications.get(candidate__full_name="Володимир")
        self.assertEqual(volodymyr.channel, Application.Channel.RESPONSE)
        self.assertEqual(volodymyr.stage, Application.Stage.VIBER)
        self.assertEqual(volodymyr.note, "досвіду нема, студент")
        self.assertEqual(volodymyr.candidate.age, 20)
        self.assertEqual(volodymyr.candidate.phone, "+380931818875")
        self.assertEqual(volodymyr.candidate.district, Candidate.District.ZALIZNYCHNYI)

        self.assertEqual(volodymyr.candidate.phone_display, "093 181-88-75")
        names = list(self.vacancy.applications.values_list("candidate__full_name", flat=True))
        self.assertEqual(names, ["Володимир", "Марко", "Руслан", "Андрій"])  # порядок як у таблиці

        ruslan = self.vacancy.applications.get(candidate__full_name="Руслан")
        self.assertEqual((ruslan.channel, ruslan.stage), ("recommended", "thinking"))

        andrii = self.vacancy.applications.get(candidate__full_name="Андрій")
        self.assertEqual((andrii.channel, andrii.stage), ("sourced", "new"))
        self.assertIn("Статус у таблиці: Щось інше", andrii.note)

        # Повторний імпорт — без дублікатів.
        again = import_csv(self.vacancy, SHEET_CSV.encode("utf-8"), default_channel="sourced")
        self.assertEqual((again.created, again.already_present), (0, 4))
        self.assertEqual(Candidate.objects.count(), 4)

    def test_import_view(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.force_login(self.user)
        url = reverse("recruiting:vacancy_import", args=[self.vacancy.pk])
        upload = SimpleUploadedFile("sheet.csv", SHEET_CSV.encode("utf-8"), content_type="text/csv")
        response = self.client.post(url, {"file": upload, "channel": "sourced"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.vacancy.applications.count(), 4)

    def test_import_rejects_unknown_columns(self):
        with self.assertRaises(ValueError):
            import_csv(self.vacancy, "a,b\n1,2\n".encode(), default_channel="sourced")


class InlineNoteTests(BaseTestCase):
    def test_note_saved_via_fetch(self):
        self.client.force_login(self.user)
        candidate = Candidate.objects.create(full_name="Іван", phone="0671234567")
        application, _ = attach_candidate(self.vacancy, candidate)
        url = reverse("recruiting:application_note_inline", args=[application.pk])
        response = self.client.post(url, {"note": "досвід кур'єром 4 місяці"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(response.json(), {"ok": True})
        application.refresh_from_db()
        self.assertEqual(application.note, "досвід кур'єром 4 місяці")

    def test_add_candidate_from_applicant_link(self):
        self.client.force_login(self.user)
        url = reverse("recruiting:vacancy_add_candidate", args=[self.vacancy.pk])
        data = {
            "channel": "sourced",  # буде перевизначено посиланням
            "resume_url": "https://www.work.ua/employer/my/applicants/41468274/",
            "full_name": "Володимир",
            "age": "20",
            "phone": "093 181-88-75",
            "district": "zaliznychnyi",
            "note": "студент",
            "source": "manual",
        }
        self.client.post(url, data)
        application = self.vacancy.applications.get()
        self.assertEqual(application.channel, Application.Channel.RESPONSE)
        self.assertEqual(application.workua_response_id, "41468274")
        self.assertEqual(application.note, "студент")
        # Те саме посилання вдруге — не дублюється.
        self.client.post(url, {**data, "phone": "0500000000"})
        self.assertEqual(self.vacancy.applications.count(), 1)
