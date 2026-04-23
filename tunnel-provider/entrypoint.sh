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
RELOAD_LAST_TS=0
RELOAD_DEBOUNCE=30  # секунд тишины перед reload

# Слушаем события и выставляем флаг — debounce обработает их пачкой
inotifywait -m -e close_write,moved_to,create "$GSG_CONFIG_DIR" 2>/dev/null | while read path action file; do
    if [ "$file" = ".reload_singbox" ] || [ "$file" = "devices.json" ] || [ "$file" = "subscription.json" ]; then
        echo "[INFO] Hot-Reload trigger: $file"
        touch "$RELOAD_TRIGGER_FILE"
    fi
done &

# Debounce-обработчик: объединяет частые события — reload не чаще раза в 30 секунд
(
RELOAD_LAST_TS=0
while true; do
    if [ -f "$RELOAD_TRIGGER_FILE" ]; then
        NOW=$(date +%s)
        ELAPSED=$(( NOW - RELOAD_LAST_TS ))
        if [ "$ELAPSED" -lt "$RELOAD_DEBOUNCE" ]; then
            # Слишком рано — ждём оставшееся время и попробуем снова
            WAIT=$(( RELOAD_DEBOUNCE - ELAPSED ))
            echo "[INFO] Hot-Reload debounce: ждём ${WAIT}с (последний reload ${ELAPSED}с назад)"
            sleep "$WAIT"
            continue
        fi
        rm -f "$RELOAD_TRIGGER_FILE"
        RELOAD_LAST_TS=$(date +%s)
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

        # Post-reload force health-check: reload пересоздаёт proxy instances и
        # теряет history. Без этого fallback-группы первые секунды думают что
        # все узлы мертвы. Форсим проверку сразу — history заполняется за 3-5с.
        HEALTH_URL="http://www.gstatic.com/generate_204"
        GROUPS=$(curl -s http://127.0.0.1:9090/proxies 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    for n, p in d.get('proxies', {}).items():
        if p.get('type','').lower() in ('fallback','urltest','url-test','loadbalance','load-balance'):
            print(n)
except: pass
" 2>/dev/null)
        for g in $GROUPS; do
            ENC=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$g")
            curl -s --max-time 8 "http://127.0.0.1:9090/group/${ENC}/delay?url=${HEALTH_URL}&timeout=5000" > /dev/null &
        done
        wait
        echo "[INFO] Post-reload health-check: все группы обновлены"

        rm -f "$GSG_CONFIG_DIR/.reload_singbox"
    fi
    sleep 0.5
done
) &

# ── Watchdog: переподписка при падении нод ───────────────────
# Если backend изменил конфиг серверов (RemnaWave), ноды станут недоступны.
# Watchdog обнаруживает это и немедленно перечитывает подписку.
# Дополнительно — плановое обновление раз в 6 часов на случай тихих изменений.
LAST_REFRESH=0
(
    sleep 90  # ждём пока Mihomo полностью стартует
    while true; do
        NOW=$(date +%s)
        # Считаем мёртвые прямые ноды (не CDN) через Mihomo API
        DEAD=$(curl -s http://127.0.0.1:9090/proxies 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
dead=0
for name,p in d.get('proxies',{}).items():
    is_direct = ('Stockholm' in name or 'NY' in name) and 'Обход' not in name
    if not is_direct: continue
    last=p.get('history',[{}])[-1].get('delay',0) if p.get('history') else 0
    if not p.get('alive',True) or last==0:
        dead+=1
print(dead)
" 2>/dev/null || echo 0)

        ELAPSED=$(( NOW - LAST_REFRESH ))
        # Обновляем если: 3+ прямых нод мертво и прошло >5 мин с последнего обновления
        # ИЛИ плановое обновление каждые 6 часов
        if { [ "$DEAD" -ge 3 ] && [ "$ELAPSED" -ge 300 ]; } || [ "$ELAPSED" -ge 21600 ]; then
            if [ "$DEAD" -ge 3 ]; then
                RESTART_REASON="subscription refresh: ${DEAD} мёртвых прямых нод"
                echo "[WARN] Node watchdog: ${DEAD} прямых нод недоступны — обновляем подписку и hard-restart"
            else
                RESTART_REASON="плановое обновление подписки (6ч)"
                echo "[INFO] Плановое обновление подписки (6ч) + hard-restart"
            fi
            python3 /usr/local/bin/generate_config.py
            # После переподписки делаем hard-restart через web-orchestrator
            # чтобы избежать "призрачных" health-check состояний после API reload
            GSG_INTERNAL_TOKEN="${GSG_INTERNAL_TOKEN:-gsg-internal-restart-v1}"
            RESTART_RESP=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
                -H "Content-Type: application/json" \
                -H "X-Internal-Token: ${GSG_INTERNAL_TOKEN}" \
                -d "{\"reason\": \"${RESTART_REASON}\"}" \
                http://127.0.0.1:8080/api/tunnel-hard-restart 2>/dev/null || echo "000")
            if [ "$RESTART_RESP" = "200" ]; then
                echo "[INFO] Hard-restart запрошен успешно (${RESTART_REASON})"
            elif [ "$RESTART_RESP" = "429" ]; then
                echo "[INFO] Hard-restart: cooldown активен, пропускаем"
            else
                echo "[WARN] Hard-restart недоступен (HTTP ${RESTART_RESP}), fallback: API reload"
                curl -s -X PUT -H "Content-Type: application/json" \
                    -d '{"path": "/etc/mihomo/config.yaml"}' \
                    http://127.0.0.1:9090/configs > /dev/null || true
                sleep 2
                curl -s -X PUT -H "Content-Type: application/json" \
                    -d '{"name": "auto"}' \
                    http://127.0.0.1:9090/proxies/GLOBAL > /dev/null || true
            fi
            LAST_REFRESH=$(date +%s)
            echo "[INFO] Подписка обновлена"
        fi
        sleep 60  # проверка каждую минуту
    done
) &

# ── Health watchdog: авто-восстановление при "призрачных" health-check ───────
# Проверяет группы auto и ai каждые 30 секунд.
# Если 2 подряд проверки вернули пустой результат или всё timeout — запрашивает hard-restart.
(
    sleep 120  # ждём полной инициализации Mihomo и health-check'ов
    HEALTH_FAIL_COUNT=0
    HEALTH_CHECK_URL="http://www.gstatic.com/generate_204"
    while true; do
        sleep 30

        # Опрашиваем группы auto и ai через Mihomo
        AUTO_RESULT=$(curl -s --max-time 10 \
            "http://127.0.0.1:9090/group/auto/delay?url=${HEALTH_CHECK_URL}&timeout=5000" 2>/dev/null || echo "{}")
        AI_RESULT=$(curl -s --max-time 10 \
            "http://127.0.0.1:9090/group/ai/delay?url=${HEALTH_CHECK_URL}&timeout=5000" 2>/dev/null || echo "{}")

        # Проверяем: пустой JSON {} или все значения = 0 (timeout/error в Mihomo = delay 0)
        IS_DEAD=$(python3 -c "
import json, sys
def is_dead(s):
    try:
        d = json.loads(s)
        if not d: return True
        vals = [v for v in d.values() if isinstance(v, (int, float))]
        return bool(vals) and all(v == 0 for v in vals)
    except: return True
a = is_dead(sys.argv[1])
b = is_dead(sys.argv[2])
print('yes' if (a and b) else 'no')
" "$AUTO_RESULT" "$AI_RESULT" 2>/dev/null || echo "no")

        if [ "$IS_DEAD" = "yes" ]; then
            HEALTH_FAIL_COUNT=$(( HEALTH_FAIL_COUNT + 1 ))
            echo "[WARN] Health watchdog: группы auto/ai мертвы (проверка ${HEALTH_FAIL_COUNT}/2)"
        else
            if [ "$HEALTH_FAIL_COUNT" -gt 0 ]; then
                echo "[INFO] Health watchdog: группы восстановились (было ${HEALTH_FAIL_COUNT} fail)"
            fi
            HEALTH_FAIL_COUNT=0
        fi

        if [ "$HEALTH_FAIL_COUNT" -ge 2 ]; then
            echo "[WARN] Health watchdog: 2 подряд провала — запрашиваем hard-restart"
            HEALTH_FAIL_COUNT=0
            GSG_INTERNAL_TOKEN="${GSG_INTERNAL_TOKEN:-gsg-internal-restart-v1}"
            RESTART_RESP=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
                -H "Content-Type: application/json" \
                -H "X-Internal-Token: ${GSG_INTERNAL_TOKEN}" \
                -d '{"reason": "health-check failed: auto/ai groups dead 60s"}' \
                http://127.0.0.1:8080/api/tunnel-hard-restart 2>/dev/null || echo "000")
            if [ "$RESTART_RESP" = "200" ]; then
                echo "[INFO] Health watchdog: hard-restart запрошен успешно"
            elif [ "$RESTART_RESP" = "429" ]; then
                echo "[INFO] Health watchdog: cooldown активен, пропускаем restart"
            else
                echo "[WARN] Health watchdog: hard-restart недоступен (HTTP ${RESTART_RESP})"
            fi
        fi
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
            # Прогреваем health-check всех fallback/url-test групп сразу после старта
            STARTUP_GROUPS=$(curl -s http://127.0.0.1:9090/proxies 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    for n, p in d.get('proxies', {}).items():
        if p.get('type','').lower() in ('fallback','urltest','url-test','loadbalance','load-balance'):
            print(n)
except: pass
" 2>/dev/null)
            for g in $STARTUP_GROUPS; do
                ENC=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))" "$g")
                curl -s --max-time 8 "http://127.0.0.1:9090/group/${ENC}/delay?url=http://www.gstatic.com/generate_204&timeout=5000" > /dev/null &
            done
            wait
            echo "[INFO] Startup health-check: группы прогреты"
            break
        fi
    done
) &
exec /usr/local/bin/mihomo -d /etc/mihomo -f "$MIHOMO_CONFIG" 2>&1 | tee /etc/gsg/sing-box.log
