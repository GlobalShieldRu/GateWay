#!/bin/bash
set -e

# Источник истины для сети — ОС (`ip route`, `ip addr`). DHCP-пул — settings.json.
# Env vars GSG_GATEWAY_IP/GSG_LAN_INTERFACE/GSG_DHCP_START/END больше НЕ читаем —
# они дублировали то что уже в ОС и приводили к рассинхронизации (инцидент 2026-05-13).

SETTINGS_FILE="/etc/gsg/settings.json"

# Проверка глобального флага settings.json:dhcp_enabled.
# Если пользователь выключил DHCP в UI (gateway-only режим — IP раздаёт
# основной роутер, GSG только проксирует), dnsmasq не запускаем.
# При изменении флага entrypoint завершается → docker restart=always поднимет.
DHCP_ENABLED="true"
if [ -f "$SETTINGS_FILE" ]; then
    DHCP_ENABLED=$(python3 -c "import json; v=json.load(open('$SETTINGS_FILE')).get('dhcp_enabled', True); print(str(v).lower())" 2>/dev/null || echo "true")
fi

if [ "$DHCP_ENABLED" != "true" ]; then
    echo "[INFO] DHCP disabled by settings.json (gateway-only mode)."
    echo "[INFO] Контейнер ждёт изменения флага. Раздачу IP делает внешний роутер."
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

echo "[INFO] Registry DHCP starting (network autodetect from ip route/ip addr)..."

# Генерация конфига (внутри сам читает iface/gsg_ip из системы)
python3 /app/config_generator.py

# Цикл слежения за изменениями (debounce 2с чтобы не триггерить каскадно).
# Реагируем на:
#   - settings.json — изменение dhcp_enabled или dhcp_start/end
#   - devices.json — резервации IP по MAC
#   - .reload_dhcp — явный триггер перегенерации
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
        # Если флаг остался true — мог поменяться dhcp_start/end → регенерим
        echo "[INFO] settings.json change, regenerating dnsmasq config..."
        python3 /app/config_generator.py
        [ -f "$PIDFILE" ] && kill -HUP "$(cat "$PIDFILE")" 2>/dev/null || true
    elif [ "$file" = ".reload_dhcp" ] || [ "$file" = "devices.json" ]; then
        sleep 2
        echo "[INFO] Config change detected, regenerating..."
        python3 /app/config_generator.py
        [ -f "$PIDFILE" ] && kill -HUP "$(cat "$PIDFILE")" 2>/dev/null || true
    fi
done &

exec dnsmasq --no-daemon --conf-file="/etc/dnsmasq.conf" --keep-in-foreground --pid-file="$PIDFILE"
