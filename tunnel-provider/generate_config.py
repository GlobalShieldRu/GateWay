import os, json, yaml, httpx, time, re
from pathlib import Path

GSG_CONFIG_DIR = Path("/etc/gsg")
GSG_DEVICES_FILE = GSG_CONFIG_DIR / "devices.json"
GSG_RULES_FILE = GSG_CONFIG_DIR / "rules.json"
GSG_RULESETS_FILE = GSG_CONFIG_DIR / "rulesets.json"
GSG_SUBSCRIPTION_FILE = GSG_CONFIG_DIR / "subscription.json"
GSG_NODES_FILE = GSG_CONFIG_DIR / "nodes.json"
GSG_DEVICE_FILE = GSG_CONFIG_DIR / "device.json"
MIHOMO_CONFIG = Path("/etc/mihomo/config.yaml")

GLOBALSHIELD_DOMAIN = "globalshield.ru"

def main():
    def load_json(p, default):
        try:
            with open(p, 'r') as f: return json.load(f)
        except: return default

    devices = load_json(GSG_DEVICES_FILE, {})
    user_rules = load_json(GSG_RULES_FILE, {"direct": [], "proxy": []})
    rulesets = load_json(GSG_RULESETS_FILE, {"rkn_bypass": True, "ru_direct": True})
    sub_data = load_json(GSG_SUBSCRIPTION_FILE, {"url": "", "global_node": "auto"})

    url = sub_data.get("url", "")
    global_node = sub_data.get("global_node", "auto")
    server_config = {}
    nodes = []

    # Загружаем идентификатор устройства
    device_data = load_json(GSG_DEVICE_FILE, {})
    device_id    = device_data.get("device_id", "")
    device_token = device_data.get("device_token", "")

    if url:
        # Валидируем домен — принимаем только GlobalShield
        from urllib.parse import urlparse
        parsed_url = urlparse(url)
        host = parsed_url.hostname or ""
        if not (host == GLOBALSHIELD_DOMAIN or host.endswith("." + GLOBALSHIELD_DOMAIN)):
            print(f"[ERROR] Subscription URL domain not allowed: {host}")
            with open(GSG_NODES_FILE, 'w') as f:
                json.dump({"nodes": [], "updated": str(time.time()), "error": "invalid_domain"}, f)
            return

        try:
            headers = {
                "User-Agent": "Mihomo/1.18.10 (GSG-Smart-Gateway)",
                "X-Device-ID": device_id,
                "X-Device-Token": device_token,
            }
            r = httpx.get(url, headers=headers, timeout=15.0, follow_redirects=True)
            if r.status_code == 401:
                print("[ERROR] Subscription auth failed: device not registered or subscription inactive")
                with open(GSG_NODES_FILE, 'w') as f:
                    json.dump({"nodes": [], "updated": str(time.time()), "error": "unauthorized"}, f)
                return
            r.raise_for_status()

            # Сервер может выдать/обновить токен в заголовке ответа
            new_token = r.headers.get("X-Device-Token", "")
            if new_token and new_token != device_token:
                device_data["device_token"] = new_token
                with open(GSG_DEVICE_FILE, 'w') as f:
                    json.dump(device_data, f)
                print(f"[INFO] Device token updated")

            parsed_yaml = yaml.safe_load(r.text)
            if isinstance(parsed_yaml, dict):
                server_config = parsed_yaml
                nodes = server_config.get("proxies") or []
        except Exception as e:
            print(f"[ERROR] Failed to fetch config: {e}")

    if not isinstance(server_config, dict): server_config = {}

    gui_nodes = [{"tag": n["name"], "type": n["type"], "server": n["server"], "server_port": n.get("port", 443)} for n in nodes]
    with open(GSG_NODES_FILE, 'w') as f: json.dump({"nodes": gui_nodes, "updated": str(time.time())}, f)

    node_names = [n["name"] for n in nodes]

    global_node_kw = global_node  # keyword to resolve after groups are built

    server_config["tproxy-port"] = int(os.getenv("GSG_TPROXY_PORT", "12345"))
    server_config["mixed-port"] = 2080
    server_config["mode"] = "rule"
    server_config["allow-lan"] = True
    server_config["external-controller"] = "0.0.0.0:9090"
    server_config["log-level"] = "silent"
    server_config["ipv6"] = False

    # ИСПРАВЛЕНО: Добавлен блок nameserver, иначе ядро не может резолвить сайты
    server_config["dns"] = {
        "enable": True,
        "listen": "0.0.0.0:1053",
        "ipv6": False,
        "nameserver": ["8.8.8.8", "1.1.1.1", "77.88.8.8"],
        "default-nameserver": ["8.8.8.8", "1.1.1.1"]
    }

    if "sniffer" not in server_config:
        server_config["sniffer"] = {"enable": True, "sniff": {"HTTP": {"ports": [80, 8080], "override-destination": True}, "TLS": {"ports": [443, 8443]}, "QUIC": {"ports": [443, 8443]}}}
    elif "sniff" in server_config["sniffer"] and "QUIC" not in server_config["sniffer"]["sniff"]:
        server_config["sniffer"]["sniff"]["QUIC"] = {"ports": [443, 8443]}

    if "proxies" not in server_config or not server_config["proxies"]:
        server_config["proxies"] = [{"name": "GSG-FALLBACK", "type": "direct"}]

    if "proxy-groups" not in server_config:
        server_config["proxy-groups"] = []

    # Гарантируем наличие группы "auto" — все узлы, лучший по пингу
    existing_group_names = [g["name"] for g in server_config["proxy-groups"]]
    if "auto" not in existing_group_names:
        auto_proxies = node_names if node_names else ["GSG-FALLBACK"]
        server_config["proxy-groups"].insert(0, {
            "name": "auto", "type": "url-test",
            "proxies": auto_proxies,
            "url": "http://www.gstatic.com/generate_204", "interval": 300,
            "lazy": False
        })

    # ── Geo-based proxy groups ───────────────────────────────────────────────
    # Правила ссылаются на группы, а не на конкретные узлы.
    # Если узел недоступен — Mihomo выбирает следующий живой по пингу.
    # Если узел переименован — он останется в группе, пока имя содержит ключевое слово.
    # Если все узлы группы пропали — группа не создаётся, используется fallback 'auto'.

    _built_groups = {}  # keyword -> group_name

    def _make_url_test(name, proxies):
        return {"name": name, "type": "url-test", "proxies": proxies,
                "url": "http://www.gstatic.com/generate_204", "interval": 300, "lazy": False}

    def resolve_assign(keyword):
        """Разрешает ключевое слово (assigned_node) в имя geo-группы.
        Создаёт группу при первом обращении. Возвращает 'auto' если узлов нет."""
        if not keyword or keyword == "auto":
            return "auto"
        kw = keyword.lower()
        if kw in _built_groups:
            return _built_groups[kw]
        matched = [n for n in node_names if re.search(re.escape(kw), n, re.I)]
        if matched:
            gname = f"gsg-{kw}"
            server_config["proxy-groups"].append(_make_url_test(gname, matched))
            _built_groups[kw] = gname
        else:
            _built_groups[kw] = "auto"
        return _built_groups[kw]

    # US-группа — для сервисов с гео-ограничением (Gemini, Claude, ChatGPT)
    US_KEYWORDS = ['ny', 'new york', 'new-york', 'us', 'usa', 'america', 'american']
    us_nodes = [n for n in node_names if re.search(r'ny\b|new[\s\-]?york|\bus\b|\busa\b', n, re.I)]
    if us_nodes:
        server_config["proxy-groups"].append(_make_url_test("gsg-us", us_nodes))
        us_target = "gsg-us"
        # Регистрируем US-ключевые слова — resolve_assign переиспользует gsg-us
        for kw in US_KEYWORDS:
            _built_groups[kw] = "gsg-us"
    else:
        us_target = "auto"

    # Резолвим global_node в группу
    global_node = resolve_assign(global_node_kw)

    rules = []
    rule_providers = {}
    sub_rules = {}

    # Блокируем инфраструктуру iCloud Private Relay.
    # iOS обнаружит недоступность relay и автоматически отключит Private Relay для этой сети,
    # показав уведомление пользователю. Без этого speedtest и ряд диагностик не работают.
    rules.append("DOMAIN,mask.icloud.com,REJECT")
    rules.append("DOMAIN,mask-h2.icloud.com,REJECT")

    if rulesets.get('rkn_bypass', True):
        rule_providers['rkn-domains'] = {
            "type": "http", "behavior": "domain", "format": "text",
            "url": "https://community.antifilter.download/list/domains.lst",
            "path": "./rules/rkn-domains.txt", "interval": 86400
        }

    for ip, info in devices.items():
        mode = info.get('mode', 'smart')
        assign = info.get('assigned_node', 'auto')

        # Резолвим assigned_node в geo-группу (или 'auto')
        target = resolve_assign(assign) if assign != 'auto' else global_node

        if mode == 'block':
            rules.append(f"SRC-IP-CIDR,{ip}/32,REJECT")
        elif mode == 'bypass':
            rules.append(f"SRC-IP-CIDR,{ip}/32,DIRECT")
        elif mode == 'global':
            rules.append(f"SRC-IP-CIDR,{ip}/32,{target}")
        else:
            sub_name = f"smart_{ip.replace('.', '_')}"
            device_sub = []

            # US-only сервисы через gsg-us группу (лучший US-узел по пингу)
            device_sub.append(f"DOMAIN-SUFFIX,gemini.google.com,{us_target}")
            device_sub.append(f"DOMAIN-SUFFIX,generativelanguage.googleapis.com,{us_target}")
            device_sub.append(f"DOMAIN-SUFFIX,claude.ai,{us_target}")
            device_sub.append(f"DOMAIN-SUFFIX,anthropic.com,{us_target}")
            device_sub.append(f"DOMAIN-SUFFIX,openai.com,{us_target}")
            device_sub.append(f"DOMAIN-SUFFIX,chatgpt.com,{us_target}")

            # Сервисы скорости/диагностики
            device_sub.append(f"DOMAIN-SUFFIX,speedtest.net,{target}")
            device_sub.append(f"DOMAIN-SUFFIX,ookla.com,{target}")
            device_sub.append(f"DOMAIN-SUFFIX,fast.com,{target}")
            device_sub.append(f"DOMAIN-SUFFIX,nperf.com,{target}")

            if rulesets.get('rkn_bypass', True):
                device_sub.append(f"GEOSITE,youtube,{target}")
                # TikTok: per-device tiktok_node резолвится в группу
                tiktok_target = resolve_assign(info.get('tiktok_node', 'auto'))
                if tiktok_target == 'auto':
                    tiktok_target = target
                device_sub.append(f"GEOSITE,tiktok,{tiktok_target}")
                device_sub.append(f"GEOSITE,meta,{target}")
                device_sub.append(f"GEOSITE,instagram,{target}")
                device_sub.append(f"GEOSITE,twitter,{target}")
                device_sub.append(f"GEOSITE,telegram,{target}")
                device_sub.append(f"GEOIP,telegram,{target}")
                device_sub.append(f"IP-CIDR,5.28.192.0/18,{target}")
                device_sub.append(f"RULE-SET,rkn-domains,{target}")

            device_sub.append("MATCH,DIRECT")

            if device_sub:
                sub_rules[sub_name] = device_sub
                rules.append(f"SUB-RULE,(SRC-IP-CIDR,{ip}/32),{sub_name}")

    for d in user_rules.get('direct', []): rules.append(f"DOMAIN-SUFFIX,{d},DIRECT")
    for d in user_rules.get('proxy', []): rules.append(f"DOMAIN-SUFFIX,{d},{global_node}")

    # Smart fallback for devices NOT in devices.json (new/guest devices).
    # Without this they would hit MATCH,VPN and all traffic goes through the tunnel —
    # which breaks speedtest, some Russian services, and wastes VPN bandwidth.
    global_smart = []
    global_smart.append(f"DOMAIN-SUFFIX,gemini.google.com,{us_target}")
    global_smart.append(f"DOMAIN-SUFFIX,generativelanguage.googleapis.com,{us_target}")
    global_smart.append(f"DOMAIN-SUFFIX,claude.ai,{us_target}")
    global_smart.append(f"DOMAIN-SUFFIX,anthropic.com,{us_target}")
    global_smart.append(f"DOMAIN-SUFFIX,openai.com,{us_target}")
    global_smart.append(f"DOMAIN-SUFFIX,chatgpt.com,{us_target}")
    global_smart.append(f"DOMAIN-SUFFIX,speedtest.net,{global_node}")
    global_smart.append(f"DOMAIN-SUFFIX,ookla.com,{global_node}")
    global_smart.append(f"DOMAIN-SUFFIX,fast.com,{global_node}")
    global_smart.append(f"DOMAIN-SUFFIX,nperf.com,{global_node}")
    if rulesets.get('rkn_bypass', True):
        global_smart.append(f"GEOSITE,youtube,{global_node}")
        global_smart.append(f"GEOSITE,tiktok,{global_node}")
        global_smart.append(f"GEOSITE,meta,{global_node}")
        global_smart.append(f"GEOSITE,instagram,{global_node}")
        global_smart.append(f"GEOSITE,twitter,{global_node}")
        global_smart.append(f"GEOSITE,telegram,{global_node}")
        global_smart.append(f"GEOIP,telegram,{global_node}")
        global_smart.append(f"IP-CIDR,5.28.192.0/18,{global_node}")
        global_smart.append(f"RULE-SET,rkn-domains,{global_node}")
    global_smart.append("MATCH,DIRECT")
    sub_rules["smart_default"] = global_smart
    rules.append("SUB-RULE,(SRC-IP-CIDR,0.0.0.0/0),smart_default")

    server_config["rule-providers"] = rule_providers
    if sub_rules: server_config["sub-rules"] = sub_rules
    server_config["rules"] = rules + server_config.get("rules", [])

    MIHOMO_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with open(MIHOMO_CONFIG, 'w') as f: yaml.dump(server_config, f, allow_unicode=True)

if __name__ == "__main__":
    main()
