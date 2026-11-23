FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN DJANGO_DEBUG=0 DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput

RUN useradd --create-home app
USER app

EXPOSE 8000
# PORT задають хостинги (Render, Railway…); локально — 8000.
# gthread: браузери (Chrome) заздалегідь відкривають «порожні» з'єднання; звичайні (sync) процеси gunicorn
# на них зависали до тайм-ауту й віддавали голу сторінку «Internal Server Error». Потоки цього не мають.
# timeout 120 — запас для ручної синхронізації з Work.ua.
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py ensure_admin && gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2 --worker-class gthread --threads 8 --timeout 120"]
