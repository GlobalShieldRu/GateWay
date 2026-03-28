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
        r = httpx.get(url, headers=headers, timeout=15.0, follow_redirects=True)
        r.raise_for_status()
        parsed_yaml = yaml.safe_load(r.text)
        if isinstance(parsed_yaml, dict):
            server_config = parsed_yaml
            nodes = server_config.get("proxies") or []

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

    if "sniffer" not in server_config:
        server_config["sniffer"] = {"enable": True, "sniff": {"HTTP": {"ports": [80, 8080], "override-destination": True}, "TLS": {"ports": [443, 8443]}}}

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
        if not group.get("enabled", True):
            continue

        group_id = group.get("id", "unknown")
        g_name = f"CUSTOM-{group_id}"

        filter_str = group.get("node_filter", "").lower()
        matched_nodes = [n for n in node_names if filter_str in n.lower()]

        if matched_nodes and g_name not in existing_group_names:
            server_config["proxy-groups"].append({
                "name": g_name,
                "type": "url-test",
                "proxies": matched_nodes,
                "url": "http://www.gstatic.com/generate_204",
                "interval": 300,
                "lazy": False, "tolerance": 50
            })
            existing_group_names.append(g_name)

    rules = []
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
        if not group.get("enabled", True):
            continue

        g_name = f"CUSTOM-{group.get('id', 'unknown')}"
        filter_str = group.get("node_filter", "").lower()
        matched_nodes = [n for n in node_names if filter_str in n.lower()]

        target = g_name if matched_nodes else "auto"

        for domain in group.get("domains", []):
            domain = domain.strip()
            if domain:
                custom_routing_rules.append(f"DOMAIN-SUFFIX,{domain},{target}")

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

            if rulesets.get('rkn_bypass', True):
                device_sub.append(f"GEOSITE,youtube,{target}")
                device_sub.append(f"GEOSITE,meta,{target}")
                device_sub.append(f"GEOSITE,instagram,{target}")
                device_sub.append(f"GEOSITE,twitter,{target}")
                device_sub.append(f"GEOSITE,telegram,{target}")

                device_sub.append(f"RULE-SET,rkn-domains,{target}")

            device_sub.append("MATCH,DIRECT")

            if device_sub:
                sub_rules[sub_name] = device_sub
                rules.append(f"SUB-RULE,(SRC-IP-CIDR,{ip}/32),{sub_name}")

    rules.extend(custom_routing_rules)

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
