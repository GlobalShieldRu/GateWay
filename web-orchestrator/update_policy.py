"""Политика OTA-обновлений: сравнение версий и поэтапная раскатка (staged rollout).

Чистые функции без I/O — main.py скачивает релиз/rollout.json и читает файлы,
здесь только решение «обновляться или нет». Покрыто tests/unit/test_update_policy.py.

Модель раскатки:
  - к каждому GitHub-релизу приложен asset rollout.json:
      {"paused": false, "stages": [{"after_hours": 0, "percent": 0},
                                   {"after_hours": 24, "percent": 10},
                                   {"after_hours": 72, "percent": 100}]}
    отсчёт after_hours — от published_at релиза;
  - устройство попадает в корзину 0..99 по sha256(device_id) — стабильно между
    проверками, у разных устройств равномерно;
  - канал "canary" обновляется сразу, "stable" — когда корзина < percent;
  - paused — аварийный стоп для ВСЕХ каналов, включая canary;
  - нет rollout.json / битый — stable ждёт DEFAULT_STABLE_DELAY_HOURS после публикации.
"""
import hashlib
from datetime import datetime, timezone
from typing import Optional

CHANNELS = ("stable", "canary")
DEFAULT_CHANNEL = "stable"
# Консервативный дефолт, если rollout.json к релизу не приложен или не скачался
DEFAULT_STABLE_DELAY_HOURS = 72
# Повтор той же версии после неудачной попытки (автооткат) — не раньше чем через N часов.
# Без этого сломанный релиз крутил бы build → healthcheck → rollback каждый час.
RETRY_SAME_VERSION_HOURS = 24


def parse_version(v) -> Optional[tuple]:
    """'v1.14.10' → (1, 14, 10). Не-числовые/пустые версии → None."""
    if not isinstance(v, str):
        return None
    v = v.strip().lstrip("vV")
    if not v:
        return None
    parts = v.split(".")
    try:
        nums = tuple(int(p) for p in parts)
    except ValueError:
        return None
    if any(n < 0 for n in nums):
        return None
    return nums


def is_newer(latest, current) -> bool:
    """True, если latest строго новее current. Числовое сравнение: 1.14.10 > 1.14.9.
    Нераспознанная версия с любой стороны → False (не обновляемся вслепую)."""
    a, b = parse_version(latest), parse_version(current)
    if a is None or b is None:
        return False
    # Выравниваем длину: 1.15 == 1.15.0
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


def device_bucket(device_id: str) -> int:
    """Корзина устройства 0..99. Без device_id — 99: обновится только на 100%."""
    if not device_id:
        return 99
    return int(hashlib.sha256(device_id.encode()).hexdigest(), 16) % 100


def parse_published_at(s) -> Optional[datetime]:
    """'2026-10-24T12:00:00Z' (формат GitHub API) → aware datetime UTC."""
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def validate_rollout(rollout) -> Optional[dict]:
    """Нормализует rollout.json. Невалидный → None (дальше работает консервативный дефолт)."""
    if not isinstance(rollout, dict):
        return None
    stages = rollout.get("stages")
    if not isinstance(stages, list) or not stages:
        return None
    norm = []
    for st in stages:
        if not isinstance(st, dict):
            return None
        try:
            h = float(st.get("after_hours"))
            p = int(st.get("percent"))
        except (TypeError, ValueError):
            return None
        if h < 0:
            return None
        norm.append({"after_hours": h, "percent": max(0, min(100, p))})
    norm.sort(key=lambda s: s["after_hours"])
    return {"paused": bool(rollout.get("paused", False)), "stages": norm}


def current_percent(rollout: dict, hours_since_publish: float) -> int:
    """Процент раскатки на текущий момент: последняя наступившая стадия.
    rollout — уже прошедший validate_rollout. До первой стадии — 0%."""
    pct = 0
    for st in rollout["stages"]:
        if hours_since_publish >= st["after_hours"]:
            pct = st["percent"]
    return pct


def decide_auto_update(*, current: str, latest: str, channel: str, device_id: str,
                       published_at, rollout, now: datetime,
                       last_attempt: Optional[dict] = None) -> tuple:
    """Решение авто-обновления. Возвращает (update: bool, reason: str).

    reason — человекочитаемое объяснение для лога [AUTO-UPDATE]: канал, корзина,
    процент, причина отказа. Ручная кнопка «Обновить» в UI эту функцию НЕ вызывает.
    last_attempt — {"version", "at"(iso)} из прошлой авто-попытки (см. main.py).
    """
    if not is_newer(latest, current):
        return False, f"нет новой версии (текущая {current}, последняя {latest or '?'})"

    if channel not in CHANNELS:
        channel = DEFAULT_CHANNEL
    bucket = device_bucket(device_id)
    ctx = f"канал={channel}, корзина={bucket}"

    # Защита от цикла update → rollback: та же версия уже пробовалась и мы всё ещё
    # на старой — значит был автооткат. Ждём RETRY_SAME_VERSION_HOURS.
    if isinstance(last_attempt, dict) and last_attempt.get("version") == latest:
        at = parse_published_at(last_attempt.get("at"))
        if at is not None:
            since = (now - at).total_seconds() / 3600
            if since < RETRY_SAME_VERSION_HOURS:
                return False, (f"{ctx}: v{latest} уже пробовали {since:.1f} ч назад "
                               f"(вероятно автооткат), повтор через {RETRY_SAME_VERSION_HOURS} ч")

    ro = validate_rollout(rollout)
    # Пауза — аварийный стоп раскатки: останавливает и канарейку тоже
    if ro is not None and ro["paused"]:
        return False, f"{ctx}: раскатка на паузе"

    if channel == "canary":
        return True, f"{ctx}: канарейка обновляется сразу"

    pub = parse_published_at(published_at)
    if pub is None:
        return False, f"{ctx}: у релиза нет published_at — ждём"
    hours = (now - pub).total_seconds() / 3600

    if ro is None:
        if hours >= DEFAULT_STABLE_DELAY_HOURS:
            return True, (f"{ctx}: rollout.json нет, прошло {hours:.1f} ч "
                          f">= {DEFAULT_STABLE_DELAY_HOURS} ч — обновляемся")
        return False, (f"{ctx}: rollout.json нет, прошло {hours:.1f} ч "
                       f"< {DEFAULT_STABLE_DELAY_HOURS} ч — ждём")

    pct = current_percent(ro, hours)
    if bucket < pct:
        return True, f"{ctx}, раскатка={pct}% ({hours:.1f} ч после публикации): обновляемся"
    return False, f"{ctx}, раскатка={pct}% ({hours:.1f} ч после публикации): корзина вне раскатки — ждём"
