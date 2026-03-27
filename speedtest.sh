#!/usr/bin/env bash
# GSG Speedtest — generates VPN traffic through different nodes for dot visualization
#
# Strategy:
#   1. Temporarily switch Mihomo to "global" mode → all proxy traffic forces through VPN
#   2. Explicitly route downloads via Mihomo mixed-port (10.10.1.139:2080)
#   3. Switch nodes via Mihomo API, download large files, restore rule mode
#
# Usage: ./speedtest.sh [duration_seconds]

DURATION=${1:-25}
PARALLEL=4
PROXY="http://10.10.1.139:2080"
MIHOMO="http://127.0.0.1:9090"

# Any large-file CDN works since Mihomo global mode forces everything through VPN
URLS=(
    "https://ash-speed.hetzner.com/100MB.bin"
    "https://speed.cloudflare.com/__down?bytes=104857600"
    "https://fra-de-ping.vultr.com/vultr.com.100MB.bin"
    "https://proof.ovh.net/files/100Mb.dat"
)

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'

header() { echo -e "${BOLD}${BLUE}▶ $*${NC}"; }
ok()     { echo -e "  ${GREEN}✓ $*${NC}"; }
warn()   { echo -e "  ${YELLOW}⚠ $*${NC}"; }

# ── SSH helpers ───────────────────────────────────────────────────────────────

mihomo() {
    # Run curl against Mihomo API inside the tunnel container
    ssh -o BatchMode=yes -o ConnectTimeout=4 root@10.10.1.139 \
        "docker exec gsg-tunnel curl -s $*" 2>/dev/null
}

mihomo_patch() {
    ssh -o BatchMode=yes -o ConnectTimeout=4 root@10.10.1.139 \
        "docker exec gsg-tunnel curl -s -X PATCH '${MIHOMO}/configs' \
         -H 'Content-Type: application/json' -d '$1'" 2>/dev/null
}

set_mode() {
    # global = all proxy traffic through VPN; rule = normal routing
    mihomo_patch "{\"mode\":\"$1\"}" > /dev/null
    if [ "$1" = "global" ]; then
        # Also point the GLOBAL selector group to auto (default is DIRECT)
        mihomo "-X PUT '${MIHOMO}/proxies/GLOBAL' \
            -H 'Content-Type: application/json' -d '{\"name\":\"auto\"}'" > /dev/null
    fi
    echo "  Mihomo mode → $1"
}

set_node() {
    # Force auto url-test group to specific node
    mihomo "-X PUT '${MIHOMO}/proxies/auto' \
        -H 'Content-Type: application/json' -d '{\"name\":\"$1\"}'" > /dev/null
}

current_node() {
    mihomo "${MIHOMO}/proxies/auto" | \
        python3 -c "import sys,json; print(json.load(sys.stdin).get('now','?'))" 2>/dev/null
}

list_nodes() {
    # Returns lines: "node_name\tdelay_ms" — reads members of the 'auto' url-test group
    mihomo "${MIHOMO}/proxies" | python3 -c "
import sys, json
d = json.load(sys.stdin)
proxies = d.get('proxies', {})
# Get members of the auto url-test group
members = proxies.get('auto', {}).get('all', [])
for name in members:
    info = proxies.get(name, {})
    h = info.get('history', [])
    delay = h[-1].get('delay', 0) if h else 0
    print(f'{name}\t{delay}')
" 2>/dev/null
}

find_node_by() {
    # Find first VPN node whose name contains $1 (case-insensitive, optionally excludes $2)
    local pattern="$1" exclude="$2"
    local nodes
    nodes=$(list_nodes)
    # Only apply exclude filter when non-empty (grep -v "" inverts all lines)
    if [ -n "$exclude" ]; then
        nodes=$(echo "$nodes" | grep -vF "$exclude")
    fi
    echo "$nodes" | grep -i "$pattern" | head -1 | cut -f1
}

active_conns() {
    mihomo "${MIHOMO}/connections" | \
        python3 -c "import sys,json; print(len(json.load(sys.stdin).get('connections',[])))" 2>/dev/null || echo "?"
}

vpn_traffic() {
    # Show per-chain breakdown of active connections
    mihomo "${MIHOMO}/connections" | python3 -c "
import sys, json
from collections import Counter
d = json.load(sys.stdin)
conns = d.get('connections', [])
by_chain = Counter()
for c in conns:
    chain = (c.get('chains') or ['DIRECT'])[0]
    by_chain[chain] += 1
direct = by_chain.pop('DIRECT', 0)
vpn    = sum(by_chain.values())
print(f'  Total={len(conns)}  VPN={vpn}  Direct={direct}')
for node, cnt in by_chain.most_common(3):
    print(f'    {node[:40]}: {cnt}')
" 2>/dev/null
}

# ── Download runner ───────────────────────────────────────────────────────────

run_phase() {
    local label="$1" node_name="$2" num="${3:-$PARALLEL}"
    local pids=() url_count=${#URLS[@]} i=0

    echo ""
    header "$label  (${DURATION}s · ${num} streams)"

    if [ -n "$node_name" ]; then
        set_node "$node_name"
        sleep 0.4
    fi
    echo "  auto → $(current_node)"

    local end=$((SECONDS + DURATION))
    while [ $SECONDS -lt $end ]; do
        local active=0
        for pid in "${pids[@]}"; do kill -0 "$pid" 2>/dev/null && ((active++)); done

        while [ $active -lt $num ] && [ $SECONDS -lt $end ]; do
            curl -s -o /dev/null --proxy "$PROXY" \
                --max-time $((DURATION + 15)) \
                "${URLS[$((i % url_count))]}" &
            pids+=($!); ((i++)); ((active++))
        done

        sleep 4
        echo -ne "\r$(vpn_traffic | tr '\n' '|')   "
    done

    echo ""
    for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null; done
    wait 2>/dev/null
}

# ── Main ──────────────────────────────────────────────────────────────────────

echo -e "${BOLD}GSG Speedtest — VPN dot visualization test${NC}"
echo -e "Proxy: ${CYAN}${PROXY}${NC}  |  ${DURATION}s per phase  |  ${PARALLEL} streams"
echo ""

# Check proxy reachability
if ! curl -s --proxy "$PROXY" --max-time 5 -o /dev/null "http://www.gstatic.com/generate_204"; then
    echo -e "${RED}✗ Proxy ${PROXY} unreachable. Check GSG is running.${NC}"
    exit 1
fi
ok "Proxy reachable"

# Show available nodes
echo ""
echo -e "${CYAN}VPN nodes:${NC}"
list_nodes | while IFS=$'\t' read name delay; do
    status=$([ "$delay" -gt 0 ] 2>/dev/null && echo "${GREEN}✓${NC}" || echo "${RED}✗${NC}")
    echo -e "  $status $name  ${delay}ms"
done

# Switch Mihomo to global mode so ALL proxy traffic goes through VPN
echo ""
set_mode "global"

# Trigger healthcheck so auto picks best node
mihomo "-X GET '${MIHOMO}/proxies/auto/delay?url=http%3A%2F%2Fwww.gstatic.com%2Fgenerate_204&timeout=5000'" > /dev/null 2>&1
sleep 1

# Phase 1: Auto (best latency node)
run_phase "Phase 1: AUTO" "" $PARALLEL

# Phase 2: NY node
NY=$(find_node_by "ny" "")
if [ -n "$NY" ]; then
    run_phase "Phase 2: NY — $NY" "$NY" $PARALLEL
else
    warn "No NY node found (names: $(list_nodes | cut -f1 | tr '\n' ';'))"
fi

# Phase 3: Stockholm node
STK=$(find_node_by "stockholm\|stk\|stockholm" "$NY")
[ -z "$STK" ] && STK=$(list_nodes | grep -iv "$NY" | head -1 | cut -f1)
if [ -n "$STK" ]; then
    run_phase "Phase 3: STK — $STK" "$STK" $PARALLEL
else
    warn "No STK node found"
fi

# Phase 4: Heavy load (back to auto)
run_phase "Phase 4: HEAVY MIX" "" $((PARALLEL * 3))

# Restore
echo ""
set_mode "rule"
mihomo "-X GET '${MIHOMO}/proxies/auto/delay?url=http%3A%2F%2Fwww.gstatic.com%2Fgenerate_204&timeout=5000'" > /dev/null 2>&1
ok "Restored rule mode"
echo ""
echo -e "${CYAN}Final:${NC} auto=$(current_node)  conns=$(active_conns)"
