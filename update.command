#!/bin/bash
# Оновлення CRM до останньої версії.
# macOS: подвійний клік на цьому файлі у Finder. Або в Терміналі: ./update.command
# Дані CRM (база) і ваш файл .env не змінюються.

cd "$(dirname "$0")" || exit 1

if [ ! -f .env ]; then
  echo "Не знайдено файл .env у папці $(pwd)."
  echo "Скопіюйте .env.example у .env і впишіть свої паролі (див. README)."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker не запущений. Відкрийте Docker Desktop, дочекайтеся «Engine running» і запустіть оновлення ще раз."
  exit 1
fi

echo "1/2 Завантажую оновлення з GitHub…"
if ! git pull --ff-only; then
  echo
  echo "Не вдалося завантажити оновлення (див. повідомлення вище)."
  echo "Найчастіша причина — змінені вручну файли проєкту. Нічого не видалено; напишіть, будь ласка, розробнику."
  exit 1
fi

echo "2/2 Перезбираю і перезапускаю CRM (дані зберігаються)…"
docker compose up -d --build || exit 1

PORT=$(grep -E '^CRM_PORT=' .env | cut -d= -f2 | tr -d '[:space:]')
echo
echo "Готово! CRM: http://localhost:${PORT:-8100}"
