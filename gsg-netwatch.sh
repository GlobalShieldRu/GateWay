#!/bin/bash
# GSG Network Watchdog
# Detects upstream gateway loss → stops containers → falls back to DHCP
# so the device becomes reachable at a new IP after being moved to another router.

set -e

IFACE_CONF="/etc/network/interfaces.d/gsg-lan.conf"
CHECK_INTERVAL=20   # seconds between checks
FAIL_THRESHOLD=9    # 9 × 20s = 3 minutes before fallback (survives router reboots)
INSTALL_DIR="/root/GSG"

log()  { logger -t gsg-netwatch "$*"; echo "[gsg-netwatch] $*"; }

# ── Read static config ────────────────────────────────────────────────────────
read_iface() { awk '/^auto /{print $2; exit}' "$IFACE_CONF" 2>/dev/null; }
read_gw()    { awk '/gateway /{print $2; exit}' "$IFACE_CONF" 2>/dev/null; }

IFACE=$(read_iface)
GW=$(read_gw)

if [ -z "$IFACE" ] || [ -z "$GW" ]; then
    log "No static config found in $IFACE_CONF — watchdog not needed, exiting."
    exit 0
fi

log "Watching gateway $GW on $IFACE (threshold: ${FAIL_THRESHOLD}×${CHECK_INTERVAL}s)"

# ── Monitor loop ──────────────────────────────────────────────────────────────
fail=0
while true; do
    if ping -c 2 -W 3 -I "$IFACE" "$GW" >/dev/null 2>&1; then
        fail=0
    else
        fail=$((fail + 1))
        log "Gateway $GW unreachable ($fail/$FAIL_THRESHOLD)"

        if [ "$fail" -ge "$FAIL_THRESHOLD" ]; then
            log "Gateway lost — stopping GSG and switching to DHCP..."

            # Stop GSG containers so our DHCP server doesn't conflict with new router
            cd "$INSTALL_DIR" && docker compose stop 2>/dev/null || true

            # Flush static IP
            ip addr flush dev "$IFACE" 2>/dev/null || true

            # Get IP from new router via DHCP
            if command -v dhclient >/dev/null 2>&1; then
                dhclient -v "$IFACE" 2>/dev/null || true
            elif command -v udhcpc >/dev/null 2>&1; then
                udhcpc -i "$IFACE" -q 2>/dev/null || true
            fi

            NEW_IP=$(ip -4 addr show "$IFACE" | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)

            log "New IP via DHCP: ${NEW_IP:-not obtained}"
            log "To reconfigure GSG run: bash $INSTALL_DIR/install.sh"

            # Write recovery hint to a file (readable even without SSH)
            cat > /tmp/gsg-recovery.txt << EOF
GSG moved to new network.
New IP (DHCP): ${NEW_IP:-not obtained}
Reconfigure:  ssh root@${NEW_IP:-<new-ip>}
              bash $INSTALL_DIR/install.sh
EOF

            # Rename static config so we don't loop after service restart
            mv "$IFACE_CONF" "${IFACE_CONF}.bak" 2>/dev/null || true

            log "Done. Connect to ${NEW_IP:-new IP} and run install.sh"
            exit 0
        fi
    fi

    sleep $CHECK_INTERVAL
done
