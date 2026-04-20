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

# ── Обновляем geosite.dat (runetfreedom, до запуска Mihomo) ──
# Маркер-файл означает: в geosite.dat есть категория ru-available-only-inside.
# Без маркера generate_config.py пропустит GEOSITE,ru-available-only-inside,DIRECT.
GEOSITE_URL="https://github.com/runetfreedom/russia-v2ray-rules-dat/releases/latest/download/geosite.dat"
GEOSITE_PATH="/etc/mihomo/geosite.dat"
GEOSITE_MARKER="/etc/mihomo/.geosite-runetfreedom"

if curl -sL --connect-timeout 20 -o /tmp/geosite.dat.tmp "$GEOSITE_URL" 2>/dev/null \
   && [ -s /tmp/geosite.dat.tmp ]; then
    mv /tmp/geosite.dat.tmp "$GEOSITE_PATH"
    touch "$GEOSITE_MARKER"
    echo "[INFO] geosite.dat обновлён (runetfreedom/russia-v2ray-rules-dat)"
else
    rm -f /tmp/geosite.dat.tmp
    if [ -f "$GEOSITE_MARKER" ]; then
        echo "[INFO] geosite.dat: скачать не удалось, используем предыдущую версию runetfreedom"
    else
        echo "[WARN] geosite.dat: bundled версия (ru_direct будет пропущен до успешного скачивания)"
    fi
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
RELOAD_TRIGGER_FILE="/tmp/gsg_reload_pending"

# Слушаем события и выставляем флаг — debounce обработает их пачкой
inotifywait -m -e close_write,moved_to,create "$GSG_CONFIG_DIR" 2>/dev/null | while read path action file; do
    if [ "$file" = ".reload_singbox" ] || [ "$file" = "devices.json" ] || [ "$file" = "subscription.json" ]; then
        echo "[INFO] Hot-Reload trigger: $file"
        touch "$RELOAD_TRIGGER_FILE"
    fi
done &

# Debounce-обработчик: ждёт 1 секунду тишины после последнего события, затем делает один reload
while true; do
    if [ -f "$RELOAD_TRIGGER_FILE" ]; then
        sleep 1  # ждём накопления событий
        rm -f "$RELOAD_TRIGGER_FILE"
        echo "[INFO] Hot-Reload: применяем изменения"
        python3 /usr/local/bin/generate_config.py

        curl -s -X PUT -H "Content-Type: application/json" \
            -d '{"path": "/etc/mihomo/config.yaml"}' \
            http://127.0.0.1:9090/configs > /dev/null || true

        # Восстанавливаем GLOBAL → auto после reload (Mihomo сбрасывает на DIRECT)
        sleep 2
        curl -s -X PUT -H "Content-Type: application/json" \
            -d '{"name": "auto"}' \
            http://127.0.0.1:9090/proxies/GLOBAL > /dev/null || true
        echo "[INFO] GLOBAL selector восстановлен: auto"

        rm -f "$GSG_CONFIG_DIR/.reload_singbox"
    fi
    sleep 0.5
done &

# ── Периодическое обновление подписки каждые 6 часов ────────
# Нужно чтобы подхватывать изменения на серверах (смена портов, UUID и т.д.)
# без ручного вмешательства или перезапуска контейнера.
(
    while true; do
        sleep 21600  # 6 часов
        echo "[INFO] Плановое обновление подписки..."
        python3 /usr/local/bin/generate_config.py
        curl -s -X PUT -H "Content-Type: application/json" \
            -d '{"path": "/etc/mihomo/config.yaml"}' \
            http://127.0.0.1:9090/configs > /dev/null || true
        sleep 2
        curl -s -X PUT -H "Content-Type: application/json" \
            -d '{"name": "auto"}' \
            http://127.0.0.1:9090/proxies/GLOBAL > /dev/null || true
        echo "[INFO] Подписка обновлена"
    done
) &

echo "[INFO] Запуск Mihomo Core..."
# Выставляем GLOBAL → auto после старта (в фоне, ждём готовности API)
(
    for i in $(seq 1 15); do
        sleep 2
        if curl -s http://127.0.0.1:9090/proxies/GLOBAL > /dev/null 2>&1; then
            curl -s -X PUT -H "Content-Type: application/json" \
                -d '{"name": "auto"}' \
                http://127.0.0.1:9090/proxies/GLOBAL > /dev/null || true
            echo "[INFO] GLOBAL selector выставлен: auto (старт)"
            break
        fi
    done
) &
exec /usr/local/bin/mihomo -d /etc/mihomo -f "$MIHOMO_CONFIG" 2>&1 | tee /etc/gsg/sing-box.log
