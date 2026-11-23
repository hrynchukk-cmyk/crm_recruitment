from django import template

register = template.Library()


def uk_plural(n: int, one: str, few: str, many: str) -> str:
    """Українська множина: 1 день, 2 дні, 5 днів, 11 днів, 21 день."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


@register.filter
def days(n) -> str:
    """{{ 12|days }} → «12 днів»."""
    return f"{n} {uk_plural(n, 'день', 'дні', 'днів')}"
