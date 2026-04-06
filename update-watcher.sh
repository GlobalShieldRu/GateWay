#!/usr/bin/env bash
# GSG Update Watcher — демон, следит за триггером обновления через inotifywait.
# Запускается как systemd-сервис (gsg-updater.service), не через cron.
set -euo pipefail

# ── Конфигурация ──────────────────────────────────────────────────────────────
GSG_DIR="/root/GSG"
CONFIG_VOL="/var/lib/docker/volumes/gsg_gsg_config/_data"
TRIGGER="$CONFIG_VOL/.update_trigger"
LOG="$CONFIG_VOL/.update_log"
STATE_FILE="$CONFIG_VOL/.update_state.json"

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

save_state() {
    local git_hash git_short version timestamp
    git_hash=$(cd "$GSG_DIR" && git rev-parse HEAD 2>/dev/null || echo "")
    git_short=$(cd "$GSG_DIR" && git rev-parse --short HEAD 2>/dev/null || echo "")
    version=$(grep 'GSG_VERSION = ' "$GSG_DIR/web-orchestrator/main.py" 2>/dev/null \
              | sed 's/.*"\(.*\)".*/\1/' || echo "unknown")
    timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    python3 -c "
import json
state = {
    'pre_update':  {'git_hash': '$git_hash', 'git_hash_short': '$git_short', 'version': '$version', 'timestamp': '$timestamp'},
    'post_update': {'git_hash': '', 'version': '', 'status': 'pending', 'timestamp': ''},
    'last_rollback': None
}
print(json.dumps(state, indent=2))
" > "$STATE_FILE"
    log "Состояние сохранено: v$version ($git_short)"
}

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

run_healthcheck() {
    local attempt="${1:-1}"
    log "Healthcheck (попытка $attempt/3)..."
    if ! curl -sf --max-time 5 http://127.0.0.1:8080/api/version > /dev/null 2>&1; then
        FAIL_REASON="web-orchestrator недоступен (/api/version)"; return 1
    fi
    if ! curl -sf --max-time 5 http://127.0.0.1:9090/version > /dev/null 2>&1; then
        FAIL_REASON="Mihomo недоступен (порт 9090)"; return 1
    fi
    if ! docker exec gsg-dhcp pidof dnsmasq > /dev/null 2>&1; then
        FAIL_REASON="dnsmasq не работает"; return 1
    fi
    if ! curl -sf --max-time 10 http://connectivitycheck.gstatic.com/generate_204 > /dev/null 2>&1; then
        FAIL_REASON="нет доступа в интернет"; return 1
    fi
    log "Healthcheck пройден"
    return 0
}

do_rollback() {
    local trigger_type="${1:-auto}"
    local reason="${2:-нет причины}"
    log "===== ОТКАТ ($trigger_type): $reason ====="
    local prev_hash=""
    if [[ -f "$STATE_FILE" ]]; then
        prev_hash=$(python3 -c "
import json
try:
    with open('$STATE_FILE') as f: d = json.load(f)
    print(d.get('pre_update', {}).get('git_hash', ''))
except: print('')
" 2>/dev/null || echo "")
    fi
    if [[ -z "$prev_hash" ]]; then
        log "ОШИБКА: нет hash для отката"
        send_telegram "GSG Rollback FAILED — нет hash"
        return 1
    fi
    log "Откат к $prev_hash..."
    cd "$GSG_DIR"
    git reset --hard "$prev_hash" >> "$LOG" 2>&1 || { log "ОШИБКА: git reset не удался"; return 1; }
    # Восстанавливаем исполняемые биты после git reset
    chmod +x "$GSG_DIR"/*.sh 2>/dev/null || true
    log "docker compose build (rollback)..."
    docker compose build >> "$LOG" 2>&1 || { log "ОШИБКА: build не удался"; return 1; }
    docker compose up -d >> "$LOG" 2>&1
    update_post_state "rolled_back"
    local prev_version
    prev_version=$(python3 -c "
import json
try:
    with open('$STATE_FILE') as f: d = json.load(f)
    print(d.get('pre_update', {}).get('version', '?'))
except: print('?')
" 2>/dev/null || echo "?")
    log "Откат завершён — v$prev_version ($prev_hash)"
    send_telegram "GSG Rollback — v${prev_version} (${trigger_type}): ${reason}"
}

# ── Обработка одного триггера ─────────────────────────────────────────────────
process_trigger() {
    local content
    content=$(cat "$TRIGGER" 2>/dev/null || echo "")

    # Ручной откат
    if echo "$content" | grep -q "rollback_requested"; then
        log "===== Ручной откат запрошен ====="
        rm -f "$TRIGGER"
        do_rollback "manual" "запрошен через веб-интерфейс"
        return
    fi

    # Обновление
    log "===== Обновление запущено ====="
    FAIL_REASON=""
    save_state
    cd "$GSG_DIR"

    log "[STAGE:1/6] Загрузка обновлений..."
    if ! git -c http.timeout=30 -c http.lowSpeedLimit=0 fetch origin main >> "$LOG" 2>&1; then
        log "ОШИБКА: git fetch не удался"
        rm -f "$TRIGGER"
        update_post_state "failed_fetch"
        return
    fi

    log "[STAGE:2/6] Применение обновлений..."
    if ! git reset --hard origin/main >> "$LOG" 2>&1; then
        log "ОШИБКА: git reset не удался"
        rm -f "$TRIGGER"
        update_post_state "failed_reset"
        return
    fi

    # Восстанавливаем исполняемые биты
    chmod +x "$GSG_DIR"/*.sh 2>/dev/null || true

    log "[STAGE:3/6] Сборка контейнеров..."
    # docker compose v5 может возвращать RC=0 даже при ошибке (баг BuildKit).
    # Дополнительно проверяем вывод на признаки ошибки.
    # set +e нужен чтобы set -euo pipefail не убил скрипт при RC≠0 до проверки.
    _BUILD_TMP=$(mktemp)
    set +e
    docker compose build 2>&1 | tee -a "$LOG" > "$_BUILD_TMP"
    _BUILD_RC=${PIPESTATUS[0]}
    set -e
    if [ "$_BUILD_RC" -ne 0 ] || grep -qiE 'failed to (build|solve)|^ERROR:' "$_BUILD_TMP"; then
        rm -f "$_BUILD_TMP"
        log "ОШИБКА: docker compose build не удался"
        rm -f "$TRIGGER"
        update_post_state "failed_build"
        return
    fi
    rm -f "$_BUILD_TMP"

    rm -f "$TRIGGER"
    log "[STAGE:4/6] Запуск сервисов..."
    docker compose up -d >> "$LOG" 2>&1
    update_post_state "pending"

    log "[STAGE:5/6] Ожидание запуска..."
    sleep 15

    log "[STAGE:6/6] Проверка работоспособности..."
    local hc_ok=false
    for attempt in 1 2 3; do
        if run_healthcheck "$attempt"; then
            hc_ok=true
            break
        fi
        [[ "$attempt" -lt 3 ]] && { log "Повтор через 15 сек..."; sleep 15; }
    done

    if $hc_ok; then
        update_post_state "healthy"
        local new_version
        new_version=$(grep 'GSG_VERSION = ' "$GSG_DIR/web-orchestrator/main.py" 2>/dev/null \
                      | sed 's/.*"\(.*\)".*/\1/' || echo "?")
        log "===== Обновление завершено: v$new_version ====="
        send_telegram "GSG обновлён до v${new_version}"
    else
        log "Healthcheck провален: $FAIL_REASON"
        update_post_state "failed_healthcheck"
        do_rollback "auto" "$FAIL_REASON"
    fi
}

# ── Главный цикл (демон) ─────────────────────────────────────────────────────
log "Update watcher запущен (inotifywait mode)"

# Обработать триггер если уже существует при старте
if [[ -f "$TRIGGER" ]]; then
    log "Триггер найден при старте, обрабатываем..."
    process_trigger
fi

# Ждём новых триггеров через inotifywait
while true; do
    # Проверяем триггер который мог появиться пока обрабатывался предыдущий
    # (inotifywait завершается после первого события, не отслеживает параллельные создания)
    if [[ -f "$TRIGGER" ]]; then
        log "Триггер найден в цикле ожидания, обрабатываем..."
        process_trigger
        continue
    fi
    inotifywait -q -e close_write,create,moved_to "$CONFIG_VOL" 2>/dev/null | while read -r path action file; do
        if [[ "$file" == ".update_trigger" ]]; then
            log "Триггер обнаружен ($action)"
            process_trigger
        fi
    done
    # Если inotifywait упал — пауза и перезапуск
    sleep 5
done
