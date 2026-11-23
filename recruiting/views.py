from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .dashboard import PIPELINE, build_dashboard
from .forms import (
    AddCandidateForm,
    CandidateForm,
    CompanyForm,
    ImportForm,
    NoteForm,
    NoteInlineForm,
    StageForm,
    VacancyForm,
)
from .importer import import_csv
from .integrations.workua import WorkUaError, sync_vacancy
from .models import Application, Candidate, Company, Vacancy
from .services import add_note, attach_candidate, change_stage, find_existing_candidate

# Кнопки швидкого результату дзвінка на картці кандидата.
QUICK_STAGES = [
    Application.Stage.NO_ANSWER,
    Application.Stage.VIBER,
    Application.Stage.CALLBACK,
    Application.Stage.THINKING,
    Application.Stage.INTERVIEW,
    Application.Stage.REJECTED_BY_US,
    Application.Stage.REJECTED_BY_CANDIDATE,
]


def _safe_next(request, fallback: str) -> str:
    target = request.POST.get("next") or request.GET.get("next")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return fallback


def _channel_count(channel: str, *, new_only: bool = False) -> Count:
    condition = Q(applications__channel=channel)
    if new_only:
        condition &= Q(applications__stage=Application.Stage.NEW)
    return Count("applications", filter=condition)


@login_required
def vacancy_list(request):
    status = request.GET.get("status", Vacancy.Status.OPEN)
    C = Application.Channel
    vacancies = Vacancy.objects.select_related("company", "recruiter").annotate(
        responses=_channel_count(C.RESPONSE),
        responses_new=_channel_count(C.RESPONSE, new_only=True),
        recommended=_channel_count(C.RECOMMENDED),
        recommended_new=_channel_count(C.RECOMMENDED, new_only=True),
        sourced=_channel_count(C.SOURCED),
        sourced_new=_channel_count(C.SOURCED, new_only=True),
        interviews=Count("applications", filter=Q(applications__stage=Application.Stage.INTERVIEW)),
        hired=Count("applications", filter=Q(applications__stage=Application.Stage.HIRED)),
    ).order_by("status", "-created_at", "-pk")  # явно: запит з підрахунками не бере сортування з моделі
    if status != "all":
        vacancies = vacancies.filter(status=status)
    if request.GET.get("mine"):
        vacancies = vacancies.filter(recruiter=request.user)
    return render(
        request,
        "recruiting/vacancy_list.html",
        {"vacancies": vacancies, "status": status, "statuses": Vacancy.Status.choices},
    )


@login_required
def dashboard(request):
    include_paused = bool(request.GET.get("paused"))
    mine = bool(request.GET.get("mine"))
    data = build_dashboard(include_paused=include_paused, recruiter=request.user if mine else None)
    return render(
        request,
        "recruiting/dashboard.html",
        {
            "data": data,
            "include_paused": include_paused,
            "mine": mine,
            "pipeline": [(key, label) for key, label, _ in PIPELINE],
        },
    )


@login_required
def company_list(request):
    companies = Company.objects.annotate(
        open_vacancies=Count("vacancies", filter=Q(vacancies__status=Vacancy.Status.OPEN)),
        all_vacancies=Count("vacancies"),
    )
    return render(request, "recruiting/company_list.html", {"companies": companies})


@login_required
def company_form(request, pk=None):
    company = get_object_or_404(Company, pk=pk) if pk else None
    form = CompanyForm(request.POST or None, instance=company)
    if request.method == "POST" and form.is_valid():
        company = form.save()
        messages.success(
            request,
            f"Підприємство «{company}» збережено."
            + ("" if company.has_location else " Точку на карті не вказано — на дашборді його не буде видно."),
        )
        return redirect(_safe_next(request, reverse("recruiting:company_list")))
    return render(
        request,
        "recruiting/company_form.html",
        {"form": form, "company": company, "next": request.GET.get("next", "")},
    )


@login_required
def vacancy_form(request, pk=None):
    vacancy = get_object_or_404(Vacancy, pk=pk) if pk else None
    form = VacancyForm(request.POST or None, instance=vacancy, initial=None if vacancy else {"recruiter": request.user})
    if request.method == "POST" and form.is_valid():
        vacancy = form.save()
        messages.success(request, f"Вакансію «{vacancy.title}» збережено.")
        # Після збереження — на вкладку «Вакансії», на фільтр зі статусом цієї вакансії, з підсвіченим рядком.
        return redirect(f"{reverse('recruiting:vacancy_list')}?status={vacancy.status}&saved={vacancy.pk}")
    return render(request, "recruiting/vacancy_form.html", {"form": form, "vacancy": vacancy})


@login_required
def vacancy_detail(request, pk):
    vacancy = get_object_or_404(Vacancy.objects.select_related("company", "recruiter"), pk=pk)
    channel = request.GET.get("channel")
    stage = request.GET.get("stage", "active")
    query = request.GET.get("q", "").strip()

    all_applications = vacancy.applications.all()
    counts: dict[tuple[str, str], int] = {
        (ch, st): n
        for ch, st, n in all_applications.values_list("channel", "stage").annotate(n=Count("id")).values_list(
            "channel", "stage", "n"
        )
    }

    def count(ch=None, stages=None):
        return sum(n for (c, s), n in counts.items() if (ch is None or c == ch) and (stages is None or s in stages))

    if channel is None:
        # За замовчуванням — перший розділ, де є кандидати (зазвичай «Відгуки»).
        channel = next(
            (ch for ch in Application.Channel.values if count(ch)), Application.Channel.RESPONSE
        )
    elif channel not in Application.Channel.values:
        channel = "all"

    channel_tabs = [
        (value, label, count(value), count(value, [Application.Stage.NEW]))
        for value, label in Application.Channel.choices
    ]
    channel_tabs.append(("all", "Усі розділи", count(), count(None, [Application.Stage.NEW])))

    in_channel = None if channel == "all" else channel
    stage_tabs = [("active", "Усі в роботі", count(in_channel, Application.ACTIVE_STAGES))]
    stage_tabs += [(value, label, count(in_channel, [value])) for value, label in Application.Stage.choices]
    stage_tabs.append(("all", "Усі", count(in_channel)))

    applications = all_applications.select_related("candidate")
    if in_channel:
        applications = applications.filter(channel=in_channel)
    if stage == "active":
        applications = applications.filter(stage__in=Application.ACTIVE_STAGES)
    elif stage in Application.Stage.values:
        applications = applications.filter(stage=stage)
    if query:
        applications = applications.filter(
            Q(candidate__full_name__icontains=query)
            | Q(candidate__phone__icontains=query)
            | Q(note__icontains=query)
        )

    return render(
        request,
        "recruiting/vacancy_detail.html",
        {
            "vacancy": vacancy,
            "applications": applications,
            "channel": channel,
            "channel_tabs": channel_tabs,
            "stage_tabs": stage_tabs,
            "stage": stage,
            "query": query,
            "stage_choices": Application.Stage.choices,
            "district_labels": dict(Candidate.District.choices),
        },
    )


@login_required
@require_POST
def vacancy_sync_workua(request, pk):
    vacancy = get_object_or_404(Vacancy, pk=pk)
    try:
        result = sync_vacancy(vacancy, user=request.user)
    except WorkUaError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, result.summary())
    return redirect(vacancy)


@login_required
def vacancy_add_candidate(request, pk):
    vacancy = get_object_or_404(Vacancy, pk=pk)
    initial = {"city": vacancy.city}
    if request.GET.get("channel") in Application.Channel.values:
        initial["channel"] = request.GET["channel"]
    form = AddCandidateForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        response_id = form.workua_response_id()
        existing = response_id and vacancy.applications.filter(workua_response_id=response_id).first()
        if existing:
            messages.warning(request, f"Це посилання вже є на вакансії: {existing.candidate}.")
            return redirect(existing)
        candidate = find_existing_candidate(
            phone=data["phone"], email=data["email"], workua_resume_id=form.workua_resume_id()
        )
        if candidate:
            messages.info(request, f"Кандидат уже є в базі ({candidate}), додано наявного — без дубліката.")
        else:
            candidate = form.save()
        application, created = attach_candidate(
            vacancy,
            candidate,
            channel=data["channel"],
            source=candidate.source,
            user=request.user,
            workua_response_id=response_id,
        )
        if not created:
            messages.warning(
                request, f"Цей кандидат уже є на вакансії (розділ «{application.get_channel_display()}»)."
            )
        elif data["note"]:
            application.note = data["note"]
            application.save(update_fields=["note", "updated_at"])
        if "add_another" in request.POST:
            messages.success(request, f"Додано: {candidate}.")
            return redirect(reverse("recruiting:vacancy_add_candidate", args=[vacancy.pk]) + f"?channel={data['channel']}")
        return redirect(application)
    return render(request, "recruiting/candidate_form.html", {"form": form, "vacancy": vacancy})


@login_required
def vacancy_import(request, pk):
    vacancy = get_object_or_404(Vacancy, pk=pk)
    form = ImportForm(request.POST or None, request.FILES or None)
    result = None
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        if upload.size > 5 * 1024 * 1024:
            form.add_error("file", "Файл завеликий (максимум 5 МБ).")
        else:
            try:
                result = import_csv(
                    vacancy, upload.read(), default_channel=form.cleaned_data["channel"], user=request.user
                )
            except ValueError as exc:
                form.add_error("file", str(exc))
            else:
                if not result.errors:
                    messages.success(request, result.summary())
                    return redirect(vacancy.get_absolute_url() + "?channel=all")
    return render(request, "recruiting/vacancy_import.html", {"vacancy": vacancy, "form": form, "result": result})


@login_required
def application_detail(request, pk):
    application = get_object_or_404(
        Application.objects.select_related("candidate", "vacancy__company"), pk=pk
    )
    other_applications = application.candidate.applications.exclude(pk=application.pk).select_related(
        "vacancy__company"
    )
    stage_labels = dict(Application.Stage.choices)
    initial_stage = request.GET.get("stage")
    if initial_stage not in Application.Stage.values:
        initial_stage = application.stage
    return render(
        request,
        "recruiting/application_detail.html",
        {
            "application": application,
            "candidate": application.candidate,
            "events": application.events.select_related("author"),
            "stage_form": StageForm(
                initial={"stage": initial_stage, "interview_at": application.interview_at}
            ),
            "note_form": NoteForm(),
            "quick_stages": [(s.value, stage_labels[s]) for s in QUICK_STAGES],
            "channel_choices": Application.Channel.choices,
            "other_applications": other_applications,
        },
    )


@login_required
@require_POST
def application_stage(request, pk):
    application = get_object_or_404(Application.objects.select_related("candidate"), pk=pk)
    form = StageForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Невірні дані статусу.")
        return redirect(application)

    new_stage = form.cleaned_data["stage"]
    interview_at = form.cleaned_data["interview_at"]
    comment = form.cleaned_data["comment"]

    # Запросили на співбесіду без дати — відкриваємо картку, щоб рекрутер її вказав.
    if new_stage == Application.Stage.INTERVIEW and not interview_at and not application.interview_at:
        messages.info(request, f"{application.candidate}: вкажіть дату і час співбесіди.")
        return redirect(reverse("recruiting:application_detail", args=[pk]) + "?stage=interview#status")

    if interview_at and interview_at != application.interview_at:
        application.interview_at = interview_at
        application.save(update_fields=["interview_at", "updated_at"])
        when = timezone.localtime(interview_at).strftime("%d.%m.%Y %H:%M")
        comment = f"Співбесіда: {when}" + (f"\n{comment}" if comment else "")

    if change_stage(application, new_stage, user=request.user, comment=comment):
        messages.success(request, f"{application.candidate}: статус «{application.get_stage_display()}».")
    elif comment:
        messages.success(request, "Збережено.")
    return redirect(_safe_next(request, application.get_absolute_url()))


@login_required
@require_POST
def application_note(request, pk):
    application = get_object_or_404(Application, pk=pk)
    form = NoteForm(request.POST)
    if form.is_valid():
        add_note(application, form.cleaned_data["text"], user=request.user)
    return redirect(application)


@login_required
@require_POST
def application_note_inline(request, pk):
    """Зберігає «Примітку» прямо з таблиці кандидатів (як клітинку в Google Таблиці)."""
    application = get_object_or_404(Application, pk=pk)
    form = NoteInlineForm(request.POST)
    if not form.is_valid():
        if request.headers.get("X-Requested-With") == "fetch":
            return JsonResponse({"ok": False}, status=400)
        messages.error(request, "Примітка задовга.")
        return redirect(application)
    application.note = form.cleaned_data["note"].strip()
    application.save(update_fields=["note", "updated_at"])
    if request.headers.get("X-Requested-With") == "fetch":
        return JsonResponse({"ok": True})
    return redirect(_safe_next(request, application.get_absolute_url()))


@login_required
@require_POST
def application_channel(request, pk):
    """Перенести кандидата в інший розділ вакансії (якщо потрапив не туди)."""
    application = get_object_or_404(Application, pk=pk)
    channel = request.POST.get("channel")
    if channel in Application.Channel.values and channel != application.channel:
        old = application.get_channel_display()
        application.channel = channel
        application.save(update_fields=["channel", "updated_at"])
        add_note(application, f"Перенесено з розділу «{old}» у «{application.get_channel_display()}».", request.user)
    return redirect(application)


@login_required
def candidate_edit(request, pk):
    candidate = get_object_or_404(Candidate, pk=pk)
    form = CandidateForm(request.POST or None, instance=candidate)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Дані кандидата збережено.")
        return redirect(_safe_next(request, "recruiting:vacancy_list"))
    return render(
        request,
        "recruiting/candidate_form.html",
        {"form": form, "candidate": candidate, "next": request.GET.get("next", "")},
    )
