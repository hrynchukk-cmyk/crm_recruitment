import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Створює адміністратора з DJANGO_SUPERUSER_USERNAME / DJANGO_SUPERUSER_PASSWORD, якщо його ще немає. "
        "Зручно на хостингах без доступу до консолі. Нічого не робить, якщо змінні не задані."
    )

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "").strip()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
        if not username or not password:
            return
        User = get_user_model()
        if User.objects.filter(username=username).exists():
            return
        User.objects.create_superuser(username=username, password=password, email="")
        self.stdout.write(f"Створено адміністратора {username}")
