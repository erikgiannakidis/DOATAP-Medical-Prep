# DOATAP Medical Prep

Первый выпуск Telegram-бота для тренировки медицинских вопросов DOATAP. Python 3.12, PostgreSQL и `python-telegram-bot`.

## Возможности

- Интерфейс Ελληνικά / Русский / English.
- Греческие вопросы по Ανατομία, Φυσιολογία и Φαρμακολογία.
- Быстрые тесты на 10, 25 или 50 вопросов.
- Сохранение ответа, продолжение после перезапуска, история и избранное.
- Повторный клик и кнопки старого теста не изменяют текущий ответ.
- Неизменяемая копия вопросов и ключей для каждой попытки.

Вопросы после импорта имеют статус ответа `UNVERIFIED`. Бот сохраняет выбор, но не сообщает выдуманный правильный ответ и не включает такой вопрос в процент результата. Оценка рассчитывается только по ключам `OFFICIAL` / `MEDICAL_REVIEWED`.

Это учебный пилот. Экзамен с таймером, Clinical Bank, административная проверка ключей и AI Tutor ещё не входят в этот выпуск. Бот не связан с организацией DOATAP. Автоматический разбор PDF проверяет структуру, но не заменяет редакционную проверку текста и медицинских ключей.

## Подготовка банка

Оригинальный PDF и полный банк вопросов не входят в открытый репозиторий. Поместите имеющийся у вас PDF в `materials/`, затем:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/parse_question_bank.py "materials/ΠΡΟΚΛΙΝΙΚΕΣ-ΕΠΙΣΤΗΜΕΣ(1).pdf"
```

Команда создаёт `data/questions.jsonl` и `data/import_report.json`. Проблемные записи сохраняются с `NEEDS_REVIEW` и не выдаются в тестах. Оригинальный текст и страница сохраняются для проверки.

## Запуск с PostgreSQL через Docker Compose

1. Установите Docker с Compose на сервере. Подготовьте банк по инструкции выше или скопируйте свой `data/questions.jsonl` на сервер.
2. Создайте секреты локально на сервере:

```bash
python3 scripts/configure.py
```

Скрипт запрашивает токен BotFather скрытым вводом, создаёт случайный пароль PostgreSQL и сохраняет `.env` с правами `0600`. Файл исключён из Git и Docker-образа. Если `.env` уже есть, скрипт его не перезаписывает. Для пилота можно ограничить доступ, указав числовые Telegram ID в `ALLOWED_USER_IDS`.

3. Соберите образ и проверьте базу:

```bash
docker compose up -d db --wait
docker compose run --rm --build bot check
docker compose up -d --build bot
docker compose logs --tail=50 bot
```

База хранится в отдельном томе и не публикует порт наружу. Бот использует long polling и не требует публичного URL или входящего порта. Держите только один экземпляр бота на один токен. Не удаляйте том PostgreSQL при обновлении.

При каждой загрузке банка импорт обновляет данные идемпотентно; пользовательские попытки сохраняются. Ранее проверенные ключи и соответствующий им текст защищены от перезаписи обычным импортом.

## Запуск без Docker

Используйте постоянно работающий Python worker и PostgreSQL. Задайте переменные:

```text
BOT_TOKEN=<секрет BotFather>
DATABASE_URL=postgresql+psycopg://<user>:<url-encoded-password>@<host>:5432/<database>
QUESTION_BANK_PATH=data/questions.jsonl
ALLOWED_USER_IDS=<необязательно: Telegram ID через запятую>
```

```bash
.venv/bin/python -m doatap check
.venv/bin/python -m doatap run
```

`check` импортирует банк и проверяет базу, не обращаясь к Telegram. Для локальных проверок допускается явно задать SQLite (`DATABASE_URL=sqlite:///doatap.db`); для облачного запуска используйте PostgreSQL. Секреты добавляются в настройки хостинга, а не в GitHub или переписку.

## Проверка первого запуска

Откройте бот → `/start` → Русский → Быстрый тест → ΑΝΑΤΟΜΙΑ → 10 → выберите ответы → посмотрите итог. Для банка без ключей итог должен показывать число сохранённых ответов и отсутствие оценки. Проверьте продолжение теста после перезапуска, историю и избранное.

## Тесты

```bash
.venv/bin/python -m pytest -q
```

GitHub Actions дополнительно запускает тесты хранения на PostgreSQL 17 и собирает Docker-образ. Тесты не используют настоящий токен и не отправляют сообщения в Telegram. Тесты PostgreSQL используют отдельные временные схемы; не запускайте их на рабочей базе.

## Документация библиотек

- [python-telegram-bot: Application](https://docs.python-telegram-bot.org/en/stable/telegram.ext.application.html)
- [SQLAlchemy: PostgreSQL / psycopg](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg)
- [Docker Compose: ожидание готовности базы](https://docs.docker.com/compose/how-tos/startup-order/)
