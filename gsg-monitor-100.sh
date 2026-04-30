#!/bin/bash
# gsg-monitor-100.sh — постоянный мониторинг трафика устройства 10.10.1.100.
#
# Раз в 5 секунд снимает /connections Mihomo и фиксирует уникальные пары
# (chain, host_or_ip) в /var/log/gsg-monitor-100.log с временной меткой.
#
# DIRECT-соединения помечаются "🟡 DIRECT-LEAK?" — это потенциальные leak'и
# (домен пошёл напрямую вместо VPN). Через NY/Stockholm — обычная запись.
#
# Защита от спама: каждый домен/IP логируется только при первом появлении или
# смене chain (например DIRECT → NY).
#
# Просмотр: tail -f /var/log/gsg-monitor-100.log
#           grep "🟡" /var/log/gsg-monitor-100.log  (только leak'и)

set -u

TARGET_IP="${1:-10.10.1.100}"
LOG_FILE="/var/log/gsg-monitor-${TARGET_IP##*.}.log"
INTERVAL=5
STATE_FILE="/tmp/gsg-monitor-${TARGET_IP##*.}.state"

> "$STATE_FILE"  # очищаем state при старте

log() {
    local ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$ts] $*" | tee -a "$LOG_FILE"
}

log "=== gsg-monitor for $TARGET_IP started ==="

while true; do
    curl -s --max-time 3 "http://127.0.0.1:9090/connections" 2>/dev/null > /tmp/conn-${TARGET_IP##*.}.json || { sleep "$INTERVAL"; continue; }

    python3 - <<PYEOF >> /tmp/new-${TARGET_IP##*.}.txt
import json, os
try:
    d = json.load(open("/tmp/conn-${TARGET_IP##*.}.json"))
except: exit()
target = "$TARGET_IP"
state_file = "$STATE_FILE"
seen = set()
if os.path.exists(state_file):
    with open(state_file) as f:
        seen = set(line.strip() for line in f if line.strip())

new_lines = []
for c in d.get("connections", []):
    m = c.get("metadata") or {}
    if m.get("sourceIP") != target: continue
    chain = (c.get("chains") or ["?"])[0]
    host = m.get("host") or ""
    ip = m.get("destinationIP") or ""
    port = m.get("destinationPort") or ""
    proto = m.get("network") or ""
    # Ключ: chain + host. Если host пуст — chain + ip
    ident = host if host else f"_ip_{ip}"
    key = f"{chain}|{ident}"
    if key in seen: continue
    new_lines.append((chain, host or "NO-SNI", ip, port, proto))
    seen.add(key)

with open(state_file, "w") as f:
    f.write("\n".join(seen))

for chain, host, ip, port, proto in new_lines:
    is_leak = "DIRECT" in chain
    flag = "🟡 LEAK?" if is_leak else "✅"
    print(f"{flag} {chain:30s} | {host:50s} | {ip}:{port} {proto}")
PYEOF

    if [ -s /tmp/new-${TARGET_IP##*.}.txt ]; then
        while IFS= read -r line; do
            log "$line"
        done < /tmp/new-${TARGET_IP##*.}.txt
        > /tmp/new-${TARGET_IP##*.}.txt
    fi

    sleep "$INTERVAL"
done
