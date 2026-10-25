FROM python:3.11-slim AS builder
ARG DEBIAN_FRONTEND=noninteractive
ARG PIP_INDEX_URL=https://pypi.org/simple/
WORKDIR /app

# Устанавливаем gcc и заголовочные файлы Python для сборки psutil
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc python3-dev libc-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --prefix=/install -r requirements.txt

FROM python:3.11-slim
ARG DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# docker-cli — для synchronous regen через `docker exec gsg-tunnel …`
# из handlers'ов. Без inotify+debounce цикла который иногда проглатывает
# события (инцидент 2026-07-02).
# В Debian 13 CLI вынесен в пакет docker-cli (docker.io — только демон, а CLI у
# него в Recommends и при --no-install-recommends не ставится); в Debian 12
# пакета docker-cli нет, и CLI лежит в docker.io. Сборка падает, если docker не появился.
RUN apt-get update \
    && (apt-get install -y --no-install-recommends docker-cli \
        || apt-get install -y --no-install-recommends docker.io) \
    && rm -rf /var/lib/apt/lists/* \
    && docker --version

# Переносим готовые скомпилированные пакеты
COPY --from=builder /install /usr/local
COPY . .

# Запуск с отключенным спамом в консоль
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--log-level", "warning"]
