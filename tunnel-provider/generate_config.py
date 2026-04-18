import os
import re
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

    # Файл для хранения полных VLESS-конфигов в персистентном volume
    PROXIES_BACKUP = GSG_CONFIG_DIR / "proxies_backup.yaml"

    if url:
        headers = {"User-Agent": "Mihomo/1.18.10 (GSG-Smart-Gateway)"}
        try:
            r = httpx.get(url, headers=headers, timeout=15.0, follow_redirects=True)
            r.raise_for_status()
            parsed_yaml = yaml.safe_load(r.text)
            if isinstance(parsed_yaml, dict):
                server_config = parsed_yaml
                nodes = server_config.get("proxies") or []
                # Сохраняем полные VLESS-конфиги в volume при каждом успешном fetch
                if nodes:
                    try:
                        with open(PROXIES_BACKUP, 'w') as f:
                            yaml.dump(nodes, f, allow_unicode=True, default_flow_style=False)
                        print(f"[INFO] Бэкап прокси сохранён: {len(nodes)} узлов → {PROXIES_BACKUP}", flush=True)
                    except Exception as e:
                        print(f"[WARN] Не удалось сохранить бэкап прокси: {e}", flush=True)
        except Exception as e:
            print(f"[WARN] Failed to fetch subscription: {e}", flush=True)

    if not isinstance(server_config, dict):
        server_config = {}

    gui_nodes = [{"tag": n["name"], "type": n["type"], "server": n["server"], "server_port": n.get("port", 443)} for n in nodes]

    # Защита: не перезаписывать nodes.json пустым если предыдущий был непустой
    if not gui_nodes and GSG_NODES_FILE.exists():
        try:
            prev = json.loads(GSG_NODES_FILE.read_text())
            if prev.get("nodes"):
                print(f"[WARN] Подписка вернула 0 узлов, сохраняю предыдущие {len(prev['nodes'])} узлов", flush=True)
                gui_nodes = prev["nodes"]
                nodes = [{"name": n["tag"], "type": n["type"], "server": n["server"], "port": n["server_port"]} for n in gui_nodes]

                # Восстанавливаем полные VLESS-конфиги из бэкапа в volume (надёжный источник)
                if PROXIES_BACKUP.exists():
                    try:
                        backup_proxies = yaml.safe_load(PROXIES_BACKUP.read_text())
                        if isinstance(backup_proxies, list) and backup_proxies:
                            server_config = {"proxies": backup_proxies}
                            nodes = backup_proxies  # используем полные конфиги из бэкапа
                            print(f"[INFO] Восстановлен из бэкапа прокси: {len(backup_proxies)} узлов", flush=True)
                    except Exception as e:
                        print(f"[WARN] Не удалось прочитать бэкап прокси: {e}", flush=True)

                # Fallback на config.yaml только если бэкап недоступен
                if not server_config.get("proxies"):
                    if MIHOMO_CONFIG.exists():
                        try:
                            old_cfg = yaml.safe_load(MIHOMO_CONFIG.read_text()) or {}
                            old_proxies = old_cfg.get("proxies", [])
                            # Используем только если там реальные ноды (не GSG-FALLBACK)
                            real_proxies = [p for p in old_proxies if p.get("name") != "GSG-FALLBACK" and p.get("type") != "direct"]
                            if real_proxies:
                                server_config = old_cfg
                                print(f"[WARN] Восстановлен из config.yaml: {len(real_proxies)} реальных прокси", flush=True)
                            else:
                                print(f"[WARN] config.yaml содержит только GSG-FALLBACK, пропускаем", flush=True)
                        except Exception:
                            pass
        except Exception:
            pass

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
    server_config["log-level"] = "silent"
    server_config["keep-alive-interval"] = 15    # TCP keepalive каждые 15с — быстро детектируем мёртвые соединения
    server_config["keep-alive-idle-time"] = 15   # начинать keepalive через 15с простоя (было 600 — 10 мин!)
    server_config["unified-delay"] = True        # Нормализует latency для url-test групп
    server_config["find-process-mode"] = "off"   # Не определять процессы (не нужно для TPROXY)
    server_config["profile"] = {
        "store-selected": True,   # Сохранять выбор proxy-group при перезагрузке конфига
        "store-fake-ip": True,    # Сохранять DNS mappings при перезагрузке
    }
    server_config["ipv6"] = False
    server_config["geodata-mode"] = True
    server_config["geo-auto-update"] = True
    server_config["geo-update-interval"] = 24  # часов
    server_config["geox-url"] = {
        # runetfreedom: 1449 звёзд, обновление каждые 6ч, чистые категории.
        # Категория ru-available-only-inside не содержит YouTube/Telegram.
        # Mihomo кешируeт файл локально и обновляет каждые geo-update-interval часов.
        # При недоступности GitHub — использует последний закешированный файл.
        "geoip":   "https://github.com/runetfreedom/russia-v2ray-rules-dat/releases/latest/download/geoip.dat",
        "geosite": "https://github.com/runetfreedom/russia-v2ray-rules-dat/releases/latest/download/geosite.dat",
    }

    server_config["dns"] = {
        "enable": True,
        "listen": "0.0.0.0:1053",
        "ipv6": False,
        "nameserver": ["8.8.8.8", "1.1.1.1", "77.88.8.8"],
        "default-nameserver": ["8.8.8.8", "1.1.1.1"]
    }

    server_config["sniffer"] = {
        "enable": True,
        "parse-pure-ip": False,  # Не тратить CPU на IP-only соединения
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

    # --- Парсер строк rules: определяем тип по содержимому ---
    import ipaddress as _ipaddress

    def _get_rules(pg: dict) -> list:
        """Возвращает список строк правил группы (новый формат rules или старый domains)."""
        rules_list = pg.get("rules")
        if rules_list is not None:
            return rules_list
        # Миграция: domains → rules
        return pg.get("domains", [])

    def _parse_rule_line(line: str, target: str) -> list:
        """
        Парсит одну строку из textarea правил и возвращает список Mihomo-правил.
        Возвращает [] для пустых строк и комментариев.
        Типы:
          - 10.10.2.0/24 или 192.168.1.0/24  → IP-CIDR,<net>,<target>,no-resolve
          - 192.168.1.1 (одиночный IP без маски) → IP-CIDR,<ip>/32,<target>,no-resolve
          - kinopoisk.ru (содержит точку)        → DOMAIN-SUFFIX,<domain>,<target>
          - kinopoisk (без точек и слешей)       → DOMAIN-KEYWORD,<keyword>,<target>
        """
        line = line.strip()
        if not line or line.startswith('#'):
            return []
        # Проверяем IP-CIDR (содержит '/')
        if '/' in line:
            try:
                _ipaddress.ip_network(line, strict=False)
                return [f"IP-CIDR,{line},{target},no-resolve"]
            except ValueError:
                pass
        # Проверяем одиночный IP без маски
        try:
            addr = _ipaddress.ip_address(line)
            return [f"IP-CIDR,{line}/32,{target},no-resolve"]
        except ValueError:
            pass
        # Домен или ключевое слово
        clean = line.split('://')[-1].split('/')[0]
        if '.' in clean:
            return [f"DOMAIN-SUFFIX,{clean},{target}"]
        else:
            return [f"DOMAIN-KEYWORD,{clean},{target}"]

    # --- PROXY GROUPS ---
    # Миграция: если proxy_groups нет, строим из старого формата
    proxy_groups = user_rules.get("proxy_groups")
    if not proxy_groups:
        proxy_groups = [
            {"id": "auto", "name": "Auto", "node_filter": "", "type": "fallback", "builtin": True, "rules": []},
        ]
        # Миграция AI — тип берём из ai_settings если есть, иначе fallback
        ai_s = user_rules.get("ai_settings", {})
        ai_domains = ai_s.get("domains") or ["gemini", "openai", "chatgpt", "anthropic", "claude", "aistudio.google.com"]
        proxy_groups.append({
            "id": "ai", "name": "AI", "node_filter": ai_s.get("node_filter", "NY"),
            "type": ai_s.get("type", "fallback"), "builtin": True, "rules": ai_domains
        })
        # Bypass (direct) — встроенная группа
        direct_list = user_rules.get("direct", [])
        proxy_groups.append({
            "id": "bypass", "name": "Bypass", "node_filter": "",
            "type": "direct", "builtin": True, "rules": direct_list
        })
        # Предустановленные группы
        proxy_groups.append({
            "id": "exchanges", "name": "Биржи", "node_filter": "", "type": "url-test", "builtin": False,
            "rules": [
                "mexc.com","mexc.co","mexc.fm","mexc.la","mocortech.com","mexcsensors.com",
                "binance.com","binance.cloud","binance.me","bnbstatic.com","binance.vision",
                "bybit.com","bybit.cloud","bycsi.com",
                "okx.com","okx.cab","okex.com","ouxyi.com","okbn.com",
                "coinbase.com","kraken.com","kucoin.com","kcs.top",
                "gate.io","gateio.live","gatedata.org",
                "htx.com","huobi.com","hbfile.net",
                "bitget.com","crypto.com","deribit.com","phemex.com","bingx.com",
                "tangem.com","tangem.org",
                "coingecko.com","coinmarketcap.com","tradingview.com",
            ]
        })
        proxy_groups.append({
            "id": "social", "name": "Соцсети", "node_filter": "", "type": "url-test", "builtin": False,
            "rules": [
                "instagram.com","cdninstagram.com","facebook.com","fbcdn.net","fb.com",
                "twitter.com","x.com","twimg.com",
                "linkedin.com","licdn.com",
                "pinterest.com","pinimg.com",
                "reddit.com","redd.it","redditstatic.com",
                "threads.net","threads.com",
                "discord.com","discord.gg","discordapp.com",
            ]
        })
        proxy_groups.append({
            "id": "streaming", "name": "Стриминг", "node_filter": "", "type": "url-test", "builtin": False,
            "rules": [
                "netflix.com","nflxvideo.net","nflximg.net","nflxso.net",
                "spotify.com","scdn.co","spotifycdn.com",
                "disneyplus.com","disney-plus.net","bamgrid.com",
                "hbomax.com","max.com",
                "twitch.tv","twitchcdn.net","twitchsvc.net",
                "deezer.com",
                "soundcloud.com","sndcdn.com",
            ]
        })
        # Миграция proxy-доменов → в Auto
        proxy_list = user_rules.get("proxy", [])
        if proxy_list:
            proxy_groups[0]["rules"] = proxy_list  # Auto — первый элемент

    # Создаём Mihomo proxy-groups
    print(f"\n[GSG] === PROXY GROUPS ({len(proxy_groups)}) ===", flush=True)
    for pg in proxy_groups:
        g_id = pg.get("id", "unknown")
        g_type = pg.get("type", "url-test")

        # Bypass — не создаёт proxy-group, правила идут DIRECT
        if g_type == "direct" or g_id == "bypass":
            print(f"  {pg.get('name',g_id)} [{g_id}] — DIRECT, {len(_get_rules(pg))} правил", flush=True)
            continue

        filter_str = pg.get("node_filter", "").lower().strip()

        if filter_str:
            filters = [f.strip() for f in filter_str.split(',') if f.strip()]
            matched = [n for n in node_names if any(f in n.lower() for f in filters)]
        else:
            matched = list(node_names)

        proxies = matched if matched else (node_names if node_names else ["GSG-FALLBACK"])

        # Для fallback-групп добавляем DIRECT последним — если все ноды упали,
        # незаблокированные сайты продолжат работать вместо полного отсутствия интернета.
        # Для url-test НЕ добавляем: DIRECT имеет нулевую латентность и выиграет конкуренцию.
        if g_type == "fallback" and "DIRECT" not in proxies:
            proxies = proxies + ["DIRECT"]

        group_cfg = {
            "name": g_id, "type": g_type, "proxies": proxies,
            "url": "http://www.gstatic.com/generate_204", "interval": 120, "lazy": g_id != "auto",
            "timeout": 5000,          # Таймаут health-check (5 сек)
            "max-failed-times": 3,    # Выводить узел из ротации после 3 ошибок
        }
        if g_type == "url-test":
            group_cfg["tolerance"] = 50
        if g_id not in existing_group_names:
            server_config["proxy-groups"].append(group_cfg)
            existing_group_names.append(g_id)
        else:
            # Перезаписываем группу из подписки нашими настройками (тип, ноды, параметры)
            for i, eg in enumerate(server_config["proxy-groups"]):
                if eg.get("name") == g_id:
                    server_config["proxy-groups"][i] = group_cfg
                    break
        print(f"  {pg.get('name',g_id)} [{g_id}] — {g_type}, {len(proxies)} узлов, {len(_get_rules(pg))} правил", flush=True)

    custom_groups = user_rules.get("custom_groups", [])

    # --- ROUTE OVERRIDES ---
    # Санитизация имени узла для использования в имени proxy-group.
    # Заменяем ASCII-пробелы и спецсимволы на дефис, сохраняем эмодзи и буквы.
    def sanitize_node_name(name):
        # Заменяем ASCII-пунктуацию и пробелы на дефис
        result = re.sub(r'[ \t|/\\:*?"<>]+', '-', name)
        # Убираем дефисы в начале/конце и дублирующиеся
        result = re.sub(r'-{2,}', '-', result).strip('-')
        return result

    route_overrides = user_rules.get("route_overrides", [])

    # Создаём GSG-ROUTE-* proxy-groups для уникальных node: targets
    seen_node_groups = {}  # node_name -> gsg_group_name
    for override in route_overrides:
        target = override.get("target", "")
        if not target.startswith("node:"):
            continue
        node_name = target[len("node:"):]
        if node_name in seen_node_groups:
            continue
        if node_name not in node_names:
            print(f"[WARN] route_overrides: узел '{node_name}' не найден в подписке, пропускаем", flush=True)
            seen_node_groups[node_name] = None
            continue
        g_name = f"GSG-ROUTE-{sanitize_node_name(node_name)}"
        if g_name not in existing_group_names:
            server_config["proxy-groups"].append({
                "name": g_name, "type": "select",
                "proxies": [node_name]
            })
            existing_group_names.append(g_name)
        seen_node_groups[node_name] = g_name

    bypass_network_rules = []  # IP-CIDR правила из групп собираются в секции доменных правил

    # WebRTC/VoIP порты всегда DIRECT (Телемост, FaceTime, STUN/TURN, RTP)
    voip_bypass_rules = [
        "DST-PORT,3478-3497,DIRECT",
        "DST-PORT,16384-16387,DIRECT",
        "DST-PORT,5223,DIRECT",
    ]

    # Формируем правила из route_overrides
    override_rules = []
    for override in route_overrides:
        domain = override.get("domain", "").strip()
        target = override.get("target", "").strip()
        if not domain or not target:
            continue

        # Определяем тип правила: нет точки → KEYWORD, иначе SUFFIX
        rule_type = "DOMAIN-KEYWORD" if '.' not in domain else "DOMAIN-SUFFIX"

        if target == "DIRECT" or target == "REJECT":
            resolved_target = target
        elif target.startswith("node:"):
            node_name = target[len("node:"):]
            resolved_target = seen_node_groups.get(node_name)
            if resolved_target is None:
                print(f"[WARN] route_overrides: пропускаем правило для '{domain}', узел недоступен", flush=True)
                continue
        elif target.startswith("group:"):
            group_name = target[len("group:"):]
            if group_name not in existing_group_names:
                print(f"[WARN] route_overrides: группа '{group_name}' не найдена, пропускаем правило для '{domain}'", flush=True)
                continue
            resolved_target = group_name
        else:
            print(f"[WARN] route_overrides: неизвестный формат target '{target}', пропускаем", flush=True)
            continue

        override_rules.append(f"{rule_type},{domain},{resolved_target}")

    if override_rules:
        print(f"\n[GSG] === ROUTE OVERRIDES ({len(override_rules)} правил) ===", flush=True)
        for r in override_rules:
            print(f"  {r}", flush=True)

    domain_rules = []
    ip_rules = []
    rule_providers = {}
    sub_rules = {}

    if rulesets.get('rkn_bypass', True):
        rule_providers['rkn-domains'] = {
            "type": "http", "behavior": "domain", "format": "text",
            "url": "https://community.antifilter.download/list/domains.lst",
            "path": "./rules/rkn-domains.txt", "interval": 86400,
            "size-limit": 104857600  # 100 MB максимум
        }

    # Telegram CIDR — всегда, независимо от rkn_bypass.
    # Telegram-клиенты коннектятся по IP напрямую, минуя DNS,
    # поэтому GEOSITE,telegram недостаточно.
    rule_providers['telegram-cidr'] = {
        "type": "http", "behavior": "ipcidr", "format": "text",
        "url": "https://core.telegram.org/resources/cidr.txt",
        "path": "./rules/telegram-cidr.txt", "interval": 86400,
        "size-limit": 10485760  # 10 MB максимум
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

    # --- 1. ПРАВИЛА ИЗ PROXY GROUPS ---
    for pg in proxy_groups:
        g_id = pg.get("id", "unknown")
        g_type = pg.get("type", "url-test")
        pg_rules = _get_rules(pg)
        exclusions = pg.get("exclusions", [])

        # Exclusions группы: эти домены идут DIRECT для устройств использующих группу.
        # Добавляем ПЕРЕД правилами самой группы, чтобы иметь приоритет.
        # Применяем только для не-direct групп (у bypass/direct exclusions бессмысленны).
        if exclusions and g_type != "direct" and g_id != "bypass":
            for ex in exclusions:
                ex = ex.strip()
                if not ex:
                    continue
                # group:ИмяГруппы → раскрываем все правила указанной группы
                if ex.startswith('group:'):
                    ref_name = ex[len('group:'):]
                    ref_group = next((p for p in proxy_groups if p.get('name') == ref_name or p.get('id') == ref_name), None)
                    if ref_group is None:
                        print(f"[WARN] exclusions: группа '{ref_name}' не найдена, пропускаем", flush=True)
                        continue
                    ref_rules = _get_rules(ref_group)
                    print(f"  [{pg.get('name', g_id)}] exclusion group:{ref_name} → {len(ref_rules)} правил", flush=True)
                    for rr in ref_rules:
                        for mihomo_rule in _parse_rule_line(rr, "DIRECT"):
                            domain_rules.append(mihomo_rule)
                    continue
                for mihomo_rule in _parse_rule_line(ex, "DIRECT"):
                    domain_rules.append(mihomo_rule)

        if not pg_rules:
            continue

        # Bypass/direct → DIRECT, остальные → через proxy-group
        if g_type == "direct" or g_id == "bypass":
            target = "DIRECT"
        elif g_id in existing_group_names:
            target = g_id
        else:
            target = global_node

        # IP-CIDR правила из группы выносим в начало (bypass_network_rules)
        for line in pg_rules:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            # Определяем: это IP/CIDR или домен/ключевое слово?
            is_ip = False
            if '/' in line:
                try:
                    _ipaddress.ip_network(line, strict=False)
                    is_ip = True
                except ValueError:
                    pass
            if not is_ip:
                try:
                    _ipaddress.ip_address(line)
                    is_ip = True
                except ValueError:
                    pass
            for mihomo_rule in _parse_rule_line(line, target):
                if mihomo_rule.startswith("IP-CIDR,"):
                    bypass_network_rules.append(mihomo_rule)
                else:
                    domain_rules.append(mihomo_rule)

    # ru_direct: сайты, доступные только внутри России — идут напрямую.
    # Требует geosite.dat от runetfreedom (маркер /etc/mihomo/.geosite-runetfreedom).
    # Категория ru-available-only-inside НЕ содержит YouTube/Telegram (отдельные категории).
    _has_runetfreedom = os.path.exists("/etc/mihomo/.geosite-runetfreedom")
    if rulesets.get('ru_direct', True) and _has_runetfreedom:
        domain_rules.append("GEOSITE,ru-available-only-inside,DIRECT")
        print("[GSG] ru_direct: GEOSITE,ru-available-only-inside → DIRECT", flush=True)
    elif rulesets.get('ru_direct', True):
        print("[GSG] ru_direct: пропущен — geosite.dat ещё не обновлён до runetfreedom (перезапустите туннель)", flush=True)

    # --- 3. ПРАВИЛА УСТРОЙСТВ ---
    def _looks_like_mac(s: str) -> bool:
        parts = s.split(':')
        return len(parts) == 6 and all(len(p) == 2 for p in parts)

    for key, info in devices.items():
        # Поддерживаем оба формата: MAC-ключ (новый) и IP-ключ (старый)
        if _looks_like_mac(key):
            # Новый формат: ключ = MAC, IP берём из reserved_ip или static_ip
            ip = info.get('reserved_ip') or info.get('static_ip') or info.get('current_ip', '')
            if not ip:
                print(f"[WARN] Устройство {key}: нет reserved_ip, пропускаем", flush=True)
                continue
        else:
            # Старый формат: ключ = IP
            ip = key
            # Если есть reserved_ip/static_ip — используем его для правил
            reserved = info.get('reserved_ip') or info.get('static_ip', '')
            if reserved:
                ip = reserved

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
            # Telegram IP-правила — всегда (клиенты коннектятся по IP)
            device_sub.append(f"GEOSITE,telegram,{target}")
            device_sub.append(f"GEOIP,telegram,{target}")
            device_sub.append(f"RULE-SET,telegram-cidr,{target}")
            device_sub.append(f"IP-CIDR,5.28.192.0/22,{target}")
            # TikTok — через tiktok_node если задан, иначе через target
            tiktok_node_tag = info.get('tiktok_node', 'auto')
            tiktok_target = target
            if tiktok_node_tag and tiktok_node_tag != 'auto':
                for name in node_names:
                    if tiktok_node_tag.lower() in name.lower() or name == tiktok_node_tag:
                        tiktok_target = name
                        break
            device_sub.append(f"GEOSITE,tiktok,{tiktok_target}")
            for td in ["tiktokv.com", "tiktokcdn.com", "tiktokcdn-eu.com", "tiktokcdn-us.com", "bytedance.com", "byteimg.com", "ibytedtos.com", "ipstatp.com", "musical.ly"]:
                device_sub.append(f"DOMAIN-SUFFIX,{td},{tiktok_target}")
            # Добавляем domain_rules из proxy_groups ДО rkn-domains,
            # чтобы AI-домены (gemini, claude и др.) имели приоритет
            # над rkn-domains где они тоже могут быть прописаны.
            for dr in domain_rules:
                device_sub.append(dr)
            if rulesets.get('rkn_bypass', True):
                for site in ["youtube", "meta", "instagram", "twitter"]:
                    device_sub.append(f"GEOSITE,{site},{target}")
                device_sub.append(f"RULE-SET,rkn-domains,{target}")
            device_sub.append("MATCH,DIRECT")
            sub_rules[sub_name] = device_sub
            ip_rules.append(f"SUB-RULE,(SRC-IP-CIDR,{ip}/32),{sub_name}")

    server_config["rule-providers"] = rule_providers
    if sub_rules:
        server_config["sub-rules"] = sub_rules

    # --- 4. WHITELIST BYPASS MODE ---
    # Когда провайдер включает "белые списки", весь трафик должен идти через VPN.
    # Активируется через rulesets.json: {"whitelist_bypass": true}
    if rulesets.get('whitelist_bypass', False):
        print("\n[GSG] === WHITELIST BYPASS MODE: ON ===", flush=True)
        print("  Весь трафик всех устройств идёт через VPN (обход белых списков провайдера)", flush=True)
        # В этом режиме очищаем sub-rules и ip_rules устройств,
        # заменяя их на глобальный проксирующий MATCH
        sub_rules = {}
        device_ip_rules = []
        for key, info in devices.items():
            if _looks_like_mac(key):
                ip = info.get('reserved_ip') or info.get('static_ip') or info.get('current_ip', '')
                if not ip:
                    continue
            else:
                ip = info.get('reserved_ip') or info.get('static_ip') or key
            mode = info.get('mode', 'smart')
            if mode == 'block':
                device_ip_rules.append(f"SRC-IP-CIDR,{ip}/32,REJECT")
            else:
                # Все устройства (кроме заблокированных) идут через VPN
                assign = info.get('assigned_node', 'auto')
                target = global_node
                if assign != 'auto':
                    for name in node_names:
                        if assign.lower() in name.lower():
                            target = name; break
                device_ip_rules.append(f"SRC-IP-CIDR,{ip}/32,{target}")
        ip_rules = device_ip_rules

    # --- GEOIP правила для AI-сервисов ---
    # Вставляются ПОСЛЕ domain_rules (чтобы bypass-домены типа yandex.ru не уходили в AI)
    # но ПЕРЕД ip_rules/SubRules устройств (чтобы QUIC/UDP трафик без SNI правильно маршрутизировался).
    # Проблема: Safari/Chrome используют QUIC (HTTP/3, UDP), Mihomo не может прочитать SNI из QUIC →
    # соединения без hostname попадают в auto (Stockholm) вместо AI (NY) → Google видит Швецию → Gemini заблокирован.
    geoip_ai_rules = []
    if "ai" in existing_group_names:
        geoip_ai_rules.append("GEOIP,google,ai")
        geoip_ai_rules.append("GEOIP,cloudflare,ai")
        # Anthropic (claude.ai, api.anthropic.com) — AS399358, собственный IP-диапазон,
        # не входит в cloudflare GEOIP. Нужен явный CIDR чтобы ECH/QUIC трафик без SNI шёл через NY.
        geoip_ai_rules.append("IP-CIDR,160.79.104.0/22,ai,no-resolve")
        geoip_ai_rules.append("IP-CIDR,160.79.108.0/22,ai,no-resolve")
        print(f"\n[GSG] GEOIP+CIDR AI rules: {geoip_ai_rules}", flush=True)
    else:
        print("\n[GSG] [WARN] Группа 'ai' не найдена, GEOIP,google и GEOIP,cloudflare не добавлены", flush=True)

    server_config["rules"] = voip_bypass_rules + bypass_network_rules + override_rules + domain_rules + geoip_ai_rules + ip_rules + custom_routing_rules + [f"MATCH,{global_node}"]

    MIHOMO_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    with open(MIHOMO_CONFIG, 'w') as f:
        yaml.dump(server_config, f, allow_unicode=True)

if __name__ == "__main__":
    main()
