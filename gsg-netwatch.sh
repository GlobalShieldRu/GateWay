#!/bin/bash
# GSG Network Watchdog
# Detects upstream gateway loss → stops containers → falls back to DHCP
# so the device becomes reachable at a new IP after being moved to another router.

set -e

GSG_NET_CONF="/etc/gsg/network.conf"   # написан install.sh, не зависит от network manager
CHECK_INTERVAL=20                        # секунд между проверками
FAIL_THRESHOLD=9                         # 9 × 20s = 3 минуты (переживает перезагрузку роутера)
INSTALL_DIR="/root/GSG"

log() { logger -t gsg-netwatch "$*"; echo "[gsg-netwatch] $*"; }

# ── Читаем параметры из GSG network.conf ─────────────────────────────────────
if [ ! -f "$GSG_NET_CONF" ]; then
    log "Нет $GSG_NET_CONF — watchdog не нужен, выходим."
    exit 0
fi

IFACE=$(grep '^IFACE=' "$GSG_NET_CONF" | cut -d= -f2)
GW=$(grep '^GW='    "$GSG_NET_CONF" | cut -d= -f2)
NM_METHOD=$(grep '^NM_METHOD=' "$GSG_NET_CONF" | cut -d= -f2)   # netplan | networkmanager | ifupdown

if [ -z "$IFACE" ] || [ -z "$GW" ]; then
    log "IFACE или GW не задан в $GSG_NET_CONF — выходим."
    exit 0
fi

log "Слежу за шлюзом $GW на $IFACE (метод: ${NM_METHOD:-auto}, порог: ${FAIL_THRESHOLD}×${CHECK_INTERVAL}s)"

# ── Функция DHCP-fallback ────────────────────────────────────────────────────
do_dhcp_fallback() {
    log "Шлюз потерян — останавливаем GSG и переходим на DHCP..."

    # Останавливаем контейнеры (наш DHCP не должен конфликтовать с новым роутером)
    cd "$INSTALL_DIR" && docker compose stop 2>/dev/null || true

    # Переходим на DHCP в зависимости от network manager
    if [ "$NM_METHOD" = "netplan" ] || command -v netplan &>/dev/null; then
        log "Переключаемся на DHCP через netplan..."
        # Удаляем наш статический конфиг, восстанавливаем DHCP
        rm -f /etc/netplan/01-gsg-lan.yaml
        # Определяем renderer из оставшихся конфигов
        _RENDERER=$(grep -rh 'renderer:' /etc/netplan/ 2>/dev/null | head -1 | awk '{print $2}')
        [ -z "$_RENDERER" ] && _RENDERER="networkd"
        cat > /etc/netplan/90-dhcp-restore.yaml << EOF
network:
  version: 2
  renderer: ${_RENDERER}
  ethernets:
    ${IFACE}:
      dhcp4: true
EOF
        chmod 600 /etc/netplan/90-dhcp-restore.yaml
        netplan apply 2>/dev/null || true

    elif [ "$NM_METHOD" = "networkmanager" ] && command -v nmcli &>/dev/null; then
        log "Переключаемся на DHCP через nmcli..."
        CONN=$(nmcli -t -f NAME,DEVICE con show --active 2>/dev/null | grep ":${IFACE}$" | cut -d: -f1 | head -1)
        if [ -n "$CONN" ]; then
            nmcli connection modify "$CONN" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns "" 2>/dev/null || true
            nmcli connection up "$CONN" 2>/dev/null || true
        else
            ip addr flush dev "$IFACE" 2>/dev/null || true
            dhclient -v "$IFACE" 2>/dev/null || dhcpcd -k "$IFACE" 2>/dev/null || true
        fi

    elif [ "$NM_METHOD" = "dhcpcd" ] || (systemctl is-active --quiet dhcpcd 2>/dev/null && [ -f /etc/dhcpcd.conf ]); then
        log "Переключаемся на DHCP через dhcpcd..."
        sed -i "/^# GSG static IP/,/^$/d" /etc/dhcpcd.conf 2>/dev/null || true
        sed -i "/^interface ${IFACE}/,/^$/d" /etc/dhcpcd.conf 2>/dev/null || true
        systemctl restart dhcpcd 2>/dev/null || true

    elif command -v dhclient &>/dev/null; then
        log "Переключаемся на DHCP через dhclient..."
        ip addr flush dev "$IFACE" 2>/dev/null || true
        dhclient -v "$IFACE" 2>/dev/null || true

    elif command -v udhcpc &>/dev/null; then
        ip addr flush dev "$IFACE" 2>/dev/null || true
        udhcpc -i "$IFACE" -q 2>/dev/null || true
    fi

    # Ждём получения IP
    sleep 5
    NEW_IP=$(ip -4 addr show "$IFACE" | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)
    log "Новый IP через DHCP: ${NEW_IP:-не получен}"

    # Убираем наш network.conf чтобы не зациклиться при перезапуске watchdog
    mv "$GSG_NET_CONF" "${GSG_NET_CONF}.bak" 2>/dev/null || true

    # Записываем подсказку для восстановления
    cat > /tmp/gsg-recovery.txt << EOF
GSG перемещён в новую сеть.
Новый IP (DHCP): ${NEW_IP:-не получен}
Для переконфигурации: ssh root@${NEW_IP:-<new-ip>}
                      bash $INSTALL_DIR/install.sh
EOF

    log "Готово. Подключайтесь к ${NEW_IP:-новому IP} и запустите install.sh"
    exit 0
}

# ── Основной цикл мониторинга ────────────────────────────────────────────────
fail=0
while true; do
    if ping -c 2 -W 3 -I "$IFACE" "$GW" >/dev/null 2>&1; then
        fail=0
    else
        fail=$((fail + 1))
        log "Шлюз $GW недоступен ($fail/$FAIL_THRESHOLD)"
        if [ "$fail" -ge "$FAIL_THRESHOLD" ]; then
            do_dhcp_fallback
        fi
    fi
    sleep $CHECK_INTERVAL
done
