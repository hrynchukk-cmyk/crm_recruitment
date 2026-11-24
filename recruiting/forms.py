from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.utils import timezone

from .models import Application, Candidate, Company, Vacancy, parse_workua_link
from .roles import ADMIN, RECRUITER, ROLE_CHOICES, active_admins_count, role_of, set_role


class VacancyForm(forms.ModelForm):
    # Довше за поле моделі, щоб можна було вставити повне посилання на вакансію.
    workua_job_id = forms.CharField(
        label="ID або посилання на вакансію на Work.ua",
        max_length=255,
        required=False,
        help_text="Наприклад: https://www.work.ua/jobs/1234567/ — потрібно для підтягування відгуків.",
    )

    new_company = forms.CharField(
        label="…або нове підприємство",
        max_length=255,
        required=False,
        help_text="Якщо підприємства ще немає у списку — впишіть назву, і його буде створено.",
    )

    class Meta:
        model = Vacancy
        fields = [
            "company",
            "new_company",
            "title",
            "city",
            "salary",
            "headcount",
            "status",
            "opened_on",
            "recruiter",
            "workua_job_id",
            "description",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 5}),
            "opened_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, can_add_company: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.can_add_company = can_add_company
        self.fields["company"].required = False
        self.fields["company"].help_text = "Щоб підприємство з'явилося на дашборді, позначте його на карті в розділі «Підприємства»."
        self.fields["opened_on"].required = False
        if not can_add_company:
            # Рекрутер вибирає лише з наявних підприємств; нові додає адміністратор.
            del self.fields["new_company"]
            self.fields["company"].help_text = "Немає потрібного підприємства? Попросіть адміністратора додати його."

    def clean_opened_on(self):
        # Не вказали — вважаємо, що вакансію опублікували сьогодні.
        return self.cleaned_data.get("opened_on") or timezone.localdate()

    def clean(self):
        cleaned = super().clean()
        name = (cleaned.get("new_company") or "").strip() if self.can_add_company else ""
        if name:
            # Створюємо одразу: модельна валідація вакансії потребує збереженого підприємства.
            # Повторне введення тієї ж назви не створить дубліката.
            company = Company.objects.filter(name__iexact=name).first()
            cleaned["company"] = company or Company.objects.create(name=name)
        elif not cleaned.get("company"):
            self.add_error("company", "Виберіть підприємство або впишіть нове." if self.can_add_company else "Виберіть підприємство.")
        return cleaned

    def clean_workua_job_id(self):
        value = self.cleaned_data["workua_job_id"].strip()
        # Дозволяємо вставити повне посилання: https://www.work.ua/jobs/1234567/
        if "work.ua" in value:
            parts = [p for p in value.split("/") if p.isdigit()]
            value = parts[-1] if parts else value
        if value and not value.isdigit():
            raise forms.ValidationError("Вкажіть номер вакансії або посилання на неї на Work.ua.")
        if value:
            duplicate = Vacancy.objects.filter(workua_job_id=value).exclude(pk=self.instance.pk).first()
            if duplicate:
                raise forms.ValidationError(
                    f"Ця вакансія Work.ua вже є в CRM: «{duplicate.title}» ({duplicate.company}). "
                    "Відкрийте її у списку вакансій або залиште це поле порожнім."
                )
        return value


class CompanyForm(forms.ModelForm):
    """Підприємство з точкою на карті (широту й довготу заповнює карта на сторінці)."""

    email = forms.EmailField(
        label="Email",
        required=False,
        widget=forms.TextInput(attrs={"inputmode": "email", "placeholder": "необов'язково"}),
        error_messages={"invalid": "Схоже, це не email. Якщо невідомий — залиште поле порожнім."},
    )

    class Meta:
        model = Company
        fields = ["name", "address", "latitude", "longitude", "contact_person", "phone", "email", "notes"]
        labels = {"address": "Адреса"}
        widgets = {
            "address": forms.TextInput(attrs={"placeholder": "напр. вул. Городоцька, 172"}),
            "latitude": forms.HiddenInput(),
            "longitude": forms.HiddenInput(),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def clean(self):
        cleaned = super().clean()
        lat, lng = cleaned.get("latitude"), cleaned.get("longitude")
        if (lat is None) != (lng is None):
            raise forms.ValidationError("Точку на карті задано не повністю — поставте її ще раз.")
        if lat is not None and not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise forms.ValidationError("Некоректні координати — поставте точку на карті ще раз.")
        return cleaned


# Те, що пишуть у таблицях замість «невідомо», — вважаємо порожнім значенням.
EMPTY_PLACEHOLDERS = {"-", "—", "–", "?", "немає", "нема", "нет", "n/a", "na", "немає email", "немає пошти"}


class CandidateForm(forms.ModelForm):
    # Звичайні текстові поля замість type=email / type=url: браузер не блокує форму
    # англомовними підказками, а перевірку робимо самі з повідомленнями українською.
    resume_url = forms.CharField(
        label="Посилання (Work.ua)",
        required=False,
        max_length=500,
        help_text="Посилання на відгук (…/my/applicants/…), рекомендацію (…/my/recommended/…) або резюме (…/resumes/…).",
        widget=forms.TextInput(attrs={"placeholder": "https://www.work.ua/…", "autofocus": True, "inputmode": "url"}),
    )
    email = forms.CharField(
        label="Email",
        required=False,
        max_length=254,
        widget=forms.TextInput(attrs={"placeholder": "необов'язково", "inputmode": "email", "autocomplete": "off"}),
    )

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
        labels = {"notes": "Загальні нотатки про людину"}
        widgets = {
            "age": forms.NumberInput(attrs={"min": 14, "max": 90}),
            "phone": forms.TextInput(attrs={"placeholder": "093 123-45-67", "inputmode": "tel"}),
            "resume_text": forms.Textarea(attrs={"rows": 5}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["district"].choices = [("", "Не вказано"), *Candidate.District.choices]

    def clean_email(self):
        value = self.cleaned_data["email"].strip()
        if value.lower() in EMPTY_PLACEHOLDERS:
            return ""
        if value:
            try:
                validate_email(value)
            except ValidationError:
                raise forms.ValidationError(
                    "Схоже, це не email. Якщо email невідомий — просто залиште поле порожнім."
                ) from None
        return value

    def clean_resume_url(self):
        value = self.cleaned_data["resume_url"].strip()
        if not value or value.lower() in EMPTY_PLACEHOLDERS:
            return ""
        if value.startswith("/"):  # скопійовано лише шлях: /my/applicants/123
            value = "https://www.work.ua" + value
        elif "://" not in value:
            value = "https://" + value
        try:
            URLValidator(schemes=["http", "https"])(value)
        except ValidationError:
            raise forms.ValidationError(
                "Не схоже на посилання. Приклад: https://www.work.ua/resumes/1234567/"
            ) from None
        return value

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

    # Рідко потрібні поля — у блоці «Додатково», щоб форма була короткою, як рядок таблиці.
    EXTRA_FIELDS = ("city", "email", "source", "resume_text")

    def main_fields(self):
        return [field for field in self if field.name not in self.EXTRA_FIELDS]

    def extra_fields(self):
        return [field for field in self if field.name in self.EXTRA_FIELDS]

    def extra_has_errors(self) -> bool:
        return any(self.errors.get(name) for name in self.EXTRA_FIELDS)

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


class TeamUserForm(forms.ModelForm):
    """Користувач CRM і його роль (сторінка «Команда», лише для адміністратора)."""

    role = forms.ChoiceField(
        label="Роль",
        choices=ROLE_CHOICES,
        initial=RECRUITER,
        widget=forms.RadioSelect,
        help_text="Рекрутер: дашборд і вакансії повністю, підприємства — лише перегляд, без «Адмінки». "
        "Адміністратор: усе.",
    )
    is_active = forms.BooleanField(label="Доступ до CRM увімкнено", required=False, initial=True)
    password1 = forms.CharField(label="Пароль", required=False, strip=False,
                                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password2 = forms.CharField(label="Пароль ще раз", required=False, strip=False,
                                widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    class Meta:
        model = get_user_model()
        fields = ["first_name", "last_name", "username", "email"]
        labels = {"first_name": "Ім'я", "last_name": "Прізвище", "username": "Логін для входу", "email": "Email"}
        help_texts = {"username": "Латиницею, без пробілів, напр. olena або olena.koval"}

    def __init__(self, *args, acting_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.acting_user = acting_user
        self.is_new = self.instance.pk is None
        if self.is_new:
            self.fields["password1"].required = True
            self.fields["password2"].required = True
        else:
            self.fields["role"].initial = role_of(self.instance) or RECRUITER
            self.fields["is_active"].initial = self.instance.is_active
            self.fields["password1"].label = "Новий пароль"
            self.fields["password1"].help_text = "Залиште порожнім, щоб не змінювати."
            self.fields["password2"].label = "Новий пароль ще раз"
        self.order_fields(["first_name", "last_name", "username", "email", "role", "is_active", "password1", "password2"])

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 or p2:
            if p1 != p2:
                self.add_error("password2", "Паролі не збігаються.")
            else:
                try:
                    validate_password(p1, self.instance)
                except ValidationError as exc:
                    self.add_error("password1", exc)
        role, active = cleaned.get("role"), cleaned.get("is_active")
        if not self.is_new:
            if self.instance == self.acting_user and (role != ADMIN or not active):
                raise forms.ValidationError("Не можна забрати права адміністратора чи вимкнути доступ самому собі.")
            was_active_admin = self.instance.is_superuser and self.instance.is_active
            if was_active_admin and (role != ADMIN or not active) and active_admins_count() <= 1:
                raise forms.ValidationError("Це останній адміністратор — спершу призначте іншого.")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_active = self.cleaned_data.get("is_active", True)
        if self.cleaned_data.get("password1"):
            user.set_password(self.cleaned_data["password1"])
        user.save()
        set_role(user, self.cleaned_data["role"])
        return user


class TransferImportForm(forms.Form):
    file = forms.FileField(
        label="Файл перенесення (.json)",
        help_text="Файл, вивантажений кнопкою «Вивантажити дані» в іншій копії CRM.",
    )
    confirm = forms.BooleanField(
        label="Розумію: усі підприємства, вакансії й кандидати в цій CRM буде замінено даними з файлу",
        error_messages={"required": "Поставте позначку, щоб підтвердити заміну даних."},
    )
