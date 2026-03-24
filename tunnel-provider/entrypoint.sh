#!/bin/bash
set -e

GSG_CONFIG_DIR="/etc/gsg"
MIHOMO_CONFIG="/etc/mihomo/config.yaml"

mkdir -p "$GSG_CONFIG_DIR"
mkdir -p "$(dirname $MIHOMO_CONFIG)"

# ── Гарантируем рабочий DNS в контейнере ────────────────────
if ! grep -q "^nameserver" /etc/resolv.conf 2>/dev/null; then
    echo "[INFO] /etc/resolv.conf пустой — добавляем резервные DNS"
    printf "nameserver 8.8.8.8\nnameserver 1.1.1.1\n" >> /etc/resolv.conf
fi

# ── Получаем подписку ДО запуска Mihomo ──────────────────────
# Проверяем: есть ли URL подписки в конфиге
HAS_URL=$(python3 -c "
import json, sys
try:
    d = json.load(open('${GSG_CONFIG_DIR}/subscription.json'))
    print('yes' if d.get('url','').strip() else 'no')
except:
    print('no')
" 2>/dev/null || echo "no")

if [ "$HAS_URL" = "yes" ]; then
    MAX_ATTEMPTS=12
    RETRY_DELAY=5

    for attempt in $(seq 1 $MAX_ATTEMPTS); do
        echo "[INFO] Загрузка подписки (попытка ${attempt}/${MAX_ATTEMPTS})..."
        python3 /usr/local/bin/generate_config.py

        NODE_COUNT=$(python3 -c "
import json
try:
    d = json.load(open('${GSG_CONFIG_DIR}/nodes.json'))
    print(len(d.get('nodes', [])))
except:
    print(0)
" 2>/dev/null || echo 0)

        if [ "$NODE_COUNT" -gt 0 ]; then
            echo "[INFO] Получено узлов: ${NODE_COUNT} — запускаем Mihomo"
            break
        fi

        if [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
            echo "[WARN] Узлы не получены, повтор через ${RETRY_DELAY}с..."
            sleep "$RETRY_DELAY"
        else
            echo "[WARN] Подписка недоступна после ${MAX_ATTEMPTS} попыток — запускаем с минимальным конфигом"
        fi
    done
else
    echo "[INFO] URL подписки не задан — генерируем минимальный конфиг"
    python3 /usr/local/bin/generate_config.py
fi

# ── Мониторинг изменений (на лету) ───────────────────────────
inotifywait -m -e close_write,moved_to,create "$GSG_CONFIG_DIR" 2>/dev/null | while read path action file; do
    if [ "$file" = ".reload_singbox" ] || [ "$file" = "devices.json" ] || [ "$file" = "subscription.json" ]; then
        echo "[INFO] Hot-Reload: $file изменён"
        python3 /usr/local/bin/generate_config.py

        curl -s -X PUT -H "Content-Type: application/json" \
            -d '{"path": "/etc/mihomo/config.yaml"}' \
            http://127.0.0.1:9090/configs > /dev/null || true

        curl -s -X DELETE http://127.0.0.1:9090/connections > /dev/null || true

        rm -f "$GSG_CONFIG_DIR/.reload_singbox"
    fi
done &

echo "[INFO] Запуск Mihomo Core..."
exec /usr/local/bin/mihomo -d /etc/mihomo -f "$MIHOMO_CONFIG" 2>&1 | tee /etc/gsg/sing-box.log
