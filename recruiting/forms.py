from django import forms

from .models import Application, Candidate, Vacancy


class VacancyForm(forms.ModelForm):
    # Довше за поле моделі, щоб можна було вставити повне посилання на вакансію.
    workua_job_id = forms.CharField(
        label="ID або посилання на вакансію на Work.ua",
        max_length=255,
        required=False,
        help_text="Наприклад: https://www.work.ua/jobs/1234567/ — потрібно для підтягування відгуків.",
    )

    class Meta:
        model = Vacancy
        fields = [
            "company",
            "title",
            "city",
            "salary",
            "headcount",
            "status",
            "recruiter",
            "workua_job_id",
            "description",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 5})}

    def clean_workua_job_id(self):
        value = self.cleaned_data["workua_job_id"].strip()
        # Дозволяємо вставити повне посилання: https://www.work.ua/jobs/1234567/
        if "work.ua" in value:
            parts = [p for p in value.split("/") if p.isdigit()]
            value = parts[-1] if parts else value
        if value and not value.isdigit():
            raise forms.ValidationError("Вкажіть номер вакансії або посилання на неї на Work.ua.")
        return value


class CandidateForm(forms.ModelForm):
    class Meta:
        model = Candidate
        fields = ["full_name", "phone", "email", "city", "birth_year", "source", "resume_url", "resume_text", "notes"]
        widgets = {
            "resume_text": forms.Textarea(attrs={"rows": 6}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }


class StageForm(forms.Form):
    stage = forms.ChoiceField(label="Статус", choices=Application.Stage.choices)
    comment = forms.CharField(
        label="Коментар",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Коментар (необов'язково), напр. причина відмови"}),
    )


class NoteForm(forms.Form):
    text = forms.CharField(
        label="Нотатка",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Напр.: передзвонити в понеділок після 14:00"}),
    )


class InterviewForm(forms.ModelForm):
    class Meta:
        model = Application
        fields = ["interview_at"]
        widgets = {"interview_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")}
