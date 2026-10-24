"""Unit-тесты web-orchestrator/update_policy.py: сравнение версий и staged rollout."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "web-orchestrator"))
import update_policy as up  # noqa: E402

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
ROLLOUT = {"paused": False, "stages": [{"after_hours": 0, "percent": 0},
                                       {"after_hours": 24, "percent": 10},
                                       {"after_hours": 72, "percent": 100}]}


def _pub(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _id_with_bucket(pred):
    """Подбирает device_id, корзина которого удовлетворяет pred."""
    for i in range(10000):
        did = f"GSG-TEST{i:06d}"
        if pred(up.device_bucket(did)):
            return did
    raise AssertionError("не нашли device_id")


@pytest.mark.parametrize("latest,current,expected", [
    ("1.14.10", "1.14.9", True),     # строковое сравнение давало False
    ("1.14.9", "1.14.10", False),
    ("1.14.1", "1.14.1", False),
    ("v1.15.0", "1.14.99", True),
    ("2.0.0", "1.99.99", True),
    ("1.15", "1.15.0", False),
    ("1.15.1", "1.15", True),
    ("", "1.14.1", False),
    (None, "1.14.1", False),
    ("1.15.0-rc1", "1.14.1", False),  # нераспознанное — не обновляемся вслепую
    ("1.15.0", "dev", False),
])
def test_is_newer(latest, current, expected):
    assert up.is_newer(latest, current) is expected


def test_device_bucket_stable_and_in_range():
    assert up.device_bucket("GSG-AABBCCDDEEFF") == up.device_bucket("GSG-AABBCCDDEEFF")
    buckets = {up.device_bucket(f"GSG-{i}") for i in range(2000)}
    assert min(buckets) >= 0 and max(buckets) <= 99
    assert len(buckets) == 100  # распределение покрывает все корзины
    assert up.device_bucket("") == 99


def test_bucket_formula_matches_spec():
    import hashlib
    did = "GSG-0123456789AB"
    assert up.device_bucket(did) == int(hashlib.sha256(did.encode()).hexdigest(), 16) % 100


def _decide(**kw):
    args = dict(current="1.14.1", latest="1.15.0", channel="stable", device_id="GSG-X",
                published_at=_pub(1), rollout=ROLLOUT, now=NOW, last_attempt=None)
    args.update(kw)
    return up.decide_auto_update(**args)


def test_no_update_when_not_newer():
    ok, reason = _decide(latest="1.14.1")
    assert not ok and "нет новой версии" in reason


def test_canary_updates_immediately():
    ok, reason = _decide(channel="canary", published_at=_pub(0), rollout=None)
    assert ok and "канарейка" in reason


def test_stable_first_stage_zero_percent():
    ok, reason = _decide(published_at=_pub(1), device_id=_id_with_bucket(lambda b: b == 0))
    assert not ok and "раскатка=0%" in reason


def test_stable_partial_stage_by_bucket():
    inside = _id_with_bucket(lambda b: b < 10)
    outside = _id_with_bucket(lambda b: b >= 10)
    ok_in, r_in = _decide(published_at=_pub(30), device_id=inside)
    ok_out, r_out = _decide(published_at=_pub(30), device_id=outside)
    assert ok_in and "раскатка=10%" in r_in
    assert not ok_out and "корзина вне раскатки" in r_out
    assert "канал=stable" in r_out and "корзина=" in r_out


def test_stable_full_stage():
    ok, _ = _decide(published_at=_pub(80), device_id=_id_with_bucket(lambda b: b == 99))
    assert ok


def test_paused_blocks_stable():
    ok, reason = _decide(published_at=_pub(200), rollout={**ROLLOUT, "paused": True})
    assert not ok and "пауз" in reason


@pytest.mark.parametrize("rollout", [None, {}, {"stages": []}, {"stages": [{"after_hours": "x"}]}, "garbage"])
def test_missing_or_bad_rollout_conservative_default(rollout):
    ok_early, r = _decide(published_at=_pub(71), rollout=rollout)
    ok_late, _ = _decide(published_at=_pub(73), rollout=rollout)
    assert not ok_early and "rollout.json нет" in r
    assert ok_late


def test_unknown_channel_falls_back_to_stable():
    ok, reason = _decide(channel="beta", published_at=_pub(1), rollout=None)
    assert not ok and "канал=stable" in reason


def test_no_published_at_waits():
    ok, _ = _decide(published_at=None)
    assert not ok


def test_retry_backoff_after_failed_attempt():
    recent = {"version": "1.15.0", "at": (NOW - timedelta(hours=2)).isoformat()}
    old = {"version": "1.15.0", "at": (NOW - timedelta(hours=25)).isoformat()}
    other = {"version": "1.14.5", "at": (NOW - timedelta(hours=1)).isoformat()}
    assert _decide(channel="canary", last_attempt=recent)[0] is False
    assert _decide(channel="canary", last_attempt=old)[0] is True
    assert _decide(channel="canary", last_attempt=other)[0] is True


def test_stages_unsorted_and_clamped():
    ro = {"stages": [{"after_hours": 72, "percent": 150}, {"after_hours": 0, "percent": -5}]}
    norm = up.validate_rollout(ro)
    assert norm["stages"][0] == {"after_hours": 0.0, "percent": 0}
    assert up.current_percent(norm, 100) == 100
    assert up.current_percent(norm, 10) == 0


def test_main_py_uses_numeric_compare():
    """main.py импортировать в CI тяжело (детект LAN-IP, docker) — проверяем исходник:
    строковых сравнений версий не осталось, обе точки используют is_newer."""
    src = (Path(__file__).resolve().parents[2] / "web-orchestrator" / "main.py").read_text()
    assert "> GSG_VERSION" not in src
    assert src.count("is_newer(latest, GSG_VERSION)") == 2


def test_release_and_rollout_scripts_default_matches_policy():
    """Дефолтный rollout.json из release.sh и rollout.sh валиден и одинаков."""
    import json
    import re
    root = Path(__file__).resolve().parents[2]
    found = []
    for script in ("release.sh", "rollout.sh"):
        src = (root / script).read_text()
        m = re.search(r"<<'JSON'\n(.*?)\nJSON\n", src, re.S)
        assert m, f"{script}: heredoc с дефолтным rollout.json не найден"
        ro = up.validate_rollout(json.loads(m.group(1)))
        assert ro is not None and ro["paused"] is False
        found.append(ro)
    assert found[0] == found[1]
    assert [s["percent"] for s in found[0]["stages"]] == [0, 10, 100]
    assert up.current_percent(found[0], 72) == 100
