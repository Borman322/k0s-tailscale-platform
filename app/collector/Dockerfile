# Сборщик вакансий. Образ рассчитан на два сценария:
#   compose (/opt/jobmon): `command:` в compose переопределяет CMD — цикл «обход → спим INTERVAL_SECONDS»;
#   разовый запуск:
#     docker run --rm -e DB_DSN=postgresql://… vacancies                                   # обход → Postgres
#     docker run --rm vacancies python collector.py --dry-run --top 40                     # без БД
#     docker run --rm -v "$PWD/companies.yml:/app/companies.yml" vacancies python discover.py --from-boards
#   сборка: docker buildx build --platform linux/amd64 -t vacancies:latest .
FROM python:3.13-slim

LABEL org.opencontainers.image.title="vacancies-collector" \
      org.opencontainers.image.description="Сборщик вакансий: адаптеры ATS и досок, подбор slug'ов"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    VACANCIES_DATA=/data

WORKDIR /app

# зависимости — отдельным слоем: меняются реже кода; psycopg2-binary не требует ни gcc, ни libpq-dev
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Не root. /data — единственное место записи (кэш проб, отчёт discover); код и companies.yml остаются только на чтение,
# поэтому работает и `docker run --read-only`. Кэш — лишь ускорение: хотите сохранять между запусками — смонтируйте том на /data
# (VOLUME в образе не объявлен намеренно: иначе каждое пересоздание контейнера оставляет анонимный том-сироту).
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir /data && chown app:app /data
USER app

# Без портов и без демона: запустился, обошёл источники, записал в Postgres и завершился. Расписание — снаружи (цикл в compose, cron, CronJob).
# Секреты (DB_DSN, HH_TOKEN, HH_USER_AGENT) — только переменными окружения при запуске, в образ не кладём.
CMD ["python", "collector.py"]
