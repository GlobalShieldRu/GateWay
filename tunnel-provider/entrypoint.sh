#!/bin/bash
set -e

GSG_CONFIG_DIR="/etc/gsg"
MIHOMO_CONFIG="/etc/mihomo/config.yaml"

mkdir -p "$GSG_CONFIG_DIR"
mkdir -p "$(dirname $MIHOMO_CONFIG)"

python3 /app/generate_config.py

# Мониторинг изменений (на лету)
inotifywait -m -e close_write,moved_to,create "$GSG_CONFIG_DIR" 2>/dev/null | while read path action file; do
    if [ "$file" = ".reload_singbox" ] || [ "$file" = "devices.json" ] || [ "$file" = "subscription.json" ]; then
        echo "[INFO] Hot-Reload triggered by $file"
        python3 /app/generate_config.py

        # Обновляем конфиг в ядре Mihomo через API (без перезапуска процесса)
        curl -s -X PUT -H "Content-Type: application/json" -d '{"path": "/etc/mihomo/config.yaml"}' http://127.0.0.1:9090/configs > /dev/null || true

        # КРИТИЧЕСКИ ВАЖНО: Сбрасываем старые сессии, чтобы трафик мгновенно пошел через новый узел
        curl -s -X DELETE http://127.0.0.1:9090/connections > /dev/null || true

        rm -f "$GSG_CONFIG_DIR/.reload_singbox"
    fi
done &

echo "[INFO] Starting Mihomo Core..."
exec /usr/local/bin/mihomo -d /etc/mihomo -f "$MIHOMO_CONFIG" 2>&1 | tee /etc/gsg/sing-box.log
