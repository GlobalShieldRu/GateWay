FROM debian:bookworm-slim
ARG DEBIAN_FRONTEND=noninteractive
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates python3 python3-yaml python3-httpx inotify-tools gzip \
    && rm -rf /var/lib/apt/lists/*

# Поднимаем версию до максимально стабильной из ветки 1.18
ENV MIHOMO_VERSION="v1.18.10"

# Скачиваем ТОЛЬКО ядро. Оффлайн базы удалены, так как теперь работают Rule Providers
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then PKG="linux-arm64"; \
    elif [ "$ARCH" = "x86_64" ]; then PKG="linux-amd64-compatible"; \
    else echo "Unsupported arch: $ARCH" && exit 1; fi && \
    curl -fL "https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/mihomo-${PKG}-${MIHOMO_VERSION}.gz" | gzip -d > /usr/local/bin/mihomo && \
    chmod +x /usr/local/bin/mihomo && \
    mkdir -p /etc/mihomo

COPY . .
RUN chmod +x entrypoint.sh
ENTRYPOINT ["./entrypoint.sh"]
