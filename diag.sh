#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  GSG Smart Gateway — Диагностика
#  Запуск: bash diag.sh
#  Вывод: текстовый отчёт + /tmp/gsg-diag-YYYYMMDD-HHMMSS.txt
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'

TS=$(date +%Y%m%d-%H%M%S)
OUT="/tmp/gsg-diag-${TS}.txt"
WARN_COUNT=0
ERR_COUNT=0

section()  { echo -e "\n${CYAN}${BOLD}═══ $1 ═══${NC}"; echo -e "\n=== $1 ===" >> "$OUT"; }
ok()       { echo -e "  ${GREEN}✓${NC} $1"; echo "  OK  $1" >> "$OUT"; }
warn()     { echo -e "  ${YELLOW}⚠${NC}  $1"; echo "  WARN $1" >> "$OUT"; WARN_COUNT=$((WARN_COUNT+1)); }
err()      { echo -e "  ${RED}✗${NC}  $1"; echo "  ERR  $1" >> "$OUT"; ERR_COUNT=$((ERR_COUNT+1)); }
info()     { echo -e "       $1"; echo "       $1" >> "$OUT"; }
raw()      { echo "$1" | tee -a "$OUT"; }

# Число из файла proc/sys, пустая строка если нет
sysval()   { sysctl -n "$1" 2>/dev/null || echo "н/д"; }

echo -e "${CYAN}${BOLD}"
echo "  GSG Smart Gateway — Диагностика"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo -e "${NC}"
echo "GSG Smart Gateway Diagnostic Report" > "$OUT"
echo "Generated: $(date)" >> "$OUT"
echo "======================================" >> "$OUT"

# ─────────────────────────────────────────────────────────────────────────────
section "Система"

KERNEL=$(uname -r)
ARCH=$(uname -m)
HOSTNAME=$(hostname)
UPTIME=$(uptime -p 2>/dev/null || uptime)
info "Хост:    $HOSTNAME"
info "Ядро:    $KERNEL ($ARCH)"
info "Аптайм:  $UPTIME"

# Железо
CPUMODEL=$(grep -m1 'Model name\|Hardware\|model name' /proc/cpuinfo 2>/dev/null | sed 's/.*: //' | xargs || echo "н/д")
CPUCORES=$(nproc)
info "CPU:     $CPUMODEL ($CPUCORES ядер)"

# Версия GSG
GSG_VER=$(docker exec gsg-web-orchestrator python3 -c "import sys; sys.path.insert(0,'/app'); exec(open('/app/main.py').read().split('GSG_VERSION')[1].split('\n')[0].replace('=','').strip()[:20])" 2>/dev/null \
    || grep -m1 'GSG_VERSION' /root/GSG/web-orchestrator/main.py 2>/dev/null | grep -oP '"[^"]+"' | tr -d '"' \
    || echo "н/д")
info "GSG:     v$GSG_VER"

# ─────────────────────────────────────────────────────────────────────────────
section "Температура и CPU"

# Поддерживается на большинстве SBC
TEMPS=$(find /sys/class/thermal/thermal_zone*/temp 2>/dev/null)
if [ -n "$TEMPS" ]; then
    for f in $TEMPS; do
        ZONE=$(dirname "$f" | xargs basename)
        TYPE=$(cat "$(dirname $f)/type" 2>/dev/null || echo "$ZONE")
        VAL=$(( $(cat "$f") / 1000 ))
        LINE="${TYPE}: ${VAL}°C"
        if   [ "$VAL" -ge 80 ]; then err  "ПЕРЕГРЕВ  $LINE"
        elif [ "$VAL" -ge 65 ]; then warn "Высокая температура: $LINE"
        else ok "$LINE"
        fi
    done
else
    info "Датчики температуры не найдены"
fi

# CPU частота
FREQ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || echo "")
MAX_FREQ=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq 2>/dev/null || echo "")
if [ -n "$FREQ" ] && [ -n "$MAX_FREQ" ]; then
    CUR_MHZ=$(( FREQ / 1000 ))
    MAX_MHZ=$(( MAX_FREQ / 1000 ))
    PCT=$(( CUR_MHZ * 100 / MAX_MHZ ))
    if [ "$PCT" -lt 50 ]; then
        warn "CPU throttling: ${CUR_MHZ}/${MAX_MHZ} МГц (${PCT}%)"
    else
        ok "CPU частота: ${CUR_MHZ}/${MAX_MHZ} МГц"
    fi
fi

# Load average
LOAD=$(cat /proc/loadavg | awk '{print $1, $2, $3}')
LOAD1=$(cat /proc/loadavg | awk '{print $1}' | cut -d. -f1)
if [ "$LOAD1" -ge "$CPUCORES" ]; then
    warn "Высокая нагрузка: $LOAD (ядер: $CPUCORES)"
else
    ok "Load average: $LOAD"
fi

# ─────────────────────────────────────────────────────────────────────────────
section "Память"

MEM_TOTAL=$(awk '/MemTotal/{print $2}' /proc/meminfo)
MEM_AVAIL=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
MEM_USED=$(( MEM_TOTAL - MEM_AVAIL ))
MEM_PCT=$(( MEM_USED * 100 / MEM_TOTAL ))
SWAP_TOTAL=$(awk '/SwapTotal/{print $2}' /proc/meminfo)
SWAP_FREE=$(awk '/SwapFree/{print $2}' /proc/meminfo)
SWAP_USED=$(( SWAP_TOTAL - SWAP_FREE ))

info "RAM:  $(( MEM_USED/1024 )) / $(( MEM_TOTAL/1024 )) МБ (${MEM_PCT}%)"
if [ "$MEM_PCT" -ge 90 ]; then
    err  "Критически мало памяти: ${MEM_PCT}% занято"
elif [ "$MEM_PCT" -ge 75 ]; then
    warn "Памяти мало: ${MEM_PCT}% занято"
else
    ok "Память: ${MEM_PCT}% занято"
fi

if [ "$SWAP_TOTAL" -gt 0 ]; then
    SWAP_PCT=$(( SWAP_USED * 100 / SWAP_TOTAL ))
    info "Swap: $(( SWAP_USED/1024 )) / $(( SWAP_TOTAL/1024 )) МБ (${SWAP_PCT}%)"
    if [ "$SWAP_PCT" -ge 50 ]; then
        warn "Активно используется swap (${SWAP_PCT}%) — возможна нехватка RAM"
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
section "Сеть"

# Основной интерфейс
ETH=$(ip route show default 2>/dev/null | awk '/default/{print $5}' | head -1)
if [ -n "$ETH" ]; then
    RX_ERR=$(cat /sys/class/net/${ETH}/statistics/rx_errors 2>/dev/null || echo 0)
    TX_ERR=$(cat /sys/class/net/${ETH}/statistics/tx_errors 2>/dev/null || echo 0)
    RX_DROP=$(cat /sys/class/net/${ETH}/statistics/rx_dropped 2>/dev/null || echo 0)
    SPEED=$(cat /sys/class/net/${ETH}/speed 2>/dev/null || echo "н/д")
    DUPLEX=$(cat /sys/class/net/${ETH}/duplex 2>/dev/null || echo "н/д")
    CARRIER=$(cat /sys/class/net/${ETH}/carrier 2>/dev/null || echo 0)

    if [ "$CARRIER" = "1" ]; then
        ok "Интерфейс $ETH: ${SPEED}Mbps/${DUPLEX}, carrier OK"
    else
        err "Интерфейс $ETH: нет carrier (кабель?)"
    fi
    [ "$RX_ERR" -gt 100 ]  && warn "${ETH} RX errors: $RX_ERR" || true
    [ "$TX_ERR" -gt 100 ]  && warn "${ETH} TX errors: $TX_ERR" || true
    [ "$RX_DROP" -gt 1000 ] && warn "${ETH} RX dropped: $RX_DROP" || true
    info "Ошибки: RX_err=$RX_ERR TX_err=$TX_ERR RX_drop=$RX_DROP"
else
    warn "Не удалось определить основной интерфейс"
fi

# TCP retransmissions
TCP_RETRANS=$(awk '/TCPRetransFail/{print $2}' /proc/net/netstat 2>/dev/null || echo 0)
TCP_SEG=$(awk '/^Tcp:/{getline; print $11}' /proc/net/snmp 2>/dev/null || echo 0)
info "TCP retransmissions: $TCP_RETRANS"

# ─────────────────────────────────────────────────────────────────────────────
section "Conntrack"

CT_MAX=$(sysval net.netfilter.nf_conntrack_max)
CT_CUR=$(sysval net.netfilter.nf_conntrack_count)

if [ "$CT_MAX" = "н/д" ]; then
    info "nf_conntrack не загружен"
else
    CT_PCT=$(( CT_CUR * 100 / CT_MAX ))
    info "Соединений: $CT_CUR / $CT_MAX (${CT_PCT}%)"
    if   [ "$CT_PCT" -ge 90 ]; then err  "Conntrack почти полон! (${CT_PCT}%) — SSH будет отваливаться"
    elif [ "$CT_PCT" -ge 70 ]; then warn "Conntrack заполнен на ${CT_PCT}%"
    else ok "Conntrack: ${CT_PCT}% (${CT_CUR}/${CT_MAX})"
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
section "Параметры ядра (ключевые)"

check_sysctl() {
    local key="$1" expected="$2" label="$3"
    local val
    val=$(sysval "$key")
    if [ "$val" = "н/д" ]; then
        info "$label: не поддерживается этим ядром"
    elif [ "$val" = "$expected" ]; then
        ok "$label: $val"
    else
        warn "$label: $val (ожидается $expected)"
    fi
}

check_sysctl net.ipv4.tcp_congestion_control bbr        "TCP congestion"
check_sysctl net.core.default_qdisc           fq         "Qdisc"
check_sysctl net.core.rmem_max                16777216   "rmem_max"
check_sysctl net.core.wmem_max                16777216   "wmem_max"
check_sysctl net.netfilter.nf_conntrack_max   131072     "conntrack_max"
check_sysctl vm.swappiness                    10         "swappiness"
check_sysctl kernel.panic                     10         "panic reboot"

# ─────────────────────────────────────────────────────────────────────────────
section "Docker контейнеры"

if command -v docker &>/dev/null; then
    while IFS= read -r line; do
        NAME=$(echo "$line" | awk '{print $1}')
        STATUS=$(echo "$line" | awk '{print $2}')
        if echo "$STATUS" | grep -qi "up"; then
            ok "$NAME: $STATUS"
        else
            err "$NAME: $STATUS"
        fi
    done < <(docker ps -a --format "{{.Names}} {{.Status}}" 2>/dev/null || echo "docker ps failed unknown")
else
    warn "Docker не установлен"
fi

# ─────────────────────────────────────────────────────────────────────────────
section "Хранилище"

df -h --output=target,size,used,avail,pcent 2>/dev/null | grep -v tmpfs | while read -r line; do
    PCT=$(echo "$line" | grep -oP '\d+(?=%)' || echo 0)
    if   [ "${PCT:-0}" -ge 90 ]; then err  "Диск: $line"
    elif [ "${PCT:-0}" -ge 75 ]; then warn "Диск: $line"
    else info "$line"
    fi
done

# eMMC/SD смарт-данные (если доступны)
if command -v mmc &>/dev/null; then
    MMC_LIFE=$(mmc extcsd read /dev/mmcblk0 2>/dev/null | grep -i 'life time\|pre-eol' | head -5 || echo "")
    [ -n "$MMC_LIFE" ] && info "eMMC: $MMC_LIFE"
fi

# ─────────────────────────────────────────────────────────────────────────────
section "Последние ошибки ядра"

DMESG_ERR=$(dmesg -T --level=err,crit,alert,emerg 2>/dev/null | tail -20 || dmesg | grep -E 'error|Error|panic|BUG|OOM' | tail -20)
if [ -z "$DMESG_ERR" ]; then
    ok "Критических ошибок в dmesg нет"
else
    warn "Ошибки в dmesg:"
    echo "$DMESG_ERR" | while read -r line; do info "  $line"; done
fi

# ─────────────────────────────────────────────────────────────────────────────
section "Итог"

if   [ "$ERR_COUNT" -gt 0 ];  then STATUS="${RED}${BOLD}ПРОБЛЕМЫ${NC} (ошибок: $ERR_COUNT, предупреждений: $WARN_COUNT)"
elif [ "$WARN_COUNT" -gt 0 ]; then STATUS="${YELLOW}${BOLD}ПРЕДУПРЕЖДЕНИЯ${NC} ($WARN_COUNT)"
else STATUS="${GREEN}${BOLD}ОК${NC}"
fi

echo -e "  Статус: $STATUS"
echo -e "  Отчёт сохранён: ${CYAN}$OUT${NC}"
echo ""

echo "" >> "$OUT"
echo "SUMMARY: errors=$ERR_COUNT warnings=$WARN_COUNT" >> "$OUT"

# Если передан аргумент --send — вывести содержимое для вставки в поддержку
if [ "${1:-}" = "--send" ]; then
    echo -e "${CYAN}${BOLD}═══ Содержимое для отправки в поддержку ═══${NC}"
    cat "$OUT"
fi
