from django.contrib import admin

from .models import Application, ApplicationEvent, Candidate, Company, Vacancy


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ["name", "contact_person", "phone", "email"]
    search_fields = ["name", "contact_person", "phone"]


@admin.register(Vacancy)
class VacancyAdmin(admin.ModelAdmin):
    list_display = ["title", "company", "city", "status", "recruiter", "workua_job_id", "created_at"]
    list_filter = ["status", "company", "recruiter"]
    search_fields = ["title", "company__name", "workua_job_id"]


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ["full_name", "phone", "email", "city", "source", "created_at"]
    list_filter = ["source", "city"]
    search_fields = ["full_name", "phone", "email", "workua_resume_id"]


class ApplicationEventInline(admin.TabularInline):
    model = ApplicationEvent
    extra = 0
    readonly_fields = ["kind", "author", "from_stage", "to_stage", "text", "created_at"]
    can_delete = False


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ["candidate", "vacancy", "stage", "source", "applied_at"]
    list_filter = ["stage", "source", "vacancy__company"]
    search_fields = ["candidate__full_name", "candidate__phone", "vacancy__title"]
    autocomplete_fields = ["candidate", "vacancy"]
    inlines = [ApplicationEventInline]
