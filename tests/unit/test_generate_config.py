"""Offline-тесты tunnel-provider/generate_config.py: прогон на фикстурах и
проверка инвариантов сгенерированного Mihomo config.yaml.

Сеть не нужна: httpx.get подменяется ответом из tests/fixtures/subscription_proxies.yaml.
Пути /etc/gsg и /etc/mihomo переопределяются env GSG_CONFIG_DIR / GSG_MIHOMO_CONFIG.
"""
import importlib.util
import json
import shutil
from pathlib import Path

import httpx
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
GEN_PATH = REPO / "tunnel-provider" / "generate_config.py"
FIXTURE_FILES = ["rules.json", "devices.json", "nodes.json", "subscription.json",
                 "settings.json", "rulesets.json"]
BUILTIN_TARGETS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"}


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _run_generator(tmp_path, monkeypatch, *, subscription="ok", runetfreedom=True, drop=(), extra=None):
    """Готовит каталоги из фикстур, запускает main() генератора, возвращает (cfg, raw_yaml).
    extra — {имя_файла: путь} дополнительно положить в /etc/gsg (например бэкап прокси)."""
    gsg_dir = tmp_path / "gsg"
    mihomo_dir = tmp_path / "mihomo"
    gsg_dir.mkdir(parents=True)
    mihomo_dir.mkdir()
    for name in FIXTURE_FILES:
        if name not in drop:
            shutil.copy(FIXTURES / name, gsg_dir / name)
    for name, src in (extra or {}).items():
        shutil.copy(src, gsg_dir / name)
    if runetfreedom:
        (mihomo_dir / ".geosite-runetfreedom").touch()

    monkeypatch.setenv("GSG_CONFIG_DIR", str(gsg_dir))
    monkeypatch.setenv("GSG_MIHOMO_CONFIG", str(mihomo_dir / "config.yaml"))
    monkeypatch.setenv("GSG_TPROXY_PORT", "12345")

    sub_text = (FIXTURES / "subscription_proxies.yaml").read_text()

    def fake_get(url, *a, **kw):
        assert "example.invalid" in url, f"неожиданный сетевой запрос: {url}"
        if subscription == "down":
            raise httpx.ConnectError("подписка недоступна (тест)")
        return _FakeResp(sub_text)

    monkeypatch.setattr(httpx, "get", fake_get)

    # Свежий импорт на каждый тест: пути считаются на уровне модуля из env
    spec = importlib.util.spec_from_file_location("gsg_generate_config", GEN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.main()

    raw = (mihomo_dir / "config.yaml").read_text()
    return yaml.safe_load(raw), raw


def _rule_target(rule: str) -> str:
    parts = rule.split(",")
    if parts[0] == "MATCH":
        return parts[1]
    if parts[0] == "SUB-RULE":
        return rule.rsplit(",", 1)[1]
    return parts[2]


def _assert_invariants(cfg, raw):
    # 1. DNS: основные резолверы только DoH — plain UDP перехватывается фильтром провайдера
    ns = cfg["dns"]["nameserver"]
    assert ns, "dns.nameserver пуст"
    assert all(isinstance(s, str) and s.startswith("https://") for s in ns), ns

    # 2. Адрес боевого устройства не должен утекать в конфиг
    assert "10.10.1.139" not in raw

    rules = cfg["rules"]
    # 3. Catch-all MATCH есть и стоит последним
    assert any(r.startswith("MATCH,") for r in rules)
    assert rules[-1].startswith("MATCH,")

    # 4. GEOIP,ru,DIRECT раньше любых per-device SRC-IP-CIDR правил
    geo_idx = next(i for i, r in enumerate(rules) if r.startswith("GEOIP,ru,DIRECT"))
    dev_idx = [i for i, r in enumerate(rules)
               if r.startswith("SRC-IP-CIDR,") or r.startswith("SUB-RULE,(SRC-IP-CIDR")]
    assert dev_idx, "нет per-device правил — фикстура devices.json не применилась"
    assert geo_idx < min(dev_idx)

    # 5. Proxy-группы ссылаются только на существующие прокси/группы
    proxy_names = {p["name"] for p in cfg["proxies"]}
    group_names = {g["name"] for g in cfg["proxy-groups"]}
    known = proxy_names | group_names | BUILTIN_TARGETS
    for g in cfg["proxy-groups"]:
        missing = [p for p in g.get("proxies", []) if p not in known]
        assert not missing, f"группа {g['name']} ссылается на несуществующие: {missing}"

    # 6. Цели правил (и sub-rules) существуют — иначе Mihomo не загрузит конфиг
    sub_rules = cfg.get("sub-rules", {})
    for r in rules:
        t = _rule_target(r)
        if r.startswith("SUB-RULE,"):
            assert t in sub_rules, f"{r}: sub-rule {t} не определён"
        else:
            assert t in known, f"{r}: цель {t} не существует"
    for name, sr in sub_rules.items():
        for r in sr:
            assert _rule_target(r) in known, f"sub-rule {name}: {r}"


def test_invariants_with_subscription(tmp_path, monkeypatch):
    cfg, raw = _run_generator(tmp_path, monkeypatch)
    _assert_invariants(cfg, raw)
    # sanity: фикстуры реально отработали
    rules = cfg["rules"]
    assert "SRC-IP-CIDR,192.168.1.104/32,REJECT" in rules
    assert "SRC-IP-CIDR,192.168.1.103/32,DIRECT" in rules
    assert "SRC-IP-CIDR,192.168.1.102/32,ai" in rules
    assert "SUB-RULE,(SRC-IP-CIDR,192.168.1.101/32),smart_192_168_1_101" in rules
    assert "GEOSITE,ru-available-only-inside,DIRECT" in rules
    assert any(g["name"] == "user_streaming" for g in cfg["proxy-groups"])
    # route_overrides на несуществующие узел/группу отбрасываются
    assert not any("missing-node.example" in r or "missing-group.example" in r for r in rules)
    # Секреты/URL подписки в конфиг не попадают
    assert "TEST-NOT-A-REAL-TOKEN" not in raw


def test_invariants_without_runetfreedom_marker(tmp_path, monkeypatch):
    cfg, raw = _run_generator(tmp_path, monkeypatch, runetfreedom=False)
    _assert_invariants(cfg, raw)
    assert "GEOSITE,ru-available-only-inside,DIRECT" not in cfg["rules"]


def test_invariants_empty_install(tmp_path, monkeypatch):
    """Свежая установка: ни подписки, ни узлов, ни правил — конфиг всё равно валиден."""
    cfg, raw = _run_generator(tmp_path, monkeypatch,
                              drop=("rules.json", "nodes.json", "subscription.json"))
    assert [p["name"] for p in cfg["proxies"]] == ["GSG-FALLBACK"]
    _assert_invariants(cfg, raw)


def test_subscription_down_restores_backup(tmp_path, monkeypatch):
    """Подписка недоступна, но есть proxies_backup.yaml — узлы восстанавливаются из бэкапа."""
    first, _ = _run_generator(tmp_path / "first", monkeypatch)
    backup = tmp_path / "first" / "gsg" / "proxies_backup.yaml"
    assert backup.exists(), "успешный fetch должен сохранять бэкап прокси"

    cfg, raw = _run_generator(tmp_path / "second", monkeypatch, subscription="down",
                              extra={"proxies_backup.yaml": backup})
    _assert_invariants(cfg, raw)
    assert {p["name"] for p in cfg["proxies"]} == {p["name"] for p in first["proxies"]}


@pytest.mark.xfail(strict=True, reason=(
    "Известный дефект: подписка недоступна, бэкапа proxies_backup.yaml и старого "
    "config.yaml нет, а nodes.json непустой → proxies=[GSG-FALLBACK], но группы "
    "ссылаются на имена узлов из nodes.json, и Mihomo такой конфиг не примет. "
    "Поведение генератора в этой задаче не меняем — тест фиксирует дефект."))
def test_subscription_down_without_backup(tmp_path, monkeypatch):
    cfg, raw = _run_generator(tmp_path, monkeypatch, subscription="down")
    _assert_invariants(cfg, raw)
