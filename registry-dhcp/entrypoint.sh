#!/bin/bash
set -e

# Твои переменные на месте — для логов и наглядности
export GATEWAY_IP="${GSG_GATEWAY_IP:-10.10.1.139}"
export DHCP_START="${GSG_DHCP_START:-10.10.1.100}"
export DHCP_END="${GSG_DHCP_END:-10.10.1.200}"
export DNS_IP="${GSG_DNS_IP:-10.10.1.139}"
export LAN_IFACE="${GSG_LAN_INTERFACE:-eth0}"

# Проверка глобального флага settings.json:dhcp_enabled.
# Если пользователь выключил DHCP в UI (gateway-only режим — IP раздаёт
# основной роутер, GSG только проксирует), dnsmasq не запускаем.
# При изменении флага entrypoint завершается → docker restart=always поднимет.
SETTINGS_FILE="/etc/gsg/settings.json"
DHCP_ENABLED="true"
if [ -f "$SETTINGS_FILE" ]; then
    DHCP_ENABLED=$(python3 -c "import json; v=json.load(open('$SETTINGS_FILE')).get('dhcp_enabled', True); print(str(v).lower())" 2>/dev/null || echo "true")
fi

if [ "$DHCP_ENABLED" != "true" ]; then
    echo "[INFO] DHCP disabled by settings.json (gateway-only mode)."
    echo "[INFO] Контейнер ждёт изменения флага. Раздачу IP делает внешний роутер."
    # Сторожим settings.json — при возврате флага → exit для рестарта
    inotifywait -m -e modify,create,close_write "/etc/gsg" 2>/dev/null | while read path action file; do
        if [ "$file" = "settings.json" ] || [ "$file" = ".dhcp_restart_request" ]; then
            sleep 1
            NEW=$(python3 -c "import json; v=json.load(open('$SETTINGS_FILE')).get('dhcp_enabled', True); print(str(v).lower())" 2>/dev/null || echo "true")
            if [ "$NEW" = "true" ]; then
                echo "[INFO] dhcp_enabled re-enabled — выход для рестарта"
                exit 0
            fi
        fi
    done
    exit 0
fi

echo "[INFO] Registry DHCP starting..."
echo "[INFO] Gateway: $GATEWAY_IP | DNS: $DNS_IP"
echo "[INFO] Range: $DHCP_START - $DHCP_END on $LAN_IFACE"

# Генерация конфига
python3 /app/config_generator.py

# Цикл слежения за изменениями (debounce 2с чтобы не триггерить каскадно).
# Также реагируем на settings.json — если dhcp_enabled выключили, выходим
# (docker restart=always поднимет, entrypoint попадёт в gateway-only ветку).
PIDFILE="/var/run/dnsmasq.pid"
inotifywait -m -e modify,create,close_write "/etc/gsg" 2>/dev/null | while read path action file; do
    if [ "$file" = "settings.json" ] || [ "$file" = ".dhcp_restart_request" ]; then
        sleep 1
        NEW=$(python3 -c "import json; v=json.load(open('$SETTINGS_FILE')).get('dhcp_enabled', True); print(str(v).lower())" 2>/dev/null || echo "true")
        if [ "$NEW" != "true" ]; then
            echo "[INFO] dhcp_enabled выключен — выход для перехода в gateway-only режим"
            [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null || true
            exit 0
        fi
    elif [ "$file" = "dhcp.json" ] || [ "$file" = ".reload_dhcp" ] || [ "$file" = "devices.json" ]; then
        sleep 2
        echo "[INFO] Config change detected, regenerating..."
        python3 /app/config_generator.py
        [ -f "$PIDFILE" ] && kill -HUP "$(cat "$PIDFILE")" 2>/dev/null || true
    fi
done &

# Запуск dnsmasq с pid-файлом
exec dnsmasq --no-daemon --conf-file="/etc/dnsmasq.conf" --keep-in-foreground --pid-file="$PIDFILE"
