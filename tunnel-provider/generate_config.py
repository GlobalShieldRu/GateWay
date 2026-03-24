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

    matched_global = "auto"
    if global_node != "auto":
        for n in node_names:
            if global_node.lower() in n.lower():
                matched_global = n
                break
    global_node = matched_global

    server_config["tproxy-port"] = int(os.getenv("GSG_TPROXY_PORT", "12345"))
    server_config["mixed-port"] = 2080
    server_config["mode"] = "rule"
    server_config["allow-lan"] = True
    server_config["external-controller"] = "0.0.0.0:9090"
    server_config["log-level"] = "info"
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

    # Гарантируем наличие группы "auto" — на неё ссылаются все правила
    existing_group_names = [g["name"] for g in server_config["proxy-groups"]]
    if "auto" not in existing_group_names:
        auto_proxies = node_names if node_names else ["GSG-FALLBACK"]
        server_config["proxy-groups"].insert(0, {
            "name": "auto", "type": "url-test",
            "proxies": auto_proxies,
            "url": "http://www.gstatic.com/generate_204", "interval": 300
        })

    rules = []
    rule_providers = {}
    sub_rules = {}

    # Находим NY-узел для geo-restricted сервисов (Gemini, Claude)
    ny_node = next((n for n in node_names if re.search(r'ny|new[\s\-]?york', n, re.I)), None)

    if rulesets.get('rkn_bypass', True):
        rule_providers['rkn-domains'] = {
            "type": "http", "behavior": "domain", "format": "text",
            "url": "https://community.antifilter.download/list/domains.lst",
            "path": "./rules/rkn-domains.txt", "interval": 86400
        }

    for ip, info in devices.items():
        mode = info.get('mode', 'smart')
        assign = info.get('assigned_node', 'auto')

        target = global_node
        if assign != 'auto':
            for name in node_names:
                if assign.lower() in name.lower():
                    target = name
                    break

        if mode == 'block':
            rules.append(f"SRC-IP-CIDR,{ip}/32,REJECT")
        elif mode == 'bypass':
            rules.append(f"SRC-IP-CIDR,{ip}/32,DIRECT")
        elif mode == 'global':
            rules.append(f"SRC-IP-CIDR,{ip}/32,{target}")
        else:
            sub_name = f"smart_{ip.replace('.', '_')}"
            device_sub = []

            # US-only сервисы всегда через NY-узел
            us_target = ny_node or target
            device_sub.append(f"DOMAIN-SUFFIX,gemini.google.com,{us_target}")
            device_sub.append(f"DOMAIN-SUFFIX,generativelanguage.googleapis.com,{us_target}")
            device_sub.append(f"DOMAIN-SUFFIX,claude.ai,{us_target}")
            device_sub.append(f"DOMAIN-SUFFIX,anthropic.com,{us_target}")
            device_sub.append(f"DOMAIN-SUFFIX,openai.com,{us_target}")
            device_sub.append(f"DOMAIN-SUFFIX,chatgpt.com,{us_target}")

            if rulesets.get('rkn_bypass', True):
                device_sub.append(f"GEOSITE,youtube,{target}")
                # TikTok: use per-device tiktok_node if set, else device target
                tiktok_raw = info.get('tiktok_node', 'auto')
                tiktok_target = target  # default
                if tiktok_raw and tiktok_raw != 'auto':
                    for name in node_names:
                        if tiktok_raw.lower() in name.lower():
                            tiktok_target = name
                            break
                device_sub.append(f"GEOSITE,tiktok,{tiktok_target}")
                device_sub.append(f"GEOSITE,meta,{target}")
                device_sub.append(f"GEOSITE,instagram,{target}")
                device_sub.append(f"GEOSITE,twitter,{target}")
                device_sub.append(f"GEOSITE,telegram,{target}")
                device_sub.append(f"RULE-SET,rkn-domains,{target}")

            device_sub.append("MATCH,DIRECT")

            if device_sub:
                sub_rules[sub_name] = device_sub
                rules.append(f"SUB-RULE,(SRC-IP-CIDR,{ip}/32),{sub_name}")

    for d in user_rules.get('direct', []): rules.append(f"DOMAIN-SUFFIX,{d},DIRECT")
    for d in user_rules.get('proxy', []): rules.append(f"DOMAIN-SUFFIX,{d},{global_node}")
    rules.append(f"MATCH,{global_node}")

    server_config["rule-providers"] = rule_providers
    if sub_rules: server_config["sub-rules"] = sub_rules
    server_config["rules"] = rules + server_config.get("rules", [])

    MIHOMO_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with open(MIHOMO_CONFIG, 'w') as f: yaml.dump(server_config, f, allow_unicode=True)

if __name__ == "__main__":
    main()
