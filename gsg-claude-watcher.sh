#!/bin/bash
# gsg-claude-watcher.sh — auto-detector Claude/Anthropic трафика идущего мимо VPN.
#
# Если Claude Code или claude.ai обращается к новому домену (s-cdn.anthropic.com,
# claude.com, statsig.anthropic.com и т.п.), который не покрыт правилами и идёт
# через DIRECT — Anthropic блокирует RU-IP и пользователь теряет доступ.
#
# Watcher каждые 60 сек:
#   1. Читает /connections Mihomo
#   2. Находит "Claude-pattern" соединения которые идут НЕ через NY
#   3. Авто-добавляет домен в группу `ai` (через rules.json + Mihomo reload)
#   4. Шлёт Telegram-уведомление "Auto-routed <domain> via NY"
#
# Принципы:
#   - Идемпотентность: повторно не добавляет
#   - Cooldown 30с между reload (чтобы не дёргать Mihomo каждую секунду)
#   - Whitelist patterns строгий: anthropic.com / claude.ai / claude.com /
#     statsig.com (Anthropic analytics) / sentry-anthropic.io
#   - НЕ трогает соединения если они уже идут через NY (всё хорошо)

set -u

CONFIG_DIR="/var/lib/docker/volumes/gsg_gsg_config/_data"
RULES_FILE="${CONFIG_DIR}/rules.json"
LOG_FILE="/var/log/gsg-claude-watcher.log"
MIHOMO_API="http://127.0.0.1:9090"
TG_CONFIG="${CONFIG_DIR}/telegram.json"
CHECK_INTERVAL=60
RELOAD_COOLDOWN=30

last_reload=0

log() {
    local ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$ts] $*" | tee -a "$LOG_FILE"
}

tg_alert() {
    local msg="$1"
    [ -f "$TG_CONFIG" ] || return 0
    local token chat
    token=$(python3 -c "import json;print(json.load(open('$TG_CONFIG')).get('bot_token',''))" 2>/dev/null || echo "")
    chat=$(python3 -c "import json;print(json.load(open('$TG_CONFIG')).get('chat_id',''))" 2>/dev/null || echo "")
    [ -z "$token" ] || [ -z "$chat" ] && return 0
    curl -s --max-time 5 "https://api.telegram.org/bot${token}/sendMessage" \
        -d "chat_id=${chat}" \
        -d "text=🛡️ Claude-watcher: ${msg}" \
        -d "parse_mode=HTML" > /dev/null 2>&1
}

log "=== gsg-claude-watcher started (interval=${CHECK_INTERVAL}s) ==="
sleep 30  # дать системе подняться

while true; do
    # Получаем список Claude-pattern соединений идущих НЕ через NY
    LEAKS=$(curl -s --max-time 5 "${MIHOMO_API}/connections" 2>/dev/null | python3 -c "
import json, sys, re
try:
    d = json.load(sys.stdin)
except:
    sys.exit(0)

# Whitelist: домены интегрированные в Claude/Anthropic/clipboard-AI workflows
# которые при leak в DIRECT блокируют RU-IP и ломают подписку/чат.
patterns = re.compile(r'(anthropic\.com|claude\.ai|claude\.com|statsig-anthropic\.com|sentry-anthropic|console\.anthropic|stripe\.com|stripe\.network|datadoghq\.com|chatgpt\.com|openai\.com|vercel\.com|vercel\.app)', re.I)

# IP-блоки которые Mihomo всё равно должен матчить через ai (для leaks без SNI)
anthropic_ips = ['160.79.104.', '160.79.108.', '160.79.112.']

leaks = set()
for c in d.get('connections', []):
    m = c.get('metadata') or {}
    host = (m.get('host') or '').lower().strip()
    ip = m.get('destinationIP') or ''
    chains = c.get('chains') or []
    chain_str = ' '.join(chains)

    # 1. SNI matches Anthropic/Claude pattern
    is_claude = bool(host and patterns.search(host))
    # 2. IP в Anthropic ASN
    is_anthropic_ip = any(ip.startswith(p) for p in anthropic_ips)

    if not (is_claude or is_anthropic_ip):
        continue

    # Проверяем: идёт ли через NY?
    if 'NY' in chain_str:
        continue  # OK, через VPN — пропускаем

    # Это leak. Извлекаем root domain (для авто-добавления):
    # api.anthropic.com -> anthropic.com
    # s-cdn.anthropic.com -> anthropic.com
    if host:
        parts = host.split('.')
        if len(parts) >= 2:
            root = '.'.join(parts[-2:])
            leaks.add(root)

for d in sorted(leaks):
    print(d)
" 2>/dev/null)

    if [ -n "$LEAKS" ]; then
        # Cooldown
        now=$(date +%s)
        elapsed=$((now - last_reload))
        if [ "$elapsed" -lt "$RELOAD_COOLDOWN" ]; then
            log "LEAK обнаружен ($LEAKS), но cooldown активен ($((RELOAD_COOLDOWN - elapsed))s)"
        else
            # Добавляем все leak-домены в группу ai
            ADDED=$(python3 <<PYEOF
import json
p = "$RULES_FILE"
d = json.load(open(p))
leaks = """$LEAKS""".strip().split("\n")
added = []
for g in d.get("proxy_groups", []):
    if g.get("id") == "ai":
        rules = g.setdefault("rules", [])
        for leak in leaks:
            leak = leak.strip()
            if leak and leak not in rules:
                rules.insert(0, leak)
                added.append(leak)
        break
if added:
    json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
print(",".join(added))
PYEOF
)
            if [ -n "$ADDED" ]; then
                log "AUTO-ADD в группу ai: $ADDED"
                # Regenerate Mihomo config и reload
                docker exec gsg-tunnel python3 /usr/local/bin/generate_config.py > /dev/null 2>&1
                curl -s -X PUT -H "Content-Type: application/json" \
                    -d '{"path": "/etc/mihomo/config.yaml"}' \
                    "${MIHOMO_API}/configs" > /dev/null 2>&1
                last_reload=$(date +%s)
                tg_alert "Auto-routed via NY: <code>${ADDED}</code>"
            fi
        fi
    fi

    sleep "$CHECK_INTERVAL"
done
