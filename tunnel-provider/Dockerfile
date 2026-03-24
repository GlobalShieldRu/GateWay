FROM debian:bookworm-slim
ARG DEBIAN_FRONTEND=noninteractive
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates python3 python3-pip inotify-tools gzip \
    && pip3 install --no-cache-dir --break-system-packages httpx PyYAML \
    && rm -rf /var/lib/apt/lists/*

ENV MIHOMO_VERSION="v1.18.10"

RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then PKG="linux-arm64"; \
    elif [ "$ARCH" = "x86_64" ]; then PKG="linux-amd64-compatible"; \
    elif [ "$ARCH" = "armv7l" ]; then PKG="linux-armv7l"; \
    else echo "Unsupported arch: $ARCH" && exit 1; fi && \
    curl -fL "https://github.com/MetaCubeX/mihomo/releases/download/${MIHOMO_VERSION}/mihomo-${PKG}-${MIHOMO_VERSION}.gz" | gzip -d > /usr/local/bin/mihomo && \
    chmod +x /usr/local/bin/mihomo && \
    mkdir -p /etc/mihomo

COPY generate_config.py /usr/local/bin/generate_config.py
RUN chmod +x /usr/local/bin/generate_config.py

COPY entrypoint.sh .
RUN chmod +x entrypoint.sh
ENTRYPOINT ["./entrypoint.sh"]
