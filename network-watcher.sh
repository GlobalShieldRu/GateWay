#!/usr/bin/env bash
# GSG Network Watcher — применяет изменения сети из UI к хосту.
# Слушает /etc/gsg/.network_reconfig_request, регенерирует netplan + apply.
# DHCP-пул пишет в settings.json (контейнер gsg-dhcp подхватит через inotify).
#
# Архитектура источников истины:
#   - IP/iface/gateway/DNS — netplan (что мы пишем) → ip route/ip addr → читают контейнеры
#   - DHCP-пул start/end — settings.json
#   - Никакого network.json или .env (удалены — дубликаты-источников приводили к багам).
set -euo pipefail

GSG_DIR="/root/GSG"
CONFIG_VOL="/var/lib/docker/volumes/gsg_gsg_config/_data"
TRIGGER="$CONFIG_VOL/.network_reconfig_request"
SETTINGS_JSON="$CONFIG_VOL/settings.json"
LOG="$CONFIG_VOL/.network_log"
NETPLAN_FILE="/etc/netplan/01-gsg-lan.yaml"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

apply_network() {
    log "Получен запрос на перенастройку сети"

    if [[ ! -f "$TRIGGER" ]]; then
        log "Триггер исчез — пропускаем"
        return 0
    fi

    # Парсим payload из триггера (web-orchestrator пишет туда настройки)
    local GSG_IP PREFIX UP_GW UP_DNS DHCP_START DHCP_END
    GSG_IP=$(python3 -c "import json; print(json.load(open('$TRIGGER'))['gsg_ip'])")
    PREFIX=$(python3 -c "import json; print(json.load(open('$TRIGGER'))['prefix'])")
    UP_GW=$(python3 -c "import json; print(json.load(open('$TRIGGER'))['upstream_gateway'])")
    UP_DNS=$(python3 -c "import json; print(', '.join(json.load(open('$TRIGGER'))['upstream_dns']))")
    DHCP_START=$(python3 -c "import json; print(json.load(open('$TRIGGER'))['dhcp_start'])")
    DHCP_END=$(python3 -c "import json; print(json.load(open('$TRIGGER'))['dhcp_end'])")

    # Интерфейс берём из текущего default route — пользователь его не задаёт,
    # это физический параметр устройства (end0 на NanoPi, eth0 на OrangePi).
    local IFACE
    IFACE=$(ip -o -4 route show default | awk '{print $5}' | head -1)

    log "Конфигурация: iface=$IFACE ip=$GSG_IP/$PREFIX gw=$UP_GW dns=$UP_DNS pool=$DHCP_START..$DHCP_END"

    # ── 1. Регенерация /etc/netplan/01-gsg-lan.yaml ─────────────────────────
    log "Пишу $NETPLAN_FILE"
    cat > "$NETPLAN_FILE" <<EOF
network:
  version: 2
  renderer: networkd
  ethernets:
    ${IFACE}:
      addresses: [${GSG_IP}/${PREFIX}]
      dhcp4: false
      routes:
        - to: default
          via: ${UP_GW}
      nameservers:
        addresses: [${UP_DNS}]
EOF
    chmod 600 "$NETPLAN_FILE"

    # ── 2. DHCP-пул → settings.json (gsg-dhcp подхватит через inotify) ──────
    log "Обновляю DHCP-пул в settings.json"
    python3 <<PYEOF
import json
p = "$SETTINGS_JSON"
try:
    d = json.load(open(p))
except Exception:
    d = {}
d["dhcp_start"] = "$DHCP_START"
d["dhcp_end"]   = "$DHCP_END"
json.dump(d, open(p, "w"), indent=2)
PYEOF

    # ── 3. Удаляем триггер ДО netplan apply (apply разорвёт сеансы) ─────────
    rm -f "$TRIGGER"

    # ── 4. Применяем netplan ────────────────────────────────────────────────
    # SSH/UI отвалятся здесь. Пользователь переподключается к новому IP.
    log "netplan apply — текущие сетевые сеансы будут разорваны"
    netplan apply 2>&1 | tee -a "$LOG" || true

    log "Перенастройка завершена. Новый адрес GSG: http://${GSG_IP}:8080"
}

# ── Основной цикл ─────────────────────────────────────────────────────────────
log "GSG Network Watcher запущен, слушаю $TRIGGER"

mkdir -p "$CONFIG_VOL"

# Если триггер уже есть на старте — обработать сразу.
[[ -f "$TRIGGER" ]] && apply_network || true

inotifywait -m -e create,moved_to,close_write "$CONFIG_VOL" 2>/dev/null | while read _path _action file; do
    if [[ "$file" == ".network_reconfig_request" ]]; then
        sleep 0.5  # debounce
        if [[ -f "$TRIGGER" ]]; then
            apply_network || log "ОШИБКА при apply_network (exit=$?)"
        fi
    fi
done
