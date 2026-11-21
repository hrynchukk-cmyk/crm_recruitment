from django import forms

from .models import Application, Candidate, Vacancy, parse_workua_link


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
        fields = [
            "resume_url",
            "full_name",
            "age",
            "phone",
            "district",
            "city",
            "email",
            "source",
            "resume_text",
            "notes",
        ]
        labels = {"resume_url": "Посилання (Work.ua)", "notes": "Загальні нотатки про людину"}
        help_texts = {
            "resume_url": "Посилання на відгук (…/my/applicants/…), рекомендацію (…/my/recommended/…) "
            "або резюме (…/resumes/…).",
        }
        widgets = {
            "resume_url": forms.URLInput(attrs={"placeholder": "https://www.work.ua/…", "autofocus": True}),
            "age": forms.NumberInput(attrs={"min": 14, "max": 90}),
            "phone": forms.TextInput(attrs={"placeholder": "093 123-45-67", "inputmode": "tel"}),
            "resume_text": forms.Textarea(attrs={"rows": 5}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def clean(self):
        cleaned = super().clean()
        if "work.ua" in (cleaned.get("resume_url") or "") and cleaned.get("source") == Candidate.Source.MANUAL:
            cleaned["source"] = Candidate.Source.WORKUA
        return cleaned


class AddCandidateForm(CandidateForm):
    """Додавання кандидата на вакансію — як новий рядок у таблиці."""

    channel = forms.ChoiceField(
        label="Розділ вакансії",
        choices=Application.Channel.choices,
        initial=Application.Channel.SOURCED,
        widget=forms.RadioSelect,
        help_text="Для посилань з Work.ua розділ визначається автоматично.",
    )
    note = forms.CharField(
        label="Примітка",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Напр.: досвід експедитором 3+ роки, уточнити графік"}),
    )

    class Meta(CandidateForm.Meta):
        fields = ["resume_url", "full_name", "age", "phone", "district", "city", "email", "source", "resume_text"]

    field_order = ["channel", "resume_url", "full_name", "age", "phone", "district", "note"]

    def clean(self):
        cleaned = super().clean()
        link_channel, link_id = parse_workua_link(cleaned.get("resume_url") or "")
        if link_channel:
            cleaned["channel"] = link_channel
        self.workua_link = (link_channel, link_id)
        return cleaned

    def workua_resume_id(self) -> str:
        return self.workua_link[1] if self.workua_link[0] == "sourced" else ""

    def workua_response_id(self) -> str:
        return self.workua_link[1] if self.workua_link[0] in ("response", "recommended") else ""


class NoteInlineForm(forms.Form):
    note = forms.CharField(required=False, max_length=2000)


class ImportForm(forms.Form):
    file = forms.FileField(
        label="CSV-файл",
        help_text="Google Таблиці: відкрийте вкладку вакансії → Файл → Завантажити → CSV.",
    )
    channel = forms.ChoiceField(
        label="Розділ для рядків без посилання Work.ua",
        choices=Application.Channel.choices,
        initial=Application.Channel.SOURCED,
    )


class StageForm(forms.Form):
    stage = forms.ChoiceField(label="Статус", choices=Application.Stage.choices)
    interview_at = forms.DateTimeField(
        label="Дата і час співбесіди",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M"],
    )
    comment = forms.CharField(
        label="Коментар",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Коментар (необов'язково), напр. причина відмови"}),
    )


class NoteForm(forms.Form):
    text = forms.CharField(
        label="Нотатка",
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Напр.: передзвонити в понеділок після 14:00"}),
    )
