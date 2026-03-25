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

# Переносим готовые скомпилированные пакеты
COPY --from=builder /install /usr/local
COPY . .

# Запуск с отключенным спамом в консоль
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--log-level", "warning"]
