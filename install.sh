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
for cmd in git curl python3; do
    command -v "$cmd" &>/dev/null || MISSING+=("$cmd")
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

# Прописываем DNS для Docker-контейнеров (иначе apt внутри контейнеров не резолвит хосты)
if [ ! -f /etc/docker/daemon.json ] || ! grep -q '"dns"' /etc/docker/daemon.json 2>/dev/null; then
    info "Настройка DNS для Docker..."
    mkdir -p /etc/docker
    cat > /etc/docker/daemon.json << 'DOCKEREOF'
{
  "dns": ["8.8.8.8", "1.1.1.1"]
}
DOCKEREOF
    systemctl restart docker
    success "Docker DNS настроен"
fi

if ! docker compose version &>/dev/null 2>&1; then
    info "Устанавливаем docker-compose-plugin..."
    apt-get install -y -qq docker-compose-plugin 2>/dev/null || \
    { mkdir -p /usr/local/lib/docker/cli-plugins
      curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
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
read -rp "  IP для GSG [${SUGGESTED_IP}]: " GATEWAY_IP
GATEWAY_IP="${GATEWAY_IP:-${SUGGESTED_IP}}"

read -rp "  LAN-интерфейс [${DETECTED_IFACE}]: " LAN_IFACE
LAN_IFACE="${LAN_IFACE:-${DETECTED_IFACE}}"

SUBNET_PREFIX=$(echo "$GATEWAY_IP" | cut -d. -f1-3)
DEFAULT_START="${SUBNET_PREFIX}.100"
DEFAULT_END="${SUBNET_PREFIX}.200"

echo ""
read -rp "  DHCP пул — начало [${DEFAULT_START}]: " DHCP_START
DHCP_START="${DHCP_START:-$DEFAULT_START}"
read -rp "  DHCP пул — конец  [${DEFAULT_END}]: " DHCP_END
DHCP_END="${DHCP_END:-$DEFAULT_END}"

echo ""

# ── Системные настройки ───────────────────────
info "Включение IP forwarding..."
echo "net.ipv4.ip_forward = 1" > /etc/sysctl.d/99-gsg.conf
sysctl -p /etc/sysctl.d/99-gsg.conf -q
success "IP forwarding включён"

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

# ── Docker конфиг ─────────────────────────────
info "Запись конфигурации..."
cat > "$INSTALL_DIR/.env" << EOF
GSG_GATEWAY_IP=${GATEWAY_IP}
GSG_LAN_INTERFACE=${LAN_IFACE}
GSG_DHCP_START=${DHCP_START}
GSG_DHCP_END=${DHCP_END}
GSG_TPROXY_PORT=12345
EOF

sed -i "s|GSG_GATEWAY_IP=.*|GSG_GATEWAY_IP=${GATEWAY_IP}|" docker-compose.yml
sed -i "s|GSG_LAN_INTERFACE=.*|GSG_LAN_INTERFACE=${LAN_IFACE}|" docker-compose.yml
sed -i "s|GSG_DHCP_START=.*|GSG_DHCP_START=${DHCP_START}|" docker-compose.yml
sed -i "s|GSG_DHCP_END=.*|GSG_DHCP_END=${DHCP_END}|" docker-compose.yml
sed -i "s|GSG_GATEWAY_IP=.*|GSG_GATEWAY_IP=${GATEWAY_IP}|g" docker-compose.yml

success "Docker конфиг записан"

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

# ── Сборка и запуск ───────────────────────────
echo ""
info "Проверка доступности PyPI..."
PIP_BUILD_ARGS=""
if ! curl -sf --max-time 5 https://pypi.org/simple/ > /dev/null 2>&1; then
    warn "pypi.org недоступен — используем зеркало mirrors.aliyun.com"
    PIP_BUILD_ARGS="--build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/"
fi

info "Сборка Docker образов (может занять несколько минут)..."
docker compose build $PIP_BUILD_ARGS

info "Запуск контейнеров..."
docker compose up -d

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

echo ""
success "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
success "  GSG установлен и запущен!"
echo ""
echo -e "  Веб-интерфейс:  ${CYAN}http://${GATEWAY_IP}:8080${NC}"
if [ -n "$GSG_PASSWORD" ]; then
echo -e "  Пароль входа:   ${CYAN}${GSG_PASSWORD}${NC}  ← сохраните!"
fi
echo -e "  Роутер:         ${CYAN}${UPSTREAM_GW}${NC}"
echo -e "  DHCP пул:       ${CYAN}${DHCP_START} — ${DHCP_END}${NC}"
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

# Метод 1: /etc/network/interfaces.d/ (Debian / Raspberry Pi OS)
if [ -d /etc/network/interfaces.d ] || [ -f /etc/network/interfaces ]; then
    mkdir -p /etc/network/interfaces.d
    if [ -f /etc/network/interfaces ]; then
        sed -i "/^auto ${LAN_IFACE}/d" /etc/network/interfaces
        sed -i "/^allow-hotplug ${LAN_IFACE}/d" /etc/network/interfaces
        sed -i "/^iface ${LAN_IFACE} inet/d" /etc/network/interfaces
    fi
    cat > /etc/network/interfaces.d/gsg-lan.conf << EOF
auto ${LAN_IFACE}
iface ${LAN_IFACE} inet static
    address ${GATEWAY_IP}/24
    gateway ${UPSTREAM_GW}
    dns-nameservers 8.8.8.8 1.1.1.1
EOF
    success "Записано: /etc/network/interfaces.d/gsg-lan.conf"
fi

# Метод 2: Netplan (Ubuntu 20.04+)
if command -v netplan &>/dev/null; then
    cat > /etc/netplan/01-gsg-lan.yaml << EOF
network:
  version: 2
  renderer: networkd
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
    success "Netplan: конфигурация записана"
fi

if [ "${GATEWAY_IP}" != "${CURRENT_IP}" ]; then
    echo ""
    warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    warn "  Сейчас IP сменится: ${CURRENT_IP} → ${GATEWAY_IP}"
    warn "  SSH-сессия прервётся — это нормально."
    warn "  Подключайтесь к новому адресу: ssh root@${GATEWAY_IP}"
    warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    sleep 3
    ip addr flush dev "${LAN_IFACE}" 2>/dev/null || true
    ip addr add "${GATEWAY_IP}/24" dev "${LAN_IFACE}"
    ip link set "${LAN_IFACE}" up
    ip route del default 2>/dev/null || true
    ip route add default via "${UPSTREAM_GW}" 2>/dev/null || true
fi
