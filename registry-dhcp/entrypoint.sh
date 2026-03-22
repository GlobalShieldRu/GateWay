#!/bin/bash
set -e

# Твои переменные на месте — для логов и наглядности
export GATEWAY_IP="${GSG_GATEWAY_IP:-10.10.1.139}"
export DHCP_START="${GSG_DHCP_START:-10.10.1.100}"
export DHCP_END="${GSG_DHCP_END:-10.10.1.200}"
export DNS_IP="${GSG_DNS_IP:-10.10.1.139}"
export LAN_IFACE="${GSG_LAN_INTERFACE:-eth0}"

echo "[INFO] Registry DHCP starting..."
echo "[INFO] Gateway: $GATEWAY_IP | DNS: $DNS_IP"
echo "[INFO] Range: $DHCP_START - $DHCP_END on $LAN_IFACE"

# Генерация конфига
python3 /app/config_generator.py

# Цикл слежения за изменениями
inotifywait -m -e modify,create "/etc/gsg" 2>/dev/null | while read path action file; do
    if [ "$file" = "dhcp.json" ] || [ "$file" = ".reload_dhcp" ]; then
        echo "[INFO] Config change detected, regenerating..."
        python3 /app/config_generator.py
        pkill -HUP dnsmasq || true
    fi
done &

# Запуск dnsmasq (убраны конфликтующие флаги)
exec dnsmasq --no-daemon --conf-file="/etc/dnsmasq.conf" --keep-in-foreground
