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

DEVICE_FILE="$CONFIG_VOL/device.json"
GSG_API="https://api.globalshield.ru/v1"

# ── Вспомогательные функции ───────────────────────────────────────────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

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
    # dnsmasq отсутствует если DHCP выключен в gateway-only режиме — это нормально.
    # Проверяем только когда dhcp_enabled = true.
    DHCP_ENABLED=$(python3 -c "
import json
try:
    print('yes' if json.load(open('$CONFIG_VOL/settings.json')).get('dhcp_enabled', True) else 'no')
except: print('yes')
" 2>/dev/null || echo "yes")
    if [ "$DHCP_ENABLED" = "yes" ] && ! docker exec gsg-dhcp pidof dnsmasq > /dev/null 2>&1; then
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
        gsg_notify update_failed "Откат не выполнен: нет hash предыдущей версии" "trigger=${trigger_type}" "reason=${reason}"
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
    gsg_notify update_failed "Откат на v${prev_version}: ${reason}" "version=${prev_version}" "trigger=${trigger_type}"
}

# Есть ли на устройстве исполняемый файл из ExecStart юнита (.timer — всегда да).
# Префиксы systemd (-, @, +, !) отбрасываем.
unit_exec_present() {
    local unit_src="$1" exe
    [[ "$unit_src" == *.service ]] || return 0
    exe=$(sed -n 's/^ExecStart=[-@+!]*\([^ ]*\).*/\1/p' "$unit_src" | head -1)
    [[ -z "$exe" || -x "$exe" ]]
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
    if ! git -c http.timeout=30 -c http.lowSpeedLimit=0 fetch --tags --force origin main >> "$LOG" 2>&1; then
        log "ОШИБКА: git fetch не удался"
        rm -f "$TRIGGER"
        update_post_state "failed_fetch"
        return
    fi

    log "[STAGE:2/6] Применение обновлений..."
    # Ставим тег релиза, а не HEAD main: раскатка идёт до 72 ч, и поздние
    # stable-устройства должны получить тот же код, что канарейки, а не то, что
    # успело попасть в main за это время. Версию решения пишет web-orchestrator
    # в триггер (target_version=X.Y.Z); без неё — последний тег vX.Y.Z; без
    # тегов (форк/старая установка) — прежнее поведение origin/main.
    local target_version target_ref
    target_version=$(echo "$content" | sed -n 's/^target_version=\([0-9][0-9.]*\)$/\1/p' | head -1)
    if [[ -n "$target_version" ]] && git rev-parse -q --verify "refs/tags/v$target_version" >/dev/null; then
        target_ref="v$target_version"
    else
        target_ref=$(git tag -l 'v[0-9]*' --sort=-v:refname | head -1)
        [[ -n "$target_ref" ]] || target_ref="origin/main"
    fi
    log "Цель обновления: $target_ref"
    # Запоминаем хэш собственного скрипта ДО git reset — если он изменился, в
    # конце цикла рестартим gsg-updater чтобы systemd подхватил новый код.
    SELF_HASH_BEFORE=$(sha256sum "$0" 2>/dev/null | awk '{print $1}')
    if ! git reset --hard "$target_ref" >> "$LOG" 2>&1; then
        log "ОШИБКА: git reset не удался"
        rm -f "$TRIGGER"
        update_post_state "failed_reset"
        return
    fi

    # Восстанавливаем исполняемые биты
    chmod +x "$GSG_DIR"/*.sh 2>/dev/null || true

    # ── Миграция: network.json → settings.json (только dhcp_start/end) ──
    # До v1.13.5 параметры сети жили в /etc/gsg/network.json. Теперь IP/iface/
    # gateway/DNS читаются напрямую из ОС (`ip route`, `ip addr`,
    # /etc/resolv.conf), а DHCP-пул переехал в settings.json. Удаляем устаревший
    # файл после переноса данных.
    if [[ -f "$CONFIG_VOL/network.json" ]]; then
        log "Миграция: переношу dhcp_start/end из network.json в settings.json, удаляю network.json"
        python3 <<PYEOF 2>&1 | tee -a "$LOG"
import json, os
nj = "$CONFIG_VOL/network.json"
sj = "$CONFIG_VOL/settings.json"
try:
    n = json.load(open(nj))
except Exception:
    n = {}
try:
    s = json.load(open(sj))
except Exception:
    s = {}
if "dhcp_start" in n: s["dhcp_start"] = n["dhcp_start"]
if "dhcp_end" in n: s["dhcp_end"] = n["dhcp_end"]
json.dump(s, open(sj, "w"), indent=2)
os.remove(nj)
print("migrated and removed network.json")
PYEOF
    fi

    # Также удаляем устаревший /root/GSG/.env (был в v1.13.3-4; теперь
    # docker-compose.yml не использует переменные).
    [[ -f "$GSG_DIR/.env" ]] && { rm -f "$GSG_DIR/.env"; log "удалён устаревший .env"; }

    # Host-скрипты в /usr/local/bin/ — обновляем из репо при изменении.
    log "Синхронизация host-скриптов..."
    if [[ -f "$GSG_DIR/network-detect/main.py" ]]; then
        target_script="/usr/local/bin/gsg-network-detect.py"
        if [[ ! -f "$target_script" ]] || ! cmp -s "$GSG_DIR/network-detect/main.py" "$target_script"; then
            cp "$GSG_DIR/network-detect/main.py" "$target_script"
            chmod +x "$target_script"
            log "  + gsg-network-detect.py (new/changed)"
        fi
    fi
    if [[ -f "$GSG_DIR/gsg-warmer.sh" ]]; then
        target_script="/usr/local/bin/gsg-warmer"
        if [[ ! -f "$target_script" ]] || ! cmp -s "$GSG_DIR/gsg-warmer.sh" "$target_script"; then
            cp "$GSG_DIR/gsg-warmer.sh" "$target_script"
            chmod +x "$target_script"
            log "  + gsg-warmer (new/changed)"
        fi
    fi

    # Синхронизация systemd-юнитов: если в репо появились новые *.service/*.timer —
    # ставим их без участия пользователя. Иначе фичи требующие host-сервисов
    # (например network-watcher) недоступны до ручного `systemctl enable` —
    # пользователь GSG этого делать не будет (см. feedback memory: GSG автономен).
    # См. install.sh — он делает то же при первой установке.
    log "Синхронизация systemd-юнитов..."
    UNITS_CHANGED=0
    for unit_src in "$GSG_DIR"/gsg-*.service "$GSG_DIR"/gsg-*.timer; do
        [[ -f "$unit_src" ]] || continue
        unit_name="$(basename "$unit_src")"
        target="/etc/systemd/system/$unit_name"
        # Юнит, чей ExecStart на устройство не установлен (служебные сервисы
        # разработки, отключённый в install.sh gsg-watchdog), не включаем:
        # иначе systemd крутит его в вечном рестарте с 203/EXEC. Если он уже
        # был включён прошлым OTA — выключаем.
        if ! unit_exec_present "$unit_src"; then
            if systemctl is-enabled "$unit_name" >/dev/null 2>&1 || systemctl is-active "$unit_name" >/dev/null 2>&1; then
                systemctl disable --now "$unit_name" >> "$LOG" 2>&1 || true
                log "  − ${unit_name} выключен: ExecStart не установлен на устройстве"
            fi
            continue
        fi
        if [[ ! -f "$target" ]] || ! cmp -s "$unit_src" "$target"; then
            log "  + ${unit_name} (new/changed)"
            cp "$unit_src" "$target"
            UNITS_CHANGED=1
        fi
    done
    if [[ "$UNITS_CHANGED" -eq 1 ]]; then
        systemctl daemon-reload
        for unit_src in "$GSG_DIR"/gsg-*.service "$GSG_DIR"/gsg-*.timer; do
            [[ -f "$unit_src" ]] || continue
            unit_name="$(basename "$unit_src")"
            unit_exec_present "$unit_src" || continue
            # gsg-updater (мы сами) — restart позже отдельно, чтобы не убить
            # текущий процесс посреди OTA-цикла.
            [[ "$unit_name" == "gsg-updater.service" ]] && continue
            systemctl enable "$unit_name" >> "$LOG" 2>&1 || true
            # Для .timer — restart нормально (он перезапустит timer schedule).
            # Для .service — рестарт нужен чтобы подхватить новый код.
            systemctl restart "$unit_name" >> "$LOG" 2>&1 || true
            log "  ↻ ${unit_name} enabled+restarted"
        done
    fi

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
        gsg_notify update_ok "" "version=${new_version}"
        # Если сам update-watcher.sh изменился — рестартим себя через systemd,
        # иначе текущий процесс продолжит работать со старым кодом до reboot.
        SELF_HASH_AFTER=$(sha256sum "$0" 2>/dev/null | awk '{print $1}')
        if [[ "$SELF_HASH_BEFORE" != "$SELF_HASH_AFTER" ]]; then
            log "update-watcher.sh обновился — рестарт gsg-updater для подхвата нового кода"
            # --no-block: systemd запустит рестарт асинхронно, текущий процесс
            # завершит цикл process_trigger, потом systemd убъёт его и поднимет с новым кодом.
            systemctl restart gsg-updater.service --no-block 2>/dev/null || true
        fi
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
