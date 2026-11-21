from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import CandidateForm, InterviewForm, NoteForm, StageForm, VacancyForm
from .integrations.workua import WorkUaError, sync_vacancy
from .models import Application, Candidate, Vacancy
from .services import add_note, attach_candidate, change_stage, find_existing_candidate


def _safe_next(request, fallback: str) -> str:
    target = request.POST.get("next") or request.GET.get("next")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return target
    return fallback


@login_required
def vacancy_list(request):
    status = request.GET.get("status", Vacancy.Status.OPEN)
    vacancies = Vacancy.objects.select_related("company", "recruiter").annotate(
        total=Count("applications"),
        new=Count("applications", filter=Q(applications__stage=Application.Stage.NEW)),
        in_progress=Count(
            "applications",
            filter=Q(applications__stage__in=Application.ACTIVE_STAGES) & ~Q(applications__stage=Application.Stage.NEW),
        ),
        hired=Count("applications", filter=Q(applications__stage=Application.Stage.HIRED)),
    )
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
def vacancy_form(request, pk=None):
    vacancy = get_object_or_404(Vacancy, pk=pk) if pk else None
    form = VacancyForm(request.POST or None, instance=vacancy, initial=None if vacancy else {"recruiter": request.user})
    if request.method == "POST" and form.is_valid():
        vacancy = form.save()
        messages.success(request, "Вакансію збережено.")
        return redirect(vacancy)
    return render(request, "recruiting/vacancy_form.html", {"form": form, "vacancy": vacancy})


@login_required
def vacancy_detail(request, pk):
    vacancy = get_object_or_404(Vacancy.objects.select_related("company", "recruiter"), pk=pk)
    stage = request.GET.get("stage", "active")
    query = request.GET.get("q", "").strip()

    applications = vacancy.applications.select_related("candidate")
    counts = dict(applications.values_list("stage").annotate(n=Count("id")).values_list("stage", "n"))

    if stage == "active":
        applications = applications.filter(stage__in=Application.ACTIVE_STAGES)
    elif stage in Application.Stage.values:
        applications = applications.filter(stage=stage)
    if query:
        applications = applications.filter(
            Q(candidate__full_name__icontains=query) | Q(candidate__phone__icontains=query)
        )

    tabs = [("active", "Усі в роботі", sum(counts.get(s, 0) for s in Application.ACTIVE_STAGES))]
    tabs += [(value, label, counts.get(value, 0)) for value, label in Application.Stage.choices]
    tabs.append(("all", "Усі", sum(counts.values())))

    return render(
        request,
        "recruiting/vacancy_detail.html",
        {
            "vacancy": vacancy,
            "applications": applications,
            "tabs": tabs,
            "stage": stage,
            "query": query,
            "stage_choices": Application.Stage.choices,
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
    form = CandidateForm(request.POST or None, initial={"city": vacancy.city})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        candidate = find_existing_candidate(phone=data["phone"], email=data["email"])
        if candidate:
            messages.info(request, f"Кандидат з таким телефоном/email уже є в базі: {candidate}. Додано його.")
        else:
            candidate = form.save()
        application, created = attach_candidate(vacancy, candidate, source=data["source"], user=request.user)
        if not created:
            messages.warning(request, "Цей кандидат уже є на вакансії.")
        return redirect(application)
    return render(request, "recruiting/candidate_form.html", {"form": form, "vacancy": vacancy})


@login_required
def application_detail(request, pk):
    application = get_object_or_404(
        Application.objects.select_related("candidate", "vacancy__company"), pk=pk
    )
    other_applications = (
        application.candidate.applications.exclude(pk=application.pk).select_related("vacancy__company")
    )
    return render(
        request,
        "recruiting/application_detail.html",
        {
            "application": application,
            "candidate": application.candidate,
            "events": application.events.select_related("author"),
            "stage_form": StageForm(initial={"stage": application.stage}),
            "note_form": NoteForm(),
            "interview_form": InterviewForm(instance=application),
            "other_applications": other_applications,
        },
    )


@login_required
@require_POST
def application_stage(request, pk):
    application = get_object_or_404(Application, pk=pk)
    form = StageForm(request.POST)
    if form.is_valid():
        changed = change_stage(
            application, form.cleaned_data["stage"], user=request.user, comment=form.cleaned_data["comment"]
        )
        if changed:
            messages.success(
                request, f"{application.candidate}: статус «{application.get_stage_display()}»."
            )
    else:
        messages.error(request, "Невірний статус.")
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
def application_interview(request, pk):
    application = get_object_or_404(Application, pk=pk)
    form = InterviewForm(request.POST, instance=application)
    if form.is_valid():
        form.save()
        when = application.interview_at
        add_note(
            application,
            f"Співбесіда: {when:%d.%m.%Y %H:%M}" if when else "Дату співбесіди прибрано",
            user=request.user,
        )
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
