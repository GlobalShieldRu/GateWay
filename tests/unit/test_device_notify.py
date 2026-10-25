"""Уведомления устройства идут только через API GlobalShield.

1. В репозитории нет прямых вызовов Telegram Bot API и строк, похожих на токен бота
   (кроме release.sh — скрипта мейнтейнера, и его .release.env.example).
2. web-orchestrator/device_notify.py собирает корректный запрос notify.
"""
import asyncio
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "web-orchestrator"))
import device_notify as dn  # noqa: E402

# Склеено, чтобы сам тест не попадал под собственную проверку
TG_API = "api.telegram" + ".org"
TOKEN_RE = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")
ALLOWED = {"release.sh", ".release.env.example"}


def _repo_files():
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split("\n")
        files = [ROOT / f for f in out if f]
    except (OSError, subprocess.CalledProcessError):
        files = [p for p in ROOT.rglob("*") if ".git" not in p.parts]
    for path in files:
        rel = path.relative_to(ROOT)
        if rel.name in ALLOWED or rel.parts[0] == "tests" or not path.is_file():
            continue
        yield rel, path


def test_no_telegram_api_or_bot_token_in_repo():
    hits = []
    for rel, path in _repo_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if TG_API in text:
            hits.append(f"{rel}: {TG_API}")
        if TOKEN_RE.search(text):
            hits.append(f"{rel}: похоже на токен бота")
    assert not hits, "\n".join(hits)


DEVICE = {"device_id": "GSG-abc123", "device_token": "tok-xyz"}


def test_build_request():
    req = dn.build_notify_request("https://api.globalshield.ru/v1/", DEVICE, "tunnel_restart",
                                  "health-check", {"source": "web-orchestrator", "empty": "", "none": None})
    assert req["url"] == "https://api.globalshield.ru/v1/devices/notify"
    assert req["headers"] == {"X-Device-ID": "GSG-abc123", "X-Device-Token": "tok-xyz"}
    assert req["json"] == {"kind": "tunnel_restart", "text": "health-check",
                           "fields": {"source": "web-orchestrator"}}


def test_build_request_limits():
    fields = {f"k{i}": "v" * 500 for i in range(30)}
    req = dn.build_notify_request("https://x/v1", DEVICE, "feedback", "m" * 5000, fields)
    assert len(req["json"]["text"]) == dn.TEXT_MAX
    assert len(req["json"]["fields"]) == dn.FIELDS_MAX
    assert all(len(v) == dn.FIELD_VALUE_MAX for v in req["json"]["fields"].values())


@pytest.mark.parametrize("device", [{}, None, {"device_id": "x"}, {"device_token": "y"}])
def test_unregistered_device_skipped(device):
    assert dn.build_notify_request("https://x/v1", device, "update_ok") is None
    assert asyncio.run(dn.send_device_notify("https://x/v1", device, "update_ok")) is False


def test_unknown_kind():
    with pytest.raises(ValueError):
        dn.build_notify_request("https://x/v1", DEVICE, "html")
    assert asyncio.run(dn.send_device_notify("https://x/v1", DEVICE, "html")) is False
