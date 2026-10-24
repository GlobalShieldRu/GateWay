#!/usr/bin/env bash
# GSG Smart Gateway — управление поэтапной раскаткой релиза (asset rollout.json)
# Использование:
#   ./rollout.sh status [X.Y.Z]          — показать rollout.json и текущий процент
#   ./rollout.sh pause  [X.Y.Z]          — остановить раскатку на stable (канарейки не затрагивает)
#   ./rollout.sh resume [X.Y.Z]          — снять паузу
#   ./rollout.sh set <percent> [X.Y.Z]   — зафиксировать процент (заменяет все стадии одной:
#                                          {"after_hours": 0, "percent": N}); set 100 — раскатить всем
# Без версии — последний релиз (releases/latest). Изменения загружаются через
# `gh release upload --clobber`; устройства подхватывают их на следующей ежечасной проверке.
# Логика решения на устройстве: web-orchestrator/update_policy.py.

set -euo pipefail

REPO="${GSG_REPO:-GlobalShieldRu/GateWay}"

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'
log() { echo -e "${BLUE}[rollout]${NC} $*"; }
ok()  { echo -e "${GREEN}[ok]${NC} $*"; }
die() { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

command -v gh >/dev/null || die "gh CLI не найден: https://cli.github.com"
command -v python3 >/dev/null || die "нужен python3"

CMD="${1:-}"
[[ -z "$CMD" ]] && usage
shift

PERCENT=""
if [[ "$CMD" == "set" ]]; then
  PERCENT="${1:-}"
  [[ "$PERCENT" =~ ^[0-9]+$ ]] && (( 10#$PERCENT <= 100 )) || die "set: процент 0..100, получено: '${PERCENT}'"
  shift
fi
case "$CMD" in status|pause|resume|set) ;; *) usage ;; esac

VERSION="${1:-}"
if [[ -z "$VERSION" ]]; then
  TAG=$(gh release view --repo "$REPO" --json tagName -q .tagName) || die "не удалось получить последний релиз"
else
  [[ "$VERSION" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Версия должна быть в формате X.Y.Z, получено: $VERSION"
  TAG="v${VERSION#v}"
fi
PUBLISHED_AT=$(gh release view "$TAG" --repo "$REPO" --json publishedAt -q .publishedAt) || die "релиз $TAG не найден"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
RJ="$WORK/rollout.json"

HAS_ASSET=true
if ! gh release download "$TAG" --repo "$REPO" -p rollout.json -D "$WORK" >/dev/null 2>&1; then
  HAS_ASSET=false
fi

if [[ "$CMD" == "status" ]]; then
  log "Релиз $TAG, опубликован $PUBLISHED_AT"
  if ! $HAS_ASSET; then
    log "rollout.json нет — stable обновится через 72 ч после публикации, канарейки сразу"
    exit 0
  fi
  python3 - "$RJ" "$PUBLISHED_AT" <<'PY'
import json, sys
from datetime import datetime, timezone
ro = json.load(open(sys.argv[1]))
pub = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
hours = (datetime.now(timezone.utc) - pub).total_seconds() / 3600
pct = 0
for st in sorted(ro.get("stages", []), key=lambda s: s.get("after_hours", 0)):
    if hours >= st.get("after_hours", 0):
        pct = st.get("percent", 0)
print(json.dumps(ro, indent=2, ensure_ascii=False))
state = "ПАУЗА" if ro.get("paused") else f"{pct}% stable-устройств"
print(f"Прошло {hours:.1f} ч после публикации, сейчас: {state}")
PY
  exit 0
fi

# Без asset начинаем с дефолта release.sh, чтобы pause/set работали и на старых релизах
if ! $HAS_ASSET; then
  log "rollout.json у $TAG нет — создаём из дефолта"
  cat > "$RJ" <<'JSON'
{"paused": false, "stages": [{"after_hours": 0, "percent": 0}, {"after_hours": 24, "percent": 10}, {"after_hours": 72, "percent": 100}]}
JSON
fi

python3 - "$RJ" "$CMD" "$PERCENT" <<'PY'
import json, sys
path, cmd, pct = sys.argv[1], sys.argv[2], sys.argv[3]
ro = json.load(open(path))
if cmd == "pause":
    ro["paused"] = True
elif cmd == "resume":
    ro["paused"] = False
elif cmd == "set":
    ro["stages"] = [{"after_hours": 0, "percent": int(pct, 10)}]
    if ro.get("paused"):
        print("[warn] раскатка на паузе — процент не применится до ./rollout.sh resume", file=sys.stderr)
with open(path, "w") as f:
    json.dump(ro, f, indent=2, ensure_ascii=False)
    f.write("\n")
print(json.dumps(ro, ensure_ascii=False))
PY

gh release upload "$TAG" "$RJ" --repo "$REPO" --clobber
ok "rollout.json обновлён для $TAG ($CMD${PERCENT:+ $PERCENT})"
