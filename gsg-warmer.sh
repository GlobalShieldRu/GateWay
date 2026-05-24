#!/bin/bash
# gsg-warmer — поддерживает src-port в whitelist anti-DDoS upstream.
# Решает cold-start lag (10-15с на первом коннекте к RU-сервисам после простоя).
# См. Research/2026-05-24-cold-start-lag-results.md

set -u
LOG=/var/log/gsg-warmer.log
PARALLEL=6
# Таймаут должен покрывать первый retry-cycle TCP (anti-DDoS дропает первые SYN-7+с).
# Иначе warmer сам ловит cold-start и не прогревает.
TIMEOUT=10

DOMAINS=(
    # Банки — самые жёсткие anti-DDoS
    tinkoff.ru
    www.tinkoff.ru
    mobile-bank.cdn-tinkoff.ru
    trbcdn.net
    sberbank.ru
    online.sberbank.ru
    vtb.ru
    alfabank.ru
    raiffeisen.ru
    gazprombank.ru
    # Госуслуги
    www.gosuslugi.ru
    nalog.ru
    mos.ru
    parking.mos.ru
    # Маркетплейсы
    www.wildberries.ru
    www.ozon.ru
    www.avito.ru
    market.yandex.ru
    # Контент / Yandex / Mail.ru
    www.yandex.ru
    ya.ru
    mail.ru
    www.kinopoisk.ru
    2gis.ru
    komanda.fit
)

warm() {
    local d="$1" t0 dt
    t0=$(date +%s%3N)
    if curl --head --silent --max-time "$TIMEOUT" -o /dev/null "https://$d/" 2>/dev/null; then
        dt=$(( $(date +%s%3N) - t0 ))
        echo "ok ${dt}ms $d"
    else
        dt=$(( $(date +%s%3N) - t0 ))
        echo "fail ${dt}ms $d"
    fi
}
export -f warm
export TIMEOUT

ts=$(date -Is)
results=$(printf '%s\n' "${DOMAINS[@]}" | xargs -P "$PARALLEL" -I{} bash -c 'warm "$@"' _ {})
ok=$(echo "$results" | grep -c '^ok ')
fail=$(echo "$results" | grep -c '^fail ')
echo "$ts run ok=$ok fail=$fail total=${#DOMAINS[@]}" >> "$LOG"
echo "$results" | grep '^fail ' | sed "s/^/$ts /" >> "$LOG"
