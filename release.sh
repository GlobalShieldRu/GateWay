#!/usr/bin/env bash
# GSG Smart Gateway — Release Script
# Использование:
#   ./release.sh 1.2.0              — полный релиз: тег + GitHub Release + деплой + Telegram
#   ./release.sh 1.2.0 --no-deploy  — без деплоя на сервер
#   ./release.sh 1.2.0 --dry-run    — показать что будет сделано, ничего не менять

set -euo pipefail

# ─── Конфигурация ────────────────────────────────────────────────────────────
SERVER="root@10.10.1.139"
REPO_DIR="/root/GSG"
MAIN_PY="web-orchestrator/main.py"
CHANGELOG="CHANGELOG.md"

TG_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TG_CHAT="${TELEGRAM_NOTIFY_CHAT_ID:-}"
# ─────────────────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log()  { echo -e "${BLUE}[release]${NC} $*"; }
ok()   { echo -e "${GREEN}[ok]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
die()  { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# ─── Аргументы ───────────────────────────────────────────────────────────────
VERSION="${1:-}"
DEPLOY=true
DRY_RUN=false

for arg in "${@:2}"; do
  case "$arg" in
    --no-deploy) DEPLOY=false ;;
    --dry-run)   DRY_RUN=true ;;
    *) die "Неизвестный аргумент: $arg" ;;
  esac
done

[[ -z "$VERSION" ]] && die "Укажи версию: ./release.sh 1.2.0"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Версия должна быть в формате X.Y.Z, получено: $VERSION"

# ─── Проверки ────────────────────────────────────────────────────────────────
log "Проверка окружения..."

[[ -f "$MAIN_PY" ]] || die "Файл не найден: $MAIN_PY (запускай из корня репозитория)"
[[ -f "$CHANGELOG" ]] || warn "CHANGELOG.md не найден — релиз будет без описания"

# Проверяем что нет незакоммиченных изменений
if ! git diff --quiet || ! git diff --cached --quiet; then
  die "Есть незакоммиченные изменения. Закоммить всё перед релизом."
fi

# Проверяем что мы на main
CURRENT_BRANCH=$(git branch --show-current)
[[ "$CURRENT_BRANCH" == "main" ]] || die "Релиз только из ветки main, сейчас: $CURRENT_BRANCH"

# Тег уже существует?
if git tag -l "v$VERSION" | grep -q "v$VERSION"; then
  die "Тег v$VERSION уже существует"
fi

# Читаем текущую версию
CURRENT_VERSION=$(grep 'GSG_VERSION = ' "$MAIN_PY" | sed 's/.*"\(.*\)".*/\1/')
log "Текущая версия: ${BOLD}$CURRENT_VERSION${NC} → Новая версия: ${BOLD}$VERSION${NC}"

# ─── Dry run ─────────────────────────────────────────────────────────────────
if $DRY_RUN; then
  echo ""
  echo -e "${BOLD}Dry run — что будет сделано:${NC}"
  echo "  1. Обновить GSG_VERSION в $MAIN_PY: $CURRENT_VERSION → $VERSION"
  echo "  2. git commit + git tag v$VERSION"
  echo "  3. git push origin main --tags"
  echo "  4. gh release create v$VERSION"
  $DEPLOY && echo "  5. SSH деплой на $SERVER" || echo "  5. Деплой пропущен (--no-deploy)"
  echo "  6. Telegram уведомление"
  echo ""
  exit 0
fi

# ─── Подтверждение ───────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}Релиз v$VERSION${NC}"
$DEPLOY && echo "  Деплой: $SERVER" || echo "  Деплой: пропущен"
echo ""
read -rp "Продолжить? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || { log "Отменено."; exit 0; }

# ─── 1. Обновляем версию в main.py ───────────────────────────────────────────
log "Обновляем GSG_VERSION в $MAIN_PY..."
sed -i.bak "s/GSG_VERSION = \"$CURRENT_VERSION\"/GSG_VERSION = \"$VERSION\"/" "$MAIN_PY"
rm -f "${MAIN_PY}.bak"

# Проверяем что замена прошла
grep "GSG_VERSION = \"$VERSION\"" "$MAIN_PY" > /dev/null || die "Не удалось обновить версию в $MAIN_PY"
ok "Версия обновлена"

# ─── 2. Коммитим версию ──────────────────────────────────────────────────────
log "Создаём коммит версии..."
git add "$MAIN_PY"
git commit -m "chore: release v$VERSION"

# ─── 3. Создаём тег ──────────────────────────────────────────────────────────
log "Создаём тег v$VERSION..."

# Извлекаем секцию из CHANGELOG для этой версии
RELEASE_NOTES=""
if [[ -f "$CHANGELOG" ]]; then
  RELEASE_NOTES=$(awk "/^## \[$VERSION\]/{found=1; next} found && /^## \[/{exit} found{print}" "$CHANGELOG" | sed '/^[[:space:]]*$/d')
fi

if [[ -n "$RELEASE_NOTES" ]]; then
  git tag -a "v$VERSION" -m "$(printf 'v%s\n\n%s' "$VERSION" "$RELEASE_NOTES")"
else
  # Fallback: генерируем из git log с прошлого тега
  PREV_TAG=$(git tag --sort=-version:refname | head -1)
  if [[ -n "$PREV_TAG" ]]; then
    AUTO_LOG=$(git log "${PREV_TAG}..HEAD~1" --pretty=format:"- %s" | grep -v "^- chore: release" | head -20)
    git tag -a "v$VERSION" -m "$(printf 'v%s\n\n%s' "$VERSION" "$AUTO_LOG")"
    warn "Секция в CHANGELOG не найдена, использован git log"
  else
    git tag -a "v$VERSION" -m "v$VERSION"
  fi
fi
ok "Тег v$VERSION создан"

# ─── 4. Пушим ────────────────────────────────────────────────────────────────
log "Пушим в origin..."
git push origin main
git push origin "v$VERSION"
ok "Запушено"

# ─── 5. GitHub Release ───────────────────────────────────────────────────────
log "Создаём GitHub Release..."

if command -v gh &>/dev/null; then
  GH_NOTES=""
  if [[ -n "$RELEASE_NOTES" ]]; then
    GH_NOTES="$RELEASE_NOTES"
  else
    PREV_TAG=$(git tag --sort=-version:refname | grep -v "v$VERSION" | head -1)
    if [[ -n "$PREV_TAG" ]]; then
      GH_NOTES=$(git log "${PREV_TAG}..HEAD~1" --pretty=format:"- %s" | grep -v "^- chore: release" | head -20)
    fi
  fi

  gh release create "v$VERSION" \
    --title "GSG Smart Gateway v$VERSION" \
    --notes "${GH_NOTES:-Релиз v$VERSION}" \
    --latest
  ok "GitHub Release создан: https://github.com/GlobalShieldRu/GateWay/releases/tag/v$VERSION"
else
  warn "gh CLI не найден — GitHub Release не создан. Установи: https://cli.github.com"
fi

# ─── 6. Деплой ───────────────────────────────────────────────────────────────
if $DEPLOY; then
  log "Деплой на $SERVER..."

  DEPLOY_OUTPUT=$(ssh "$SERVER" "
    set -e
    cd $REPO_DIR
    echo '=== git fetch ===' && git fetch origin
    echo '=== git reset ===' && git reset --hard origin/main
    echo '=== docker build ===' && docker compose build --no-cache 2>&1 | tail -20
    echo '=== docker up ===' && docker compose up -d
    echo '=== status ===' && docker compose ps --format 'table {{.Name}}\t{{.Status}}'
  " 2>&1) || { die "Деплой завершился с ошибкой:\n$DEPLOY_OUTPUT"; }

  ok "Деплой завершён"
  echo "$DEPLOY_OUTPUT" | tail -10
else
  warn "Деплой пропущен (--no-deploy)"
fi

# ─── 7. Telegram уведомление ─────────────────────────────────────────────────
if [[ -n "$TG_TOKEN" && -n "$TG_CHAT" ]]; then
  log "Отправляем Telegram уведомление..."

  # Форматируем release notes для Telegram (первые 5 строк)
  TG_CHANGES=""
  if [[ -n "$RELEASE_NOTES" ]]; then
    TG_CHANGES=$(echo "$RELEASE_NOTES" | head -8 | sed 's/^### /\n<b>/; s/$/<\/b>/' | sed 's/^- /• /')
  fi

  DEPLOY_STATUS="не деплоился"
  $DEPLOY && DEPLOY_STATUS="задеплоен на <code>$SERVER</code>"

  TG_TEXT="🛡 <b>GSG Smart Gateway v${VERSION}</b>

${TG_CHANGES}
━━━━━━━━━━━━━━━
📦 <a href=\"https://github.com/GlobalShieldRu/GateWay/releases/tag/v${VERSION}\">Release на GitHub</a>
🚀 ${DEPLOY_STATUS}
#release #gsg"

  # Отправляем через локальный прокси (Telegram заблокирован в РФ)
  PROXY_ARGS=""
  if curl -s --proxy "http://127.0.0.1:2080" --max-time 3 https://t.me > /dev/null 2>&1; then
    PROXY_ARGS="--proxy http://127.0.0.1:2080"
  fi

  HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" $PROXY_ARGS \
    -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\": \"${TG_CHAT}\", \"text\": $(echo "$TG_TEXT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'), \"parse_mode\": \"HTML\", \"disable_web_page_preview\": true}" \
    2>/dev/null)

  [[ "$HTTP_STATUS" == "200" ]] && ok "Telegram уведомление отправлено" || warn "Telegram: HTTP $HTTP_STATUS"
else
  warn "Telegram: TELEGRAM_BOT_TOKEN или TELEGRAM_NOTIFY_CHAT_ID не заданы"
fi

# ─── Готово ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}Релиз v$VERSION готов${NC}"
echo -e "  GitHub: https://github.com/GlobalShieldRu/GateWay/releases/tag/v$VERSION"
