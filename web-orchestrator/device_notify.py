"""Служебные уведомления устройства через API GlobalShield.

Устройство не ходит в Telegram само: оно шлёт `POST {api}/devices/notify`
с заголовками X-Device-ID / X-Device-Token, а сообщение собирает и отправляет
backend. Тот же формат использует update-watcher.sh на хосте.
"""
import logging
from typing import Optional

import httpx

KINDS = ("feedback", "tunnel_restart", "update_failed", "update_ok", "auto_route")
TEXT_MAX = 2000
FIELDS_MAX = 20
FIELD_VALUE_MAX = 100


def build_notify_request(api_base: str, device: dict, kind: str, text: str = "",
                         fields: Optional[dict] = None) -> Optional[dict]:
    """Собирает запрос notify. None — устройство не зарегистрировано (нет id/токена)."""
    if kind not in KINDS:
        raise ValueError(f"unknown notify kind: {kind}")
    device_id = (device or {}).get("device_id", "")
    device_token = (device or {}).get("device_token", "")
    if not device_id or not device_token:
        return None
    clean = {}
    for key, value in (fields or {}).items():
        if value is None or value == "":
            continue
        clean[str(key)] = str(value)[:FIELD_VALUE_MAX]
        if len(clean) >= FIELDS_MAX:
            break
    return {
        "url": f"{api_base.rstrip('/')}/devices/notify",
        "json": {"kind": kind, "text": (text or "")[:TEXT_MAX], "fields": clean},
        "headers": {"X-Device-ID": device_id, "X-Device-Token": device_token},
    }


async def send_device_notify(api_base: str, device: dict, kind: str, text: str = "",
                             fields: Optional[dict] = None) -> bool:
    """Best-effort отправка. Никогда не бросает исключений; True — backend принял."""
    try:
        req = build_notify_request(api_base, device, kind, text, fields)
    except ValueError as e:
        logging.error(f"[notify] {e}")
        return False
    if req is None:
        logging.info(f"[notify] устройство не зарегистрировано — уведомление {kind} пропущено")
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(req["url"], json=req["json"], headers=req["headers"])
        if r.status_code != 200:
            logging.warning(f"[notify] {kind}: API ответил {r.status_code}")
            return False
        return True
    except Exception as e:
        logging.warning(f"[notify] {kind}: {e}")
        return False
