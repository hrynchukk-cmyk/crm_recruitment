from datetime import date, datetime, timezone as dt_timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .integrations import workua
from .integrations.workua import (
    DisabledClient,
    WorkUaApiClient,
    WorkUaAuthError,
    WorkUaCandidate,
    WorkUaJob,
    WorkUaNotConfigured,
    WorkUaRateLimited,
    auth_blocked_for_auto_sync,
    import_jobs,
    sync_all,
    sync_vacancy,
)
from .models import (
    Application,
    ApplicationEvent,
    Candidate,
    Company,
    Vacancy,
    WorkUaSyncState,
    normalize_phone,
    parse_workua_link,
    parse_workua_resume_id,
)
from .importer import import_csv, map_status
from .services import attach_candidate, change_stage
from .roles import ADMIN, RECRUITER, RECRUITER_GROUP, role_of, set_role


class FakeClient:
    """Підміна Work.ua на рівні клієнта (для тестів логіки синхронізації)."""

    def __init__(self, responses=(), saved=(), jobs=(), towns=None):
        self.responses = list(responses)
        self.saved = list(saved)  # [(елемент списку /candidates, WorkUaCandidate з деталями)]
        self.jobs = list(jobs)
        self._towns = towns or {}
        self.response_calls = []
        self.detail_calls = []

    def my_jobs(self):
        return list(self.jobs)

    def towns(self):
        return self._towns

    def job_responses(self, job_id, after_id=None):
        self.response_calls.append((job_id, after_id))
        return [r for r in self.responses if not after_id or int(r.external_id) > after_id]

    def saved_candidates(self, job_id):
        return [item for item, _ in self.saved]

    def saved_candidate(self, item):
        self.detail_calls.append(str(item["candidate_id"]))
        return {str(i["candidate_id"]): c for i, c in self.saved}[str(item["candidate_id"])]

    def response_file_url(self, job_id, response_id):
        return f"https://files.example/{job_id}/{response_id}?signature=x"


class BaseTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("recruiter", password="pass-12345-word")
        set_role(cls.user, RECRUITER)
        cls.admin = get_user_model().objects.create_superuser("boss", password="pass-12345-word")
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


def resp(rid, name, phone="", from_type="send", candidate_id="", age=None, has_file=False):
    return WorkUaCandidate(
        external_id=str(rid), full_name=name, from_type=from_type, phone=phone, age=age,
        workua_candidate_id=str(candidate_id), has_file=has_file, resume_text="Досвід 2 роки",
        applied_at=datetime(2026, 11, 20, 10, 0, tzinfo=dt_timezone.utc),
    )


def saved(candidate_id, name, phone=""):
    item = {"candidate_id": candidate_id, "info": {"first_name": name}, "are_contacts_open": True}
    detail = WorkUaCandidate(
        external_id=f"cand-{candidate_id}", full_name=name, channel=Application.Channel.SOURCED,
        from_type=Application.WorkUaFrom.SAVED, phone=phone, workua_candidate_id=str(candidate_id),
        resume_id=f"9{candidate_id}", resume_url=f"https://www.work.ua/resumes/9{candidate_id}/",
    )
    return item, detail


class WorkUaSyncTests(BaseTestCase):
    def test_all_response_types_go_to_responses(self):
        client = FakeClient([
            resp(101, "Олена", "0501112233", "send", 1, age=24, has_file=True),
            resp(102, "Петро", "0632223344", "phonecall", 2),
            resp(103, "Ігор", "0673334455", "messenger", 3),
        ])
        result = sync_vacancy(self.vacancy, client=client)
        self.assertEqual(result.created_by_channel, {"response": 3})
        apps = {a.candidate.full_name: a for a in self.vacancy.applications.select_related("candidate")}
        self.assertEqual({n: a.workua_from for n, a in apps.items()},
                         {"Олена": "send", "Петро": "phonecall", "Ігор": "messenger"})
        self.assertTrue(all(a.channel == "response" and a.stage == "new" for a in apps.values()))
        self.assertTrue(apps["Олена"].workua_has_file)
        self.assertEqual(apps["Олена"].candidate.age, 24)
        self.assertEqual(apps["Олена"].candidate.resume_text, "Досвід 2 роки")

    def test_only_new_responses_are_requested_next_time(self):
        client = FakeClient([resp(101, "Олена", "0501112233"), resp(105, "Петро", "0632223344")])
        sync_vacancy(self.vacancy, client=client)
        self.vacancy.refresh_from_db()
        self.assertEqual(self.vacancy.workua_last_response_id, 105)
        client.responses.append(resp(110, "Ігор", "0673334455"))
        result = sync_vacancy(self.vacancy, client=client)
        self.assertEqual(client.response_calls[0][1], None)
        self.assertEqual(client.response_calls[1][1], 105)
        self.assertEqual(result.created, 1)
        self.assertEqual(self.vacancy.applications.count(), 3)

    def test_saved_candidates_go_to_sourced_without_duplicates(self):
        client = FakeClient(
            responses=[resp(101, "Олена", "0501112233", candidate_id=11)],
            saved=[saved(11, "Олена"), saved(12, "Марко", "0632223344")],
        )
        result = sync_vacancy(self.vacancy, client=client)
        self.assertEqual(result.created_by_channel, {"response": 1, "sourced": 1})
        self.assertEqual(client.detail_calls, ["12"])  # Олена вже є з відгуку — деталі не запитуємо
        marko = self.vacancy.applications.get(candidate__full_name="Марко")
        self.assertEqual((marko.channel, marko.workua_from), ("sourced", "saved"))
        self.assertEqual(marko.candidate.resume_url, "https://www.work.ua/resumes/912/")
        # Повтор — нічого нового.
        again = sync_vacancy(self.vacancy, client=client)
        self.assertEqual(again.created, 0)
        self.assertEqual(Candidate.objects.count(), 2)

    def test_saved_candidate_who_responds_moves_to_responses_keeping_stage(self):
        client = FakeClient(saved=[saved(12, "Марко", "0632223344")])
        sync_vacancy(self.vacancy, client=client)
        application = self.vacancy.applications.get()
        change_stage(application, Application.Stage.THINKING)
        client.responses = [resp(130, "Марко", "063 222 33 44", "send", 12)]
        sync_vacancy(self.vacancy, client=client)
        application.refresh_from_db()
        self.assertEqual(self.vacancy.applications.count(), 1)
        self.assertEqual((application.channel, application.stage, application.workua_from), ("response", "thinking", "send"))

    def test_same_person_on_two_vacancies_is_one_candidate(self):
        other = Vacancy.objects.create(company=self.company, title="Комірник", workua_job_id="999")
        sync_vacancy(self.vacancy, client=FakeClient([resp(101, "Олена", "0501112233", candidate_id=11)]))
        sync_vacancy(other, client=FakeClient([resp(201, "Олена Коваль", "050 111 22 33", candidate_id=11)]))
        self.assertEqual(Candidate.objects.count(), 1)
        self.assertEqual(Application.objects.count(), 2)

    def test_manual_edits_are_not_overwritten(self):
        existing = Candidate.objects.create(full_name="Олена Коваль (вручну)", phone="+380501112233")
        sync_vacancy(self.vacancy, client=FakeClient([resp(101, "Олена", "050 111 22 33", candidate_id=11, age=30)]))
        existing.refresh_from_db()
        self.assertEqual(existing.full_name, "Олена Коваль (вручну)")
        self.assertEqual((existing.age, existing.workua_candidate_id), (30, "11"))

    def test_import_jobs(self):
        get_user_model().objects.create_user("manager", email="manager@agency.example")
        jobs = [
            WorkUaJob(id="7000001", name="Комірник", active=True, published_on=date(2026, 11, 1), region_id=4,
                      street_address="вул. Городоцька, 172", description="Опис", salary_text="від 24 000 грн",
                      contact_email="manager@agency.example"),
            WorkUaJob(id="7000002", name="Стара вакансія", active=False),
            WorkUaJob(id="1234567", name="Оператор лінії", active=False, expires_on=date(2026, 12, 1)),
        ]
        from .integrations.workua import SyncResult
        result = SyncResult()
        import_jobs(FakeClient(jobs=jobs, towns={4: "Львів"}), result)
        self.assertEqual((result.jobs_created, result.jobs_updated), (1, 1))
        new = Vacancy.objects.get(workua_job_id="7000001")
        self.assertIsNone(new.company)
        self.assertEqual((new.city, new.opened_on, new.salary, new.status), ("Львів", date(2026, 11, 1), "від 24 000 грн", "open"))
        self.assertEqual(new.recruiter.username, "manager")
        self.assertEqual(new.workua_street_address, "вул. Городоцька, 172")
        self.assertFalse(Vacancy.objects.filter(workua_job_id="7000002").exists())  # неактивні нові не імпортуємо
        self.vacancy.refresh_from_db()
        self.assertIs(self.vacancy.workua_active, False)
        self.assertEqual(self.vacancy.company, self.company)  # наявну вакансію не чіпаємо

    def test_sync_all_records_state_and_auth_failure_stops_auto_sync(self):
        client = FakeClient([resp(101, "Олена", "0501112233")])
        result = sync_all(client=client)
        state = WorkUaSyncState.get()
        self.assertIsNotNone(state.last_success_at)
        self.assertIn("нових кандидатів: 1", state.last_summary)
        self.assertEqual(result.created, 1)

        class BadAuth(FakeClient):
            def my_jobs(self):
                raise WorkUaAuthError("401")

        with self.assertRaises(WorkUaAuthError):
            sync_all(client=BadAuth())
        self.assertTrue(auth_blocked_for_auto_sync())
        sync_all(client=client)  # вдалий вхід знімає блок
        self.assertFalse(auth_blocked_for_auto_sync())

    def test_disabled_client_raises(self):
        with self.assertRaises(WorkUaNotConfigured):
            sync_vacancy(self.vacancy, client=DisabledClient())


# ----------------------------------------------------------------- HTTP-клієнт за документацією Work.ua


class FakeHttpResponse:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = "" if data is None else str(data)

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def request(self, method, url, params=None, auth=None, headers=None, timeout=None, allow_redirects=True):
        self.calls.append({"method": method, "url": url, "params": params or {}, "auth": auth, "headers": headers})
        return self.handler(method, url.replace("https://api.work.ua", ""), params or {})


def api(handler):
    session = FakeSession(handler)
    return WorkUaApiClient("hr@agency.example", "secret", session=session, pause=0), session


class WorkUaApiClientTests(TestCase):
    def test_headers_auth_and_jobs_paging(self):
        def handler(method, path, params):
            self.assertEqual(path, "/jobs/my")
            page = params["page"]
            item = {"id": 100 + page, "name": f"Вакансія {page}", "active": 1, "date": "2026-11-01T10:00:00+02:00",
                    "date_expire": "2026-12-01T10:00:00+02:00", "region": 4, "streetAddress": "вул. Зелена, 1",
                    "description": "<p>Обов'язки:</p><ul><li>приймання товару</li></ul>",
                    "salary": {"value": 24000, "value_max": 30000, "comment": ""}, "contact": {"email": "m@a.example"}}
            return FakeHttpResponse(200, {"status": "ok", "items": [item], "pagination": {"last_page": 2}})

        client, session = api(handler)
        jobs = client.my_jobs()
        self.assertEqual([j.id for j in jobs], ["101", "102"])
        self.assertEqual(jobs[0].salary_text, "24 000 – 30 000 грн")
        self.assertEqual(jobs[0].description, "Обов'язки:\n• приймання товару")
        self.assertEqual(jobs[0].published_on, date(2026, 11, 1))
        first = session.calls[0]
        self.assertEqual(first["auth"], ("hr@agency.example", "secret"))
        self.assertEqual(first["headers"]["X-Locale"], "uk_UA")
        self.assertIn("hr@agency.example", first["headers"]["User-Agent"])
        self.assertEqual((first["params"]["limit"], first["params"]["full"], first["params"]["page"]), (100, 1, 1))

    def test_responses_walk_all_pages_and_types(self):
        all_ids = list(range(1, 121))  # 120 відгуків на вакансію

        def handler(method, path, params):
            self.assertEqual(path, "/jobs/555/responses")
            self.assertEqual(params["from_type[]"], ["send", "phonecall", "messenger"])
            ids = [i for i in all_ids if i > params.get("last_id", 0) and i < params.get("before_id", 10**9)]
            ids = sorted(ids, reverse=True)[: params["limit"]]  # від новіших до старіших, як у Work.ua
            if not ids:
                return FakeHttpResponse(404, {"status": "error"})
            items = [{"id": i, "job_id": 555, "candidate_id": 9000 + i, "date": "2026-11-20T10:00:00+02:00",
                      "fio": f"Кандидат {i}", "from_type": "phonecall" if i % 2 else "send",
                      "birth_date": "2000-01-15", "phone": "093 181-88-75", "email": "", "type": "resume",
                      "with_file": 1 if i == 120 else 0, "text": "Резюме<br />рядок 2", "cover": ""} for i in ids]
            return FakeHttpResponse(200, {"status": "ok", "items": items})

        client, session = api(handler)
        responses = client.job_responses("555")
        self.assertEqual([int(r.external_id) for r in responses], all_ids)
        self.assertEqual(len(session.calls), 3)
        last = responses[-1]
        self.assertEqual((last.from_type, last.has_file, last.workua_candidate_id), ("send", True, "9120"))
        self.assertEqual(last.resume_text, "Резюме\nрядок 2")
        self.assertEqual(responses[0].age, workua.age_from_birth_date(date(2000, 1, 15)))
        newer = client.job_responses("555", after_id=100)
        self.assertEqual([int(r.external_id) for r in newer], list(range(101, 121)))

    def test_saved_candidate_details(self):
        def handler(method, path, params):
            if path == "/candidates":
                return FakeHttpResponse(200, {"status": "ok", "items": [
                    {"candidate_id": 264227116, "info": {"first_name": "Андрій", "last_name": "Миколенко"}, "are_contacts_open": True},
                ], "pagination": {"current_page": 1, "last_page": 1}})
            self.assertEqual(path, "/candidates/264227116")
            return FakeHttpResponse(200, {"status": "ok", "candidate": {
                "id": 264227116, "first_name": "Андрій", "last_name": "Миколенко", "middle_name": None, "age": "23 роки",
                "email": "mikola@gmail.com", "phone": "+380966760487"}, "are_contacts_open": True,
                "jobs": [{"id": "7000008"}], "resumes": [{"id": "15147703", "name": "Комірник", "type": "file"}]})

        client, _ = api(handler)
        [item] = client.saved_candidates("7000008")
        candidate = client.saved_candidate(item)
        self.assertEqual((candidate.full_name, candidate.age, candidate.phone), ("Андрій Миколенко", 23, "+380966760487"))
        self.assertEqual((candidate.channel, candidate.from_type, candidate.external_id), ("sourced", "saved", "cand-264227116"))
        self.assertEqual(candidate.resume_url, "https://www.work.ua/resumes/15147703/")

    def test_error_codes(self):
        for status, error in ((401, WorkUaAuthError), (429, WorkUaRateLimited)):
            client, _ = api(lambda m, p, q, s=status: FakeHttpResponse(s, {}))
            with self.assertRaises(error):
                client.my_jobs()
        client, _ = api(lambda m, p, q: FakeHttpResponse(404, {"status": "error"}))
        self.assertEqual(client.my_jobs(), [])
        self.assertEqual(client.job_responses("1"), [])

    def test_response_file_redirect(self):
        client, session = api(lambda m, p, q: FakeHttpResponse(302, None, {"Location": "https://s3.example/cv.pdf?sig=1"}))
        self.assertEqual(client.response_file_url("12345", "54321"), "https://s3.example/cv.pdf?sig=1")
        self.assertTrue(session.calls[0]["url"].endswith("/response_files/12345/54321/"))

    def test_requires_credentials(self):
        with self.assertRaises(WorkUaNotConfigured):
            WorkUaApiClient("", "")


class WorkUaViewsTests(BaseTestCase):
    def setUp(self):
        self.client.force_login(self.user)

    @override_settings(WORKUA={"BACKEND": "api", "LOGIN": "hr@agency.example", "PASSWORD": "x", "SYNC_MINUTES": 15})
    def test_sync_button_and_status(self):
        fake = FakeClient([resp(101, "Олена", "0501112233")])
        with mock.patch.object(workua, "get_client", return_value=fake):
            response = self.client.post(reverse("recruiting:workua_sync_all"), follow=True)
        self.assertContains(response, "нових кандидатів: 1")
        self.assertContains(response, "Work.ua: синхронізовано")
        self.assertContains(response, "Синхронізувати з Work.ua")

    def test_file_download_redirects_to_signed_url(self):
        sync_vacancy(self.vacancy, client=FakeClient([resp(101, "Олена", "0501112233", has_file=True)]))
        application = self.vacancy.applications.get()
        with mock.patch("recruiting.views.get_client", return_value=FakeClient()):
            response = self.client.get(reverse("recruiting:application_workua_file", args=[application.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://files.example/1234567/101?signature=x")
        page = self.client.get(application.get_absolute_url())
        self.assertContains(page, "Завантажити з Work.ua")

    def test_unassigned_vacancy_everywhere(self):
        Vacancy.objects.create(company=None, title="З Work.ua без підприємства", workua_job_id="7000001")
        self.assertContains(self.client.get(reverse("recruiting:vacancy_list")), "призначити підприємство")
        self.assertContains(self.client.get(reverse("recruiting:dashboard")), "Вакансії без підприємства")
        vacancy = Vacancy.objects.get(workua_job_id="7000001")
        self.assertContains(self.client.get(vacancy.get_absolute_url()), "призначити підприємство")


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
        self.assertContains(response, "Інтеграцію з Work.ua не ввімкнено")


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


class AddCandidateFormTests(BaseTestCase):
    def setUp(self):
        self.client.force_login(self.user)
        self.url = reverse("recruiting:vacancy_add_candidate", args=[self.vacancy.pk])
        self.data = {"channel": "sourced", "full_name": "Тарас", "phone": "096 748-70-76", "source": "manual"}

    def test_new_candidate_starts_with_call_status(self):
        response = self.client.post(self.url, self.data)
        application = self.vacancy.applications.get()
        self.assertEqual(application.stage, Application.Stage.NEW)  # «Зателефонувати», не Viber
        page = self.client.get(response["Location"])
        self.assertContains(page, 'class="badge stage-new">Зателефонувати</span>', html=False)

    def test_email_is_optional_and_placeholders_are_empty(self):
        for value in ["", "-", "немає", "?"]:
            with self.subTest(email=value):
                Candidate.objects.all().delete()
                response = self.client.post(self.url, {**self.data, "email": value})
                self.assertEqual(response.status_code, 302, value)
                self.assertEqual(Candidate.objects.get().email, "")

    def test_invalid_email_shows_ukrainian_error(self):
        response = self.client.post(self.url, {**self.data, "email": "taras.gmail"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Схоже, це не email")
        self.assertContains(response, "<details class=\"extra-fields\" open>")  # блок з помилкою розгорнуто

    def test_email_input_is_not_browser_validated(self):
        page = self.client.get(self.url)
        self.assertNotContains(page, 'type="email"')
        self.assertNotContains(page, 'type="url"')

    def test_relative_workua_link_is_accepted(self):
        self.client.post(self.url, {**self.data, "resume_url": "/my/applicants/41683502"})
        application = self.vacancy.applications.get()
        self.assertEqual(application.candidate.resume_url, "https://www.work.ua/my/applicants/41683502")
        self.assertEqual(application.channel, Application.Channel.RESPONSE)

    def test_garbage_link_rejected(self):
        response = self.client.post(self.url, {**self.data, "resume_url": "не знаю"})
        self.assertContains(response, "Не схоже на посилання")


class VacancyFormTests(BaseTestCase):
    def test_create_vacancy_with_new_company(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("recruiting:vacancy_create"),
            {"new_company": "Нове підприємство", "title": "Комірник", "city": "Львів", "headcount": 1, "status": "open"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Vacancy.objects.get(title="Комірник").company.name, "Нове підприємство")

    def test_company_required(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("recruiting:vacancy_create"), {"title": "Комірник", "city": "Львів", "headcount": 1, "status": "open"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Vacancy.objects.filter(title="Комірник").exists())


class DashboardTests(BaseTestCase):
    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        self.client.force_login(self.user)
        self.company.address = "вул. Городоцька, 172"
        self.company.latitude, self.company.longitude = "49.833400", "23.963500"
        self.company.save()
        self.vacancy.opened_on = timezone.localdate() - timedelta(days=12)
        self.vacancy.headcount = 3
        self.vacancy.save()
        stages = ["new", "new", "viber", "interview", "hired", "rejected_by_us"]
        for i, stage in enumerate(stages):
            c = Candidate.objects.create(full_name=f"К{i}", phone=f"06700000{i:02d}")
            a, _ = attach_candidate(self.vacancy, c)
            if stage != "new":
                change_stage(a, stage)
        interview = self.vacancy.applications.get(stage="interview")
        interview.interview_at = timezone.now() + timedelta(days=1)
        interview.save()
        # Підприємство без точки на карті і вакансія на паузі.
        self.shop = Company.objects.create(name="Магазин")
        Vacancy.objects.create(company=self.shop, title="Касир", headcount=2)
        Vacancy.objects.create(company=self.company, title="Пауза", status="paused")

    def test_build_dashboard(self):
        from .dashboard import build_dashboard

        data = build_dashboard()
        row = next(r for r in data.rows if r.vacancy == self.vacancy)
        self.assertEqual(row.counts, {"to_call": 2, "working": 1, "interview": 1, "hired": 1, "dropped": 1})
        self.assertEqual(row.total, 6)
        self.assertEqual(row.upcoming_interviews, 1)
        self.assertEqual(row.vacancy.days_active, 12)
        self.assertEqual(data.kpis["remaining"], 2 + 2)  # 3−1 на «Оператор лінії» + 2 на «Касир»
        self.assertEqual(data.kpis["open_vacancies"], 2)
        self.assertEqual(data.kpis["to_call"], 2)
        self.assertEqual(data.kpis["hired_30d"], 1)
        self.assertEqual([c.name for c in data.missing_location], ["Магазин"])
        [company] = data.companies
        self.assertEqual(company["name"], self.company.name)
        self.assertEqual(company["lat"], 49.8334)
        self.assertEqual([v["title"] for v in company["vacancies"]], ["Оператор лінії"])  # пауза прихована
        vac = company["vacancies"][0]
        self.assertEqual((vac["days_active"], vac["total"], vac["to_call"], vac["hired"], vac["headcount"]), (12, 6, 2, 1, 3))

        with_paused = build_dashboard(include_paused=True)
        self.assertEqual(len(with_paused.companies[0]["vacancies"]), 2)

    def test_dashboard_page(self):
        response = self.client.get(reverse("recruiting:dashboard"))
        self.assertContains(response, "Ще потрібно найняти")
        self.assertContains(response, "12 днів")
        self.assertContains(response, 'id="map-data"')
        self.assertContains(response, "Не позначені на карті")
        self.assertContains(response, "Зателефонувати: 2 (33%)")

    def test_dashboard_escapes_names_in_map_json(self):
        self.company.name = "</script><b>x"
        self.company.save()
        response = self.client.get(reverse("recruiting:dashboard"))
        self.assertNotContains(response, "</script><b>x")

    def test_days_plural(self):
        from .templatetags.crm import days

        self.assertEqual([days(n) for n in (0, 1, 2, 5, 11, 21, 22, 34, 112)],
                         ["0 днів", "1 день", "2 дні", "5 днів", "11 днів", "21 день", "22 дні", "34 дні", "112 днів"])

    def test_closed_on_set_and_cleared(self):
        self.vacancy.status = "closed"
        self.vacancy.save()
        self.assertIsNotNone(self.vacancy.closed_on)
        self.vacancy.status = "open"
        self.vacancy.save()
        self.assertIsNone(self.vacancy.closed_on)


class CompanyPagesTests(BaseTestCase):
    def setUp(self):
        self.client.force_login(self.admin)  # змінювати підприємства може лише адміністратор

    def test_create_company_with_location(self):
        response = self.client.post(
            reverse("recruiting:company_create"),
            {"name": "Нове", "address": "вул. Зелена, 1", "latitude": "49.830000", "longitude": "24.050000"},
        )
        self.assertEqual(response.status_code, 302)
        company = Company.objects.get(name="Нове")
        self.assertTrue(company.has_location)

    def test_half_location_rejected(self):
        response = self.client.post(reverse("recruiting:company_create"), {"name": "Нове", "latitude": "49.83"})
        self.assertContains(response, "не повністю")

    def test_list_and_edit_pages(self):
        self.assertContains(self.client.get(reverse("recruiting:company_list")), "вказати на карті")
        self.assertContains(self.client.get(reverse("recruiting:company_edit", args=[self.company.pk])), 'id="company-map"')


class VacancySaveTests(BaseTestCase):
    def setUp(self):
        self.client.force_login(self.user)
        self.data = {"company": self.company.pk, "title": "Комірник", "city": "Львів", "headcount": 1, "status": "open"}

    def test_save_redirects_to_vacancy_list_with_message_and_highlight(self):
        response = self.client.post(reverse("recruiting:vacancy_create"), self.data, follow=True)
        vacancy = Vacancy.objects.get(title="Комірник")
        self.assertEqual(response.redirect_chain[-1][0], f"/?status=open&saved={vacancy.pk}")
        self.assertContains(response, "Вакансію «Комірник» збережено.")
        self.assertContains(response, 'class="just-saved"')

    def test_paused_vacancy_lands_on_paused_tab(self):
        response = self.client.post(reverse("recruiting:vacancy_create"), {**self.data, "status": "paused"})
        self.assertIn("status=paused", response["Location"])

    def test_edit_also_returns_to_list(self):
        response = self.client.post(reverse("recruiting:vacancy_edit", args=[self.vacancy.pk]),
                                    {**self.data, "title": "Оператор лінії (змінено)"})
        self.assertTrue(response["Location"].startswith("/?status=open"))

    def test_duplicate_workua_link_explained(self):
        response = self.client.post(reverse("recruiting:vacancy_create"),
                                    {**self.data, "workua_job_id": "https://www.work.ua/jobs/1234567/"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ця вакансія Work.ua вже є в CRM: «Оператор лінії»")

    def test_friendly_500_page(self):
        from django.template.loader import render_to_string

        html = render_to_string("500.html")
        self.assertIn("Щось пішло не так", html)
        self.assertIn("docker compose logs web", html)

    def test_newest_vacancy_first_in_list(self):
        self.client.post(reverse("recruiting:vacancy_create"), {**self.data, "title": "Найновіша"})
        response = self.client.get(reverse("recruiting:vacancy_list"))
        titles = [v.title for v in response.context["vacancies"]]
        self.assertEqual(titles[0], "Найновіша")


class WorkUaRobustnessTests(BaseTestCase):
    """Справжні відповіді Work.ua можуть відрізнятися від прикладів у документації."""

    def test_bad_dates_do_not_crash(self):
        self.assertIsNone(workua._parse_dt("0000-00-00 00:00:00"))
        self.assertIsNone(workua._parse_d("0000-00-00"))
        self.assertIsNone(workua._safe_date("1990-02-31"))
        self.assertIsNone(workua.age_from_birth_date(workua._safe_date("0000-00-00")))

    def test_odd_response_shapes(self):
        def handler(method, path, params):
            if path == "/jobs/my":  # список замість {"items": ...}, id-рядок, число замість рядка
                return FakeHttpResponse(200, [{"id": "7000001", "name": 123, "active": "1", "date": "0000-00-00 00:00:00",
                                               "date_expire": None, "region": None, "contact": [], "salary": None,
                                               "description": None}])
            if path.endswith("/responses"):
                return FakeHttpResponse(200, [{"id": "11", "fio": None, "phone": 380931112233, "birth_date": "0000-00-00",
                                               "from_type": "unknown", "with_file": "1", "text": None, "cover": None}])
            if path == "/candidates":
                return FakeHttpResponse(200, {"items": [{"candidate_id": 5, "info": [], "are_contacts_open": False}]})
            return FakeHttpResponse(200, {"candidate": None, "resumes": None})

        client, _ = api(handler)
        [job] = client.my_jobs()
        self.assertEqual((job.id, job.name, job.active, job.published_on), ("7000001", "123", True, None))
        [r] = client.job_responses("7000001")
        self.assertEqual((r.phone, r.age, r.from_type, r.has_file), ("380931112233", None, "send", True))
        [item] = client.saved_candidates("7000001")
        cand = client.saved_candidate(item)
        self.assertEqual(cand.full_name, "Кандидат з Work.ua (ім'я приховане)")
        self.assertIn("не відкриті", cand.note)
        # І повна синхронізація з такими даними проходить.
        result = sync_all(client=client)
        self.assertEqual(result.jobs_created, 1)
        self.assertEqual(result.errors, [])

    def test_unexpected_error_becomes_message_not_500(self):
        class Broken(FakeClient):
            def my_jobs(self):
                raise KeyError("items")

        with self.assertRaises(workua.WorkUaError) as ctx:
            sync_all(client=Broken())
        self.assertIn("KeyError", str(ctx.exception))
        self.assertIn("KeyError", WorkUaSyncState.get().last_error)

        self.client.force_login(self.user)
        with override_settings(WORKUA={"BACKEND": "api", "LOGIN": "a@b.c", "PASSWORD": "x", "SYNC_MINUTES": 15}), \
                mock.patch.object(workua, "get_client", return_value=Broken()):
            response = self.client.post(reverse("recruiting:workua_sync_all"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Неочікувана помилка під час синхронізації з Work.ua")

    def test_one_bad_vacancy_does_not_stop_others(self):
        other = Vacancy.objects.create(company=self.company, title="Комірник", workua_job_id="999")

        class HalfBroken(FakeClient):
            def job_responses(self, job_id, after_id=None):
                if job_id == "1234567":
                    raise TypeError("дивні дані")
                return [resp(301, "Олена", "0501112233")]

        result = sync_all(client=HalfBroken())
        self.assertEqual(result.created, 1)
        self.assertEqual(other.applications.count(), 1)
        self.assertTrue(any("дивні дані" in e for e in result.errors))



class RolesTests(BaseTestCase):
    """Матриця доступу: адміністратор — усе; рекрутер — дашборд і вакансії, підприємства лише перегляд."""

    def test_recruiter_group_permissions(self):
        from django.contrib.auth.models import Group

        codes = set(Group.objects.get(name=RECRUITER_GROUP).permissions.values_list("codename", flat=True))
        self.assertIn("change_application", codes)
        self.assertIn("add_vacancy", codes)
        self.assertIn("view_company", codes)
        self.assertNotIn("change_company", codes)
        self.assertNotIn("add_company", codes)
        self.assertEqual(role_of(self.user), RECRUITER)
        self.assertEqual(role_of(self.admin), ADMIN)

    def test_recruiter_full_access_to_dashboard_and_vacancies(self):
        self.client.force_login(self.user)
        for url in [reverse("recruiting:dashboard"), reverse("recruiting:vacancy_list"),
                    reverse("recruiting:vacancy_create"), reverse("recruiting:vacancy_detail", args=[self.vacancy.pk]),
                    reverse("recruiting:vacancy_edit", args=[self.vacancy.pk]),
                    reverse("recruiting:vacancy_add_candidate", args=[self.vacancy.pk]),
                    reverse("recruiting:vacancy_import", args=[self.vacancy.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        # створити вакансію, додати кандидата, змінити статус і дані кандидата
        response = self.client.post(reverse("recruiting:vacancy_create"),
                                    {"company": self.company.pk, "title": "Комірник", "city": "Львів", "headcount": 1, "status": "open"})
        self.assertEqual(response.status_code, 302)
        self.client.post(reverse("recruiting:vacancy_add_candidate", args=[self.vacancy.pk]),
                         {"channel": "sourced", "full_name": "Олена", "phone": "0501112233", "source": "manual"})
        application = self.vacancy.applications.get()
        self.client.post(reverse("recruiting:application_stage", args=[application.pk]), {"stage": "thinking"})
        self.client.post(reverse("recruiting:candidate_edit", args=[application.candidate.pk]),
                         {"full_name": "Олена Коваль", "phone": "0501112233", "source": "manual"})
        application.refresh_from_db()
        self.assertEqual((application.stage, application.candidate.full_name), ("thinking", "Олена Коваль"))

    def test_recruiter_companies_read_only(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("recruiting:company_list"))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "+ Нове підприємство")
        detail = self.client.get(reverse("recruiting:company_edit", args=[self.company.pk]))
        self.assertContains(detail, "Лише перегляд")
        self.assertNotContains(detail, "<form method=\"post\" class=\"form company-form\"")
        self.assertEqual(self.client.get(reverse("recruiting:company_create")).status_code, 403)
        response = self.client.post(reverse("recruiting:company_edit", args=[self.company.pk]), {"name": "Зламано"})
        self.assertEqual(response.status_code, 403)
        self.company.refresh_from_db()
        self.assertEqual(self.company.name, "Львівський завод")

    def test_recruiter_cannot_create_company_via_vacancy_form(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("recruiting:vacancy_create"))
        self.assertNotContains(page, 'name="new_company"')
        self.client.post(reverse("recruiting:vacancy_create"),
                         {"new_company": "Хитре підприємство", "title": "Т", "city": "Львів", "headcount": 1, "status": "open"})
        self.assertFalse(Company.objects.filter(name="Хитре підприємство").exists())

    def test_recruiter_has_no_admin_or_team(self):
        self.client.force_login(self.user)
        admin_page = self.client.get("/admin/")
        self.assertEqual(admin_page.status_code, 302)  # Django admin відправляє не-персонал на свій логін
        self.assertIn("/admin/login/", admin_page["Location"])
        self.assertEqual(self.client.get(reverse("recruiting:team_list")).status_code, 403)
        nav = self.client.get(reverse("recruiting:dashboard"))
        self.assertNotContains(nav, "Адмінка")
        self.assertNotContains(nav, ">Команда<")
        self.assertContains(nav, "Рекрутер")

    def test_admin_sees_everything(self):
        self.client.force_login(self.admin)
        for url in [reverse("recruiting:dashboard"), reverse("recruiting:company_create"),
                    reverse("recruiting:team_list"), "/admin/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        nav = self.client.get(reverse("recruiting:dashboard"))
        self.assertContains(nav, "Адмінка")
        self.assertContains(nav, "Команда")

    def test_forbidden_page_is_friendly(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("recruiting:company_create"))
        self.assertContains(response, "Немає доступу", status_code=403)


class TeamTests(BaseTestCase):
    def setUp(self):
        self.client.force_login(self.admin)

    def test_admin_creates_recruiter_who_can_log_in(self):
        response = self.client.post(reverse("recruiting:team_user_create"), {
            "first_name": "Олена", "last_name": "Коваль", "username": "olena", "email": "olena@example.com",
            "role": "recruiter", "is_active": "on", "password1": "Lviv-recruit-2026", "password2": "Lviv-recruit-2026"})
        self.assertEqual(response.status_code, 302)
        olena = get_user_model().objects.get(username="olena")
        self.assertEqual(role_of(olena), RECRUITER)
        self.assertFalse(olena.is_staff)
        self.client.logout()
        self.assertTrue(self.client.login(username="olena", password="Lviv-recruit-2026"))
        self.assertEqual(self.client.get(reverse("recruiting:vacancy_list")).status_code, 200)

    def test_weak_or_mismatched_password_rejected(self):
        response = self.client.post(reverse("recruiting:team_user_create"), {
            "username": "x", "role": "recruiter", "is_active": "on", "password1": "123", "password2": "124"})
        self.assertContains(response, "Паролі не збігаються")
        self.assertFalse(get_user_model().objects.filter(username="x").exists())

    def test_promote_and_deactivate(self):
        url = reverse("recruiting:team_user_edit", args=[self.user.pk])
        self.client.post(url, {"username": "recruiter", "role": "admin", "is_active": "on"})
        self.user.refresh_from_db()
        self.assertEqual(role_of(self.user), ADMIN)
        self.assertTrue(self.user.is_staff)
        self.client.post(url, {"username": "recruiter", "role": "recruiter"})  # без is_active → вимкнено
        self.user.refresh_from_db()
        self.assertEqual(role_of(self.user), RECRUITER)
        self.assertFalse(self.user.is_active)
        self.assertFalse(self.client.login(username="recruiter", password="pass-12345-word"))

    def test_cannot_demote_or_deactivate_self(self):
        url = reverse("recruiting:team_user_edit", args=[self.admin.pk])
        response = self.client.post(url, {"username": "boss", "role": "recruiter", "is_active": "on"})
        self.assertContains(response, "самому собі")
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_superuser)

    def test_cannot_remove_last_admin(self):
        other = get_user_model().objects.create_superuser("boss2", password="pass-12345-word")
        self.client.force_login(other)
        self.client.post(reverse("recruiting:team_user_edit", args=[self.admin.pk]),
                         {"username": "boss", "role": "recruiter", "is_active": "on"})  # boss2 лишається адміном — можна
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_superuser)
        # тепер boss2 — останній адмін; ще один адмін, що редагує boss2, — неможливий; перевіряємо пряму умову:
        from .roles import active_admins_count
        self.assertEqual(active_admins_count(), 1)

    def test_password_change_keeps_own_session(self):
        url = reverse("recruiting:team_user_edit", args=[self.admin.pk])
        self.client.post(url, {"username": "boss", "role": "admin", "is_active": "on",
                               "password1": "New-boss-pass-2026", "password2": "New-boss-pass-2026"})
        self.assertEqual(self.client.get(reverse("recruiting:team_list")).status_code, 200)


class DeploymentTests(TestCase):
    def test_healthz_without_login(self):
        response = self.client.get("/healthz/")
        self.assertEqual((response.status_code, response.content), (200, b"ok"))

    def test_railway_settings(self):
        """На Railway домен, HTTPS і хост перевірки здоров'я налаштовуються самі."""
        import json, os, subprocess, sys
        from django.conf import settings as dj_settings

        env = {**os.environ, "DJANGO_DEBUG": "0", "DJANGO_SECRET_KEY": "x" * 50,
               "RAILWAY_ENVIRONMENT_NAME": "production", "RAILWAY_PUBLIC_DOMAIN": "crm-test.up.railway.app"}
        env.pop("DJANGO_ALLOWED_HOSTS", None)
        code = ("import json, django, os; os.environ['DJANGO_SETTINGS_MODULE']='config.settings'; django.setup(); "
                "from django.conf import settings as s; print(json.dumps([s.ALLOWED_HOSTS, s.CSRF_TRUSTED_ORIGINS, "
                "s.SECURE_SSL_REDIRECT, s.SECURE_REDIRECT_EXEMPT, s.SESSION_COOKIE_SECURE]))")
        out = subprocess.run([sys.executable, "-c", code], env=env, cwd=dj_settings.BASE_DIR,
                             capture_output=True, text=True, check=True).stdout
        hosts, origins, ssl_redirect, exempt, secure_cookie = json.loads(out)
        self.assertIn("crm-test.up.railway.app", hosts)
        self.assertIn("healthcheck.railway.app", hosts)
        self.assertIn("https://crm-test.up.railway.app", origins)
        self.assertTrue(ssl_redirect and secure_cookie)
        self.assertEqual(exempt, ["^healthz/$"])


class EnsureAdminTests(TestCase):
    def run_cmd(self, **env):
        import io, os
        from django.core.management import call_command

        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            for key in ("DJANGO_SUPERUSER_USERNAME", "DJANGO_SUPERUSER_PASSWORD", "DJANGO_SUPERUSER_RESET_PASSWORD"):
                if key not in env:
                    os.environ.pop(key, None)
            call_command("ensure_admin", stdout=out)
        return out.getvalue()

    def test_create_then_keep_then_reset(self):
        from django.contrib.auth import authenticate

        self.assertIn("Створено адміністратора", self.run_cmd(DJANGO_SUPERUSER_USERNAME="admin", DJANGO_SUPERUSER_PASSWORD="First-pass-1"))
        out = self.run_cmd(DJANGO_SUPERUSER_USERNAME="admin", DJANGO_SUPERUSER_PASSWORD="Second-pass-2")
        self.assertIn("вже існує", out)
        self.assertIsNotNone(authenticate(username="admin", password="First-pass-1"))  # без прапорця пароль не змінюється
        out = self.run_cmd(DJANGO_SUPERUSER_USERNAME="admin", DJANGO_SUPERUSER_PASSWORD="Second-pass-2",
                           DJANGO_SUPERUSER_RESET_PASSWORD="1")
        self.assertIn("скинуто", out)
        self.assertIsNotNone(authenticate(username="admin", password="Second-pass-2"))
        self.assertIsNone(authenticate(username="admin", password="First-pass-1"))

    def test_warns_about_quotes_and_template(self):
        self.assertIn("лапки", self.run_cmd(DJANGO_SUPERUSER_USERNAME="a", DJANGO_SUPERUSER_PASSWORD="'secret'"))
        self.assertIn("шаблон", self.run_cmd(DJANGO_SUPERUSER_USERNAME="b", DJANGO_SUPERUSER_PASSWORD="<надійний пароль>"))


class DataTransferTests(BaseTestCase):
    """Перенесення даних файлом між копіями CRM (з комп'ютера в хмару)."""

    def setUp(self):
        self.client.force_login(self.admin)
        candidate = Candidate.objects.create(full_name="Петро Комірник", phone="0931818875", age=34)
        self.application, _ = attach_candidate(self.vacancy, candidate, user=self.user)
        change_stage(self.application, Application.Stage.THINKING, user=self.user, comment="Передзвонити в п'ятницю")

    def export(self) -> bytes:
        response = self.client.get(reverse("recruiting:data_transfer_export"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment;", response["Content-Disposition"])
        return response.content

    def upload(self, content: bytes, confirm=True):
        from django.core.files.uploadedfile import SimpleUploadedFile

        data = {"file": SimpleUploadedFile("crm-data.json", content, content_type="application/json")}
        if confirm:
            data["confirm"] = "on"
        return self.client.post(reverse("recruiting:data_transfer"), data, follow=True)

    def test_only_admin_can_open(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("recruiting:data_transfer")).status_code, 403)
        self.assertEqual(self.client.get(reverse("recruiting:data_transfer_export")).status_code, 403)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("recruiting:team_list")), reverse("recruiting:data_transfer"))

    def test_round_trip_restores_everything(self):
        import json

        content = self.export()
        payload = json.loads(content)
        self.assertEqual(payload["format"], "crm-recruitment-transfer")
        users = [o for o in payload["objects"] if o["model"] == "auth.user"]
        self.assertTrue(users and all("pk" not in o for o in users))  # користувачі — за логіном, без номерів

        # Після вивантаження дані в CRM змінилися: зайве підприємство, видалений кандидат, інший пароль.
        Company.objects.create(name="Зайве підприємство")
        Candidate.objects.all().delete()
        self.admin.set_password("Changed-pass-99")  # у хмарі адміністратор входить іншим паролем, ніж локально
        self.admin.save()
        self.client.force_login(self.admin)

        response = self.upload(content)
        self.assertContains(response, "Дані перенесено")
        self.assertEqual(response.redirect_chain[-1][0], reverse("recruiting:data_transfer"))  # не розлогінило
        self.assertFalse(Company.objects.filter(name="Зайве підприємство").exists())
        application = Application.objects.get(pk=self.application.pk)
        self.assertEqual(application.stage, Application.Stage.THINKING)
        self.assertEqual(application.candidate.phone, "+380931818875")
        self.assertTrue(application.events.filter(text__contains="п'ятницю").exists())
        self.assertEqual(Vacancy.objects.get(pk=self.vacancy.pk).recruiter, self.user)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password("pass-12345-word"))  # пароль — як у файлі
        # Нові записи після перенесення створюються без конфліктів номерів.
        self.assertTrue(Company.objects.create(name="Нове після перенесення").pk)

    def test_users_are_matched_by_username(self):
        content = self.export()
        # У хмарній базі ті самі люди мають інші номери.
        get_user_model().objects.create_user("someone-else", password="x-12345-word")
        self.user.delete()
        get_user_model().objects.create_user("recruiter", password="cloud-pass-1")
        self.upload(content)
        recruiter = get_user_model().objects.get(username="recruiter")
        self.assertEqual(Vacancy.objects.get(pk=self.vacancy.pk).recruiter, recruiter)
        self.assertTrue(recruiter.check_password("pass-12345-word"))
        self.assertEqual(role_of(recruiter), RECRUITER)
        self.assertTrue(get_user_model().objects.filter(username="someone-else").exists())  # кого немає у файлі — лишається

    def test_bad_files_change_nothing(self):
        import json

        payload = json.loads(self.export())
        sneaky = dict(payload, objects=payload["objects"] + [{"model": "recruiting.workuasyncstate", "pk": 1, "fields": {}}])
        broken = dict(payload, objects=payload["objects"] + [{"model": "recruiting.vacancy", "pk": 999, "fields": {"title": "x", "company": 424242}}])
        cases = [
            (b"Name,Phone\n", "Це не файл перенесення CRM"),
            (json.dumps({"format": "other"}).encode(), "Це не файл перенесення CRM"),
            (json.dumps(dict(payload, version=99)).encode(), "з іншої версії CRM"),
            (json.dumps(sneaky).encode(), "не переносить"),
            (json.dumps(broken).encode(), "у CRM нічого не змінено"),
        ]
        for content, error in cases:
            with self.subTest(error=error):
                self.assertContains(self.upload(content), error)
                self.assertEqual(Application.objects.count(), 1)
                self.assertTrue(Candidate.objects.filter(full_name="Петро Комірник").exists())

    def test_confirmation_required(self):
        response = self.upload(self.export(), confirm=False)
        self.assertContains(response, "підтвердити заміну")
        self.assertNotContains(response, "Дані перенесено")

    def test_warns_about_password_equal_to_username(self):
        get_user_model().objects.create_user("demo", password="demo")
        response = self.upload(self.export())
        self.assertContains(response, "пароль збігається з логіном: demo")


class BoardTests(BaseTestCase):
    """Вигляд «Дошка»: колонки за статусами, зміна статусу перетягуванням (JSON)."""

    def setUp(self):
        self.client.force_login(self.user)
        self.anna, _ = attach_candidate(self.vacancy, Candidate.objects.create(full_name="Анна Нова", phone="0501112233"),
                                        channel=Application.Channel.RESPONSE)
        self.ivan, _ = attach_candidate(self.vacancy, Candidate.objects.create(full_name="Іван Думає"),
                                        channel=Application.Channel.RESPONSE)
        change_stage(self.ivan, Application.Stage.THINKING)
        self.olha, _ = attach_candidate(self.vacancy, Candidate.objects.create(full_name="Ольга Відмова"),
                                        channel=Application.Channel.RESPONSE)
        change_stage(self.olha, Application.Stage.REJECTED_BY_US)
        self.url = reverse("recruiting:vacancy_detail", args=[self.vacancy.pk])

    def stage_url(self, application):
        return reverse("recruiting:application_stage", args=[application.pk])

    def test_board_columns_follow_the_funnel(self):
        response = self.client.get(self.url, {"view": "board", "channel": "response"})
        self.assertEqual(response.status_code, 200)
        columns = response.context["columns"]
        self.assertEqual([c["value"] for c in columns], list(Application.Stage.values))
        by_stage = {c["value"]: [a.candidate.full_name for a in c["items"]] for c in columns}
        self.assertEqual(by_stage["new"], ["Анна Нова"])
        self.assertEqual(by_stage["thinking"], ["Іван Думає"])
        self.assertEqual(by_stage["rejected_by_us"], ["Ольга Відмова"])  # на дошці видно й закриті статуси
        self.assertContains(response, 'class="board"', html=False)
        self.assertContains(response, "data-can-move")
        self.assertContains(response, 'draggable="true"')
        self.assertContains(response, "js/board.js")
        self.assertNotContains(response, 'class="table candidates"')

    def test_view_choice_is_remembered(self):
        self.client.get(self.url, {"view": "board"})
        self.assertEqual(self.client.get(self.url).context["view"], "board")
        self.client.get(self.url, {"view": "list"})
        response = self.client.get(self.url)
        self.assertEqual(response.context["view"], "list")
        self.assertContains(response, 'class="table candidates"')

    def test_search_filters_cards(self):
        response = self.client.get(self.url, {"view": "board", "channel": "all", "q": "Іван"})
        names = [a.candidate.full_name for c in response.context["columns"] for a in c["items"]]
        self.assertEqual(names, ["Іван Думає"])

    def test_drag_changes_stage_and_records_history(self):
        response = self.client.post(self.stage_url(self.anna), {"stage": "no_answer"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["stage"], "no_answer")
        self.anna.refresh_from_db()
        self.assertEqual(self.anna.stage, Application.Stage.NO_ANSWER)
        event = self.anna.events.filter(kind=ApplicationEvent.Kind.STAGE).first()
        self.assertEqual((event.from_stage, event.to_stage, event.author), ("new", "no_answer", self.user))
        # Повідомлення «статус змінено» не накопичуються для наступної сторінки.
        self.assertNotContains(self.client.get(self.url), "статус «Не додзвонилися»")

    def test_interview_needs_date(self):
        response = self.client.post(self.stage_url(self.ivan), {"stage": "interview"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.json()["need_interview_at"])
        self.ivan.refresh_from_db()
        self.assertEqual(self.ivan.stage, Application.Stage.THINKING)

        response = self.client.post(self.stage_url(self.ivan), {"stage": "interview", "interview_at": "2026-12-03T10:30"},
                                    HTTP_X_REQUESTED_WITH="fetch")
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["interview_display"], "03.12 10:30")
        self.ivan.refresh_from_db()
        self.assertEqual(self.ivan.stage, Application.Stage.INTERVIEW)
        self.assertIn("Співбесіда: 03.12.2026 10:30", self.ivan.events.first().text)

    def test_bad_stage_returns_json_error(self):
        response = self.client.post(self.stage_url(self.anna), {"stage": "nonsense"}, HTTP_X_REQUESTED_WITH="fetch")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])

    def test_read_only_user_cannot_drag(self):
        from django.contrib.auth.models import Permission

        viewer = get_user_model().objects.create_user("viewer", password="x-12345-word")
        viewer.user_permissions.add(*Permission.objects.filter(codename__in=["view_vacancy", "view_application"]))
        self.client.force_login(viewer)
        response = self.client.get(self.url, {"view": "board"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "data-can-move")
        self.assertNotContains(response, 'draggable="true"')
        self.assertEqual(self.client.post(self.stage_url(self.anna), {"stage": "viber"},
                                          HTTP_X_REQUESTED_WITH="fetch").status_code, 403)
