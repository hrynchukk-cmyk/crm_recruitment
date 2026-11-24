import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


class Command(BaseCommand):
    help = (
        "Створює адміністратора з DJANGO_SUPERUSER_USERNAME / DJANGO_SUPERUSER_PASSWORD, якщо його ще немає. "
        "З DJANGO_SUPERUSER_RESET_PASSWORD=1 — також скидає йому пароль на значення зі змінної. "
        "Зручно на хостингах без доступу до консолі. Нічого не робить, якщо змінні не задані."
    )

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "").strip()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
        if not username or not password:
            if username or password:
                self.stdout.write("Адміністратора не створено: потрібні обидві змінні DJANGO_SUPERUSER_USERNAME і DJANGO_SUPERUSER_PASSWORD.")
            return

        # Підказки щодо типових помилок у значенні пароля (сам пароль не виводимо).
        if password != password.strip():
            self.stdout.write("Увага: DJANGO_SUPERUSER_PASSWORD починається або закінчується пробілом — він теж є частиною пароля.")
        if len(password) > 1 and password[0] == password[-1] and password[0] in "'\"":
            self.stdout.write("Увага: DJANGO_SUPERUSER_PASSWORD узято в лапки — лапки теж є частиною пароля.")
        if password.startswith("<") and password.endswith(">"):
            self.stdout.write("Увага: DJANGO_SUPERUSER_PASSWORD у кутових дужках <…> — схоже, це шаблон, а не справжній пароль.")

        User = get_user_model()
        user = User.objects.filter(username=username).first()
        if user is None:
            User.objects.create_superuser(username=username, password=password, email="")
            self.stdout.write(f"Створено адміністратора {username}.")
        elif _flag("DJANGO_SUPERUSER_RESET_PASSWORD"):
            user.set_password(password)
            user.is_superuser = user.is_staff = user.is_active = True
            user.save()
            self.stdout.write(
                f"Пароль адміністратора {username} скинуто на значення DJANGO_SUPERUSER_PASSWORD. "
                "Тепер приберіть змінну DJANGO_SUPERUSER_RESET_PASSWORD."
            )
        else:
            self.stdout.write(
                f"Адміністратор {username} вже існує — пароль не змінюю. "
                "Щоб скинути пароль на DJANGO_SUPERUSER_PASSWORD, додайте DJANGO_SUPERUSER_RESET_PASSWORD=1 і перезапустіть."
            )
