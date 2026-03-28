import os
import json
import yaml
import httpx
import time
from pathlib import Path

GSG_CONFIG_DIR = Path("/etc/gsg")
GSG_DEVICES_FILE = GSG_CONFIG_DIR / "devices.json"
GSG_RULES_FILE = GSG_CONFIG_DIR / "rules.json"
GSG_RULESETS_FILE = GSG_CONFIG_DIR / "rulesets.json"
GSG_SUBSCRIPTION_FILE = GSG_CONFIG_DIR / "subscription.json"
GSG_NODES_FILE = GSG_CONFIG_DIR / "nodes.json"
MIHOMO_CONFIG = Path("/etc/mihomo/config.yaml")

def main():
    def load_json(p, default):
        if os.path.exists(p) and os.path.getsize(p) > 0:
            with open(p, 'r') as f:
                content = f.read()
                if content.strip():
                    return json.loads(content)
        return default

    devices = load_json(GSG_DEVICES_FILE, {})
    user_rules = load_json(GSG_RULES_FILE, {"direct": [], "proxy": [], "custom_groups": []})
    rulesets = load_json(GSG_RULESETS_FILE, {"rkn_bypass": True, "ru_direct": True})
    sub_data = load_json(GSG_SUBSCRIPTION_FILE, {"url": "", "global_node": "auto"})

    url = sub_data.get("url")
    global_node = sub_data.get("global_node", "auto")
    server_config = {}
    nodes = []

    if url:
        headers = {"User-Agent": "Mihomo/1.18.10 (GSG-Smart-Gateway)"}
        try:
            r = httpx.get(url, headers=headers, timeout=15.0, follow_redirects=True)
            r.raise_for_status()
            parsed_yaml = yaml.safe_load(r.text)
            if isinstance(parsed_yaml, dict):
                server_config = parsed_yaml
                nodes = server_config.get("proxies") or []
        except Exception as e:
            print(f"[WARN] Failed to fetch subscription: {e}", flush=True)

    if not isinstance(server_config, dict):
        server_config = {}

    gui_nodes = [{"tag": n["name"], "type": n["type"], "server": n["server"], "server_port": n.get("port", 443)} for n in nodes]
    with open(GSG_NODES_FILE, 'w') as f:
        json.dump({"nodes": gui_nodes, "updated": str(time.time())}, f)

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
    server_config["log-level"] = "warning"
    server_config["ipv6"] = False

    server_config["dns"] = {
        "enable": True,
        "listen": "0.0.0.0:1053",
        "ipv6": False,
        "nameserver": ["8.8.8.8", "1.1.1.1", "77.88.8.8"],
        "default-nameserver": ["8.8.8.8", "1.1.1.1"]
    }

    server_config["sniffer"] = {
        "enable": True,
        "sniff": {
            "HTTP": {"ports": [80, 8080], "override-destination": True},
            "TLS": {"ports": [443, 8443], "override-destination": True},
            "QUIC": {"ports": [443], "override-destination": True}
        }
    }

    if "proxies" not in server_config or not server_config["proxies"]:
        server_config["proxies"] = [{"name": "GSG-FALLBACK", "type": "direct"}]

    if "proxy-groups" not in server_config:
        server_config["proxy-groups"] = []

    existing_group_names = [g["name"] for g in server_config["proxy-groups"]]

    if "auto" not in existing_group_names:
        auto_proxies = node_names if node_names else ["GSG-FALLBACK"]
        server_config["proxy-groups"].insert(0, {
            "name": "auto", "type": "url-test",
            "proxies": auto_proxies,
            "url": "http://www.gstatic.com/generate_204", "interval": 300,
            "lazy": False, "tolerance": 50
        })

    custom_groups = user_rules.get("custom_groups", [])
    for group in custom_groups:
        if not group.get("enabled", True): continue
        g_name = f"CUSTOM-{group.get('id', 'unknown')}"
        filter_str = group.get("node_filter", "").lower()
        matched_nodes = [n for n in node_names if filter_str in n.lower()]
        if matched_nodes and g_name not in existing_group_names:
            server_config["proxy-groups"].append({
                "name": g_name, "type": "url-test", "proxies": matched_nodes,
                "url": "http://www.gstatic.com/generate_204", "interval": 300,
                "lazy": False, "tolerance": 50
            })
            existing_group_names.append(g_name)

    domain_rules = []
    ip_rules = []
    rule_providers = {}
    sub_rules = {}

    if rulesets.get('rkn_bypass', True):
        rule_providers['rkn-domains'] = {
            "type": "http", "behavior": "domain", "format": "text",
            "url": "https://community.antifilter.download/list/domains.lst",
            "path": "./rules/rkn-domains.txt", "interval": 86400
        }

    custom_routing_rules = []
    for group in custom_groups:
        if not group.get("enabled", True): continue
        g_name = f"CUSTOM-{group.get('id', 'unknown')}"
        filter_str = group.get("node_filter", "").lower()
        matched_nodes = [n for n in node_names if filter_str in n.lower()]
        target = g_name if matched_nodes else "auto"
        for domain in group.get("domains", []):
            if domain.strip():
                clean_d = domain.strip().split('://')[-1].split('/')[0]
                custom_routing_rules.append(f"DOMAIN-SUFFIX,{clean_d},{target}")

    # --- 1. AI КОНТУР ---
    ai_settings = user_rules.get('ai_settings', {})
    node_filter = ai_settings.get("node_filter", "").lower()

    ai_nodes = []
    if node_filter:
        filters = [f.strip() for f in node_filter.split(',') if f.strip()]
        ai_nodes = [n for n in node_names if any(f in n.lower() for f in filters)]

    ai_target = "GSG-AI" if ai_nodes else global_node

    if ai_nodes:
        server_config['proxy-groups'].insert(0, {
            "name": "GSG-AI", "type": "fallback", "proxies": ai_nodes,
            "url": "http://www.gstatic.com/generate_204", "interval": 300, "lazy": False
        })

    # Парсим домены из интерфейса.
    # Умная логика: есть точка -> SUFFIX, нет точки -> KEYWORD
    ai_domains = ai_settings.get("domains")

    # Если список пуст (например, при первом запуске), используем эти слова по умолчанию
    if not ai_domains:
        ai_domains = [
            "gemini", "openai", "chatgpt", "anthropic", "claude", "aistudio.google.com"
        ]

    for d in ai_domains:
        d = d.strip()
        if d:
            clean_d = d.split('://')[-1].split('/')[0]
            if '.' in clean_d:
                domain_rules.append(f"DOMAIN-SUFFIX,{clean_d},{ai_target}")
            else:
                domain_rules.append(f"DOMAIN-KEYWORD,{clean_d},{ai_target}")

    # --- 2. ПОЛЬЗОВАТЕЛЬСКИЕ ДОМЕНЫ ---
    for d in user_rules.get('proxy', []):
        clean_d = d.strip().split('://')[-1].split('/')[0]
        domain_rules.append(f"DOMAIN-SUFFIX,{clean_d},{global_node}")

    for d in user_rules.get('direct', []):
        clean_d = d.strip().split('://')[-1].split('/')[0]
        domain_rules.append(f"DOMAIN-SUFFIX,{clean_d},DIRECT")

    # --- 3. ПРАВИЛА УСТРОЙСТВ ---
    for ip, info in devices.items():
        mode = info.get('mode', 'smart')
        assign = info.get('assigned_node', 'auto')
        target = global_node
        if assign != 'auto':
            for name in node_names:
                if assign.lower() in name.lower():
                    target = name; break

        if mode == 'block':
            ip_rules.append(f"SRC-IP-CIDR,{ip}/32,REJECT")
        elif mode == 'bypass':
            ip_rules.append(f"SRC-IP-CIDR,{ip}/32,DIRECT")
        elif mode == 'global':
            ip_rules.append(f"SRC-IP-CIDR,{ip}/32,{target}")
        else:
            sub_name = f"smart_{ip.replace('.', '_')}"
            device_sub = []
            if rulesets.get('rkn_bypass', True):
                for site in ["youtube", "meta", "instagram", "twitter", "telegram"]:
                    device_sub.append(f"GEOSITE,{site},{target}")
                device_sub.append(f"GEOIP,telegram,{target}")
                device_sub.append(f"RULE-SET,rkn-domains,{target}")
            device_sub.append("MATCH,DIRECT")
            sub_rules[sub_name] = device_sub
            ip_rules.append(f"SUB-RULE,(SRC-IP-CIDR,{ip}/32),{sub_name}")

    server_config["rule-providers"] = rule_providers
    if sub_rules:
        server_config["sub-rules"] = sub_rules

    server_config["rules"] = domain_rules + ip_rules + custom_routing_rules + [f"MATCH,{global_node}"]

    print("\n[GSG] === ГРУППА GSG-AI ===", flush=True)
    if ai_nodes:
        print(f"  --> Найдено узлов: {len(ai_nodes)}", flush=True)
    else:
        print(f"  --> Узлы НЕ НАЙДЕНЫ! Трафик идет через: {global_node}", flush=True)

    MIHOMO_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with open(MIHOMO_CONFIG, 'w') as f:
        yaml.dump(server_config, f, allow_unicode=True)

if __name__ == "__main__":
    main()
