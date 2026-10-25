"""Самопроверка шлюза (E2) и самолечение (E3).

Модуль разделён на две части:
- чистые функции (классификация DNS-ответов и задержек узлов, сборка checks,
  агрегатор overall, статусы узлов для дашборда, лестница самолечения) —
  без сети и часов, время передаётся параметром `now`; покрыты tests/unit;
- асинхронные пробы (DNS к LAN-IP шлюза, delay каждого узла через Mihomo,
  HTTP через mixed-port, прямой интернет) — вызываются из main.py.

Самолечение решает только «что делать»; выполняет действия main.py
(regen конфига, перечитать подписку, перезапуск ядра).
"""
import asyncio
import copy
import ipaddress
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Optional

# ── Константы проверок ────────────────────────────────────────────────────────

# Домены, которые провайдер подменял в plain-DNS (NXDOMAIN) — инцидент с YouTube/Instagram
DNS_BLOCKED_DOMAINS = ("www.youtube.com", "youtube.com", "redirector.googlevideo.com", "www.instagram.com")
# Контрольный RU-домен: если не резолвится и он — DNS шлюза не отвечает вообще
DNS_CONTROL_DOMAIN = "ya.ru"

# Ответ из этих сетей для публичного домена — заглушка/подмена, а не настоящий адрес.
# 198.18.0.0/15 (fake-ip Mihomo) намеренно не включён: при fake-ip это нормальный ответ.
DNS_STUB_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8",        # 0.0.0.0, 0.0.0.1 — типичная подмена фильтра
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
))
# Известные адреса страниц-заглушек провайдеров (блокировка по реестру РКН).
# Для доменов из DNS_BLOCKED_DOMAINS настоящий ответ — всегда адреса Google/Meta,
# поэтому ложных срабатываний этот список дать не может. Дополняется по мере находок.
DNS_RKN_STUB_IPS = frozenset({
    "95.167.13.50", "95.167.13.51",     # Ростелеком
    "62.33.207.196", "62.33.207.197",   # ТТК
})

# Протоколы поверх UDP: TCP-пинг по server_port для них бессмыслен
UDP_NODE_TYPES = frozenset({"hysteria2", "hysteria", "tuic", "wireguard"})

# URL для delay-теста узла. Первый — тот же, что у кнопки «Пинг» в UI;
# второй — запасной (gstatic через часть Reality-узлов не проходит, см. generate_config.py).
NODE_DELAY_URLS = ("https://www.google.com/", "http://www.gstatic.com/generate_204")
NODE_DELAY_TIMEOUT_MS = 5000
NODE_PROBE_CONCURRENCY = 6
TUNNEL_TEST_URL = "http://www.gstatic.com/generate_204"
DIRECT_TEST_URL = "https://ya.ru/"
# Запасные адреса для проверки прямого интернета без DNS
DIRECT_TCP_TARGETS = (("77.88.8.8", 53), ("1.1.1.1", 443))

NODE_CACHE_MAX_AGE = 900  # с: результат самопроверки старше — для дашборда «кэш пуст»

# ── Константы самолечения ────────────────────────────────────────────────────

HEAL_STREAK = 2                 # действие только после N провалов подряд (антифлап)
HEAL_COOLDOWN = {               # минимальный интервал между одинаковыми действиями, с
    "regen_config": 900,
    "refresh_subscription": 1800,
    "restart_core": 1800,       # не больше 1 рестарта ядра за 30 мин
}
HEAL_MAX_FAILED = 3             # после стольких неудачных действий подряд — пауза
HEAL_PAUSE = 6 * 3600           # пауза автодействий, с
HEAL_HISTORY_MAX = 20

ACTION_TITLES = {
    "regen_config": "перегенерация конфига + hot-reload",
    "refresh_subscription": "перечитать подписку",
    "restart_core": "перезапуск ядра Mihomo",
}


# ── Чистые функции: DNS ───────────────────────────────────────────────────────

def is_stub_ip(ip: str) -> bool:
    if ip in DNS_RKN_STUB_IPS:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # мусор вместо адреса — тоже не настоящий ответ
    return any(addr in net for net in DNS_STUB_NETWORKS)


def classify_dns_answer(rcode: Optional[str], ips: list, error: Optional[str] = None) -> dict:
    """Классифицирует ответ DNS. rcode — текст dnspython (NOERROR/NXDOMAIN/…),
    error — 'timeout' или текст ошибки, если ответа не было."""
    ips = [str(i) for i in (ips or [])]
    if error:
        status = "timeout" if error == "timeout" else "error"
        detail = "таймаут" if status == "timeout" else error[:120]
        return {"ok": False, "status": status, "ips": [], "detail": detail}
    rc = (rcode or "").upper()
    if rc == "NXDOMAIN":
        return {"ok": False, "status": "nxdomain", "ips": [], "detail": "NXDOMAIN"}
    if rc == "SERVFAIL":
        return {"ok": False, "status": "servfail", "ips": [], "detail": "SERVFAIL"}
    if rc != "NOERROR":
        return {"ok": False, "status": "error", "ips": [], "detail": rc or "нет rcode"}
    if not ips:
        return {"ok": False, "status": "empty", "ips": [], "detail": "нет A-записей"}
    stubs = [i for i in ips if is_stub_ip(i)]
    if stubs:
        return {"ok": False, "status": "stub", "ips": ips[:4], "detail": f"заглушка {stubs[0]}"}
    return {"ok": True, "status": "ok", "ips": ips[:4], "detail": ips[0]}


def build_dns_check(results: dict, server: str) -> dict:
    """results: {domain: classify_dns_answer(...)}. Контрольный домен — DNS_CONTROL_DOMAIN."""
    control = results.get(DNS_CONTROL_DOMAIN)
    failed = {d: r for d, r in results.items() if d != DNS_CONTROL_DOMAIN and not r.get("ok")}
    check = {"id": "dns_lan", "name": f"DNS по пути LAN-клиента ({server}:53)",
             "domains": results}
    if control is not None and not control.get("ok"):
        check.update(status="fail", severity="down",
                     detail=f"DNS шлюза не отвечает: {DNS_CONTROL_DOMAIN} → {control.get('detail')}")
    elif failed:
        parts = [f"{d} → {r.get('detail')}" for d, r in failed.items()]
        check.update(status="fail", severity="degraded",
                     detail="подмена/блокировка: " + "; ".join(parts))
    else:
        check.update(status="ok", detail="все домены резолвятся")
    return check


# ── Чистые функции: узлы ──────────────────────────────────────────────────────

def classify_node_delay(http_status: Optional[int], body: Optional[dict], error: Optional[str] = None) -> dict:
    """Ответ Mihomo GET /proxies/{tag}/delay → статус узла."""
    body = body if isinstance(body, dict) else {}
    if error:
        return {"status": "error", "delay": -1, "reason": f"Mihomo API недоступен: {error}"[:160]}
    delay = body.get("delay")
    if http_status == 200 and isinstance(delay, (int, float)) and delay > 0:
        return {"status": "ok", "delay": int(delay), "reason": ""}
    if http_status == 404:
        return {"status": "absent", "delay": -1, "reason": "узла нет в конфиге ядра"}
    msg = str(body.get("message") or "")
    if http_status in (408, 504) or "timeout" in msg.lower():
        return {"status": "timeout", "delay": -1, "reason": "таймаут: узел не ответил на delay-тест"}
    return {"status": "error", "delay": -1,
            "reason": (f"ошибка проверки через узел: {msg}" if msg else f"ошибка проверки (HTTP {http_status})")[:160]}


def node_protocol(node: dict) -> str:
    return str((node or {}).get("type") or "").lower()


def is_udp_node(node: dict) -> bool:
    return node_protocol(node) in UDP_NODE_TYPES


def build_nodes_check(nodes: list, results: dict) -> dict:
    total = len(nodes)
    alive = [n.get("tag") for n in nodes if (results.get(n.get("tag")) or {}).get("status") == "ok"]
    dead = {n.get("tag"): (results.get(n.get("tag")) or {}).get("reason") or "не проверен"
            for n in nodes if n.get("tag") not in alive}
    check = {"id": "nodes", "name": "Узлы (delay-тест каждого своим протоколом)",
             "alive": len(alive), "total": total, "dead": dead}
    if total == 0:
        check.update(status="fail", severity="down", detail="список узлов пуст")
    elif not alive:
        check.update(status="fail", severity="down", detail=f"все {total} узлов недоступны")
    elif dead:
        check.update(status="warn", severity="degraded",
                     detail=f"доступно {len(alive)}/{total}; недоступны: " + ", ".join(dead))
    else:
        check.update(status="ok", detail=f"доступно {total}/{total}")
    return check


def node_view(node: dict, cached: Optional[dict], now: float,
              history_delay: Optional[int] = None, max_age: float = NODE_CACHE_MAX_AGE) -> Optional[dict]:
    """Статус узла для дашборда — строго по его тегу.
    Возвращает None, если данных нет и допустим TCP-фолбэк (только TCP-протоколы)."""
    if cached and (now - float(cached.get("checked_at") or 0)) <= max_age:
        if cached.get("status") == "ok":
            return {"status": "online", "ping": int(cached.get("delay") or 0), "reason": "", "source": "selfcheck"}
        return {"status": "offline", "ping": -1, "reason": cached.get("reason") or "недоступен", "source": "selfcheck"}
    if history_delay and history_delay > 0:
        return {"status": "online", "ping": int(history_delay), "reason": "", "source": "mihomo-history"}
    if is_udp_node(node):
        return {"status": "unknown", "ping": -1,
                "reason": f"ещё не проверен ({node_protocol(node)} работает по UDP, TCP-пинг неприменим)",
                "source": "none"}
    return None


# ── Чистые функции: прочие checks и агрегатор ────────────────────────────────

def build_direct_check(direct: dict) -> dict:
    check = {"id": "direct_internet", "name": "Интернет напрямую (без туннеля)"}
    if direct.get("ok"):
        check.update(status="ok", detail=f"доступен ({direct.get('via')})")
    else:
        check.update(status="fail", severity="down", detail=f"нет прямого интернета: {direct.get('error') or '—'}")
    return check


def build_mihomo_check(api_ok: bool, error: Optional[str] = None) -> dict:
    check = {"id": "mihomo_api", "name": "Ядро Mihomo (API :9090)"}
    if api_ok:
        check.update(status="ok", detail="отвечает")
    else:
        check.update(status="fail", severity="down", detail=f"API не отвечает: {error or '—'}")
    return check


def build_tunnel_check(tunnel: dict, auto_now: Optional[str] = None) -> dict:
    check = {"id": "tunnel_http", "name": "Туннель: HTTP через mixed-port Mihomo (проверка туннеля, не DNS)",
             "auto_now": auto_now}
    if not tunnel.get("ok"):
        check.update(status="fail", severity="down",
                     detail=f"generate_204 через :2080 не прошёл: {tunnel.get('error') or tunnel.get('http_status')}")
    elif auto_now == "DIRECT":
        check.update(status="warn", severity="degraded",
                     detail="ответ получен, но группа auto ушла в DIRECT — узлы не используются")
    else:
        # Запрос идёт по правилам маршрутизации (generate_204 Google обычно уходит в
        # группу ai), поэтому auto указываем отдельно, а не как маршрут этого запроса
        check.update(status="ok", detail=f"{tunnel.get('ms')} мс" + (f"; группа auto сейчас → {auto_now}" if auto_now else ""))
    return check


def aggregate(checks: list) -> str:
    """overall: down — есть провал с severity=down; degraded — любой не-ok; иначе ok."""
    overall = "ok"
    for c in checks:
        st = c.get("status")
        if st in ("ok", "skip", None):
            continue
        if c.get("severity") == "down" and st == "fail":
            return "down"
        overall = "degraded"
    return overall


def build_facts(direct_ok: bool, mihomo_ok: bool, tunnel_ok: bool, dns_ok: bool,
                nodes: list, results: dict, core_proxies: Optional[set]) -> dict:
    tags = [n.get("tag") for n in nodes]
    return {
        "direct_ok": bool(direct_ok),
        "mihomo_ok": bool(mihomo_ok),
        "tunnel_ok": bool(tunnel_ok),
        "dns_ok": bool(dns_ok),
        "nodes_total": len(tags),
        "nodes_alive": sum(1 for t in tags if (results.get(t) or {}).get("status") == "ok"),
        "nodes_in_core": (sum(1 for t in tags if t in core_proxies) if core_proxies is not None else None),
    }


# ── Чистые функции: лестница самолечения ─────────────────────────────────────

def new_heal_state() -> dict:
    return {"streaks": {"empty": 0, "core": 0, "dns": 0}, "failed_in_row": 0,
            "paused_until": 0, "last_action": {}, "history": []}


def normalize_heal_state(state: Optional[dict]) -> dict:
    base = new_heal_state()
    if isinstance(state, dict):
        for k in base:
            v = state.get(k)
            if isinstance(base[k], (int, float)):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    base[k] = v
            elif isinstance(v, type(base[k])):
                base[k] = v
        base["streaks"] = {**new_heal_state()["streaks"], **base["streaks"]}
    return base


_STREAK_FOR_ACTION = {"refresh_subscription": "empty", "restart_core": "core", "regen_config": "dns"}


def decide_heal(state: dict, facts: dict, now: float) -> tuple:
    """Решает, какое действие выполнить. Возвращает (action|None, reason, new_state).
    Счётчики провалов обновляются всегда, даже когда действие не разрешено."""
    st = normalize_heal_state(copy.deepcopy(state))
    s = st["streaks"]

    if not facts.get("direct_ok"):
        # Нет интернета и напрямую — перезапуски не помогут, только фиксируем
        st["streaks"] = {k: 0 for k in s}
        return None, "нет интернета напрямую — только фиксируем", st

    nodes_total = facts.get("nodes_total") or 0
    in_core = facts.get("nodes_in_core")
    empty = nodes_total == 0 or (facts.get("mihomo_ok") and in_core == 0)
    core_dead = (not empty) and (not facts.get("mihomo_ok") or (facts.get("nodes_alive") or 0) == 0)
    dns_bad = (not facts.get("dns_ok")) and bool(facts.get("tunnel_ok"))

    s["empty"] = s["empty"] + 1 if empty else 0
    s["core"] = s["core"] + 1 if core_dead else 0
    s["dns"] = s["dns"] + 1 if dns_bad else 0

    healthy = (not empty and not core_dead and facts.get("dns_ok") and facts.get("tunnel_ok")
               and facts.get("mihomo_ok"))
    if healthy:
        st["failed_in_row"] = 0

    if st["paused_until"] > now:
        left = int((st["paused_until"] - now) / 60)
        return None, f"автодействия на паузе ещё {left} мин", st

    candidates = (
        ("refresh_subscription", "empty", "узлов нет (или их нет в ядре)"),
        ("restart_core", "core", "все узлы недоступны или ядро не отвечает при живом прямом интернете"),
        ("regen_config", "dns", "DNS провален при живом туннеле"),
    )
    for action, key, why in candidates:
        if s[key] < HEAL_STREAK:
            continue
        last = float(st["last_action"].get(action) or 0)
        cooldown = HEAL_COOLDOWN[action]
        if last and now - last < cooldown:
            wait = int((cooldown - (now - last)) / 60) + 1
            return None, f"{why}; {ACTION_TITLES[action]} — лимит, следующая попытка через {wait} мин", st
        return action, f"{why} ({s[key]} прогона подряд)", st
    if any(v > 0 for v in s.values()):
        return None, "провал зафиксирован, ждём подтверждения следующим прогоном", st
    return None, "", st


def record_heal_outcome(state: dict, action: str, ok: bool, now: float, detail: str = "") -> tuple:
    """Фиксирует результат действия. Возвращает (new_state, notify|None).
    notify — поля уведомления, если после HEAL_MAX_FAILED неудач подряд включена пауза."""
    st = normalize_heal_state(copy.deepcopy(state))
    st["last_action"][action] = now
    # После действия антифлап начинается заново: следующее — снова после N провалов
    st["streaks"][_STREAK_FOR_ACTION[action]] = 0
    st["history"] = (st["history"] + [{"ts": now, "action": action, "ok": bool(ok), "detail": detail[:200]}])[-HEAL_HISTORY_MAX:]
    notify = None
    if ok:
        st["failed_in_row"] = 0
    else:
        st["failed_in_row"] += 1
        if st["failed_in_row"] >= HEAL_MAX_FAILED:
            st["paused_until"] = now + HEAL_PAUSE
            notify = {"reason": f"{HEAL_MAX_FAILED} неудачных автодействия подряд, пауза 6 ч",
                      "step": action, "result": (detail or "fail")[:100]}
            st["failed_in_row"] = 0
    return st, notify


# ── Файлы ─────────────────────────────────────────────────────────────────────

def write_json_atomic(path: Path, data: dict):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def read_json_file(path: Path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


# ── Асинхронные пробы (сеть) ─────────────────────────────────────────────────

async def probe_dns_domain(server: str, domain: str, timeout: float = 3.0, attempts: int = 2,
                           port: int = 53) -> dict:
    """A-запрос к server:53 (dnspython уже в requirements). Таймаут/SERVFAIL повторяем
    один раз — одиночная потеря UDP не должна считаться поломкой."""
    try:
        import dns.asyncquery
        import dns.exception
        import dns.message
        import dns.rcode
        import dns.rdatatype
    except ImportError as e:
        return classify_dns_answer(None, [], f"dnspython недоступен: {e}")
    res = classify_dns_answer(None, [], "timeout")
    for _ in range(attempts):
        q = dns.message.make_query(domain, dns.rdatatype.A)
        try:
            resp = await dns.asyncquery.udp(q, server, timeout=timeout, port=port)
        except dns.exception.Timeout:
            res = classify_dns_answer(None, [], "timeout")
            continue
        except Exception as e:
            res = classify_dns_answer(None, [], f"{type(e).__name__}: {e}")
            continue
        ips = [rr.address for rrset in resp.answer if rrset.rdtype == dns.rdatatype.A for rr in rrset]
        res = classify_dns_answer(dns.rcode.to_text(resp.rcode()), ips)
        if res["ok"] or res["status"] in ("nxdomain", "stub", "empty"):
            break
    return res


async def probe_dns(server: str, port: int = 53) -> dict:
    domains = list(DNS_BLOCKED_DOMAINS) + [DNS_CONTROL_DOMAIN]
    results = await asyncio.gather(*(probe_dns_domain(server, d, port=port) for d in domains))
    return dict(zip(domains, results))


async def probe_nodes(nodes: list, api: str = "http://127.0.0.1:9090",
                      concurrency: int = NODE_PROBE_CONCURRENCY,
                      timeout_ms: int = NODE_DELAY_TIMEOUT_MS) -> dict:
    """Delay-тест КАЖДОГО тега через Mihomo (узел проверяется своим протоколом,
    включая Hysteria2 и узлы вне групп). Возвращает {tag: {...}}."""
    import httpx
    sem = asyncio.Semaphore(concurrency)
    out: dict = {}

    async def one(client, tag):
        enc = urllib.parse.quote(tag, safe="")
        res = None
        async with sem:
            for url in NODE_DELAY_URLS:
                try:
                    r = await client.get(f"{api}/proxies/{enc}/delay",
                                         params={"url": url, "timeout": timeout_ms})
                    try:
                        body = r.json()
                    except Exception:
                        body = {}
                    res = classify_node_delay(r.status_code, body)
                except Exception as e:
                    res = classify_node_delay(None, None, type(e).__name__)
                if res["status"] in ("ok", "absent"):
                    break
        res["checked_at"] = time.time()
        out[tag] = res

    async with httpx.AsyncClient(timeout=timeout_ms / 1000 + 3, trust_env=False) as client:
        await asyncio.gather(*(one(client, n.get("tag")) for n in nodes if n.get("tag")))
    return out


async def probe_mihomo(api: str = "http://127.0.0.1:9090") -> dict:
    """Ядро отвечает? + имена прокси в ядре + текущий выбор группы auto."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            r = await client.get(f"{api}/proxies")
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}", "proxies": None, "auto_now": None}
        proxies = r.json().get("proxies", {}) or {}
        auto_now = (proxies.get("auto") or {}).get("now")
        return {"ok": True, "error": None, "proxies": set(proxies.keys()), "auto_now": auto_now}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__, "proxies": None, "auto_now": None}


async def probe_tunnel_http(proxy: str = "http://127.0.0.1:2080", url: str = TUNNEL_TEST_URL,
                            timeout: float = 10.0) -> dict:
    import httpx
    try:
        start = time.time()
        # proxy= есть с httpx 0.26 (в образе 0.26.0), proxies= удалён в 0.28
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout, trust_env=False) as client:
            r = await client.get(url)
        ms = int((time.time() - start) * 1000)
        return {"ok": r.status_code in (200, 204), "http_status": r.status_code, "ms": ms,
                "error": None if r.status_code in (200, 204) else f"HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "http_status": None, "ms": None, "error": type(e).__name__}


async def _tcp_ok(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def probe_direct() -> dict:
    """Прямой интернет с хоста (трафик самого хоста в туннель не идёт)."""
    import httpx
    err = None
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            r = await client.get(DIRECT_TEST_URL)
        if r.status_code < 500:
            return {"ok": True, "via": DIRECT_TEST_URL, "error": None}
        err = f"HTTP {r.status_code}"
    except Exception as e:
        err = type(e).__name__
    for host, port in DIRECT_TCP_TARGETS:
        if await _tcp_ok(host, port):
            return {"ok": True, "via": f"tcp {host}:{port}", "error": None}
    return {"ok": False, "via": None, "error": err}


async def run_selfcheck(gateway_ip: str, nodes: list, api: str = "http://127.0.0.1:9090",
                        mixed_proxy: str = "http://127.0.0.1:2080", dns_port: int = 53) -> dict:
    """Один полный прогон самопроверки. Пробы идут параллельно."""
    dns_res, node_res, mihomo, tunnel, direct = await asyncio.gather(
        probe_dns(gateway_ip, dns_port), probe_nodes(nodes, api), probe_mihomo(api),
        probe_tunnel_http(mixed_proxy), probe_direct(),
    )
    dns_check = build_dns_check(dns_res, gateway_ip)
    checks = [
        build_direct_check(direct),
        build_mihomo_check(mihomo["ok"], mihomo["error"]),
        build_tunnel_check(tunnel, mihomo.get("auto_now")),
        dns_check,
        build_nodes_check(nodes, node_res),
    ]
    now = time.time()
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        "ts": now,
        "overall": aggregate(checks),
        "checks": checks,
        "nodes": node_res,
        "facts": build_facts(direct["ok"], mihomo["ok"], tunnel["ok"], dns_check["status"] == "ok",
                             nodes, node_res, mihomo.get("proxies")),
    }
