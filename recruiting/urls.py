from django.urls import path

from . import views

app_name = "recruiting"

urlpatterns = [
    path("", views.vacancy_list, name="vacancy_list"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("workua/sync/", views.workua_sync_all, name="workua_sync_all"),
    path("companies/", views.company_list, name="company_list"),
    path("team/", views.team_list, name="team_list"),
    path("team/new/", views.team_user_form, name="team_user_create"),
    path("team/<int:pk>/", views.team_user_form, name="team_user_edit"),
    path("companies/new/", views.company_form, name="company_create"),
    path("companies/<int:pk>/", views.company_form, name="company_edit"),
    path("vacancies/new/", views.vacancy_form, name="vacancy_create"),
    path("vacancies/<int:pk>/", views.vacancy_detail, name="vacancy_detail"),
    path("vacancies/<int:pk>/edit/", views.vacancy_form, name="vacancy_edit"),
    path("vacancies/<int:pk>/sync-workua/", views.vacancy_sync_workua, name="vacancy_sync_workua"),
    path("vacancies/<int:pk>/add-candidate/", views.vacancy_add_candidate, name="vacancy_add_candidate"),
    path("vacancies/<int:pk>/import/", views.vacancy_import, name="vacancy_import"),
    path("applications/<int:pk>/", views.application_detail, name="application_detail"),
    path("applications/<int:pk>/stage/", views.application_stage, name="application_stage"),
    path("applications/<int:pk>/note/", views.application_note, name="application_note"),
    path("applications/<int:pk>/note-inline/", views.application_note_inline, name="application_note_inline"),
    path("applications/<int:pk>/channel/", views.application_channel, name="application_channel"),
    path("applications/<int:pk>/workua-file/", views.application_workua_file, name="application_workua_file"),
    path("candidates/<int:pk>/edit/", views.candidate_edit, name="candidate_edit"),
]
