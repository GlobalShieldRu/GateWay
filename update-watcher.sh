#!/usr/bin/env bash
# GSG Update Watcher — следит за триггером обновления от web-orchestrator.
# Запускается на хосте через cron каждую минуту.
set -euo pipefail

GSG_DIR="/root/GSG"
CONFIG_VOL="/var/lib/docker/volumes/gsg_gsg_config/_data"
TRIGGER="$CONFIG_VOL/.update_trigger"
LOG="$CONFIG_VOL/.update_log"

if [[ ! -f "$TRIGGER" ]]; then
    exit 0
fi

echo "[$(date)] Обновление запущено..." > "$LOG"

cd "$GSG_DIR"

echo "[$(date)] git pull..." >> "$LOG"
if ! git pull origin main >> "$LOG" 2>&1; then
    echo "[$(date)] ОШИБКА: git pull не удался" >> "$LOG"
    rm -f "$TRIGGER"
    exit 1
fi

echo "[$(date)] docker compose build..." >> "$LOG"
if ! docker compose build >> "$LOG" 2>&1; then
    echo "[$(date)] ОШИБКА: docker compose build не удался" >> "$LOG"
    rm -f "$TRIGGER"
    exit 1
fi

rm -f "$TRIGGER"
echo "[$(date)] docker compose up -d..." >> "$LOG"
docker compose up -d >> "$LOG" 2>&1

echo "[$(date)] Обновление завершено успешно" >> "$LOG"
