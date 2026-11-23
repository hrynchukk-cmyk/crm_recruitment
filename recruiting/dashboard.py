"""Дані для дашборду: ключові цифри, підприємства на карті, статус вакансій."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import Application, Vacancy

S = Application.Stage

# Етапи воронки на дашборді (згруповані статуси) — у порядку руху кандидата.
PIPELINE = [
    ("to_call", "Зателефонувати", [S.NEW]),
    ("working", "У роботі", [S.NO_ANSWER, S.VIBER, S.CALLBACK, S.THINKING, S.NO_SHOW]),
    ("interview", "Співбесіди", [S.INTERVIEW, S.PASSED]),
    ("hired", "Вийшли на роботу", [S.HIRED]),
    ("dropped", "Відмови та резерв", [S.REJECTED_BY_US, S.REJECTED_BY_CANDIDATE, S.RESERVE]),
]


@dataclass
class VacancyRow:
    vacancy: Vacancy
    counts: dict[str, int]
    total: int
    upcoming_interviews: int

    @property
    def hired(self) -> int:
        return self.counts["hired"]

    @property
    def remaining(self) -> int:
        return max(self.vacancy.headcount - self.hired, 0)

    @property
    def segments(self) -> list[dict]:
        """Частини смуги воронки: (ключ, назва, кількість, частка у %)."""
        return [
            {
                "key": key,
                "label": label,
                "count": self.counts[key],
                "share": round(100 * self.counts[key] / self.total) if self.total else 0,
            }
            for key, label, _ in PIPELINE
        ]

    @property
    def summary(self) -> str:
        parts = [f"{s['label']}: {s['count']}" for s in self.segments if s["count"]]
        return "; ".join(parts) if parts else "Кандидатів ще немає"


@dataclass
class DashboardData:
    rows: list[VacancyRow]
    companies: list[dict] = field(default_factory=list)  # для карти
    missing_location: list = field(default_factory=list)  # підприємства без точки на карті
    kpis: dict = field(default_factory=dict)


def build_dashboard(*, include_paused: bool = False, recruiter=None) -> DashboardData:
    statuses = [Vacancy.Status.OPEN] + ([Vacancy.Status.PAUSED] if include_paused else [])
    vacancies = Vacancy.objects.filter(status__in=statuses).select_related("company", "recruiter")
    if recruiter is not None:
        vacancies = vacancies.filter(recruiter=recruiter)

    now = timezone.now()
    annotations = {
        key: Count("applications", filter=Q(applications__stage__in=stages)) for key, _, stages in PIPELINE
    }
    vacancies = vacancies.annotate(
        total=Count("applications"),
        upcoming=Count(
            "applications",
            filter=Q(applications__stage=S.INTERVIEW, applications__interview_at__gte=now),
        ),
        **annotations,
    )

    rows = [
        VacancyRow(
            vacancy=v,
            counts={key: getattr(v, key) for key, _, _ in PIPELINE},
            total=v.total,
            upcoming_interviews=v.upcoming,
        )
        for v in vacancies
    ]
    # Найдовше відкриті — вгорі: їм найчастіше потрібна увага.
    rows.sort(key=lambda r: (-r.vacancy.days_active, r.vacancy.title))

    companies: dict[int, dict] = {}
    missing: dict[int, object] = {}
    for row in rows:
        company = row.vacancy.company
        if not company.has_location:
            missing[company.pk] = company
            continue
        entry = companies.setdefault(
            company.pk,
            {
                "name": company.name,
                "address": company.address,
                "lat": float(company.latitude),
                "lng": float(company.longitude),
                "edit_url": company.get_absolute_url(),
                "vacancies": [],
            },
        )
        entry["vacancies"].append(
            {
                "title": row.vacancy.title,
                "url": row.vacancy.get_absolute_url(),
                "status": row.vacancy.status,
                "status_label": row.vacancy.get_status_display(),
                "days_active": row.vacancy.days_active,
                "total": row.total,
                "to_call": row.counts["to_call"],
                "interviews": row.counts["interview"],
                "upcoming_interviews": row.upcoming_interviews,
                "hired": row.hired,
                "headcount": row.vacancy.headcount,
            }
        )
    for entry in companies.values():
        entry["open_count"] = sum(1 for v in entry["vacancies"] if v["status"] == Vacancy.Status.OPEN)

    hired_recently = Application.objects.filter(stage=S.HIRED, stage_changed_at__gte=now - timedelta(days=30))
    if recruiter is not None:
        hired_recently = hired_recently.filter(vacancy__recruiter=recruiter)

    kpis = {
        "remaining": sum(r.remaining for r in rows if r.vacancy.status == Vacancy.Status.OPEN),
        "open_vacancies": sum(1 for r in rows if r.vacancy.status == Vacancy.Status.OPEN),
        "to_call": sum(r.counts["to_call"] for r in rows),
        "in_work": sum(r.counts["to_call"] + r.counts["working"] + r.counts["interview"] for r in rows),
        "upcoming_interviews": sum(r.upcoming_interviews for r in rows),
        "hired_30d": hired_recently.count(),
    }
    return DashboardData(
        rows=rows,
        companies=list(companies.values()),
        missing_location=sorted(missing.values(), key=lambda c: c.name),
        kpis=kpis,
    )
