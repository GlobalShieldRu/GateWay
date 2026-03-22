FROM python:3.11-slim
ARG DEBIAN_FRONTEND=noninteractive
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    dnsmasq inotify-tools curl && rm -rf /var/lib/apt/lists/*

COPY . .
RUN chmod +x entrypoint.sh
ENTRYPOINT ["./entrypoint.sh"]
