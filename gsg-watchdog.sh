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
#   5. Лог в /var/log/gsg-watchdog.log + уведомление через API GlobalShield
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
DEVICE_FILE="${CONFIG_DIR}/device.json"
GSG_API="https://api.globalshield.ru/v1"

fail_count=0
last_restart=0

log() {
    local ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$ts] $*" | tee -a "$LOG_FILE"
}

# Уведомление через API GlobalShield: устройство токен бота не знает, сообщение
# собирает backend. Аргументы: kind text [key=value ...]. Best-effort, всегда 0.
# Запрос делает python3 (urllib), чтобы device_token не попадал в argv/ps.
gsg_notify() {
    local kind="$1" text="$2"
    shift 2
    if [[ ! -f "$DEVICE_FILE" ]]; then
        log "notify ${kind}: устройство не зарегистрировано (нет device.json) — пропуск"
        return 0
    fi
    local result
    result=$(python3 - "$DEVICE_FILE" "$GSG_API" "$kind" "$text" "$@" <<'PY' 2>&1
import json, sys, urllib.request
dev_file, api, kind, text, *pairs = sys.argv[1:]
try:
    dev = json.load(open(dev_file))
except Exception:
    dev = {}
did, tok = dev.get("device_id", ""), dev.get("device_token", "")
if not did or not tok:
    print("устройство не зарегистрировано — пропуск")
    sys.exit(0)
fields = {}
for p in pairs[:20]:
    k, _, v = p.partition("=")
    if k and v:
        fields[k] = v[:100]
body = json.dumps({"kind": kind, "text": text[:2000], "fields": fields}).encode()
req = urllib.request.Request(api.rstrip("/") + "/devices/notify", data=body, method="POST",
                             headers={"Content-Type": "application/json",
                                      "X-Device-ID": did, "X-Device-Token": tok})
try:
    with urllib.request.urlopen(req, timeout=10) as r:
        print(f"HTTP {r.status}")
except Exception as e:
    print(f"ошибка: {e}")
PY
)
    log "notify ${kind}: ${result:-нет ответа}"
    return 0
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
    gsg_notify tunnel_restart "$reason" "source=gsg-watchdog"
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
