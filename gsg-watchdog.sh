#!/bin/bash
# gsg-watchdog.sh — host-level watchdog для GSG Smart Gateway.
#
# Запускается на хосте (OrangePi 10.10.1.139) через systemd, не в контейнере.
# Даёт доступ к `docker restart` (которого нет внутри gsg-web-orchestrator).
#
# Задачи:
#   1. Каждые 30с проверять группы auto/ai через Mihomo API
#   2. При 2 подряд провалах (60с) — docker restart gsg-tunnel
#   3. Мониторить файл-маркер /etc/gsg/.tunnel_restart_request — рестарт по запросу
#      (web-orchestrator или другие компоненты создают маркер, нам его достаточно найти)
#   4. Cooldown 90с между рестартами (защита от флаппинга)
#   5. Лог в /var/log/gsg-watchdog.log + Telegram alert (опционально)
#
# Причина существования:
# После множества API reload'ов Mihomo (`curl PUT /configs`) внутренний state
# proxy instances портится — fallback-группы начинают считать живые узлы мёртвыми.
# Единственное надёжное решение — docker restart контейнера gsg-tunnel.

set -u

CONFIG_DIR="/var/lib/docker/volumes/gsg_gsg_config/_data"
RESTART_MARKER="${CONFIG_DIR}/.tunnel_restart_request"
LOG_FILE="/var/log/gsg-watchdog.log"
COOLDOWN=90
CHECK_INTERVAL=30
FAIL_THRESHOLD=2
HEALTH_URL="http://www.gstatic.com/generate_204"
MIHOMO_API="http://127.0.0.1:9090"
TG_CONFIG="${CONFIG_DIR}/telegram.json"

fail_count=0
last_restart=0

log() {
    local ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$ts] $*" | tee -a "$LOG_FILE"
}

# Отправка Telegram-алерта (best-effort, файл telegram.json имеет bot_token и chat_id)
tg_alert() {
    local msg="$1"
    [ -f "$TG_CONFIG" ] || return 0
    local token chat
    token=$(python3 -c "import json;print(json.load(open('$TG_CONFIG')).get('bot_token',''))" 2>/dev/null)
    chat=$(python3 -c "import json;print(json.load(open('$TG_CONFIG')).get('chat_id',''))" 2>/dev/null)
    [ -z "$token" ] || [ -z "$chat" ] && return 0
    curl -s --max-time 5 "https://api.telegram.org/bot${token}/sendMessage" \
        -d "chat_id=${chat}" \
        -d "text=🛡️ GSG Watchdog: ${msg}" \
        -d "parse_mode=HTML" > /dev/null 2>&1
}

# Перезапуск gsg-tunnel с защитой от флаппинга
do_restart() {
    local reason="$1"
    local now=$(date +%s)
    local elapsed=$((now - last_restart))
    if [ "$elapsed" -lt "$COOLDOWN" ]; then
        log "SKIP restart (cooldown $((COOLDOWN - elapsed))s remaining): $reason"
        return 1
    fi
    log "RESTART gsg-tunnel: $reason"
    tg_alert "Auto-restart tunnel: $reason"
    docker restart gsg-tunnel > /dev/null 2>&1
    last_restart=$(date +%s)
    # Даём Mihomo 20 секунд на полный запуск + прогрев health-check
    sleep 20
    fail_count=0
    log "Restart complete"
}

# Проверка: возвращает 0 если группа здорова, 1 если мертва
check_group() {
    local group="$1"
    local encoded
    encoded=$(python3 -c "import urllib.parse;print(urllib.parse.quote('$group',safe=''))")
    local resp
    resp=$(curl -s --max-time 10 \
        "${MIHOMO_API}/group/${encoded}/delay?url=${HEALTH_URL}&timeout=5000" 2>/dev/null || echo "{}")
    # Группа мертва если: пустой {} или все значения = 0
    python3 -c "
import json, sys
try:
    d = json.loads('''$resp''')
    if not d: sys.exit(1)
    vals = [v for v in d.values() if isinstance(v,(int,float))]
    if not vals: sys.exit(1)
    if all(v == 0 for v in vals): sys.exit(1)
    sys.exit(0)
except: sys.exit(1)
" 2>/dev/null
    return $?
}

log "=== gsg-watchdog started (interval=${CHECK_INTERVAL}s, fail_threshold=${FAIL_THRESHOLD}, cooldown=${COOLDOWN}s) ==="
# Даём Mihomo время полноценно запуститься при старте системы
sleep 120

while true; do
    # 1) Проверка маркер-файла (запрос от web-orchestrator и т.п.)
    if [ -f "$RESTART_MARKER" ]; then
        reason=$(cat "$RESTART_MARKER" 2>/dev/null || echo "marker file")
        rm -f "$RESTART_MARKER"
        do_restart "manual: ${reason}"
    fi

    # 2) Health-check групп
    if check_group "auto" && check_group "ai"; then
        if [ "$fail_count" -gt 0 ]; then
            log "Health OK (recovered after ${fail_count} fails)"
        fi
        fail_count=0
    else
        fail_count=$((fail_count + 1))
        log "Health FAIL ${fail_count}/${FAIL_THRESHOLD} (auto or ai group dead)"
        if [ "$fail_count" -ge "$FAIL_THRESHOLD" ]; then
            do_restart "health-check: groups auto/ai dead ${FAIL_THRESHOLD}x"
        fi
    fi

    sleep "$CHECK_INTERVAL"
done
