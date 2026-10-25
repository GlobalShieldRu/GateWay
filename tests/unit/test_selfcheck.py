"""Unit-тесты web-orchestrator/selfcheck.py: классификация DNS и delay, агрегатор health,
статусы узлов по тегу, лестница самолечения (антифлап, лимиты, пауза). Без сети;
время передаётся параметром now."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "web-orchestrator"))
import selfcheck as sc  # noqa: E402

T0 = 1_800_000_000.0


# ── DNS ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rcode,ips,error,status,ok", [
    ("NOERROR", ["142.250.74.46"], None, "ok", True),
    ("NXDOMAIN", [], None, "nxdomain", False),        # инцидент: подмена провайдером
    ("SERVFAIL", [], None, "servfail", False),
    (None, [], "timeout", "timeout", False),
    (None, [], "OSError: boom", "error", False),
    ("REFUSED", [], None, "error", False),
    ("NOERROR", [], None, "empty", False),
    ("NOERROR", ["0.0.0.0"], None, "stub", False),
    ("NOERROR", ["0.0.0.1"], None, "stub", False),
    ("NOERROR", ["127.0.0.1"], None, "stub", False),
    ("NOERROR", ["127.12.0.5"], None, "stub", False),
    ("NOERROR", ["95.167.13.50"], None, "stub", False),  # известная RKN-заглушка
    ("NOERROR", ["10.1.2.3"], None, "stub", False),
    ("NOERROR", ["198.18.0.7"], None, "ok", True),       # fake-ip Mihomo — не заглушка
    ("NOERROR", ["142.250.74.46", "0.0.0.0"], None, "stub", False),
])
def test_classify_dns_answer(rcode, ips, error, status, ok):
    r = sc.classify_dns_answer(rcode, ips, error)
    assert r["status"] == status
    assert r["ok"] is ok


def _dns_all_ok():
    return {d: sc.classify_dns_answer("NOERROR", ["8.8.4.4"]) for d in
            list(sc.DNS_BLOCKED_DOMAINS) + [sc.DNS_CONTROL_DOMAIN]}


def test_dns_check_ok():
    c = sc.build_dns_check(_dns_all_ok(), "192.168.1.1")
    assert c["status"] == "ok"
    assert "192.168.1.1:53" in c["name"]


def test_dns_check_blocked_domain_is_degraded():
    res = _dns_all_ok()
    res["www.youtube.com"] = sc.classify_dns_answer("NXDOMAIN", [])
    c = sc.build_dns_check(res, "192.168.1.1")
    assert c["status"] == "fail" and c["severity"] == "degraded"
    assert "www.youtube.com" in c["detail"]


def test_dns_check_control_failed_is_down():
    res = _dns_all_ok()
    res[sc.DNS_CONTROL_DOMAIN] = sc.classify_dns_answer(None, [], "timeout")
    c = sc.build_dns_check(res, "192.168.1.1")
    assert c["status"] == "fail" and c["severity"] == "down"


# ── delay узлов ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("http,body,err,status", [
    (200, {"delay": 123}, None, "ok"),
    (200, {"delay": 0}, None, "error"),
    (404, {"message": "resource not found"}, None, "absent"),
    (504, {"message": "Timeout"}, None, "timeout"),
    (408, {"message": "Timeout"}, None, "timeout"),
    (503, {"message": "An error occurred in the delay test"}, None, "error"),
    (None, None, "ConnectError", "error"),
])
def test_classify_node_delay(http, body, err, status):
    r = sc.classify_node_delay(http, body, err)
    assert r["status"] == status
    assert (r["delay"] > 0) is (status == "ok")
    if status != "ok":
        assert r["reason"]


NODES = [
    {"tag": "🇸🇪 Stockholm | VK", "type": "vless", "server": "se1.example", "server_port": 443},
    {"tag": "⚡ Stockholm", "type": "hysteria2", "server": "se1.example", "server_port": 443},
    {"tag": "🇬🇧 London", "type": "vless", "server": "uk.example", "server_port": 443},  # вне групп
]


def test_node_view_hysteria_independent_from_vless_same_server():
    cache = {
        "🇸🇪 Stockholm | VK": {"status": "ok", "delay": 80, "checked_at": T0},
        "⚡ Stockholm": {"status": "timeout", "delay": -1, "reason": "таймаут", "checked_at": T0},
    }
    vless = sc.node_view(NODES[0], cache[NODES[0]["tag"]], T0 + 10)
    hy = sc.node_view(NODES[1], cache[NODES[1]["tag"]], T0 + 10)
    assert vless["status"] == "online" and vless["ping"] == 80
    # Тот же server, но свой статус: не копирует пинг VLESS
    assert hy["status"] == "offline" and hy["reason"] == "таймаут"


def test_node_view_hysteria_without_cache_is_unknown_not_tcp():
    v = sc.node_view(NODES[1], None, T0)
    assert v is not None and v["status"] == "unknown"
    assert "UDP" in v["reason"]


def test_node_view_vless_without_cache_allows_tcp_fallback():
    assert sc.node_view(NODES[0], None, T0) is None


def test_node_view_node_outside_groups_uses_own_selfcheck():
    """London вне групп: истории Mihomo нет никогда, но самопроверка его проверила."""
    cached = {"status": "ok", "delay": 140, "checked_at": T0}
    v = sc.node_view(NODES[2], cached, T0 + 60, history_delay=None)
    assert v == {"status": "online", "ping": 140, "reason": "", "source": "selfcheck"}


def test_node_view_stale_cache_falls_back():
    cached = {"status": "ok", "delay": 140, "checked_at": T0}
    assert sc.node_view(NODES[2], cached, T0 + sc.NODE_CACHE_MAX_AGE + 1) is None
    v = sc.node_view(NODES[2], cached, T0 + sc.NODE_CACHE_MAX_AGE + 1, history_delay=99)
    assert v["source"] == "mihomo-history" and v["ping"] == 99


def test_nodes_check_counts():
    res = {NODES[0]["tag"]: {"status": "ok"}, NODES[1]["tag"]: {"status": "timeout", "reason": "таймаут"}}
    c = sc.build_nodes_check(NODES, res)
    assert c["status"] == "warn" and c["alive"] == 1 and c["total"] == 3
    assert set(c["dead"]) == {NODES[1]["tag"], NODES[2]["tag"]}
    assert sc.build_nodes_check(NODES, {})["severity"] == "down"
    assert sc.build_nodes_check([], {})["detail"] == "список узлов пуст"


# ── агрегатор ─────────────────────────────────────────────────────────────────

def test_aggregate():
    ok = {"status": "ok"}
    assert sc.aggregate([ok, ok]) == "ok"
    assert sc.aggregate([ok, {"status": "skip"}]) == "ok"
    assert sc.aggregate([ok, {"status": "warn", "severity": "degraded"}]) == "degraded"
    assert sc.aggregate([{"status": "fail", "severity": "degraded"}]) == "degraded"
    assert sc.aggregate([{"status": "warn", "severity": "degraded"},
                         {"status": "fail", "severity": "down"}]) == "down"


def test_tunnel_check_via_direct_is_degraded():
    c = sc.build_tunnel_check({"ok": True, "ms": 50}, auto_now="DIRECT")
    assert c["status"] == "warn"
    assert "не DNS" in c["name"]
    assert sc.build_tunnel_check({"ok": False, "error": "ConnectError"})["severity"] == "down"


def test_build_facts():
    res = {NODES[0]["tag"]: {"status": "ok"}}
    f = sc.build_facts(True, True, True, False, NODES, res, {NODES[0]["tag"], "auto"})
    assert f == {"direct_ok": True, "mihomo_ok": True, "tunnel_ok": True, "dns_ok": False,
                 "nodes_total": 3, "nodes_alive": 1, "nodes_in_core": 1}
    assert sc.build_facts(True, False, False, False, NODES, {}, None)["nodes_in_core"] is None


# ── лестница самолечения ─────────────────────────────────────────────────────

GOOD = {"direct_ok": True, "mihomo_ok": True, "tunnel_ok": True, "dns_ok": True,
        "nodes_total": 3, "nodes_alive": 3, "nodes_in_core": 3}


def F(**kw):
    return {**GOOD, **kw}


def test_healthy_no_action():
    a, reason, st = sc.decide_heal(sc.new_heal_state(), GOOD, T0)
    assert a is None and reason == ""


def test_dns_fail_needs_two_runs():
    st = sc.new_heal_state()
    a, _, st = sc.decide_heal(st, F(dns_ok=False), T0)
    assert a is None
    a, _, st = sc.decide_heal(st, F(dns_ok=False), T0 + 300)
    assert a == "regen_config"


def test_dns_flap_resets_streak():
    st = sc.new_heal_state()
    _, _, st = sc.decide_heal(st, F(dns_ok=False), T0)
    _, _, st = sc.decide_heal(st, GOOD, T0 + 300)
    a, _, st = sc.decide_heal(st, F(dns_ok=False), T0 + 600)
    assert a is None


def test_dns_fail_with_dead_tunnel_is_not_regen():
    st = sc.new_heal_state()
    for i in range(3):
        a, _, st = sc.decide_heal(st, F(dns_ok=False, tunnel_ok=False), T0 + i * 300)
        assert a is None


def test_all_nodes_dead_restarts_core():
    st = sc.new_heal_state()
    _, _, st = sc.decide_heal(st, F(nodes_alive=0, tunnel_ok=False), T0)
    a, _, st = sc.decide_heal(st, F(nodes_alive=0, tunnel_ok=False), T0 + 300)
    assert a == "restart_core"


def test_mihomo_api_down_restarts_core():
    st = sc.new_heal_state()
    facts = F(mihomo_ok=False, tunnel_ok=False, nodes_alive=0, nodes_in_core=None)
    _, _, st = sc.decide_heal(st, facts, T0)
    a, _, st = sc.decide_heal(st, facts, T0 + 300)
    assert a == "restart_core"


def test_no_direct_internet_never_acts():
    st = sc.new_heal_state()
    facts = F(direct_ok=False, nodes_alive=0, dns_ok=False, tunnel_ok=False)
    for i in range(5):
        a, reason, st = sc.decide_heal(st, facts, T0 + i * 300)
        assert a is None and "нет интернета" in reason
    assert all(v == 0 for v in st["streaks"].values())


def test_empty_nodes_refresh_subscription():
    st = sc.new_heal_state()
    _, _, st = sc.decide_heal(st, F(nodes_total=0, nodes_alive=0, nodes_in_core=0), T0)
    a, _, st = sc.decide_heal(st, F(nodes_total=0, nodes_alive=0, nodes_in_core=0), T0 + 300)
    assert a == "refresh_subscription"


def test_nodes_missing_in_core_is_empty_not_restart():
    """Ядро работает на GSG-FALLBACK (узлов из nodes.json в нём нет) — перечитываем подписку."""
    st = sc.new_heal_state()
    facts = F(nodes_alive=0, nodes_in_core=0)
    _, _, st = sc.decide_heal(st, facts, T0)
    a, _, st = sc.decide_heal(st, facts, T0 + 300)
    assert a == "refresh_subscription"


def _restart_once(st, t):
    dead = F(nodes_alive=0)
    _, _, st = sc.decide_heal(st, dead, t)
    a, _, st = sc.decide_heal(st, dead, t + 300)
    return a, st


def test_core_restart_at_most_once_per_30_min():
    st = sc.new_heal_state()
    a, st = _restart_once(st, T0)
    assert a == "restart_core"
    st, _ = sc.record_heal_outcome(st, "restart_core", True, T0 + 400)
    # Снова все мертвы: два прогона есть, но 30 мин не прошло
    a, st = _restart_once(st, T0 + 700)
    assert a is None
    a, reason, st = sc.decide_heal(st, F(nodes_alive=0), T0 + 1500)
    assert a is None and "лимит" in reason
    a, _, st = sc.decide_heal(st, F(nodes_alive=0), T0 + 400 + 1801)
    assert a == "restart_core"


def test_action_resets_streak_antiflap():
    st = sc.new_heal_state()
    _, _, st = sc.decide_heal(st, F(dns_ok=False), T0)
    a, _, st = sc.decide_heal(st, F(dns_ok=False), T0 + 300)
    assert a == "regen_config"
    st, _ = sc.record_heal_outcome(st, "regen_config", False, T0 + 320)
    assert st["streaks"]["dns"] == 0


def test_three_failures_pause_6h_and_notify():
    st = sc.new_heal_state()
    t = T0
    notify = None
    for i in range(3):
        _, _, st = sc.decide_heal(st, F(dns_ok=False), t)
        a, _, st = sc.decide_heal(st, F(dns_ok=False), t + 300)
        assert a == "regen_config", i
        st, notify = sc.record_heal_outcome(st, a, False, t + 320, "DNS всё ещё NXDOMAIN")
        if i < 2:
            assert notify is None
        t += sc.HEAL_COOLDOWN["regen_config"] + 400
    assert notify and set(notify) == {"reason", "step", "result"}
    assert notify["step"] == "regen_config"
    paused_from = t - sc.HEAL_COOLDOWN["regen_config"] - 400 + 320
    assert st["paused_until"] == paused_from + sc.HEAL_PAUSE
    # На паузе — никаких действий, даже при подтверждённом провале
    for k in range(4):
        a, reason, st = sc.decide_heal(st, F(nodes_alive=0), t + k * 300)
        assert a is None and "паузе" in reason
    # Пауза кончилась — действия возобновляются
    a, _, st = sc.decide_heal(st, F(nodes_alive=0), paused_from + sc.HEAL_PAUSE + 1)
    assert a == "restart_core"


def test_success_resets_failure_counter():
    st = sc.new_heal_state()
    st, _ = sc.record_heal_outcome(st, "regen_config", False, T0)
    st, _ = sc.record_heal_outcome(st, "regen_config", False, T0 + 1000)
    st, _ = sc.record_heal_outcome(st, "restart_core", True, T0 + 2000)
    assert st["failed_in_row"] == 0
    st, n = sc.record_heal_outcome(st, "regen_config", False, T0 + 3000)
    assert n is None and st["failed_in_row"] == 1


def test_priority_empty_over_core_over_dns():
    st = sc.new_heal_state()
    facts = F(nodes_total=0, nodes_alive=0, nodes_in_core=0, dns_ok=False)
    _, _, st = sc.decide_heal(st, facts, T0)
    a, _, st = sc.decide_heal(st, facts, T0 + 300)
    assert a == "refresh_subscription"


def test_decide_does_not_mutate_input_and_normalizes_garbage():
    st = sc.new_heal_state()
    snapshot = repr(st)
    sc.decide_heal(st, F(dns_ok=False), T0)
    assert repr(st) == snapshot
    a, _, st2 = sc.decide_heal({"streaks": "garbage", "paused_until": "x"}, GOOD, T0)
    assert a is None and st2["streaks"]["dns"] == 0


def test_history_is_bounded():
    st = sc.new_heal_state()
    for i in range(sc.HEAL_HISTORY_MAX + 5):
        st, _ = sc.record_heal_outcome(st, "regen_config", True, T0 + i)
    assert len(st["history"]) == sc.HEAL_HISTORY_MAX


def test_state_survives_json_roundtrip():
    """Состояние хранится в .selfheal_state.json: float-время не должно теряться."""
    import json
    st = sc.new_heal_state()
    for i in range(3):
        st, _ = sc.record_heal_outcome(st, "regen_config", False, T0 + 0.5 + i)
    restored = sc.normalize_heal_state(json.loads(json.dumps(st)))
    assert restored["paused_until"] == st["paused_until"] > T0
    assert restored["last_action"] == st["last_action"]
    a, reason, _ = sc.decide_heal(restored, F(nodes_alive=0), T0 + 100)
    assert a is None and "паузе" in reason
