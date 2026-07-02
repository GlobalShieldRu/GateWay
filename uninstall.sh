#!/bin/bash
set -e

# ─────────────────────────────────────────────
#  GlobalShield Gateway — Деинсталляция
# ─────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[GSG]${NC} $1"; }
success() { echo -e "${GREEN}[✓]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }
error()   { echo -e "${RED}[✗]${NC} $1"; exit 1; }

INSTALL_DIR="/root/GSG"

[ "$(id -u)" -ne 0 ] && error "Запустите от root: sudo bash uninstall.sh"

echo ""
echo -e "${RED}${BOLD}  GlobalShield Gateway — Деинсталляция${NC}"
echo -e "  ─────────────────────────────────────"
echo ""
warn "Будут удалены: контейнеры, конфигурация, сетевые настройки."
warn "Устройство вернётся на DHCP."
echo ""
# При `curl | sudo bash` stdin занят потоком скрипта — read берётся из /dev/tty.
# Если запуск без tty (cron), считаем что подтверждение подразумевается.
if [ -r /dev/tty ]; then
    read -rp "Продолжить? [y/N] " confirm </dev/tty
else
    confirm="y"
    info "TTY недоступен — продолжаю без подтверждения"
fi
[[ "$confirm" =~ ^[Yy]$ ]] || { info "Отменено."; exit 0; }

# Определяем интерфейс
LAN_IFACE=$(ip route show default 2>/dev/null | awk '/default/{print $5}' | head -1)
[ -z "$LAN_IFACE" ] && LAN_IFACE="eth0"
info "Сетевой интерфейс: $LAN_IFACE"

# ── 1. Остановка контейнеров ─────────────────────
info "Остановка контейнеров..."
if [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
    cd "$INSTALL_DIR"
    docker compose down --remove-orphans 2>/dev/null || true
    docker compose rm -f 2>/dev/null || true
fi
success "Контейнеры остановлены"

# ── 2. Удаление Docker-образов GSG ──────────────
info "Удаление Docker-образов GSG..."
docker images --format '{{.Repository}} {{.ID}}' 2>/dev/null \
    | grep '^gsg-' \
    | awk '{print $2}' \
    | xargs -r docker rmi -f 2>/dev/null || true
docker volume rm gsg_gsg_config gsg_dhcp_data 2>/dev/null || true
success "Образы и volumes удалены"

# ── 3. Systemd-сервисы ──────────────────────────
info "Удаление systemd-сервисов..."
for svc in gsg.service gsg-netwatch.service; do
    systemctl stop "$svc" 2>/dev/null || true
    systemctl disable "$svc" 2>/dev/null || true
    rm -f "/etc/systemd/system/$svc"
done
systemctl daemon-reload
success "Сервисы удалены"

# ── 4. Cron-задачи ──────────────────────────────
info "Удаление cron-задач..."
(crontab -l 2>/dev/null | grep -v 'mihomo-cleanup' | grep -v 'update-watcher') | crontab - 2>/dev/null || true
rm -f /etc/cron.weekly/gsg-docker-prune
rm -f /usr/local/bin/mihomo-cleanup
rm -f /usr/local/bin/gsg-netwatch
success "Cron-задачи удалены"

# ── 5. Восстановление сети → DHCP ───────────────
info "Восстановление сети на DHCP..."

# Netplan: удаляем GSG-конфиг
if [ -f /etc/netplan/01-gsg-lan.yaml ]; then
    rm -f /etc/netplan/01-gsg-lan.yaml
    success "Удалён /etc/netplan/01-gsg-lan.yaml"
fi

# interfaces.d: удаляем GSG-конфиг
if [ -f /etc/network/interfaces.d/gsg-lan.conf ]; then
    rm -f /etc/network/interfaces.d/gsg-lan.conf
    rm -f /etc/network/interfaces.d/gsg-lan.conf.bak
    success "Удалён /etc/network/interfaces.d/gsg-lan.conf"
fi

# Применяем DHCP
if command -v netplan &>/dev/null; then
    # Проверяем есть ли хотя бы один оставшийся netplan-конфиг с интерфейсом
    if ! grep -rq "$LAN_IFACE" /etc/netplan/ 2>/dev/null; then
        cat > /etc/netplan/90-dhcp-restore.yaml << EOF
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    ${LAN_IFACE}:
      dhcp4: true
EOF
        success "Создан /etc/netplan/90-dhcp-restore.yaml (DHCP)"
    fi
fi

# Fallback: interfaces.d
if [ -d /etc/network/interfaces.d ] && ! command -v netplan &>/dev/null; then
    cat > /etc/network/interfaces.d/dhcp-restore.conf << EOF
auto ${LAN_IFACE}
iface ${LAN_IFACE} inet dhcp
EOF
    success "Создан /etc/network/interfaces.d/dhcp-restore.conf (DHCP)"
fi

# ── 6. Удаление каталога GSG ────────────────────
info "Удаление $INSTALL_DIR..."
rm -rf "$INSTALL_DIR"
success "Каталог удалён"

# ── 7. Применяем сетевые изменения ──────────────
echo ""
warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
warn "  Сейчас IP сменится на DHCP."
warn "  SSH-сессия прервётся — это нормально."
warn "  Найдите новый IP в роутере или через: arp -a"
warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
sleep 3

if command -v netplan &>/dev/null; then
    netplan apply 2>/dev/null || true
else
    ip addr flush dev "$LAN_IFACE" 2>/dev/null || true
    ifup "$LAN_IFACE" 2>/dev/null || dhclient "$LAN_IFACE" 2>/dev/null || true
fi

echo ""
echo -e "${GREEN}${BOLD}  GlobalShield Gateway удалён.${NC}"
echo -e "  Устройство переведено на DHCP."
