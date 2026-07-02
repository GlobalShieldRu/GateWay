#!/bin/bash
set -e

# ─────────────────────────────────────────────
#  GlobalShield Gateway — Установка
# ─────────────────────────────────────────────

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[GSG]${NC} $1"; }
success() { echo -e "${GREEN}[✓]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }
error()   { echo -e "${RED}[✗]${NC} $1"; exit 1; }

REPO_URL="https://github.com/GlobalShieldRu/GateWay.git"
INSTALL_DIR="/root/GSG"

echo ""
echo -e "${CYAN}${BOLD}  GlobalShield Gateway — Установщик${NC}"
echo -e "  ─────────────────────────────────────"
echo ""

# Проверка root
[ "$(id -u)" -ne 0 ] && error "Запустите скрипт от root: sudo bash install.sh"

# ── Зависимости ───────────────────────────────
info "Проверка зависимостей..."
MISSING=()
for cmd in git curl python3 inotifywait; do
    command -v "$cmd" &>/dev/null || { [[ "$cmd" == "inotifywait" ]] && MISSING+=("inotify-tools") || MISSING+=("$cmd"); }
done
if [ ${#MISSING[@]} -gt 0 ]; then
    info "Устанавливаем: ${MISSING[*]}"
    apt-get update -qq && apt-get install -y -qq "${MISSING[@]}"
fi

if ! command -v dockerd &>/dev/null; then
    info "Устанавливаем Docker CE..."
    # Удаляем конфликтующий пакет wmdocker (в Debian пакет 'docker' = оконный менеджер, не Docker CE)
    apt-get remove -y docker wmdocker 2>/dev/null || true
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
fi

# Настройки Docker daemon: DNS + storage driver.
#
# Storage driver: на устройствах с overlay-rootfs (FriendlyElec/Armbian/Buildroot
# с встроенным overlay для read-only базового образа, типа NanoPi Zero 2 с Debian
# trixie) Docker 29+ по умолчанию использует containerd-snapshotter `overlayfs`,
# который требует mount overlay-on-overlay. На таких системах это даёт ошибку
# при сборке: `failed to mount overlay: invalid argument`. Классический `overlay2`
# тоже не работает по той же причине. Единственный надёжный вариант — `vfs`:
# медленнее по диску (нет дедупликации слоёв), но работает на любой ФС.
# Детект: если `/` смонтирован как overlay → forced vfs.
ROOT_FSTYPE=$(df -T / 2>/dev/null | awk 'NR==2 {print $2}')
NEED_VFS=0
if [ "$ROOT_FSTYPE" = "overlay" ]; then
    NEED_VFS=1
    info "Обнаружена overlay-rootfs (Armbian/FriendlyElec-style). Docker будет использовать storage-driver=vfs"
fi

# Регенерируем daemon.json если: его нет / нет dns / нужен vfs но текущий не vfs
NEED_RESTART_DOCKER=0
if [ ! -f /etc/docker/daemon.json ] || ! grep -q '"dns"' /etc/docker/daemon.json 2>/dev/null; then
    NEED_RESTART_DOCKER=1
fi
if [ "$NEED_VFS" = "1" ] && ! grep -q '"storage-driver": "vfs"' /etc/docker/daemon.json 2>/dev/null; then
    NEED_RESTART_DOCKER=1
fi

if [ "$NEED_RESTART_DOCKER" = "1" ]; then
    info "Настройка Docker (DNS${NEED_VFS:+ + storage-driver=vfs})..."
    mkdir -p /etc/docker
    if [ "$NEED_VFS" = "1" ]; then
        cat > /etc/docker/daemon.json << 'DOCKEREOF'
{
  "dns": ["8.8.8.8", "1.1.1.1"],
  "storage-driver": "vfs",
  "features": {
    "containerd-snapshotter": false
  },
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  }
}
DOCKEREOF
    else
        cat > /etc/docker/daemon.json << 'DOCKEREOF'
{
  "dns": ["8.8.8.8", "1.1.1.1"],
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "3"
  }
}
DOCKEREOF
    fi
    systemctl restart docker
    sleep 3
    if systemctl is-active --quiet docker; then
        success "Docker настроен (storage=$(docker info 2>/dev/null | grep 'Storage Driver' | awk '{print $3}'))"
    else
        echo "ОШИБКА: Docker не стартует после применения daemon.json. Логи:" >&2
        journalctl -u docker.service --no-pager -n 20 >&2
        exit 1
    fi
fi

if ! docker compose version &>/dev/null 2>&1; then
    info "Устанавливаем docker-compose-plugin..."
    apt-get install -y -qq docker-compose-plugin 2>/dev/null || \
    { _OS=$(uname -s | tr '[:upper:]' '[:lower:]')
      _ARCH=$(uname -m)
      case "$_ARCH" in
          armv7l|armv7) _ARCH="armv7" ;;
          armv6l)        _ARCH="armv6" ;;
          aarch64|arm64) _ARCH="aarch64" ;;
          x86_64)        _ARCH="x86_64" ;;
      esac
      mkdir -p /usr/local/lib/docker/cli-plugins
      curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-${_OS}-${_ARCH}" \
          -o /usr/local/lib/docker/cli-plugins/docker-compose
      chmod +x /usr/local/lib/docker/cli-plugins/docker-compose; }
fi
success "Зависимости установлены"

# ── Клонирование / обновление ─────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Обновление существующей установки..."
    git -C "$INSTALL_DIR" fetch origin
    git -C "$INSTALL_DIR" reset --hard origin/main
else
    info "Клонирование репозитория..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# ── Автодетект сети ───────────────────────────
echo ""
echo -e "${CYAN}  Определение сети${NC}"
echo "  ─────────────────────────────────────"
echo ""

# Интерфейс с дефолтным маршрутом (WAN/LAN на одноплатнике)
DETECTED_IFACE=$(ip route show default 2>/dev/null | awk '/default/{print $5}' | head -1)
[ -z "$DETECTED_IFACE" ] && DETECTED_IFACE="eth0"

# Текущий IP (полученный от DHCP роутера)
CURRENT_IP=$(ip -4 addr show "$DETECTED_IFACE" 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)

# IP роутера провайдера (дефолтный шлюз)
UPSTREAM_GW=$(ip route show default 2>/dev/null | awk '/default/{print $3}' | head -1)

# Предлагаем красивый IP рядом с роутером (вне DHCP пула роутера)
if [ -n "$UPSTREAM_GW" ]; then
    SUBNET=$(echo "$UPSTREAM_GW" | cut -d. -f1-3)
    ROUTER_LAST=$(echo "$UPSTREAM_GW" | cut -d. -f4)
    if [ "$ROUTER_LAST" -le 10 ] 2>/dev/null; then
        # Роутер .1–.10 → предлагаем с конца (.254, .253...)
        SUGGESTED_IP="${SUBNET}.254"
    elif [ "$ROUTER_LAST" -ge 245 ] 2>/dev/null; then
        # Роутер .245–.254 → предлагаем с начала (.2)
        SUGGESTED_IP="${SUBNET}.2"
    else
        SUGGESTED_IP="${SUBNET}.2"
    fi
else
    SUBNET="192.168.1"
    UPSTREAM_GW="192.168.1.1"
    SUGGESTED_IP="192.168.1.254"
fi

echo -e "  Интерфейс:          ${CYAN}${DETECTED_IFACE}${NC}"
echo -e "  Текущий IP (DHCP):  ${CYAN}${CURRENT_IP:-не определён}${NC}"
echo -e "  Роутер:             ${CYAN}${UPSTREAM_GW}${NC}"
echo ""
echo -e "  ${BOLD}Рекомендуемый статический IP для GSG:${NC} ${GREEN}${SUGGESTED_IP}${NC}"
echo -e "  (Выбирается рядом с роутером, вне DHCP пула)"
echo ""
# Все `read` читаем из /dev/tty, а не из stdin — иначе при `curl | sudo bash`
# stdin занят потоком скрипта, и read съедает следующие строки install.sh
# как «пользовательский ввод». Если tty недоступен (например запуск из cron) —
# используем дефолты и не спрашиваем.
if [ -r /dev/tty ]; then
    read -rp "  IP для GSG [${SUGGESTED_IP}]: " GATEWAY_IP </dev/tty
else
    GATEWAY_IP=""
    info "TTY недоступен — использую значения по умолчанию"
fi
GATEWAY_IP="${GATEWAY_IP:-${SUGGESTED_IP}}"

if [ -r /dev/tty ]; then
    read -rp "  LAN-интерфейс [${DETECTED_IFACE}]: " LAN_IFACE </dev/tty
else
    LAN_IFACE=""
fi
LAN_IFACE="${LAN_IFACE:-${DETECTED_IFACE}}"

SUBNET_PREFIX=$(echo "$GATEWAY_IP" | cut -d. -f1-3)
DEFAULT_START="${SUBNET_PREFIX}.100"
DEFAULT_END="${SUBNET_PREFIX}.200"

echo ""
echo -e "  ${BOLD}Режим DHCP${NC}"
echo -e "    • ${GREEN}Включён${NC}  — GSG раздаёт IP клиентам (на роутере DHCP отключить)"
echo -e "    • ${YELLOW}Выключён${NC} — GSG работает только как шлюз, DHCP остаётся на роутере"
echo ""
if [ -r /dev/tty ]; then
    read -rp "  Включить DHCP-сервер на GSG? [Y/n]: " DHCP_ANSWER </dev/tty
else
    DHCP_ANSWER=""
fi
DHCP_ANSWER="${DHCP_ANSWER:-y}"
case "$DHCP_ANSWER" in
    [Nn]*) DHCP_ENABLED="false" ;;
    *)     DHCP_ENABLED="true"  ;;
esac

if [ "$DHCP_ENABLED" = "true" ]; then
    if [ -r /dev/tty ]; then
        read -rp "  DHCP пул — начало [${DEFAULT_START}]: " DHCP_START </dev/tty
        read -rp "  DHCP пул — конец  [${DEFAULT_END}]: "   DHCP_END </dev/tty
    else
        DHCP_START=""
        DHCP_END=""
    fi
    DHCP_START="${DHCP_START:-$DEFAULT_START}"
    DHCP_END="${DHCP_END:-$DEFAULT_END}"
else
    # Значения нужны в .env / docker-compose для шаблона dnsmasq, но контейнер
    # registry-dhcp в gateway-only режиме не запустит dnsmasq — он сторожит
    # settings.json:dhcp_enabled. Включить DHCP можно позже из веб-интерфейса.
    DHCP_START="$DEFAULT_START"
    DHCP_END="$DEFAULT_END"
    info "Режим: только шлюз (DHCP остаётся на роутере)"
    info "Включить DHCP позже: веб-интерфейс → «Настройки DHCP»"
fi

echo ""

# ── Системные настройки ───────────────────────
info "Настройка параметров ядра..."

# Применяет sysctl только если параметр существует в текущем ядре
sysctl_set() {
    local key="$1" val="$2"
    if sysctl -n "$key" &>/dev/null 2>&1; then
        echo "${key} = ${val}" >> /etc/sysctl.d/99-gsg.conf
    else
        warn "sysctl ${key} не поддерживается этим ядром — пропущено"
    fi
}

# Пересоздаём файл
cat > /etc/sysctl.d/99-gsg.conf << 'EOF'
# GSG Smart Gateway — параметры ядра
# Сгенерировано install.sh, параметры проверяются на совместимость с текущим ядром

# Routing (обязателен)
net.ipv4.ip_forward = 1
EOF

# BBR: пробуем загрузить модуль, если есть — включаем
if modprobe tcp_bbr 2>/dev/null; then
    echo 'tcp_bbr' > /etc/modules-load.d/gsg-bbr.conf
    sysctl_set net.core.default_qdisc fq
    sysctl_set net.ipv4.tcp_congestion_control bbr
    success "BBR congestion control включён"
else
    warn "tcp_bbr модуль недоступен (ядро без BBR) — используется cubic"
fi

# Буферы сокетов
sysctl_set net.core.rmem_max 16777216
sysctl_set net.core.wmem_max 16777216
sysctl_set net.ipv4.tcp_rmem "4096 87380 16777216"
sysctl_set net.ipv4.tcp_wmem "4096 16384 16777216"
sysctl_set net.core.netdev_max_backlog 10000
sysctl_set net.core.somaxconn 4096

# TCP tuning (критично для роутера/TPROXY под нагрузкой)
sysctl_set net.ipv4.tcp_max_syn_backlog 4096     # дефолт 128 — SYN теряются при burst
sysctl_set net.ipv4.tcp_max_tw_buckets 65536      # дефолт 4096 — TIME_WAIT flood
sysctl_set net.ipv4.tcp_mtu_probing 1             # PMTU discovery при потерях
sysctl_set net.ipv4.tcp_slow_start_after_idle 0   # не стартовать заново для idle SSE/WS
sysctl_set net.ipv4.tcp_retries2 8                # быстрее отваливаться от мёртвых
sysctl_set net.ipv4.tcp_fin_timeout 15            # экономия портов
sysctl_set net.ipv4.tcp_keepalive_time 120        # дефолт 7200 — idle соединения мрут
sysctl_set net.ipv4.tcp_keepalive_intvl 15
sysctl_set net.ipv4.tcp_keepalive_probes 3

# Порты и TIME_WAIT
sysctl_set net.ipv4.ip_local_port_range "1024 65535"
sysctl_set net.ipv4.tcp_tw_reuse 1

# Conntrack (требует модуль nf_conntrack)
if modprobe nf_conntrack 2>/dev/null || sysctl -n net.netfilter.nf_conntrack_max &>/dev/null; then
    sysctl_set net.netfilter.nf_conntrack_max 131072
    sysctl_set net.netfilter.nf_conntrack_tcp_timeout_established 7200
    sysctl_set net.netfilter.nf_conntrack_tcp_timeout_time_wait 30
    sysctl_set net.netfilter.nf_conntrack_tcp_timeout_close_wait 30
    sysctl_set net.netfilter.nf_conntrack_tcp_timeout_fin_wait 30
    sysctl_set net.netfilter.nf_conntrack_udp_timeout 180          # дефолт 30 — TikTok/QUIC паузы
    sysctl_set net.netfilter.nf_conntrack_udp_timeout_stream 600   # дефолт 120 — voice звонки
    sysctl_set net.netfilter.nf_conntrack_generic_timeout 300      # быстрее освобождаем entry
    # Hash buckets — должно быть ≈ conntrack_max/4 для минимума collisions
    echo 32768 > /sys/module/nf_conntrack/parameters/hashsize 2>/dev/null || true
    success "Conntrack настроен"
else
    warn "nf_conntrack недоступен — пропущено"
fi

# Flash/eMMC (актуально для всех SBC)
sysctl_set vm.swappiness 10
sysctl_set vm.dirty_ratio 10
sysctl_set vm.dirty_background_ratio 5
sysctl_set vm.dirty_expire_centisecs 1500
sysctl_set vm.dirty_writeback_centisecs 500

# Стабильность: авто-перезагрузка при панике ядра
sysctl_set kernel.panic 10
sysctl_set kernel.panic_on_oops 1

sysctl -p /etc/sysctl.d/99-gsg.conf -q 2>/dev/null || true
success "Параметры ядра настроены ($(grep -c '=' /etc/sysctl.d/99-gsg.conf) параметров)"

# Hardware watchdog
if [ -e /dev/watchdog ]; then
    info "Настройка hardware watchdog..."
    grep -q "^RuntimeWatchdogSec=" /etc/systemd/system.conf 2>/dev/null || {
        sed -i 's/#RuntimeWatchdogSec=0/RuntimeWatchdogSec=15/' /etc/systemd/system.conf 2>/dev/null || \
        echo "RuntimeWatchdogSec=15" >> /etc/systemd/system.conf
        sed -i 's/#WatchdogDevice=/WatchdogDevice=\/dev\/watchdog/' /etc/systemd/system.conf 2>/dev/null || \
        echo "WatchdogDevice=/dev/watchdog" >> /etc/systemd/system.conf
        systemctl daemon-reexec 2>/dev/null || true
    }
    success "Watchdog настроен (15 сек)"
fi

# ── Авто-очистка соединений Mihomo (каждые 2 часа) ────────
info "Установка авто-очистки соединений Mihomo..."
cp "${INSTALL_DIR}/mihomo-cleanup.sh" /usr/local/bin/mihomo-cleanup
chmod +x /usr/local/bin/mihomo-cleanup
# Добавляем в crontab root если ещё нет
if ! crontab -l 2>/dev/null | grep -q 'mihomo-cleanup'; then
    (crontab -l 2>/dev/null; echo "0 */2 * * * /usr/local/bin/mihomo-cleanup") | crontab -
fi
success "Авто-очистка Mihomo: каждые 2 часа (порог 1000 соединений)"

# ── Авто-очистка Docker (еженедельно) ─────────
info "Настройка автоочистки Docker..."
cat > /etc/cron.weekly/gsg-docker-prune << 'EOF'
#!/bin/bash
# Удаляем только старые образы GSG (накапливаются при обновлениях)
# Чужие образы не трогаем
LOG=/var/log/gsg-prune.log
echo "$(date): GSG prune started" >> "$LOG"
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
    | grep '^gsg-' \
    | grep -v ':latest' \
    | awk '{print $2}' \
    | xargs -r docker rmi >> "$LOG" 2>&1
# Мёртвые контейнеры GSG старше 24ч
docker ps -a --filter status=exited --filter status=created \
    --format '{{.Names}} {{.ID}}' \
    | grep '^gsg-' \
    | awk '{print $2}' \
    | xargs -r docker rm >> "$LOG" 2>&1
echo "$(date): GSG prune done" >> "$LOG"
EOF
chmod +x /etc/cron.weekly/gsg-docker-prune
success "Автоочистка Docker: еженедельно"

# ── Update Watcher (systemd сервис, inotifywait) ─────
info "Настройка Update Watcher..."
chmod +x "${INSTALL_DIR}/update-watcher.sh" "${INSTALL_DIR}"/*.sh 2>/dev/null || true
# Убираем старый cron если был
crontab -l 2>/dev/null | grep -v 'update-watcher' | crontab - 2>/dev/null || true
# Ставим systemd-сервис
cp "${INSTALL_DIR}/gsg-updater.service" /etc/systemd/system/gsg-updater.service
systemctl daemon-reload
systemctl enable gsg-updater
systemctl restart gsg-updater
# git safe directory
git config --global --add safe.directory "${INSTALL_DIR}" 2>/dev/null || true
success "Update Watcher: systemd (gsg-updater.service)"

# ── Network Watcher (systemd сервис, применяет UI-перенастройку сети) ─
info "Настройка Network Watcher..."
cp "${INSTALL_DIR}/gsg-network-watcher.service" /etc/systemd/system/gsg-network-watcher.service
systemctl daemon-reload
systemctl enable gsg-network-watcher
systemctl restart gsg-network-watcher
success "Network Watcher: systemd (gsg-network-watcher.service)"

# NOTE: gsg-watchdog (host-level) временно отключён — требует отладки логики
# check_group для fallback-групп с lazy: true. Будет включён в следующем релизе.

# Сетевая конфигурация хоста (IP/iface/gateway) применяется через netplan
# на стадии "Network setup" ниже — оттуда же контейнеры её читают через
# `ip route`/`ip addr`. Env vars и .env удалены — дубликат-источников нет.
info "Конфигурация сети будет применена после успешного запуска контейнеров"

# ── Autostart Docker при загрузке ─────────────
info "Настройка автозапуска GSG при загрузке..."
cat > /etc/systemd/system/gsg.service << EOF
[Unit]
Description=GlobalShield Gateway
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable gsg.service
success "Автозапуск включён (systemd: gsg.service)"

# ── Network Watchdog ───────────────────────────────────────────────────────────
info "Установка сетевого watchdog..."
cp "${INSTALL_DIR}/gsg-netwatch.sh" /usr/local/bin/gsg-netwatch
chmod +x /usr/local/bin/gsg-netwatch

# Сохраняем параметры сети в GSG-конфиг (независимо от network manager)
# Watchdog читает отсюда — не из ifupdown/netplan напрямую
mkdir -p /etc/gsg
if command -v netplan &>/dev/null; then
    _NM_METHOD="netplan"
elif systemctl is-active --quiet NetworkManager 2>/dev/null && command -v nmcli &>/dev/null; then
    _NM_METHOD="networkmanager"
elif systemctl is-active --quiet dhcpcd 2>/dev/null && [ -f /etc/dhcpcd.conf ]; then
    _NM_METHOD="dhcpcd"
else
    _NM_METHOD="ifupdown"
fi
cat > /etc/gsg/network.conf << EOF
IFACE=${LAN_IFACE}
GW=${UPSTREAM_GW}
STATIC_IP=${GATEWAY_IP}
NM_METHOD=${_NM_METHOD}
EOF
success "Сохранены сетевые параметры: /etc/gsg/network.conf"

# Восстанавливаем network.conf если watchdog делал fallback ранее
[ -f "/etc/gsg/network.conf.bak" ] && mv "/etc/gsg/network.conf.bak" "/etc/gsg/network.conf" 2>/dev/null || true

cat > /etc/systemd/system/gsg-netwatch.service << EOF
[Unit]
Description=GSG Network Watchdog (gateway loss → DHCP fallback)
After=network-online.target gsg.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/gsg-netwatch
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable gsg-netwatch.service
systemctl restart gsg-netwatch.service 2>/dev/null || systemctl start gsg-netwatch.service
success "Network watchdog включён (systemd: gsg-netwatch.service)"

# ── GSG Warmer — прогрев src-port для anti-DDoS upstream ────────────────────────
info "Установка GSG Warmer (cold-start lag mitigation)..."
cp "${INSTALL_DIR}/gsg-warmer.sh" /usr/local/bin/gsg-warmer
chmod +x /usr/local/bin/gsg-warmer
cp "${INSTALL_DIR}/gsg-warmer.service" /etc/systemd/system/gsg-warmer.service
cp "${INSTALL_DIR}/gsg-warmer.timer"   /etc/systemd/system/gsg-warmer.timer
systemctl daemon-reload
systemctl enable gsg-warmer.timer
systemctl restart gsg-warmer.timer 2>/dev/null || systemctl start gsg-warmer.timer
success "GSG Warmer запущен (systemd: gsg-warmer.timer, каждые 4 минуты)"

# ── Выбор зеркала PyPI ────────────────────────
echo ""
info "Выбор зеркала PyPI..."
PIP_BUILD_ARGS=""
PYPI_MIRRORS=(
    "https://pypi.org/simple/"
    "https://repo.huaweicloud.com/repository/pypi/simple/"
    "https://pypi.tuna.tsinghua.edu.cn/simple/"
    "https://mirrors.aliyun.com/pypi/simple/"
)
SELECTED_MIRROR=""
for mirror in "${PYPI_MIRRORS[@]}"; do
    if curl -sf --max-time 5 "${mirror}httpx/" > /dev/null 2>&1; then
        SELECTED_MIRROR="$mirror"
        break
    fi
done
if [ -z "$SELECTED_MIRROR" ]; then
    error "Ни одно зеркало PyPI недоступно. Проверьте подключение к интернету."
fi
if [ "$SELECTED_MIRROR" != "https://pypi.org/simple/" ]; then
    warn "pypi.org недоступен — используем зеркало: ${SELECTED_MIRROR}"
    PIP_BUILD_ARGS="--build-arg PIP_INDEX_URL=${SELECTED_MIRROR}"
else
    success "PyPI доступен напрямую"
fi

# ── Сборка и запуск ───────────────────────────
echo ""
info "Сборка Docker образов (может занять несколько минут)..."
docker compose build $PIP_BUILD_ARGS

info "Запуск контейнеров..."
docker compose up -d

# ── Ждём что все 4 контейнера в состоянии "Up" ──
# Кейс: web-orchestrator при cold-boot может ловить "Network is unreachable"
# в _detect_lan_ip если NetworkManager ещё не поднял eth0. restart:always
# ретраит, но после нескольких неудач docker переводит в backoff и бросает.
# Проверяем явно: ждём до 60с чтобы все 4 контейнера были Up. Если нет —
# перезапускаем; тогда сеть уже готова и стартует нормально.
info "Проверка что все контейнеры запустились..."
wait_containers_up() {
    local max_wait=60 elapsed=0 all_up=0
    while [ "$elapsed" -lt "$max_wait" ]; do
        all_up=$(docker compose ps --format json 2>/dev/null | \
                 python3 -c "import json,sys; c=[json.loads(l) for l in sys.stdin if l.strip()]; print(sum(1 for x in c if x.get('State')=='running'))" 2>/dev/null || echo 0)
        [ "$all_up" = "4" ] && return 0
        sleep 3; elapsed=$((elapsed+3))
    done
    return 1
}
if ! wait_containers_up; then
    warn "Не все 4 контейнера поднялись за 60с — пересоздаю (сеть уже готова)"
    docker compose up -d --force-recreate
    if ! wait_containers_up; then
        error "Контейнеры не поднимаются после 2 попыток. Логи:"
        docker compose logs --tail 20
    fi
fi

# ── DHCP-пул в settings.json (единственное GSG-решение — что раздавать клиентам) ──
# IP/iface/gateway/DNS контейнеры читают из ОС (`ip route`, `ip addr`) — не дублируем.
info "Сохраняю DHCP-пул в settings.json..."
docker exec gsg-web-orchestrator python3 -c "
import json, os
p = '/etc/gsg/settings.json'
try:
    d = json.load(open(p))
except Exception:
    d = {}
d['dhcp_start'] = '${DHCP_START}'
d['dhcp_end']   = '${DHCP_END}'
json.dump(d, open(p, 'w'), indent=2)
print('settings.json updated')
" 2>/dev/null || warn "settings.json не обновлён (DHCP-пул возьмётся из подсети по умолчанию)"

# ── Регистрация устройства в GlobalShield ─────
echo ""
info "Регистрация устройства в GlobalShield..."

HOSTNAME_VAL=$(hostname 2>/dev/null || echo "gsg-device")

# Проверяем, есть ли уже device.json в volume
EXISTING_ID=$(docker exec gsg-tunnel python3 -c "
import json
try:
    d = json.load(open('/etc/gsg/device.json'))
    print(d.get('device_id',''))
except:
    print('')
" 2>/dev/null || echo "")

if [ -n "$EXISTING_ID" ]; then
    info "Устройство уже зарегистрировано: ${EXISTING_ID:0:8}..."
else
    DEVICE_ID=$(python3 -c "import uuid; print(str(uuid.uuid4()))")
    info "Регистрация нового устройства: ${DEVICE_ID:0:8}..."

    REG_RESPONSE=$(curl -sf -X POST "https://api.globalshield.ru/v1/devices/register" \
        -H "Content-Type: application/json" \
        -d "{\"device_id\": \"${DEVICE_ID}\", \"hostname\": \"${HOSTNAME_VAL}\", \"gw_ip\": \"${GATEWAY_IP}\"}" \
        2>/dev/null || echo "")

    DEVICE_TOKEN=$(python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('device_token', ''))
except:
    print('')
" <<< "$REG_RESPONSE" 2>/dev/null || echo "")

    REG_DATE=$(date -Iseconds 2>/dev/null || date)

    # Пишем device.json прямо в Docker volume через gsg-tunnel
    docker exec gsg-tunnel python3 -c "
import json
data = {
    'device_id': '${DEVICE_ID}',
    'device_token': '${DEVICE_TOKEN}',
    'registered_at': '${REG_DATE}'
}
with open('/etc/gsg/device.json', 'w') as f:
    json.dump(data, f)
print('ok')
"
    if [ -n "$DEVICE_TOKEN" ]; then
        success "Устройство зарегистрировано и активировано"
    else
        warn "Сервер регистрации недоступен — device_id сохранён, токен будет получен позже"
        warn "Перейдите в веб-интерфейс и сохраните URL подписки для активации"
    fi
fi

# ── Генерация пароля для веб-интерфейса ───────
echo ""
info "Настройка пароля веб-интерфейса..."

# Генерируем пароль только если auth.json ещё не существует
EXISTING_AUTH=$(docker exec gsg-web-orchestrator python3 -c "
import json, os
try:
    d = json.load(open('/etc/gsg/auth.json'))
    print('exists' if d.get('hash') else '')
except:
    print('')
" 2>/dev/null || echo "")

if [ -z "$EXISTING_AUTH" ]; then
    GSG_PASSWORD=$(python3 -c "import secrets, string; print(''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(12)))")
    docker exec gsg-web-orchestrator python3 -c "
import json, hashlib, secrets
salt = secrets.token_hex(16)
pw   = '${GSG_PASSWORD}'
h    = hashlib.sha256((salt + pw).encode()).hexdigest()
with open('/etc/gsg/auth.json', 'w') as f:
    json.dump({'salt': salt, 'hash': h}, f)
print('ok')
"
else
    info "Пароль уже задан — пропускаем генерацию"
    GSG_PASSWORD=""
fi

# ── settings.json: режим DHCP, авто-обновления ────────────────────────────────
# Сливаемся с уже существующим settings.json (если был от прошлого запуска),
# чтобы не потерять пользовательские поля.
docker exec gsg-web-orchestrator python3 -c "
import json, os
path = '/etc/gsg/settings.json'
data = {}
if os.path.exists(path):
    try: data = json.load(open(path))
    except: data = {}
data['dhcp_enabled'] = ${DHCP_ENABLED}
data.setdefault('auto_update', True)
with open(path, 'w') as f:
    json.dump(data, f)
print('ok')
" >/dev/null 2>&1 || warn "Не удалось записать settings.json (запишется при первом обращении к UI)"

echo ""
success "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
success "  GSG установлен и запущен!"
echo ""
echo -e "  Веб-интерфейс:  ${CYAN}http://${GATEWAY_IP}:8080${NC}"
if [ -n "$GSG_PASSWORD" ]; then
echo -e "  Пароль входа:   ${CYAN}${GSG_PASSWORD}${NC}  ← сохраните!"
fi
echo -e "  Роутер:         ${CYAN}${UPSTREAM_GW}${NC}"
if [ "$DHCP_ENABLED" = "true" ]; then
    echo -e "  DHCP пул:       ${CYAN}${DHCP_START} — ${DHCP_END}${NC}"
else
    echo -e "  DHCP:           ${YELLOW}выключен${NC} (только шлюз, DHCP на роутере)"
fi
echo -e "  Статический IP: ${CYAN}${GATEWAY_IP}${NC} (сохранится после перезагрузки)"
echo ""
echo -e "  ${YELLOW}Следующий шаг:${NC} В настройках Wi-Fi роутера укажите шлюз по умолчанию"
echo -e "  для клиентов = ${CYAN}${GATEWAY_IP}${NC}"
echo ""
echo -e "  Для проверки статуса:"
echo -e "  ${YELLOW}docker compose -f ${INSTALL_DIR}/docker-compose.yml ps${NC}"
echo ""

# ── Применяем сетевую конфигурацию в последнюю очередь ───────────────────────
# Только сейчас — когда контейнеры уже запущены — меняем сеть
#
# Поддерживаемые платформы:
#   Netplan (Ubuntu, Armbian): netplan + networkd/NetworkManager
#   NetworkManager без netplan (Raspberry Pi OS Bookworm, некоторые Armbian)
#   dhcpcd (Raspberry Pi OS Bullseye и старше)
#   ifupdown (классический Debian)
#
# DNS: используем шлюз как первый DNS (роутер обычно резолвит) + 8.8.8.8 как fallback.

_apply_ip_immediately() {
    # Немедленно применяет IP через ip-команды (без перезагрузки)
    ip addr flush dev "${LAN_IFACE}" 2>/dev/null || true
    ip addr add "${GATEWAY_IP}/24" dev "${LAN_IFACE}"
    ip link set "${LAN_IFACE}" up
    ip route del default 2>/dev/null || true
    ip route add default via "${UPSTREAM_GW}" 2>/dev/null || true
}

if command -v netplan &>/dev/null; then
    # ── Метод: Netplan (Ubuntu, Armbian) ──────────────────────────────────────
    rm -f /etc/netplan/90-dhcp-restore.yaml

    # Определяем renderer из существующего конфига (NetworkManager или networkd)
    NETPLAN_RENDERER=$(grep -rh 'renderer:' /etc/netplan/ 2>/dev/null | head -1 | awk '{print $2}')
    [ -z "$NETPLAN_RENDERER" ] && NETPLAN_RENDERER="networkd"

    # Удаляем ifupdown-конфиг для этого интерфейса чтобы не было конфликта
    rm -f /etc/network/interfaces.d/gsg-lan.conf
    if [ -f /etc/network/interfaces ]; then
        sed -i "/^auto ${LAN_IFACE}/d" /etc/network/interfaces
        sed -i "/^allow-hotplug ${LAN_IFACE}/d" /etc/network/interfaces
        sed -i "/^iface ${LAN_IFACE} inet/,/^$/d" /etc/network/interfaces
    fi

    # Удаляем wildcard DHCP конфиги которые конфликтуют с нашим статическим IP.
    # Armbian ставит 10-dhcp-all-interfaces.yaml (match: name: "e*") — он генерирует
    # 10-netplan-all-eth-interfaces.network, который алфавитно идёт РАНЬШЕ
    # 10-netplan-${LAN_IFACE}.network → DHCP побеждает над нашим статическим IP.
    for _f in /etc/netplan/*.yaml; do
        [ "$_f" = "/etc/netplan/01-gsg-lan.yaml" ] && continue
        # Удаляем если содержит wildcard DHCP на ethernet-интерфейсы
        if grep -qE 'dhcp4: yes' "$_f" 2>/dev/null && \
           grep -qE 'name: "?(e\*|eth\*|en\*|lan\*|wan\*)' "$_f" 2>/dev/null; then
            rm -f "$_f"
            warn "Удалён конфликтующий DHCP конфиг: $_f"
        fi
    done

    cat > /etc/netplan/01-gsg-lan.yaml << EOF
network:
  version: 2
  renderer: ${NETPLAN_RENDERER}
  ethernets:
    ${LAN_IFACE}:
      addresses: [${GATEWAY_IP}/24]
      dhcp4: false
      routes:
        - to: default
          via: ${UPSTREAM_GW}
      nameservers:
        addresses: [8.8.8.8, 1.1.1.1]
EOF
    # Права 600 обязательны — netplan отказывается читать файлы с open permissions
    chmod 600 /etc/netplan/01-gsg-lan.yaml
    success "Netplan: конфигурация записана (renderer: ${NETPLAN_RENDERER})"

    if [ "${GATEWAY_IP}" != "${CURRENT_IP}" ]; then
        warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        warn "  Сейчас IP сменится: ${CURRENT_IP} → ${GATEWAY_IP}"
        warn "  SSH-сессия прервётся — это нормально."
        warn "  Подключайтесь к новому адресу: ssh root@${GATEWAY_IP}"
        warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        sleep 3
    fi
    # netplan apply генерирует конфиги networkd/NM и активирует их.
    # Если IP меняется — SSH сессия прервётся, это нормально.
    netplan apply 2>/dev/null || _apply_ip_immediately

elif systemctl is-active --quiet NetworkManager 2>/dev/null && command -v nmcli &>/dev/null; then
    # ── Метод: NetworkManager без netplan (Raspberry Pi OS Bookworm) ───────────
    # Удаляем dhcpcd и ifupdown конфиги для этого интерфейса
    sed -i "/^interface ${LAN_IFACE}/,/^$/d" /etc/dhcpcd.conf 2>/dev/null || true
    rm -f /etc/network/interfaces.d/gsg-lan.conf

    # Ищем активное соединение для нашего интерфейса
    _NM_CONN=$(nmcli -t -f NAME,DEVICE con show --active 2>/dev/null | grep ":${LAN_IFACE}$" | cut -d: -f1 | head -1)
    if [ -z "$_NM_CONN" ]; then
        # Нет активного — создаём новое
        _NM_CONN="gsg-static"
        nmcli con add type ethernet ifname "${LAN_IFACE}" con-name "${_NM_CONN}" \
            ipv4.method manual \
            ipv4.addresses "${GATEWAY_IP}/24" \
            ipv4.gateway "${UPSTREAM_GW}" \
            ipv4.dns "8.8.8.8 1.1.1.1" \
            connection.autoconnect yes 2>/dev/null || true
    else
        nmcli con modify "${_NM_CONN}" \
            ipv4.method manual \
            ipv4.addresses "${GATEWAY_IP}/24" \
            ipv4.gateway "${UPSTREAM_GW}" \
            ipv4.dns "8.8.8.8 1.1.1.1" \
            connection.autoconnect yes 2>/dev/null || true
    fi
    success "NetworkManager: соединение '${_NM_CONN}' настроено на статический IP"

    if [ "${GATEWAY_IP}" != "${CURRENT_IP}" ]; then
        warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        warn "  Сейчас IP сменится: ${CURRENT_IP} → ${GATEWAY_IP}"
        warn "  SSH-сессия прервётся — это нормально."
        warn "  Подключайтесь к новому адресу: ssh root@${GATEWAY_IP}"
        warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        sleep 3
    fi
    nmcli con up "${_NM_CONN}" 2>/dev/null || _apply_ip_immediately

elif systemctl is-active --quiet dhcpcd 2>/dev/null && [ -f /etc/dhcpcd.conf ]; then
    # ── Метод: dhcpcd (Raspberry Pi OS Bullseye и старше) ─────────────────────
    # Удаляем предыдущую GSG-конфигурацию для этого интерфейса
    sed -i "/^# GSG static IP/,/^$/d" /etc/dhcpcd.conf 2>/dev/null || true
    sed -i "/^interface ${LAN_IFACE}/,/^$/d" /etc/dhcpcd.conf 2>/dev/null || true
    cat >> /etc/dhcpcd.conf << EOF

# GSG static IP — добавлено install.sh
interface ${LAN_IFACE}
static ip_address=${GATEWAY_IP}/24
static routers=${UPSTREAM_GW}
static domain_name_servers=8.8.8.8 1.1.1.1
EOF
    success "dhcpcd: статический IP добавлен в /etc/dhcpcd.conf"

    if [ "${GATEWAY_IP}" != "${CURRENT_IP}" ]; then
        warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        warn "  Сейчас IP сменится: ${CURRENT_IP} → ${GATEWAY_IP}"
        warn "  SSH-сессия прервётся — это нормально."
        warn "  Подключайтесь к новому адресу: ssh root@${GATEWAY_IP}"
        warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        sleep 3
    fi
    systemctl restart dhcpcd 2>/dev/null || _apply_ip_immediately

elif [ -d /etc/network/interfaces.d ] || [ -f /etc/network/interfaces ]; then
    # ── Метод: ifupdown (классический Debian) ─────────────────────────────────
    mkdir -p /etc/network/interfaces.d
    if [ -f /etc/network/interfaces ]; then
        sed -i "/^auto ${LAN_IFACE}/d" /etc/network/interfaces
        sed -i "/^allow-hotplug ${LAN_IFACE}/d" /etc/network/interfaces
        sed -i "/^iface ${LAN_IFACE} inet/,/^$/d" /etc/network/interfaces
    fi
    cat > /etc/network/interfaces.d/gsg-lan.conf << EOF
auto ${LAN_IFACE}
iface ${LAN_IFACE} inet static
    address ${GATEWAY_IP}/24
    gateway ${UPSTREAM_GW}
    dns-nameservers 8.8.8.8 1.1.1.1
EOF
    success "ifupdown: конфигурация записана в /etc/network/interfaces.d/gsg-lan.conf"

    if [ "${GATEWAY_IP}" != "${CURRENT_IP}" ]; then
        warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        warn "  Сейчас IP сменится: ${CURRENT_IP} → ${GATEWAY_IP}"
        warn "  SSH-сессия прервётся — это нормально."
        warn "  Подключайтесь к новому адресу: ssh root@${GATEWAY_IP}"
        warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        sleep 3
    fi
    _apply_ip_immediately

else
    warn "Неизвестный менеджер сети — применяем через ip-команды"
    if [ "${GATEWAY_IP}" != "${CURRENT_IP}" ]; then
        warn "  IP сменится: ${CURRENT_IP} → ${GATEWAY_IP}"
        warn "  Подключайтесь: ssh root@${GATEWAY_IP}"
        sleep 3
    fi
    _apply_ip_immediately
fi
