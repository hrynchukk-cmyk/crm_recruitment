from django.urls import path

from . import views

app_name = "recruiting"

urlpatterns = [
    path("", views.vacancy_list, name="vacancy_list"),
    path("vacancies/new/", views.vacancy_form, name="vacancy_create"),
    path("vacancies/<int:pk>/", views.vacancy_detail, name="vacancy_detail"),
    path("vacancies/<int:pk>/edit/", views.vacancy_form, name="vacancy_edit"),
    path("vacancies/<int:pk>/sync-workua/", views.vacancy_sync_workua, name="vacancy_sync_workua"),
    path("vacancies/<int:pk>/add-candidate/", views.vacancy_add_candidate, name="vacancy_add_candidate"),
    path("applications/<int:pk>/", views.application_detail, name="application_detail"),
    path("applications/<int:pk>/stage/", views.application_stage, name="application_stage"),
    path("applications/<int:pk>/note/", views.application_note, name="application_note"),
    path("applications/<int:pk>/interview/", views.application_interview, name="application_interview"),
    path("candidates/<int:pk>/edit/", views.candidate_edit, name="candidate_edit"),
]
