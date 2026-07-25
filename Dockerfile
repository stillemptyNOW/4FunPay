# 4FunPay
#
# Сборка:   docker compose build
# Настройка: docker compose run --rm 4funpay python main.py
# Запуск:   docker compose up -d

FROM python:3.12-slim

# Пользовательские данные лежат в томах, поэтому образ можно пересобирать
# сколько угодно - конфиги и товарные файлы не затрагиваются.

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Kyiv

# tzdata нужен для корректных временных меток в уведомлениях и логах,
# ca-certificates - для HTTPS к funpay.com и api.telegram.org.
RUN apt-get update \
    && apt-get install --no-install-recommends -y tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Бот не выполняет ничего, что требует root.
RUN useradd --create-home --shell /bin/bash app

WORKDIR /app

# Зависимости отдельным слоем - пересобираются только при правке requirements.txt.
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . .

# Каталоги с данными создаём заранее и передаём пользователю app,
# иначе смонтированные тома окажутся принадлежащими root.
RUN mkdir -p configs logs storage/cache storage/plugins storage/products plugins \
    && chown -R app:app /app

USER app

# Проверка живости: процесс должен держать актуальный лог-файл.
# Полноценный healthcheck делает плагин watchdog - он пишет heartbeat в storage/cache.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import os, time, sys; \
p = 'storage/cache/heartbeat'; \
sys.exit(0 if os.path.exists(p) and time.time() - os.path.getmtime(p) < 300 else 1)"

CMD ["python", "main.py"]
