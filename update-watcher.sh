#!/usr/bin/env bash
# GSG Update Watcher — следит за триггером обновления от web-orchestrator.
# Запускается на хосте через cron каждую минуту.
set -euo pipefail

# ── Lockfile — предотвращает параллельный запуск ──────────────────────────────
exec 200>/tmp/gsg-update.lock
flock -n 200 || exit 0

# ── Конфигурация ──────────────────────────────────────────────────────────────
GSG_DIR="/root/GSG"
CONFIG_VOL="/var/lib/docker/volumes/gsg_gsg_config/_data"
TRIGGER="$CONFIG_VOL/.update_trigger"
LOG="$CONFIG_VOL/.update_log"
STATE_FILE="$CONFIG_VOL/.update_state.json"

# Telegram — пробуем .release.env, потом переменные окружения
RELEASE_ENV="$GSG_DIR/.release.env"
[[ -f "$RELEASE_ENV" ]] && source "$RELEASE_ENV"
TG_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TG_CHAT="${TELEGRAM_NOTIFY_CHAT_ID:-}"

# ── Вспомогательные функции ───────────────────────────────────────────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

send_telegram() {
    local text="$1"
    [[ -z "$TG_TOKEN" || -z "$TG_CHAT" ]] && return 0

    local proxy_args=""
    if curl -s --proxy "http://127.0.0.1:2080" --max-time 3 https://t.me > /dev/null 2>&1; then
        proxy_args="--proxy http://127.0.0.1:2080"
    fi

    curl -s -o /dev/null $proxy_args \
        -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "{\"chat_id\": \"${TG_CHAT}\", \"text\": $(echo "$text" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'), \"parse_mode\": \"HTML\", \"disable_web_page_preview\": true}" \
        2>/dev/null || true
}

# Сохраняет snapshot состояния перед обновлением
save_state() {
    local git_hash git_short version timestamp
    git_hash=$(cd "$GSG_DIR" && git rev-parse HEAD 2>/dev/null || echo "")
    git_short=$(cd "$GSG_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "")
    version=$(grep 'GSG_VERSION = ' "$GSG_DIR/web-orchestrator/main.py" 2>/dev/null \
              | sed 's/.*"\(.*\)".*/\1/' || echo "unknown")
    timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    python3 -c "
import json, sys
state = {
    'pre_update':  {'git_hash': '$git_hash', 'git_hash_short': '$git_short', 'version': '$version', 'timestamp': '$timestamp'},
    'post_update': {'git_hash': '', 'version': '', 'status': 'pending', 'timestamp': ''},
    'last_rollback': None
}
print(json.dumps(state, indent=2))
" > "$STATE_FILE"
    log "Состояние сохранено: v$version ($git_short)"
}

# Обновляет post_update в state-файле
update_post_state() {
    local status="$1"
    local git_hash git_short version timestamp
    git_hash=$(cd "$GSG_DIR" && git rev-parse HEAD 2>/dev/null || echo "")
    git_short=$(cd "$GSG_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "")
    version=$(grep 'GSG_VERSION = ' "$GSG_DIR/web-orchestrator/main.py" 2>/dev/null \
              | sed 's/.*"\(.*\)".*/\1/' || echo "unknown")
    timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    python3 -c "
import json
try:
    with open('$STATE_FILE') as f:
        state = json.load(f)
except Exception:
    state = {'pre_update': {}, 'post_update': {}, 'last_rollback': None}
state['post_update'] = {'git_hash': '$git_hash', 'git_hash_short': '$git_short', 'version': '$version', 'status': '$status', 'timestamp': '$timestamp'}
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null || true
}

# Healthcheck: проверяет все сервисы, возвращает 0 если всё OK
run_healthcheck() {
    local attempt="${1:-1}"
    log "Healthcheck (попытка $attempt/3)..."

    # 1. Web orchestrator
    if ! curl -sf --max-time 5 http://127.0.0.1:8080/api/version > /dev/null 2>&1; then
        FAIL_REASON="web-orchestrator недоступен (/api/version)"
        return 1
    fi

    # 2. Mihomo
    if ! curl -sf --max-time 5 http://127.0.0.1:9090/version > /dev/null 2>&1; then
        FAIL_REASON="Mihomo недоступен (порт 9090)"
        return 1
    fi

    # 3. DHCP
    if ! docker exec gsg-dhcp pidof dnsmasq > /dev/null 2>&1; then
        FAIL_REASON="dnsmasq не работает в gsg-dhcp"
        return 1
    fi

    # 4. NetEnforcer
    local ne_running
    ne_running=$(docker inspect -f '{{.State.Running}}' gsg-netenforcer 2>/dev/null || echo "false")
    if [[ "$ne_running" != "true" ]]; then
        FAIL_REASON="gsg-netenforcer не запущен"
        return 1
    fi

    # 5. Интернет
    if ! curl -sf --max-time 10 http://connectivitycheck.gstatic.com/generate_204 > /dev/null 2>&1; then
        FAIL_REASON="нет доступа в интернет"
        return 1
    fi

    log "Healthcheck пройден"
    return 0
}

# Откат к предыдущей версии
do_rollback() {
    local trigger_type="${1:-auto}"   # "auto" или "manual"
    local reason="${2:-нет причины}"

    log "===== ОТКАТ ($trigger_type): $reason ====="

    # Читаем pre_update.git_hash из state-файла
    local prev_hash=""
    if [[ -f "$STATE_FILE" ]]; then
        prev_hash=$(python3 -c "
import json
try:
    with open('$STATE_FILE') as f:
        d = json.load(f)
    print(d.get('pre_update', {}).get('git_hash', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
    fi

    if [[ -z "$prev_hash" ]]; then
        log "ОШИБКА: не удалось определить hash предыдущей версии"
        send_telegram "GSG Rollback FAILED — нет hash для отката"
        return 1
    fi

    log "Откат к $prev_hash..."
    cd "$GSG_DIR"

    if ! git reset --hard "$prev_hash" >> "$LOG" 2>&1; then
        log "ОШИБКА: git reset --hard $prev_hash не удался"
        send_telegram "GSG Rollback FAILED — git reset не удался ($prev_hash)"
        return 1
    fi

    log "docker compose build (rollback)..."
    if ! docker compose build >> "$LOG" 2>&1; then
        log "ОШИБКА: docker compose build при откате не удался"
        send_telegram "GSG Rollback FAILED — docker compose build не удался"
        return 1
    fi

    docker compose up -d >> "$LOG" 2>&1

    # Обновляем state
    local timestamp
    timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    python3 -c "
import json
try:
    with open('$STATE_FILE') as f:
        state = json.load(f)
except Exception:
    state = {'pre_update': {}, 'post_update': {}, 'last_rollback': None}
state['post_update']['status'] = 'rolled_back'
state['last_rollback'] = {
    'trigger_type': '$trigger_type',
    'reason': $(echo "$reason" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))'),
    'rolled_back_to': '$prev_hash',
    'timestamp': '$timestamp'
}
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
" 2>/dev/null || true

    local prev_version
    prev_version=$(python3 -c "
import json
try:
    with open('$STATE_FILE') as f:
        d = json.load(f)
    print(d.get('pre_update', {}).get('version', '?'))
except Exception:
    print('?')
" 2>/dev/null || echo "?")

    log "Откат завершён — вернулись к v$prev_version ($prev_hash)"
    send_telegram "GSG Rollback — откат к v${prev_version} (${trigger_type}): ${reason}"
}

# ── Главный поток ─────────────────────────────────────────────────────────────

if [[ ! -f "$TRIGGER" ]]; then
    exit 0
fi

# Читаем содержимое триггера
TRIGGER_CONTENT=$(cat "$TRIGGER" 2>/dev/null || echo "")

# Ручной откат (запрошен через веб-интерфейс)
if echo "$TRIGGER_CONTENT" | grep -q "rollback_requested"; then
    log "===== Ручной откат запрошен ====="
    rm -f "$TRIGGER"
    do_rollback "manual" "запрошен через веб-интерфейс"
    exit 0
fi

# ── Обновление ────────────────────────────────────────────────────────────────
log "===== Обновление запущено ====="
FAIL_REASON=""

# Сохраняем состояние перед обновлением
save_state

cd "$GSG_DIR"

log "git fetch..."
if ! git fetch origin main >> "$LOG" 2>&1; then
    log "ОШИБКА: git fetch не удался"
    rm -f "$TRIGGER"
    exit 1
fi

log "git reset --hard origin/main..."
if ! git reset --hard origin/main >> "$LOG" 2>&1; then
    log "ОШИБКА: git reset --hard не удался"
    rm -f "$TRIGGER"
    exit 1
fi

log "docker compose build..."
if ! docker compose build >> "$LOG" 2>&1; then
    log "ОШИБКА: docker compose build не удался"
    rm -f "$TRIGGER"
    update_post_state "failed_build"
    exit 1
fi

rm -f "$TRIGGER"
log "docker compose up -d..."
docker compose up -d >> "$LOG" 2>&1

# Обновляем post_update с pending статусом
update_post_state "pending"

# ── Healthcheck с 3 попытками ─────────────────────────────────────────────────
log "Ожидание запуска сервисов (15 сек)..."
sleep 15

HC_OK=false
for attempt in 1 2 3; do
    if run_healthcheck "$attempt"; then
        HC_OK=true
        break
    fi
    if [[ "$attempt" -lt 3 ]]; then
        log "Healthcheck не пройден: $FAIL_REASON. Повтор через 15 сек..."
        sleep 15
    fi
done

if $HC_OK; then
    update_post_state "healthy"
    NEW_VERSION=$(grep 'GSG_VERSION = ' "$GSG_DIR/web-orchestrator/main.py" 2>/dev/null \
                  | sed 's/.*"\(.*\)".*/\1/' || echo "?")
    log "===== Обновление завершено успешно: v$NEW_VERSION ====="
    send_telegram "GSG обновлён до v${NEW_VERSION}"
else
    log "Healthcheck провален после 3 попыток: $FAIL_REASON"
    log "Запускаем автоматический откат..."
    update_post_state "failed_healthcheck"
    do_rollback "auto" "$FAIL_REASON"
fi
