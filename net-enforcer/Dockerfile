FROM python:3.11-slim AS builder
ARG DEBIAN_FRONTEND=noninteractive
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc python3-dev libc-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim
WORKDIR /app
COPY --from=builder /install /usr/local
# ИСПРАВЛЕНИЕ: Добавлен procps для работы sysctl
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    nftables iproute2 iptables kmod curl iputils-ping procps \
    && rm -rf /var/lib/apt/lists/*
COPY . .
CMD ["python", "main.py"]
