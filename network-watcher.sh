#!/usr/bin/env bash
# GSG Network Watcher — демон, применяет изменения сети из UI на хост.
# Слушает /etc/gsg/.network_reconfig_request (через docker volume mount),
# читает /etc/gsg/network.json, регенерирует netplan + env в docker-compose.yml,
# перезапускает контейнеры и применяет netplan.
#
# Не вынесено в web-orchestrator потому что контейнер не имеет доступа к /etc/netplan
# и docker daemon хоста. Архитектурно повторяет update-watcher.sh.
set -euo pipefail

GSG_DIR="/root/GSG"
CONFIG_VOL="/var/lib/docker/volumes/gsg_gsg_config/_data"
TRIGGER="$CONFIG_VOL/.network_reconfig_request"
NETWORK_JSON="$CONFIG_VOL/network.json"
LOG="$CONFIG_VOL/.network_log"
NETPLAN_FILE="/etc/netplan/01-gsg-lan.yaml"
COMPOSE_FILE="$GSG_DIR/docker-compose.yml"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

apply_network() {
    log "Получен запрос на перенастройку сети"

    if [[ ! -f "$NETWORK_JSON" ]]; then
        log "ОШИБКА: $NETWORK_JSON не найден"
        return 1
    fi

    # Парсим network.json
    local IFACE GSG_IP PREFIX UP_GW UP_DNS DHCP_START DHCP_END DHCP_DNS
    IFACE=$(python3 -c "import json; print(json.load(open('$NETWORK_JSON'))['interface'])")
    GSG_IP=$(python3 -c "import json; print(json.load(open('$NETWORK_JSON'))['gsg_ip'])")
    PREFIX=$(python3 -c "import json; print(json.load(open('$NETWORK_JSON'))['prefix'])")
    UP_GW=$(python3 -c "import json; print(json.load(open('$NETWORK_JSON'))['upstream_gateway'])")
    UP_DNS=$(python3 -c "import json; print(','.join(json.load(open('$NETWORK_JSON'))['upstream_dns']))")
    DHCP_START=$(python3 -c "import json; print(json.load(open('$NETWORK_JSON'))['dhcp_start'])")
    DHCP_END=$(python3 -c "import json; print(json.load(open('$NETWORK_JSON'))['dhcp_end'])")
    DHCP_DNS=$(python3 -c "import json; print(json.load(open('$NETWORK_JSON'))['dhcp_dns'])")

    log "Новая конфигурация: iface=$IFACE ip=$GSG_IP/$PREFIX gw=$UP_GW dns=$UP_DNS pool=$DHCP_START..$DHCP_END"

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
        addresses: [$(echo "$UP_DNS" | sed 's/,/, /g')]
EOF
    chmod 600 "$NETPLAN_FILE"

    # ── 2. Обновляем env-vars в docker-compose.yml ──────────────────────────
    log "Обновляю env в $COMPOSE_FILE"
    sed -i "s|GSG_GATEWAY_IP=.*|GSG_GATEWAY_IP=${GSG_IP}|g" "$COMPOSE_FILE"
    sed -i "s|GSG_LAN_INTERFACE=.*|GSG_LAN_INTERFACE=${IFACE}|g" "$COMPOSE_FILE"
    sed -i "s|GSG_DHCP_START=.*|GSG_DHCP_START=${DHCP_START}|g" "$COMPOSE_FILE"
    sed -i "s|GSG_DHCP_END=.*|GSG_DHCP_END=${DHCP_END}|g" "$COMPOSE_FILE"

    # ── 3. Удаляем триггер ДО рестарта (рестарт убивает наш SSH/UI-сеанс) ───
    rm -f "$TRIGGER"

    # ── 4. Перезапускаем контейнеры с новыми env ────────────────────────────
    # До netplan apply — пока сеть ещё работает по старому IP. docker compose
    # пересоздаст gsg-dhcp, gsg-netenforcer с новыми GATEWAY_IP/CIDR.
    log "Пересоздаю контейнеры с новыми env"
    cd "$GSG_DIR"
    docker compose up -d --force-recreate gsg-dhcp gsg-netenforcer 2>&1 | tee -a "$LOG" || true

    # ── 5. Применяем netplan (SSH/UI отваливается здесь) ────────────────────
    log "netplan apply — текущие сетевые сеансы будут разорваны"
    netplan apply 2>&1 | tee -a "$LOG" || true

    log "Перенастройка завершена. Новый адрес GSG: http://${GSG_IP}:8080"
}

# ── Основной цикл ─────────────────────────────────────────────────────────────
log "GSG Network Watcher запущен, слушаю $TRIGGER"

mkdir -p "$CONFIG_VOL"

# Если триггер уже есть на старте (например, watcher упал во время прошлой
# попытки) — обработать сразу.
[[ -f "$TRIGGER" ]] && apply_network || true

# inotifywait не видит файлы созданные ДО его старта — нужен create-watch на
# каталог.
inotifywait -m -e create,moved_to,close_write "$CONFIG_VOL" 2>/dev/null | while read _path _action file; do
    if [[ "$file" == ".network_reconfig_request" ]]; then
        # Дебаунс на случай нескольких событий подряд (write/close_write/move)
        sleep 0.5
        if [[ -f "$TRIGGER" ]]; then
            apply_network || log "ОШИБКА при apply_network (exit=$?)"
        fi
    fi
done
