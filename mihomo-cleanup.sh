#!/bin/bash
# Автоочистка зависших соединений Mihomo
# Устанавливается в /usr/local/bin/mihomo-cleanup.sh
# Crontab: 0 */2 * * * /usr/local/bin/mihomo-cleanup.sh

MIHOMO_API="http://127.0.0.1:9090"
LOG="/var/log/mihomo-cleanup.log"
THRESHOLD=1000

COUNT=$(curl -sf "${MIHOMO_API}/connections" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('connections', [])))" 2>/dev/null)

if [ -z "$COUNT" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: не удалось получить количество соединений" >> "$LOG"
    exit 1
fi

if [ "$COUNT" -gt "$THRESHOLD" ]; then
    curl -sf -X DELETE "${MIHOMO_API}/connections" > /dev/null 2>&1
    echo "$(date '+%Y-%m-%d %H:%M:%S') CLEANUP: было ${COUNT} соединений, выполнен DELETE /connections" >> "$LOG"
fi
