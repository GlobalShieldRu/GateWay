import os
import io
import json
import zipfile
import asyncio
import time
import socket
import logging
import psutil
import httpx
import aiofiles
import hashlib
import secrets
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional
from collections import defaultdict, OrderedDict
from fastapi import FastAPI, HTTPException, Request, Response, Cookie, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

GSG_VERSION = "1.11.0"
_start_time = time.time()

app = FastAPI(title="GSG Smart Gateway API")

GSG_CONFIG_DIR = Path("/etc/gsg")
GSG_DEVICES_FILE = GSG_CONFIG_DIR / "devices.json"
GSG_ACTIVITY_FILE = GSG_CONFIG_DIR / "devices_activity.json"  # last_seen отдельно, не триггерит inotifywait
GSG_NODES_FILE = GSG_CONFIG_DIR / "nodes.json"
GSG_SUBSCRIPTION_FILE = GSG_CONFIG_DIR / "subscription.json"
GSG_RULES_FILE = GSG_CONFIG_DIR / "rules.json"
GSG_RULESETS_FILE = GSG_CONFIG_DIR / "rulesets.json"
GSG_DHCP_FILE = GSG_CONFIG_DIR / "dhcp.json"
GSG_LOG_FILE = GSG_CONFIG_DIR / "sing-box.log"
GSG_TRAFFIC_HISTORY_FILE = GSG_CONFIG_DIR / "traffic_history.json"
GSG_FEEDBACK_FILE = GSG_CONFIG_DIR / "feedback.json"
GSG_DEVICE_FILE = GSG_CONFIG_DIR / "device.json"
GSG_SETTINGS_FILE = GSG_CONFIG_DIR / "settings.json"
GSG_AUTH_FILE   = GSG_CONFIG_DIR / "auth.json"
GSG_APPS_FILE   = GSG_CONFIG_DIR / "apps.json"
DNSMASQ_LEASES  = Path("/var/lib/misc/dnsmasq.leases")

GLOBALSHIELD_DOMAIN = "globalshield.ru"
GLOBALSHIELD_API = "https://api.globalshield.ru/v1"

GATEWAY_IP = os.getenv("GSG_GATEWAY_IP", "10.10.1.139")
socket.setdefaulttimeout(0.3)

# ── Per-file write locks (prevent concurrent JSON corruption) ─────────────────
_devices_lock      = asyncio.Lock()
_traffic_lock      = asyncio.Lock()
_subscription_lock = asyncio.Lock()
_feedback_lock     = asyncio.Lock()

# ── Auth helpers ─────────────────────────────────────────────────────────────

def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations=100_000).hex()

def _load_auth() -> dict:
    try:
        return json.loads(GSG_AUTH_FILE.read_text())
    except Exception:
        return {}

def _save_auth(data: dict):
    GSG_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    GSG_AUTH_FILE.write_text(json.dumps(data))

def _verify_token(token: str | None) -> bool:
    if not token:
        return False
    auth = _load_auth()
    return token == auth.get("token")

# Public paths that don't require authentication
_PUBLIC = {"/api/login", "/api/auth/check", "/api/auth/setup", "/api/version", "/api/tunnel-hard-restart"}

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Allow public API paths and static assets
        if path in _PUBLIC or path.startswith("/static/"):
            return await call_next(request)
        # If auth is not configured yet — show setup page
        auth = _load_auth()
        if not auth.get("hash"):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Setup required"}, status_code=403)
            return FileResponse("static/setup.html")
        token = request.cookies.get("gsg_token")
        if not _verify_token(token):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            return FileResponse("static/login.html")
        return await call_next(request)

app.add_middleware(AuthMiddleware)

class TrafficMonitor:
    def __init__(self):
        self.active_conns = {}
        self.stats = defaultdict(lambda: {'total_up': 0, 'total_down': 0, 'speed_up': 0, 'speed_down': 0})
        self.node_stats = defaultdict(lambda: {'total_up': 0, 'total_down': 0, 'speed_up': 0, 'speed_down': 0})
        self.device_chains = defaultdict(lambda: defaultdict(lambda: {'speed_down': 0, 'speed_up': 0, 'total_down': 0, 'total_up': 0}))

    async def poll_mihomo(self):
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    res = await client.get("http://127.0.0.1:9090/connections", timeout=2.0)
                    if res.status_code == 200:
                        data = res.json()
                        connections = data.get("connections", [])

                        for ip in self.stats:
                            self.stats[ip]['speed_up'] = 0
                            self.stats[ip]['speed_down'] = 0
                        for node in self.node_stats:
                            self.node_stats[node]['speed_up'] = 0
                            self.node_stats[node]['speed_down'] = 0
                        for ip_key in self.device_chains:
                            for ch in self.device_chains[ip_key]:
                                self.device_chains[ip_key][ch]['speed_down'] = 0
                                self.device_chains[ip_key][ch]['speed_up'] = 0

                        current_active_ids = set()

                        for conn in connections:
                            uid = conn.get('id')
                            meta = conn.get('metadata', {})
                            ip = meta.get('sourceIP', 'unknown')
                            up = int(conn.get('upload', 0))
                            down = int(conn.get('download', 0))
                            chains = conn.get('chains', [])

                            current_active_ids.add(uid)

                            prev_up = self.active_conns.get(uid, {}).get('up', 0)
                            prev_down = self.active_conns.get(uid, {}).get('down', 0)

                            delta_up = max(0, up - prev_up)
                            delta_down = max(0, down - prev_down)

                            self.stats[ip]['total_up'] += delta_up
                            self.stats[ip]['total_down'] += delta_down
                            self.stats[ip]['speed_up'] += delta_up
                            self.stats[ip]['speed_down'] += delta_down

                            # Use chains[0] (most specific proxy), not reversed (which gives group name "auto")
                            node = next((c for c in chains if c not in ('DIRECT', 'REJECT', 'GLOBAL', '')), None)
                            if node:
                                self.node_stats[node]['total_up'] += delta_up
                                self.node_stats[node]['total_down'] += delta_down
                                self.node_stats[node]['speed_up'] += delta_up
                                self.node_stats[node]['speed_down'] += delta_down

                            host = meta.get('host') or meta.get('destinationIP', '')
                            self.active_conns[uid] = {
                                'up': up, 'down': down,
                                'src': ip,
                                'host': host,
                                'dst_port': meta.get('destinationPort', ''),
                                'dst_ip': meta.get('destinationIP', ''),
                                'network': meta.get('network', 'tcp').upper(),
                                'chains': chains,
                                'start': conn.get('start', ''),
                                'rule': conn.get('rule', ''),
                                'rule_payload': conn.get('rulePayload', ''),
                                '_seen': time.monotonic(),
                            }

                            chain_label = node if node else 'DIRECT'
                            self.device_chains[ip][chain_label]['speed_down'] += delta_down
                            self.device_chains[ip][chain_label]['speed_up'] += delta_up
                            self.device_chains[ip][chain_label]['total_down'] += delta_down
                            self.device_chains[ip][chain_label]['total_up'] += delta_up

                        self.active_conns = {k: v for k, v in self.active_conns.items() if k in current_active_ids}
                except Exception:
                    pass
                # Always evict stale connections (guards against Mihomo being unavailable)
                stale_cutoff = time.monotonic() - 300  # 5 minutes
                self.active_conns = {k: v for k, v in self.active_conns.items() if v.get('_seen', 0) >= stale_cutoff}
                await asyncio.sleep(2.0)

monitor = TrafficMonitor()


class TrafficHistory:
    def __init__(self):
        self.data: dict = {}          # ip -> {alltime_up, alltime_down, yearly, monthly, daily}
        self.nodes: dict = {}         # tag -> {alltime_up, alltime_down, yearly, monthly, daily}
        self.device_nodes: dict = {}  # ip -> {tag -> {alltime_up, alltime_down, yearly, monthly, daily}}
        self.schedule: dict = {"type": "never", "time": "00:00"}
        self._snapshots: dict = {}              # ip -> {up, down}
        self._node_snapshots: dict = {}         # tag -> {up, down}
        self._device_node_snapshots: dict = {}  # ip -> {tag -> {up, down}}

    async def load(self):
        raw = await read_json(GSG_TRAFFIC_HISTORY_FILE, {})
        self.data = raw.get("devices", {})
        self.nodes = raw.get("nodes", {})
        self.device_nodes = raw.get("device_nodes", {})
        self.schedule = raw.get("schedule", {"type": "never", "time": "00:00"})
        self._snapshots = {}
        self._node_snapshots = {}
        self._device_node_snapshots = {}

    async def save(self):
        try:
            raw = {"devices": self.data, "nodes": self.nodes, "device_nodes": self.device_nodes, "schedule": self.schedule}
            async with _traffic_lock:
                async with aiofiles.open(GSG_TRAFFIC_HISTORY_FILE, 'w') as f:
                    await f.write(json.dumps(raw, indent=2))
        except Exception:
            pass

    def _flush_bucket(self, store: dict, snapshots: dict, key: str, stat: dict):
        """Flush one entity (ip or node tag) into the given store."""
        now = datetime.now()
        cur_up = stat.get('total_up', 0)
        cur_down = stat.get('total_down', 0)
        prev = snapshots.get(key, {'up': 0, 'down': 0})
        delta_up = max(0, cur_up - prev['up'])
        delta_down = max(0, cur_down - prev['down'])
        snapshots[key] = {'up': cur_up, 'down': cur_down}
        if delta_up == 0 and delta_down == 0:
            return
        if key not in store:
            store[key] = {'alltime_up': 0, 'alltime_down': 0,
                          'yearly': {}, 'monthly': {}, 'daily': {}}
        d = store[key]
        d['alltime_up'] += delta_up
        d['alltime_down'] += delta_down
        for scope, period_key in [
            ('yearly',  now.strftime("%Y")),
            ('monthly', now.strftime("%Y-%m")),
            ('daily',   now.strftime("%Y-%m-%d")),
        ]:
            if period_key not in d[scope]:
                d[scope][period_key] = {'up': 0, 'down': 0}
            d[scope][period_key]['up'] += delta_up
            d[scope][period_key]['down'] += delta_down

    def _prune_old_daily(self):
        """Remove daily entries older than 90 days; yearly older than 5 years."""
        day_cutoff  = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        year_cutoff = str(datetime.now().year - 5)
        for store in (self.data, self.nodes):
            for entity in store.values():
                daily = entity.get('daily', {})
                for k in [k for k in daily if k < day_cutoff]:
                    del daily[k]
                yearly = entity.get('yearly', {})
                for k in [k for k in yearly if k < year_cutoff]:
                    del yearly[k]
        for ip_store in self.device_nodes.values():
            for entity in ip_store.values():
                daily = entity.get('daily', {})
                for k in [k for k in daily if k < day_cutoff]:
                    del daily[k]
                yearly = entity.get('yearly', {})
                for k in [k for k in yearly if k < year_cutoff]:
                    del yearly[k]

    def flush(self, session_stats: dict, node_stats: dict = None, device_chains: dict = None):
        for ip, stat in session_stats.items():
            self._flush_bucket(self.data, self._snapshots, ip, stat)
        if node_stats:
            for tag, stat in node_stats.items():
                self._flush_bucket(self.nodes, self._node_snapshots, tag, stat)
        if device_chains:
            for ip, chains in device_chains.items():
                if not ip:
                    continue
                if ip not in self.device_nodes:
                    self.device_nodes[ip] = {}
                if ip not in self._device_node_snapshots:
                    self._device_node_snapshots[ip] = {}
                for node_tag, stat in chains.items():
                    self._flush_bucket(
                        self.device_nodes[ip],
                        self._device_node_snapshots[ip],
                        node_tag, stat
                    )
        self._prune_old_daily()

    def reset(self, scope: str, ip: str = None):
        now = datetime.now()
        targets = [ip] if ip and ip in self.data else list(self.data.keys())
        for t in targets:
            if t not in self.data:
                continue
            d = self.data[t]
            if scope == 'all':
                self.data[t] = {'alltime_up': 0, 'alltime_down': 0,
                                 'yearly': {}, 'monthly': {}, 'daily': {}}
                if t in self._snapshots:
                    s = self._snapshots[t]
                    self._snapshots[t] = {'up': s['up'], 'down': s['down']}
            elif scope == 'daily':
                d['daily'].pop(now.strftime("%Y-%m-%d"), None)
            elif scope == 'monthly':
                d['monthly'].pop(now.strftime("%Y-%m"), None)
            elif scope == 'yearly':
                d['yearly'].pop(now.strftime("%Y"), None)

    async def run(self, mon):
        last_day = datetime.now().strftime("%Y-%m-%d")
        last_month = datetime.now().strftime("%Y-%m")
        while True:
            await asyncio.sleep(60)
            try:
                self.flush(dict(mon.stats), dict(mon.node_stats), {ip: dict(chains) for ip, chains in mon.device_chains.items()})
                await self.save()
                now = datetime.now()
                sched_type = self.schedule.get("type", "never")
                sched_time = self.schedule.get("time", "00:00")
                cur_day = now.strftime("%Y-%m-%d")
                cur_month = now.strftime("%Y-%m")
                cur_time = now.strftime("%H:%M")
                if sched_type == "daily" and cur_day != last_day and cur_time >= sched_time:
                    self.reset("daily")
                    await self.save()
                    last_day = cur_day
                elif sched_type == "monthly" and cur_month != last_month:
                    self.reset("monthly")
                    await self.save()
                    last_month = cur_month
            except Exception:
                pass


traffic_history = TrafficHistory()

_mac_vendor_cache: OrderedDict = OrderedDict()
_MAC_CACHE_MAX = 1000

@app.get("/api/vendor/{mac}")
async def get_mac_vendor(mac: str):
    oui = mac.replace(':', '').replace('-', '').upper()[:6]
    if oui in _mac_vendor_cache:
        _mac_vendor_cache.move_to_end(oui)   # LRU: mark as recently used
        return {"vendor": _mac_vendor_cache[oui]}
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(
                f"https://api.macvendors.com/{mac}",
                headers={"User-Agent": "GSG-Gateway/1.0"}
            )
            vendor = r.text.strip() if r.status_code == 200 else ""
    except Exception:
        vendor = ""
    if vendor:  # не кешируем пустые результаты — при следующем запросе попробуем снова
        _mac_vendor_cache[oui] = vendor
        if len(_mac_vendor_cache) > _MAC_CACHE_MAX:
            _mac_vendor_cache.popitem(last=False)  # evict least recently used
    return {"vendor": vendor}

async def _rotate_log():
    """Truncate sing-box.log to last 2000 lines every 10 min. Reads only tail — no full file in memory."""
    KEEP_LINES  = 2000
    THRESHOLD   = 2 * 1024 * 1024   # rotate when file exceeds 2 MB
    CHUNK       = 65536              # read 64 KB chunks from end
    while True:
        await asyncio.sleep(600)
        try:
            if not GSG_LOG_FILE.exists():
                continue
            if GSG_LOG_FILE.stat().st_size <= THRESHOLD:
                continue
            # Collect chunks from the end until we have enough newlines
            buf = b''
            pos = GSG_LOG_FILE.stat().st_size
            with open(GSG_LOG_FILE, 'rb') as f:
                while pos > 0:
                    read_size = min(CHUNK, pos)
                    pos -= read_size
                    f.seek(pos)
                    buf = f.read(read_size) + buf
                    if buf.count(b'\n') > KEEP_LINES:
                        break
            tail = b'\n'.join(buf.split(b'\n')[-KEEP_LINES:])
            if not tail.endswith(b'\n'):
                tail += b'\n'
            with open(GSG_LOG_FILE, 'wb') as f:
                f.write(tail)
        except Exception:
            pass

async def send_heartbeat():
    """Отправляет heartbeat на GlobalShield API. Вызывается при чтении/обновлении подписки."""
    try:
        device = await read_json(GSG_DEVICE_FILE, {})
        device_id    = device.get('device_id', '')
        device_token = device.get('device_token', '')
        if device_id and device_token:
            devices = await read_json(GSG_DEVICES_FILE, {})
            uptime_hours = int((time.time() - psutil.boot_time()) / 3600)

            # client_count — количество онлайн устройств из панели GSG
            client_count = 0
            try:
                active_ips = set(monitor.stats.keys())
                active_devices = await parse_arp_and_leases(active_ips)
                client_count = len(active_devices)
            except Exception:
                pass

            # mihomo_ok — TCP connect на порт 9090
            mihomo_ok = False
            try:
                s = socket.create_connection(('127.0.0.1', 9090), timeout=1)
                s.close()
                mihomo_ok = True
            except Exception:
                pass

            # active_connections
            active_connections = 0
            try:
                active_connections = len(monitor.active_conns)
            except Exception:
                pass

            # nodes_online / nodes_total
            nodes_online = 0
            nodes_total = 0
            try:
                nodes_data = await read_json(GSG_NODES_FILE, {"nodes": []})
                nodes_list = nodes_data.get("nodes", [])
                nodes_total = len(nodes_list)
                # Проверяем статус через Mihomo API
                mihomo_proxies = {}
                try:
                    async with httpx.AsyncClient(timeout=2.0) as hc:
                        r = await hc.get("http://127.0.0.1:9090/proxies")
                        if r.status_code == 200:
                            mihomo_proxies = r.json().get("proxies", {})
                except Exception:
                    pass
                for n in nodes_list:
                    tag = n.get("tag", "")
                    if tag in mihomo_proxies:
                        hist = mihomo_proxies[tag].get("history", [])
                        if hist and hist[-1].get("delay", 0) > 0:
                            nodes_online += 1
                    else:
                        nodes_online += 1  # неизвестно — считаем онлайн
            except Exception:
                pass

            # cpu_temp
            cpu_temp = 0
            try:
                temps = psutil.sensors_temperatures()
                for sensor_list in temps.values():
                    for entry in sensor_list:
                        if entry.current and entry.current > 0:
                            cpu_temp = int(entry.current)
                            break
                    if cpu_temp:
                        break
            except Exception:
                pass
            if not cpu_temp:
                try:
                    with open('/sys/class/thermal/thermal_zone0/temp') as f:
                        cpu_temp = int(f.read().strip()) // 1000
                except Exception:
                    pass

            # ram_percent
            ram_percent = 0
            try:
                ram_percent = int(psutil.virtual_memory().percent)
            except Exception:
                pass

            # disk_percent
            disk_percent = 0
            try:
                disk_percent = int(psutil.disk_usage('/').percent)
            except Exception:
                pass

            # traffic_today_down / traffic_today_up — сумма по всем устройствам за сегодня
            traffic_today_down = 0
            traffic_today_up = 0
            try:
                today_key = datetime.now().strftime("%Y-%m-%d")
                for ip_data in traffic_history.data.values():
                    daily = ip_data.get('daily', {})
                    if today_key in daily:
                        traffic_today_down += daily[today_key].get('down', 0)
                        traffic_today_up   += daily[today_key].get('up', 0)
            except Exception:
                pass

            # subscription_expiry
            subscription_expiry = ''
            try:
                sub = await read_json(GSG_SUBSCRIPTION_FILE, {})
                subscription_expiry = sub.get('expiry', '') or sub.get('last_update', '') or ''
            except Exception:
                pass

            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{GLOBALSHIELD_API}/devices/heartbeat",
                    json={
                        'version':             GSG_VERSION,
                        'client_count':        client_count,
                        'uptime_hours':        uptime_hours,
                        'mihomo_ok':           mihomo_ok,
                        'active_connections':  active_connections,
                        'nodes_online':        nodes_online,
                        'nodes_total':         nodes_total,
                        'cpu_temp':            cpu_temp,
                        'ram_percent':         ram_percent,
                        'disk_percent':        disk_percent,
                        'traffic_today_down':  traffic_today_down,
                        'traffic_today_up':    traffic_today_up,
                        'subscription_expiry': subscription_expiry,
                    },
                    headers={'X-Device-ID': device_id, 'X-Device-Token': device_token},
                )
    except Exception:
        pass



async def _migrate_devices_to_mac_keys():
    """Миграция devices.json: IP-ключи → MAC-ключи.
    Читает текущие DHCP лизы чтобы найти MAC по IP.
    Безопасна — не трогает записи у которых MAC уже является ключом."""
    try:
        if not GSG_DEVICES_FILE.exists():
            return
        raw = GSG_DEVICES_FILE.read_text().strip()
        if not raw:
            return
        data = json.loads(raw)
        if not data:
            return

        # Проверяем: если все ключи уже выглядят как MAC — миграция не нужна
        def _looks_like_mac(s: str) -> bool:
            parts = s.split(':')
            return len(parts) == 6 and all(len(p) == 2 for p in parts)

        def _looks_like_ip(s: str) -> bool:
            parts = s.split('.')
            return len(parts) == 4 and all(p.isdigit() for p in parts)

        ip_keys = [k for k in data if _looks_like_ip(k)]
        if not ip_keys:
            return  # уже мигрировано или пустой файл

        # Читаем лизы для маппинга IP → MAC
        ip_to_mac: dict = {}
        if DNSMASQ_LEASES.exists():
            try:
                for line in DNSMASQ_LEASES.read_text().splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 4:
                        mac = parts[1].lower()
                        ip  = parts[2]
                        ip_to_mac[ip] = mac
            except Exception:
                pass

        # Также читаем ARP таблицу
        try:
            lan_prefix = GATEWAY_IP.rsplit('.', 1)[0] + '.'
            with open('/proc/net/arp', 'r') as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                        if parts[0].startswith(lan_prefix):
                            ip_to_mac.setdefault(parts[0], parts[3].lower())
        except Exception:
            pass

        new_data: dict = {}
        for key, cfg in data.items():
            if _looks_like_mac(key):
                # Уже MAC-ключ — оставляем как есть
                new_data[key] = cfg
                continue
            if _looks_like_ip(key):
                # IP-ключ: ищем MAC
                mac = cfg.get('mac', '') or ip_to_mac.get(key, '')
                if mac and _looks_like_mac(mac):
                    # Мигрируем в MAC-ключ
                    new_cfg = dict(cfg)
                    # Если reserved_ip не задан — используем текущий IP как reserved
                    if not new_cfg.get('reserved_ip') and not new_cfg.get('static_ip'):
                        new_cfg['reserved_ip'] = key
                    elif new_cfg.get('static_ip') and not new_cfg.get('reserved_ip'):
                        new_cfg['reserved_ip'] = new_cfg['static_ip']
                    new_cfg['mac'] = mac
                    new_cfg['current_ip'] = key
                    # Не дублируем по MAC
                    if mac not in new_data:
                        new_data[mac] = new_cfg
                    logging.info(f"[MIGRATE] {key} → {mac} (reserved_ip={new_cfg.get('reserved_ip','')})")
                else:
                    # MAC неизвестен — сохраняем под IP-ключом временно
                    new_data[key] = cfg
                    logging.warning(f"[MIGRATE] {key}: MAC неизвестен, оставляем IP-ключ")
            else:
                new_data[key] = cfg

        GSG_DEVICES_FILE.write_text(json.dumps(new_data, indent=2))
        logging.info(f"[MIGRATE] devices.json мигрирован: {len(ip_keys)} IP-записей → MAC-ключи")
    except Exception as e:
        logging.warning(f"[MIGRATE] Ошибка миграции devices.json: {e}")


DEVICE_EVICT_DAYS = 30  # устройства без активности дольше этого удаляются

CONN_FLOOD_THRESHOLD = 500   # соединений на устройство — порог срабатывания
CONN_WATCHDOG_INTERVAL = 120  # секунд между проверками

async def _connection_watchdog():
    """Каждые 2 минуты проверяет таблицу соединений Mihomo.
    Если одно устройство занимает более CONN_FLOOD_THRESHOLD соединений — убивает их все.
    Защита от зависших VPN-клиентов и retry-штормов без блокировки устройства."""
    await asyncio.sleep(30)  # дать Mihomo время запуститься
    while True:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get("http://127.0.0.1:9090/connections", timeout=5.0)
                if r.status_code == 200:
                    data = r.json()
                    conns = data.get("connections", [])
                    # Группируем по sourceIP
                    by_ip: dict[str, list] = {}
                    for c in conns:
                        ip = c.get("metadata", {}).get("sourceIP", "")
                        if ip:
                            by_ip.setdefault(ip, []).append(c["id"])
                    for ip, ids in by_ip.items():
                        if len(ids) >= CONN_FLOOD_THRESHOLD:
                            logging.warning(
                                f"[WATCHDOG] {ip} имеет {len(ids)} соединений (>={CONN_FLOOD_THRESHOLD}) — чистим"
                            )
                            for cid in ids:
                                try:
                                    await client.delete(f"http://127.0.0.1:9090/connections/{cid}", timeout=2.0)
                                except Exception:
                                    pass
                            logging.warning(f"[WATCHDOG] {ip}: убито {len(ids)} соединений")
        except Exception as e:
            logging.debug(f"[WATCHDOG] Ошибка: {e}")
        await asyncio.sleep(CONN_WATCHDOG_INTERVAL)


async def _stale_connection_cleaner():
    """Каждые 60 секунд закрывает соединения с нулевым трафиком старше 120 секунд.
    Решает проблему зависания Telegram и других долгоживущих соединений через мёртвые ноды."""
    await asyncio.sleep(60)
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get("http://127.0.0.1:9090/connections")
                if r.status_code == 200:
                    conns = r.json().get("connections", [])
                    now = time.time()
                    closed = 0
                    for c in conns:
                        dl = c.get("download", 0)
                        ul = c.get("upload", 0)
                        start = c.get("start", "")
                        cid = c.get("id", "")
                        if not cid or not start:
                            continue
                        try:
                            from datetime import datetime, timezone
                            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                            age = now - dt.timestamp()
                        except Exception:
                            continue
                        if age > 120 and (dl + ul) == 0:
                            try:
                                await client.delete(f"http://127.0.0.1:9090/connections/{cid}", timeout=2.0)
                                closed += 1
                            except Exception:
                                pass
                    if closed:
                        logging.info(f"[STALE] Закрыто {closed} idle-соединений (0 байт, >120с)")
        except Exception as e:
            logging.debug(f"[STALE] Ошибка: {e}")
        await asyncio.sleep(60)


async def _evict_stale_devices():
    """Раз в сутки удаляет устройства, не появлявшиеся более DEVICE_EVICT_DAYS дней."""
    while True:
        await asyncio.sleep(86400)  # первый запуск через 24ч после старта
        try:
            threshold = time.time() - DEVICE_EVICT_DAYS * 86400
            try:
                activity = json.loads(GSG_ACTIVITY_FILE.read_text())
            except Exception:
                activity = {}
            async with _devices_lock:
                configs = await read_json(GSG_DEVICES_FILE, {})
                to_delete = []
                for key, cfg in configs.items():
                    # last_seen теперь в devices_activity.json
                    last_seen = activity.get(key) or cfg.get('last_seen')
                    if last_seen is None:
                        # Старая запись без last_seen — пропускаем, grace period
                        continue
                    if last_seen < threshold:
                        to_delete.append(key)
                if to_delete:
                    for key in to_delete:
                        cfg = configs.pop(key)
                        logging.info(
                            f"[EVICT] Удалено устройство {key} "
                            f"(mac={cfg.get('mac','?')}, ip={cfg.get('current_ip','?')}, "
                            f"last_seen={int(cfg.get('last_seen',0))})"
                        )
                    async with aiofiles.open(GSG_DEVICES_FILE, 'w') as f:
                        await f.write(json.dumps(configs, indent=2))
                    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_dhcp", 'w') as f:
                        await f.write("1")
                    logging.info(f"[EVICT] Итого удалено: {len(to_delete)} устройств")
                else:
                    logging.info("[EVICT] Устаревших устройств не найдено")
        except Exception as e:
            logging.warning(f"[EVICT] Ошибка при чистке устройств: {e}")


async def _auto_update_loop():
    """Ежечасная проверка: если включено автообновление и вышла новая версия — запускаем OTA."""
    await asyncio.sleep(300)  # ждём 5 минут после старта
    while True:
        try:
            settings = await read_json(GSG_SETTINGS_FILE, {"auto_update": False})
            if settings.get("auto_update"):
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest")
                    r.raise_for_status()
                    data = r.json()
                    latest = data.get("tag_name", "").lstrip("v")
                    if latest and latest != GSG_VERSION and latest > GSG_VERSION:
                        if not UPDATE_TRIGGER.exists():
                            logging.info(f"[AUTO-UPDATE] Обнаружена новая версия {latest}, запускаем OTA")
                            import os as _os
                            fd = _os.open(str(UPDATE_TRIGGER), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY, 0o644)
                            with _os.fdopen(fd, 'w') as f:
                                f.write(f"auto_update_triggered_at={datetime.now().isoformat()}\n")
                        else:
                            logging.info("[AUTO-UPDATE] Обновление уже запущено, пропускаем")
        except FileExistsError:
            pass
        except Exception as e:
            logging.warning(f"[AUTO-UPDATE] Ошибка проверки: {e}")
        await asyncio.sleep(21600)  # проверка каждые 6 часов


@app.on_event("startup")
async def startup_event():
    # Ensure DNS works (resolv.conf may be empty in network_mode:host containers)
    try:
        with open('/etc/resolv.conf', 'r') as f:
            content = f.read()
        if 'nameserver' not in content:
            with open('/etc/resolv.conf', 'a') as f:
                f.write('\nnameserver 8.8.8.8\nnameserver 1.1.1.1\n')
    except Exception:
        pass
    # Восстановить rules.json если повреждён
    try:
        rules = json.loads(GSG_RULES_FILE.read_text())
    except Exception:
        bak = Path(str(GSG_RULES_FILE) + '.bak')
        if bak.exists():
            GSG_RULES_FILE.write_text(bak.read_text())
    # Миграция devices.json: IP-ключи → MAC-ключи
    await _migrate_devices_to_mac_keys()
    await traffic_history.load()
    asyncio.create_task(monitor.poll_mihomo())
    asyncio.create_task(traffic_history.run(monitor))
    asyncio.create_task(_rotate_log())
    asyncio.create_task(_evict_stale_devices())
    asyncio.create_task(_connection_watchdog())
    # asyncio.create_task(_stale_connection_cleaner())  # отключено: убивало Discord WebSocket (keep-alive с нулевым трафиком)
    async def _periodic_heartbeat():
        await asyncio.sleep(60)  # первый — через минуту после старта
        while True:
            await send_heartbeat()
            await asyncio.sleep(300)  # каждые 5 минут
    asyncio.create_task(_periodic_heartbeat())
    asyncio.create_task(_auto_update_loop())

@app.get("/api/traffic")
async def get_traffic():
    return monitor.stats

@app.get("/api/traffic/nodes")
async def get_traffic_nodes():
    return dict(monitor.node_stats)

@app.get("/api/traffic/history")
async def get_traffic_history():
    return {"devices": traffic_history.data, "nodes": traffic_history.nodes, "device_nodes": traffic_history.device_nodes, "schedule": traffic_history.schedule}

@app.get("/api/debug/node-stats")
async def debug_node_stats():
    """Debug: show raw node_stats keys and totals to help diagnose node traffic matching."""
    return {
        "live_node_stats": {k: {"up": v["total_up"], "down": v["total_down"]} for k, v in monitor.node_stats.items()},
        "history_node_keys": list(traffic_history.nodes.keys()),
    }

class TrafficResetRequest(BaseModel):
    scope: str  # all, daily, monthly, yearly
    ip: Optional[str] = None

@app.post("/api/traffic/reset")
async def reset_traffic(data: TrafficResetRequest):
    traffic_history.reset(data.scope, data.ip)
    await traffic_history.save()
    return {"success": True}

class TrafficScheduleUpdate(BaseModel):
    type: str   # never, daily, monthly
    time: str = "00:00"

@app.put("/api/traffic/schedule")
async def update_traffic_schedule(data: TrafficScheduleUpdate):
    traffic_history.schedule = {"type": data.type, "time": data.time}
    await traffic_history.save()
    return {"success": True}

async def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        async with aiofiles.open(path, 'r') as f:
            content = await f.read()
        return json.loads(content)
    except json.JSONDecodeError:
        # Corrupt file — save a backup so data isn't silently lost
        try:
            bak = path.with_suffix(path.suffix + '.bak')
            async with aiofiles.open(bak, 'w') as f:
                await f.write(content)
        except Exception:
            pass
        return default
    except Exception:
        return default

async def _backup_rules():
    """Бэкапит rules.json перед перезаписью."""
    src = GSG_RULES_FILE
    bak = Path(str(GSG_RULES_FILE) + '.bak')
    if src.exists():
        try:
            content = src.read_text()
            if content.strip():  # не бэкапим пустой файл
                bak.write_text(content)
        except Exception:
            pass

async def parse_arp_and_leases(active_ips: set = None):
    devices: dict = {}  # ip → device
    lan_prefix = GATEWAY_IP.rsplit('.', 1)[0] + '.'

    # ── ARP table ────────────────────────────────────────────────────────────
    try:
        async with aiofiles.open('/proc/net/arp', 'r') as f:
            lines = await f.readlines()
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                    ip = parts[0]
                    if ip.startswith(lan_prefix) and ip != GATEWAY_IP and not ip.startswith("172."):
                        hostname = "Устройство"
                        try:
                            hostname = socket.gethostbyaddr(ip)[0]
                        except Exception:
                            pass
                        arp_flags = int(parts[2], 16) if len(parts) > 2 else 0
                        devices[ip] = {"ip": ip, "mac": parts[3].lower(), "hostname": hostname, "_arp_flags": arp_flags}
    except Exception:
        pass

    # ── DHCP leases ──────────────────────────────────────────────────────────
    mac_hostname: dict = {}          # mac → best hostname
    mac_lease: dict = {}             # mac → {ip, expiry}  (latest expiry wins)

    if DNSMASQ_LEASES.exists():
        try:
            async with aiofiles.open(DNSMASQ_LEASES, 'r') as f:
                async for line in f:
                    parts = line.strip().split()
                    if len(parts) < 4:
                        continue
                    expiry = int(parts[0]) if parts[0].isdigit() else 0
                    mac    = parts[1].lower()
                    ip     = parts[2]
                    name   = parts[3] if parts[3] != "*" else ""
                    if name:
                        mac_hostname[mac] = name
                    if mac not in mac_lease or expiry > mac_lease[mac]["expiry"]:
                        mac_lease[mac] = {"ip": ip, "expiry": expiry}
                    # Add lease-only entries (device not in ARP yet)
                    if ip.startswith(lan_prefix) and not ip.startswith("172.") and ip not in devices:
                        devices[ip] = {"ip": ip, "mac": mac, "hostname": name or "Устройство"}
        except Exception:
            pass

    # Apply lease hostnames to ARP entries whose name is still default
    for dev in devices.values():
        if dev["hostname"] in ("Устройство", "Unknown"):
            name = mac_hostname.get(dev["mac"], "")
            if name:
                dev["hostname"] = name

    # ── Deduplicate by MAC ───────────────────────────────────────────────────
    # For a MAC with multiple IPs, keep the "most alive" one:
    #   1. prefers IP with recent traffic (active_ips)
    #   2. falls back to the lease IP
    mac_keep: dict = {}  # mac → ip to keep
    for ip, dev in devices.items():
        mac = dev["mac"]
        if mac not in mac_keep:
            mac_keep[mac] = ip
            continue
        current_kept = mac_keep[mac]
        # Rule 0: prefer reachable ARP entry (flags & 0x2) over incomplete (0x0)
        cur_reachable  = (devices[current_kept].get("_arp_flags", 0) & 0x2) != 0
        this_reachable = (dev.get("_arp_flags", 0) & 0x2) != 0
        if this_reachable and not cur_reachable:
            mac_keep[mac] = ip
            continue
        if cur_reachable and not this_reachable:
            continue
        # Rule 1: prefer the IP that has active traffic
        if active_ips:
            current_active = current_kept in active_ips
            this_active    = ip in active_ips
            if this_active and not current_active:
                mac_keep[mac] = ip
                continue
            if current_active and not this_active:
                continue
        # Rule 2: prefer the lease IP
        lease_ip = mac_lease.get(mac, {}).get("ip")
        if lease_ip and ip == lease_ip:
            mac_keep[mac] = ip

    # Remove stale duplicates
    stale = [ip for ip, dev in devices.items() if mac_keep.get(dev["mac"]) != ip]
    for ip in stale:
        del devices[ip]

    return list(devices.values())

async def ping_tcp(host: str, port: int, timeout: float = 1.0):
    try:
        start = time.time()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, local_addr=(GATEWAY_IP, 0)),
            timeout
        )
        writer.close()
        await writer.wait_closed()
        return int((time.time() - start) * 1000)
    except Exception:
        return -1

class DeviceUpdate(BaseModel):
    mode: str
    assigned_node: str
    tiktok_node: str = "auto"
    custom_name: str = ""
    static_ip: str = ""
    mac: str = ""
    block_vpn_app: bool = False

class RulesUpdate(BaseModel):
    direct: List[str]
    proxy: List[str]
    proxy_node: Optional[str] = None

class DHCPUpdate(BaseModel):
    gateway: str
    pool_start: str
    pool_end: str
    dns: str

class GlobalNodeUpdate(BaseModel):
    global_node: str

class RouteOverride(BaseModel):
    domain: str
    target: str  # "DIRECT" | "node:<name>" | "group:<name>"

class DeleteOverridesRequest(BaseModel):
    domains: List[str]

@app.get("/api/status")
async def get_status():
    temp = 0
    try:
        if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                temp = int(f.read()) / 1000
    except Exception:
        pass

    return {
        "cpu_percent": psutil.cpu_percent(),
        "memory_used": psutil.virtual_memory().used,
        "memory_total": psutil.virtual_memory().total,
        "temperature": round(temp, 1),
        "uptime": int(psutil.boot_time())
    }

_net_cache: dict = {"data": None, "ts": 0}
_NET_CACHE_TTL = 30   # обновлять раз в 30 секунд
_tunnel_ip_cache: dict = {"data": None, "ts": 0}  # кэш внешнего IP туннеля (долгоживущий)
_TUNNEL_IP_TTL = 300  # обновлять внешний IP туннеля раз в 5 минут

@app.get("/api/network-status")
async def get_network_status():
    global _net_cache
    now = time.time()

    # Отдаём кэш если он свежий — не долбим ip-api.com каждые 5 секунд
    if _net_cache["data"] and (now - _net_cache["ts"]) < _NET_CACHE_TTL:
        return _net_cache["data"]

    direct = {"ip": "Оффлайн", "country": "-", "status": "error"}
    tunnel = {"ip": "Оффлайн", "country": "-", "status": "error"}
    youtube = {"status": "error", "ping": 0}

    _direct_services = [
        ("http://ip-api.com/json",  lambda d: {"ip": d.get("query"), "country": d.get("countryCode"), "isp": d.get("isp", ""), "org": d.get("org", "")}),
        ("https://ipwho.is/",       lambda d: {"ip": d.get("ip"), "country": d.get("country_code"), "isp": d.get("connection", {}).get("isp", ""), "org": ""}),
        ("https://ipinfo.io/json",  lambda d: {"ip": d.get("ip"), "country": d.get("country"), "isp": d.get("org", ""), "org": d.get("org", "")}),
        ("https://ipapi.co/json/",  lambda d: {"ip": d.get("ip"), "country": d.get("country_code"), "isp": d.get("org", ""), "org": d.get("asn", "")}),
        ("https://freeipapi.com/api/json", lambda d: {"ip": d.get("ipAddress"), "country": d.get("countryCode"), "isp": d.get("ispName", ""), "org": ""}),
    ]
    async with httpx.AsyncClient(timeout=4.0) as client:
        for url, parser in _direct_services:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    d = r.json()
                    parsed = parser(d)
                    if parsed.get("ip"):
                        direct = {**parsed, "status": "ok"}
                        break
            except Exception:
                continue

    # ── Tunnel health: проверяем Mihomo API (порт 9090, не занят спидтестом) ──
    # Если Mihomo отвечает → туннель жив. Внешний IP получаем отдельно, с долгим кэшем.
    global _tunnel_ip_cache
    mihomo_ok = False
    try:
        async with httpx.AsyncClient(timeout=2.0) as mc:
            mr = await mc.get("http://127.0.0.1:9090/version")
            mihomo_ok = mr.status_code == 200
    except Exception:
        pass

    if mihomo_ok:
        # Mihomo работает — туннель поднят
        # Обновляем внешний IP только если кэш устарел
        if not _tunnel_ip_cache["data"] or (now - _tunnel_ip_cache["ts"]) > _TUNNEL_IP_TTL:
            try:
                proxies = {"http://": "http://127.0.0.1:2080", "https://": "http://127.0.0.1:2080"}
                async with httpx.AsyncClient(proxies=proxies, timeout=5.0) as client:
                    r = await client.get("http://ip-api.com/json")
                    if r.status_code == 200:
                        d = r.json()
                        _tunnel_ip_cache = {"data": {"ip": d.get("query"), "country": d.get("countryCode")}, "ts": now}
            except Exception:
                pass
        ip_data = _tunnel_ip_cache["data"] or {}
        tunnel = {"ip": ip_data.get("ip", "—"), "country": ip_data.get("country", ""), "status": "ok"}

        try:
            proxies = {"http://": "http://127.0.0.1:2080", "https://": "http://127.0.0.1:2080"}
            async with httpx.AsyncClient(proxies=proxies, timeout=5.0) as client:
                start = time.time()
                yt = await client.get("https://www.youtube.com/favicon.ico", follow_redirects=True)
                if yt.status_code == 200:
                    youtube = {"status": "ok", "ping": int((time.time() - start) * 1000)}
        except Exception:
            pass

    result = {"direct": direct, "tunnel": tunnel, "youtube": youtube}
    _net_cache = {"data": result, "ts": now}
    return result

@app.get("/api/nodes/ping")
async def ping_nodes():
    """Пингует каждый уникальный сервер один раз, применяет результат ко всем узлам на нём."""
    data = await read_json(GSG_NODES_FILE, {"nodes": []})
    nodes = data.get("nodes", [])

    # Один представитель на каждый уникальный server-хост
    unique_servers: dict[str, dict] = {}
    for n in nodes:
        srv = n.get("server", "")
        if srv and srv not in unique_servers:
            unique_servers[srv] = n

    server_results: dict[str, int] = {}

    async def measure_server(srv: str, n: dict):
        tag = n.get("tag", "")
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(
                    f"http://127.0.0.1:9090/proxies/{tag}/delay",
                    params={"url": "https://www.google.com/", "timeout": 5000}
                )
                server_results[srv] = r.json().get("delay", -1) if r.status_code == 200 else -1
        except Exception:
            server_results[srv] = -1

    await asyncio.gather(*(measure_server(srv, n) for srv, n in unique_servers.items()))

    # Все узлы одного сервера получают одинаковый пинг
    return {n.get("tag", ""): server_results.get(n.get("server", ""), -1) for n in nodes}

@app.get("/api/nodes/dashboard")
async def get_nodes_dash():
    data = await read_json(GSG_NODES_FILE, {"nodes": []})
    nodes = data.get("nodes", [])

    # Один представитель на каждый уникальный server-хост
    unique_servers: dict[str, dict] = {}
    for n in nodes:
        srv = n.get("server", "")
        if srv and srv not in unique_servers:
            unique_servers[srv] = n

    # Получаем задержки из Mihomo (берём последнюю запись истории)
    mihomo_delays: dict[str, int] = {}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get("http://127.0.0.1:9090/proxies")
            if r.status_code == 200:
                for name, proxy in r.json().get("proxies", {}).items():
                    hist = proxy.get("history", [])
                    if hist:
                        mihomo_delays[name] = hist[-1].get("delay", -1)
    except Exception:
        pass

    server_ping: dict[str, int] = {}

    async def ping_server(srv: str, n: dict):
        tag = n.get("tag", "")
        p = mihomo_delays.get(tag, -1)
        if p <= 0:
            # История устарела или показывает ошибку — свежий TCP-пинг
            p = await ping_tcp(n["server"], int(n["server_port"]))
        server_ping[srv] = p

    await asyncio.gather(*(ping_server(srv, n) for srv, n in unique_servers.items()))

    for n in nodes:
        p = server_ping.get(n.get("server", ""), -1)
        n["ping"] = p
        n["status"] = "online" if p > 0 else "offline"

    return nodes

@app.get("/api/devices")
async def get_devices():
    active_ips = set(monitor.stats.keys())
    active_devices = await parse_arp_and_leases(active_ips)
    configs = await read_json(GSG_DEVICES_FILE, {})

    def _looks_like_mac(s: str) -> bool:
        parts = s.split(':')
        return len(parts) == 6 and all(len(p) == 2 for p in parts)

    # Строим маппинг MAC → конфиг (поддерживаем оба формата ключей: MAC и IP)
    mac_configs: dict = {}
    for key, cfg in configs.items():
        if _looks_like_mac(key):
            mac_configs[key] = cfg
        elif cfg.get('mac') and _looks_like_mac(cfg['mac']):
            # IP-ключ с MAC внутри — берём конфиг по MAC
            mac_configs[cfg['mac']] = cfg

    changed = False
    new_mac_entries: dict = {}
    activity: dict = {}
    try:
        activity = json.loads(GSG_ACTIVITY_FILE.read_text())
    except Exception:
        pass

    for d in active_devices:
        mac = d.get('mac', '')
        ip  = d['ip']
        if not mac:
            continue
        if mac not in mac_configs:
            # Новое устройство — создаём запись с MAC-ключом
            # Автоматически резервируем текущий IP
            new_mac_entries[mac] = {
                "mode": "smart",
                "assigned_node": "auto",
                "tiktok_node": "auto",
                "custom_name": "",
                "reserved_ip": ip,
                "mac": mac,
                "current_ip": ip,
            }
            activity[mac] = time.time()
            changed = True
        else:
            # Обновляем current_ip если изменился — это важно для маршрутизации
            if mac_configs[mac].get('current_ip') != ip:
                mac_configs[mac]['current_ip'] = ip
                changed = True
            # last_seen — пишем в отдельный файл, НЕ в devices.json
            # devices.json не триггерит inotifywait → нет лишних Mihomo hot-reload
            now = time.time()
            if now - activity.get(mac, 0) >= 30:
                activity[mac] = now

    if new_mac_entries:
        mac_configs.update(new_mac_entries)

    # Пишем activity отдельно — inotifywait его не смотрит
    try:
        GSG_ACTIVITY_FILE.write_text(json.dumps(activity))
    except Exception:
        pass

    if changed:
        # Перестраиваем configs: только MAC-ключи
        new_configs = {k: v for k, v in mac_configs.items()}
        async with _devices_lock:
            async with aiofiles.open(GSG_DEVICES_FILE, 'w') as f:
                await f.write(json.dumps(new_configs, indent=2))
        if new_mac_entries:
            async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
                await f.write("1")

    result = []
    for d in active_devices:
        mac = d.get('mac', '')
        conf = mac_configs.get(mac, {})
        current_ip = d['ip']
        result.append({
            **d,
            "mode": conf.get("mode", "smart"),
            "assigned_node": conf.get("assigned_node", "auto"),
            "tiktok_node": conf.get("tiktok_node", "auto"),
            "custom_name": conf.get("custom_name", ""),
            "current_ip": current_ip,
            "block_vpn_app": conf.get("block_vpn_app", False),
        })
    return result

@app.put("/api/devices/assign-node")
async def assign_node_to_all(body: dict):
    """Переключает assigned_node для всех устройств разом, сохраняя снапшот предыдущих значений."""
    node = body.get("assigned_node", "auto")
    async with _devices_lock:
        configs = await read_json(GSG_DEVICES_FILE, {})
        for dev in configs.values():
            if "_prev_assigned_node" not in dev:
                dev["_prev_assigned_node"] = dev.get("assigned_node", "auto")
            dev["assigned_node"] = node
        async with aiofiles.open(GSG_DEVICES_FILE, 'w') as f:
            await f.write(json.dumps(configs, indent=2))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_nftables", 'w') as f:
        await f.write("1")
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
        await f.write("1")
    return {"ok": True, "assigned_node": node, "count": len(configs)}

@app.put("/api/devices/restore-nodes")
async def restore_nodes():
    """Восстанавливает индивидуальные assigned_node из снапшота."""
    async with _devices_lock:
        configs = await read_json(GSG_DEVICES_FILE, {})
        restored = 0
        for dev in configs.values():
            if "_prev_assigned_node" in dev:
                dev["assigned_node"] = dev.pop("_prev_assigned_node")
                restored += 1
            else:
                dev["assigned_node"] = "auto"
        async with aiofiles.open(GSG_DEVICES_FILE, 'w') as f:
            await f.write(json.dumps(configs, indent=2))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_nftables", 'w') as f:
        await f.write("1")
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
        await f.write("1")
    return {"ok": True, "restored": restored}

@app.put("/api/devices/{ip}")
async def update_device(ip: str, data: DeviceUpdate):
    def _looks_like_mac(s: str) -> bool:
        parts = s.split(':')
        return len(parts) == 6 and all(len(p) == 2 for p in parts)

    async with _devices_lock:
        configs = await read_json(GSG_DEVICES_FILE, {})

        # Ищем существующую запись: сначала по MAC (новый формат), затем по IP
        new_mac = data.mac or ""
        existing_key = None
        existing = {}

        # Если передан MAC — ищем по нему
        if new_mac and _looks_like_mac(new_mac):
            existing_key = new_mac
            existing = configs.get(new_mac, {})
        # Иначе ищем IP-ключ или запись с mac==ip среди всех
        if not existing_key:
            if ip in configs:
                existing_key = ip
                existing = configs[ip]
                if not new_mac:
                    new_mac = existing.get('mac', '')
            else:
                # Поиск по MAC внутри IP-ключей
                for k, v in configs.items():
                    if not _looks_like_mac(k) and v.get('mac') == new_mac:
                        existing_key = k
                        existing = v
                        break

        if not new_mac:
            new_mac = existing.get('mac', '')

        # reserved_ip: если пользователь передал static_ip — обновляем,
        # иначе сохраняем существующее значение (для dnsmasq, не трогаем без явного запроса)
        new_reserved = data.static_ip or existing.get('reserved_ip') or existing.get('static_ip') or ''

        new_cfg = {
            "mode": data.mode,
            "assigned_node": data.assigned_node,
            "tiktok_node": data.tiktok_node,
            "custom_name": data.custom_name,
            "mac": new_mac,
            "current_ip": existing.get('current_ip', ip),
            "block_vpn_app": data.block_vpn_app,
        }
        # Сохраняем reserved_ip/static_ip только если есть (не засоряем новые записи)
        if new_reserved:
            new_cfg["reserved_ip"] = new_reserved
            new_cfg["static_ip"] = new_reserved   # обратная совместимость с dnsmasq

        # Сохраняем под MAC-ключом (или IP если MAC неизвестен)
        save_key = new_mac if (new_mac and _looks_like_mac(new_mac)) else ip

        # Удаляем старые записи (IP-ключ или дубли)
        stale = []
        if existing_key and existing_key != save_key:
            stale.append(existing_key)
        # Удаляем дубли с тем же MAC под другими ключами
        if new_mac:
            for k, v in configs.items():
                if k != save_key and (k == new_mac or v.get('mac') == new_mac):
                    stale.append(k)
        for k in stale:
            configs.pop(k, None)

        configs[save_key] = new_cfg
        async with aiofiles.open(GSG_DEVICES_FILE, 'w') as f:
            await f.write(json.dumps(configs, indent=2))

    # Reload nftables only if routing mode changed
    routing_changed = (
        data.mode != existing.get("mode") or
        data.assigned_node != existing.get("assigned_node", "auto") or
        data.tiktok_node != existing.get("tiktok_node", "auto") or
        data.block_vpn_app != existing.get("block_vpn_app", False)
    )
    if routing_changed:
        async with aiofiles.open(GSG_CONFIG_DIR / ".reload_nftables", 'w') as f:
            await f.write("1")
        async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
            await f.write("1")
    # Trigger dnsmasq reload if reserved_ip or MAC changed
    old_reserved = existing.get('reserved_ip') or existing.get('static_ip', '')
    reserved_changed = new_reserved != old_reserved
    if new_reserved or new_mac:
        async with aiofiles.open(GSG_CONFIG_DIR / ".reload_dhcp", 'w') as f:
            await f.write("1")
    # Force DHCP renew: release old lease so client gets new reserved IP
    if reserved_changed and new_mac and new_reserved:
        asyncio.create_task(_dhcp_force_renew(ip, new_mac))
    return {"success": True}


async def _dhcp_force_renew(old_ip: str, mac: str):
    """Release old DHCP lease to force client to renew and get new static IP."""
    try:
        await asyncio.sleep(3)  # Wait for dnsmasq to reload config
        iface = os.getenv("GSG_LAN_INTERFACE", "eth0")
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "gsg-dhcp", "dhcp_release", iface, old_ip, mac,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            logging.info(f"[DHCP] Released lease {old_ip} ({mac}) — client should renew")
        else:
            logging.warning(f"[DHCP] dhcp_release failed: {stderr.decode().strip()}")
    except Exception as e:
        logging.warning(f"[DHCP] Force renew failed: {e}")

def _find_device_conf(configs: dict, ip: str):
    """Находит конфиг устройства по IP: ищет MAC-ключ с current_ip==ip,
    затем IP-ключ (обратная совместимость). Возвращает (key, conf)."""
    def _looks_like_mac(s: str) -> bool:
        parts = s.split(':')
        return len(parts) == 6 and all(len(p) == 2 for p in parts)
    # MAC-ключ с current_ip
    for k, v in configs.items():
        if _looks_like_mac(k) and v.get('current_ip') == ip:
            return k, v
    # IP-ключ (старый формат)
    if ip in configs:
        return ip, configs[ip]
    return None, {}


@app.get("/api/nodes")
async def get_nodes():
    data = await read_json(GSG_NODES_FILE, {"nodes": []})
    return data.get("nodes", [])

@app.get("/api/license")
async def get_license():
    device = await read_json(GSG_DEVICE_FILE, {})
    if not device.get("device_id"):
        try:
            for iface in ("eth0", "eth1", "br-lan", "enp0s3"):
                mac_path = f"/sys/class/net/{iface}/address"
                if os.path.exists(mac_path):
                    mac = open(mac_path).read().strip().replace(":", "").upper()
                    if mac and mac != "000000000000":
                        device["device_id"] = f"GSG-{mac}"
                        break
        except Exception:
            pass
        if device.get("device_id"):
            async with aiofiles.open(GSG_DEVICE_FILE, 'w') as f:
                await f.write(json.dumps(device, indent=2))
    nodes = await read_json(GSG_NODES_FILE, {})
    error = nodes.get("error")
    return {
        "device_id": device.get("device_id", ""),
        "has_token": bool(device.get("device_token", "")),
        "registered_at": device.get("registered_at"),
        "error": error,  # "unauthorized" | "invalid_domain" | None
    }

@app.get("/api/subscription")
async def get_sub():
    asyncio.create_task(send_heartbeat())
    return await read_json(GSG_SUBSCRIPTION_FILE, {"url": "", "global_node": "auto", "last_update": None})

@app.put("/api/subscription")
async def update_sub(data: dict):
    url = data.get("url")
    if not url:
        raise HTTPException(400)

    # Валидируем домен — только globalshield.ru
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if not (host == GLOBALSHIELD_DOMAIN or host.endswith("." + GLOBALSHIELD_DOMAIN)):
        raise HTTPException(403, detail="invalid_domain")

    # Если токена нет — попробуем получить его сейчас
    device = await read_json(GSG_DEVICE_FILE, {})
    if device.get("device_id") and not device.get("device_token"):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(f"{GLOBALSHIELD_API}/devices/register", json={
                    "device_id": device["device_id"],
                    "hostname": socket.gethostname(),
                })
                if r.status_code == 200:
                    token = r.json().get("device_token", "")
                    if token:
                        device["device_token"] = token
                        async with aiofiles.open(GSG_DEVICE_FILE, 'w') as f:
                            await f.write(json.dumps(device))
        except Exception:
            pass

    async with _subscription_lock:
        sub = await read_json(GSG_SUBSCRIPTION_FILE, {})
        sub["url"] = url
        sub["last_update"] = datetime.now().isoformat()
        async with aiofiles.open(GSG_SUBSCRIPTION_FILE, 'w') as f:
            await f.write(json.dumps(sub))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
        await f.write("1")
    asyncio.create_task(send_heartbeat())
    return {"success": True}

@app.put("/api/subscription/node")
async def update_global_node(data: GlobalNodeUpdate):
    async with _subscription_lock:
        sub = await read_json(GSG_SUBSCRIPTION_FILE, {"url": "", "global_node": "auto"})
        sub["global_node"] = data.global_node
        async with aiofiles.open(GSG_SUBSCRIPTION_FILE, 'w') as f:
            await f.write(json.dumps(sub))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
        await f.write("1")
    return {"success": True}

@app.get("/api/rules")
async def get_rules():
    return await read_json(GSG_RULES_FILE, {"direct": [], "proxy": []})

@app.put("/api/rules")
async def update_rules(data: RulesUpdate):
    # Сначала ЧИТАЕМ текущий файл, чтобы не потерять настройки AI и кастомных групп
    try:
        async with aiofiles.open(GSG_RULES_FILE, 'r') as f:
            rules = json.loads(await f.read())
    except:
        rules = {"direct": [], "proxy": [], "custom_groups": []}

    # ОБНОВЛЯЕМ только нужные ключи
    rules["direct"] = [r.strip() for r in data.direct if r.strip()]
    rules["proxy"] = [r.strip() for r in data.proxy if r.strip()]
    if data.proxy_node is not None:
        rules["proxy_node"] = data.proxy_node

    # Записываем обратно всё вместе
    await _backup_rules()
    async with aiofiles.open(GSG_RULES_FILE, 'w') as f:
        await f.write(json.dumps(rules, indent=2))

    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
        await f.write("1")

    return {"success": True}

class AiSettingsRequest(BaseModel):
    node_filter: str
    domains: List[str]

@app.post("/api/rules/ai")
async def save_ai_rules(req: AiSettingsRequest):
    """Обратная совместимость: обновляет AI-группу в proxy_groups."""
    try:
        async with aiofiles.open(GSG_RULES_FILE, 'r') as f:
            rules = json.loads(await f.read())
    except:
        rules = {}

    # Миграция в proxy_groups
    rules = _ensure_proxy_groups(rules)
    for pg in rules["proxy_groups"]:
        if pg["id"] == "ai":
            pg["node_filter"] = req.node_filter
            pg["rules"] = [d.strip() for d in req.domains if d.strip()]
            pg.pop("domains", None)
            break

    await _backup_rules()
    async with aiofiles.open(GSG_RULES_FILE, 'w') as f:
        await f.write(json.dumps(rules, indent=4))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
        await f.write("1")
    return {"ok": True}


def _get_pg_rules(pg: dict) -> list:
    """Возвращает список строк правил группы (новый формат rules или старый domains)."""
    r = pg.get("rules")
    if r is not None:
        return r
    return pg.get("domains", [])


def _ensure_proxy_groups(rules: dict) -> dict:
    """Миграция: если proxy_groups нет, строим из старого формата.
    Также мигрирует domains → rules внутри существующих групп."""
    if "proxy_groups" in rules and rules["proxy_groups"]:
        # Миграция domains → rules для существующих групп
        for pg in rules["proxy_groups"]:
            if "domains" in pg and "rules" not in pg:
                pg["rules"] = pg.pop("domains")
        return rules

    groups = [
        {"id": "auto", "name": "Auto", "node_filter": "", "type": "url-test", "builtin": True, "rules": []},
    ]
    # AI
    ai_s = rules.get("ai_settings", {})
    ai_domains = ai_s.get("domains") or ["gemini", "openai", "chatgpt", "anthropic", "claude", "aistudio.google.com"]
    groups.append({
        "id": "ai", "name": "AI", "node_filter": ai_s.get("node_filter", "NY"),
        "type": "fallback", "builtin": True, "rules": ai_domains
    })
    # Bypass (direct)
    direct_list = rules.get("direct", [])
    groups.append({
        "id": "bypass", "name": "Bypass", "node_filter": "",
        "type": "direct", "builtin": True, "rules": direct_list
    })
    # Предустановленные группы
    groups.append({
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
    groups.append({
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
    groups.append({
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
    # Proxy-домены → в Auto
    proxy_list = rules.get("proxy", [])
    if proxy_list:
        groups[0]["rules"] = proxy_list  # Auto — первый

    rules["proxy_groups"] = groups
    return rules


# --- PROXY GROUPS API ---

class ProxyGroupCreate(BaseModel):
    name: str
    node_filter: str = ""
    type: str = "url-test"
    rules: List[str] = []
    exclusions: List[str] = []
    exclusions_disabled: List[str] = []
    exclusions_custom: List[str] = []
    # Обратная совместимость: старое поле
    domains: Optional[List[str]] = None

class ProxyGroupUpdate(BaseModel):
    name: Optional[str] = None
    node_filter: Optional[str] = None
    type: Optional[str] = None
    rules: Optional[List[str]] = None
    exclusions: Optional[List[str]] = None
    exclusions_disabled: Optional[List[str]] = None
    exclusions_custom: Optional[List[str]] = None
    # Обратная совместимость: старое поле
    domains: Optional[List[str]] = None

def _is_ip_entry(s: str) -> bool:
    """Возвращает True если строка выглядит как IP или CIDR."""
    import ipaddress as _ipa
    s = s.strip()
    if '/' in s:
        try:
            _ipa.ip_network(s, strict=False)
            return True
        except ValueError:
            pass
    try:
        _ipa.ip_address(s)
        return True
    except ValueError:
        return False


# --- ROUTE TESTS API ---

@app.get("/api/test/routes")
async def test_routes():
    """
    Проверяет корректность маршрутов: для каждого домена из каждой группы
    проверяет что Mihomo направит его через правильную proxy-group с правильными узлами.
    """
    results = {"ok": True, "tests": [], "errors": [], "warnings": []}

    # 1. Загружаем proxy_groups
    try:
        async with aiofiles.open(GSG_RULES_FILE, 'r') as f:
            rules = json.loads(await f.read())
    except:
        rules = {}
    rules = _ensure_proxy_groups(rules)
    proxy_groups = rules.get("proxy_groups", [])

    # 2. Загружаем узлы
    try:
        async with aiofiles.open(GSG_NODES_FILE, 'r') as f:
            nodes_data = json.loads(await f.read())
        all_node_tags = [n["tag"] for n in nodes_data.get("nodes", [])]
    except:
        all_node_tags = []

    # 3. Получаем правила Mihomo и proxy-groups
    mihomo_rules = []
    mihomo_proxies = {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            rules_resp = await client.get("http://127.0.0.1:9090/rules")
            mihomo_rules = rules_resp.json().get("rules", [])

            proxies_resp = await client.get("http://127.0.0.1:9090/proxies")
            mihomo_proxies = proxies_resp.json().get("proxies", {})
    except Exception as e:
        results["errors"].append(f"Не удалось подключиться к Mihomo API: {e}")
        results["ok"] = False
        return results

    # 4. Строим карту домен → proxy_target из Mihomo rules
    def find_mihomo_target(domain: str) -> str:
        domain = domain.lower()
        for rule in mihomo_rules:
            rtype = rule.get("type", "")
            payload = (rule.get("payload", "") or "").lower()
            proxy = rule.get("proxy", "")
            if rtype == "DomainSuffix" and (domain == payload or domain.endswith("." + payload)):
                return proxy
            if rtype == "DomainKeyword" and payload in domain:
                return proxy
        return "MATCH"

    # 5. Получаем узлы внутри proxy-group из Mihomo
    # Реальные узлы имеют тип протокола (Vless, Vmess, Trojan, Shadowsocks и т.д.)
    # Группы и служебные — URLTest, Fallback, Selector, Direct, Reject и т.д.
    group_types = {"URLTest", "Fallback", "Selector", "Direct", "Reject", "RejectDrop", "Pass", "Compatible", "Relay"}

    def get_group_nodes(group_name: str) -> list:
        pg = mihomo_proxies.get(group_name, {})
        if not pg:
            return []
        all_nodes = pg.get("all", [])
        # Оставляем только реальные узлы (не группы/служебные)
        return [n for n in all_nodes if mihomo_proxies.get(n, {}).get("type", "") not in group_types]

    # 6. Строим ожидаемые узлы для каждой нашей группы
    for pg in proxy_groups:
        g_id = pg.get("id")
        g_type = pg.get("type", "url-test")
        g_name = pg.get("name")
        pg_rules = _get_pg_rules(pg)
        node_filter = pg.get("node_filter", "").lower().strip()

        if not pg_rules:
            continue

        # Ожидаемые узлы для группы
        if g_type == "direct" or g_id == "bypass":
            expected_target = "DIRECT"
            expected_nodes = []
        else:
            expected_target = g_id  # имя proxy-group в Mihomo
            if node_filter:
                filters = [f.strip() for f in node_filter.split(',') if f.strip()]
                expected_nodes = [n for n in all_node_tags if any(f in n.lower() for f in filters)]
            else:
                expected_nodes = list(all_node_tags)

        # Реальные узлы в Mihomo proxy-group
        actual_nodes = get_group_nodes(g_id) if g_type != "direct" else []

        # Для проверки отбираем только домены/ключевые слова (без IP-CIDR)
        domain_entries = [r.strip() for r in pg_rules if r.strip() and not r.startswith('#') and '/' not in r]
        try:
            import ipaddress as _ipa
            domain_entries = [r for r in domain_entries if not _is_ip_entry(r)]
        except Exception:
            pass

        group_result = {
            "group": g_name,
            "group_id": g_id,
            "type": g_type,
            "expected_target": expected_target,
            "expected_nodes": len(expected_nodes),
            "actual_nodes": len(actual_nodes),
            "actual_node_names": actual_nodes[:6],
            "domains": domain_entries,
            "domains_tested": 0,
            "domains_ok": 0,
            "domains_wrong": 0,
            "issues": []
        }

        # Проверяем узлы группы
        if g_type != "direct" and expected_nodes:
            unexpected = set(actual_nodes) - set(expected_nodes) - {"REJECT", "DIRECT"}
            if unexpected:
                issue = f"Группа {g_name} содержит лишние узлы: {list(unexpected)}"
                group_result["issues"].append(issue)
                results["warnings"].append(issue)

            missing = set(expected_nodes) - set(actual_nodes)
            if missing:
                issue = f"Группа {g_name} не содержит ожидаемых узлов: {list(missing)}"
                group_result["issues"].append(issue)
                results["warnings"].append(issue)

        # Проверяем каждый домен/ключевое слово
        for d in domain_entries:
            d = d.strip()
            if not d:
                continue
            test_domain = d if "." in d else f"test.{d}.com"
            actual_target = find_mihomo_target(test_domain)
            group_result["domains_tested"] += 1

            if expected_target == "DIRECT":
                if actual_target == "DIRECT":
                    group_result["domains_ok"] += 1
                else:
                    group_result["domains_wrong"] += 1
                    issue = f"Домен '{d}' должен идти DIRECT (Bypass), но идёт через '{actual_target}'"
                    group_result["issues"].append(issue)
                    results["errors"].append(issue)
            else:
                if actual_target == expected_target:
                    group_result["domains_ok"] += 1
                else:
                    group_result["domains_wrong"] += 1
                    issue = f"Домен '{d}' должен идти через '{expected_target}' ({g_name}), но идёт через '{actual_target}'"
                    group_result["issues"].append(issue)
                    results["errors"].append(issue)

        results["tests"].append(group_result)

    # 7. Проверка устройств
    try:
        async with aiofiles.open(GSG_DEVICES_FILE, 'r') as f:
            devices = json.loads(await f.read())
    except:
        devices = {}

    device_tests = []
    for key, dev in devices.items():
        # devices.json может быть в двух форматах: ключ=MAC (новый) или ключ=IP (старый)
        actual_ip = dev.get("current_ip") or key
        mode = dev.get("mode", "smart")
        name = dev.get("custom_name") or dev.get("hostname") or actual_ip
        assigned = dev.get("assigned_node", "auto")
        dev_result = {
            "ip": actual_ip,
            "name": name,
            "mode": mode,
            "assigned_node": assigned,
            "issues": [],
            "ok": True
        }

        # Проверяем что assigned_node существует
        if assigned and assigned != "auto":
            found = any(assigned.lower() in n.lower() for n in all_node_tags)
            if not found:
                issue = f"Назначенный узел '{assigned}' не найден"
                dev_result["issues"].append(issue)
                dev_result["ok"] = False
                results["errors"].append(f"Устройство {name} ({actual_ip}): {issue}")

        # Проверяем что blocked-устройства не обходят блокировку через доменные правила
        if mode == "block":
            # В текущей архитектуре доменные правила (группы) приоритетнее SRC-IP-CIDR.
            # Это значит заблокированное устройство может пустить трафик через VPN для доменов из групп.
            has_group_domains = any(len(_get_pg_rules(pg)) > 0 for pg in proxy_groups if pg.get("type") != "direct")
            if has_group_domains:
                issue = "Режим Block: доменные правила групп имеют приоритет — трафик к доменам из Auto/AI может пройти через VPN"
                dev_result["issues"].append(issue)
                dev_result["ok"] = False
                results["warnings"].append(f"Устройство {name} ({actual_ip}): {issue}")

        # Проверяем что для smart-устройства sub-rules существуют в Mihomo
        if mode == "smart":
            has_sub = any(
                r.get("type") == "SubRules" and actual_ip in (r.get("payload", "") or "")
                for r in mihomo_rules
            )
            if not has_sub:
                issue = "Sub-rules не найдены в Mihomo — конфиг мог не примениться"
                dev_result["issues"].append(issue)
                dev_result["ok"] = False
                results["warnings"].append(f"Устройство {name} ({actual_ip}): {issue}")

        device_tests.append(dev_result)

    results["devices"] = device_tests

    # 8. Проверка целостности конфигурации
    # Сравниваем количество proxy-groups и правил в rules.json с тем что Mihomo реально применил
    integrity = {"config_ok": True, "issues": []}

    # Проверяем что все наши группы существуют в Mihomo
    for pg in proxy_groups:
        g_id = pg.get("id")
        g_type = pg.get("type", "url-test")
        if g_type == "direct" or g_id == "bypass":
            continue
        if g_id not in mihomo_proxies:
            issue = f"Группа '{pg.get('name')}' [{g_id}] не найдена в Mihomo — конфиг мог сброситься после перезагрузки"
            integrity["issues"].append(issue)
            integrity["config_ok"] = False

    # Проверяем что количество доменных правил совпадает (только домены, без IP)
    expected_domain_rules = 0
    for pg in proxy_groups:
        for d in _get_pg_rules(pg):
            if d.strip() and not d.startswith('#') and not _is_ip_entry(d):
                expected_domain_rules += 1
    # Считаем DomainSuffix и DomainKeyword правила в Mihomo (без системных)
    system_suffixes = {"local"}
    actual_domain_rules = sum(
        1 for r in mihomo_rules
        if r.get("type") in ("DomainSuffix", "DomainKeyword")
        and (r.get("payload", "") or "").lower() not in system_suffixes
    )
    # Route overrides тоже добавляют доменные правила
    route_overrides_count = len(rules.get("route_overrides", []))
    expected_total = expected_domain_rules + route_overrides_count

    if actual_domain_rules < expected_total * 0.5:
        issue = f"Mihomo содержит {actual_domain_rules} доменных правил, ожидалось ~{expected_total}. Конфиг мог не примениться."
        integrity["issues"].append(issue)
        integrity["config_ok"] = False

    # Проверяем rulesets.json — RKN list разворачивается Mihomo в отдельные правила,
    # поэтому проверяем что доменных правил достаточно много (RKN добавляет сотни)
    try:
        async with aiofiles.open(GSG_RULESETS_FILE, 'r') as f:
            rulesets = json.loads(await f.read())
    except:
        rulesets = {"rkn_bypass": True, "ru_direct": True}

    results["integrity"] = integrity
    if integrity["issues"]:
        results["warnings"].extend(integrity["issues"])

    if results["errors"]:
        results["ok"] = False

    return results

# --- RULESETS API ---
@app.get("/api/rulesets")
async def get_rulesets():
    return await read_json(GSG_RULESETS_FILE, {"rkn_bypass": True, "ru_direct": True})

class RulesetsUpdate(BaseModel):
    rkn_bypass: bool = True
    ru_direct: bool = True

@app.put("/api/rulesets")
async def update_rulesets(data: RulesetsUpdate):
    async with aiofiles.open(GSG_RULESETS_FILE, 'w') as f:
        await f.write(json.dumps({"rkn_bypass": data.rkn_bypass, "ru_direct": data.ru_direct}, indent=2))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
        await f.write("1")
    return {"ok": True}

@app.get("/api/groups")
async def get_groups():
    try:
        async with aiofiles.open(GSG_RULES_FILE, 'r') as f:
            rules = json.loads(await f.read())
    except:
        rules = {}
    rules = _ensure_proxy_groups(rules)

    # Обогащаем matched_nodes_count
    try:
        async with aiofiles.open(GSG_NODES_FILE, 'r') as f:
            nodes_data = json.loads(await f.read())
        node_names = [n["tag"] for n in nodes_data.get("nodes", [])]
    except:
        node_names = []

    for pg in rules["proxy_groups"]:
        filt = pg.get("node_filter", "").lower().strip()
        if filt:
            filters = [f.strip() for f in filt.split(',') if f.strip()]
            matched = [n for n in node_names if any(f in n.lower() for f in filters)]
        else:
            matched = list(node_names)
        pg["matched_nodes"] = len(matched)

    return rules["proxy_groups"]

@app.post("/api/groups")
async def create_group(req: ProxyGroupCreate):
    try:
        async with aiofiles.open(GSG_RULES_FILE, 'r') as f:
            rules = json.loads(await f.read())
    except:
        rules = {}
    rules = _ensure_proxy_groups(rules)

    new_id = f"user_{int(time.time())}"
    # Поддержка старого поля domains для обратной совместимости
    rules_list = req.rules if req.rules is not None else (req.domains or [])
    new_group = {
        "id": new_id, "name": req.name, "node_filter": req.node_filter,
        "type": req.type, "builtin": False,
        "rules": [d.strip() for d in rules_list if d.strip()],
        "exclusions": [d.strip() for d in req.exclusions if d.strip()],
        "exclusions_disabled": [d.strip() for d in req.exclusions_disabled if d.strip()],
        "exclusions_custom": [d.strip() for d in req.exclusions_custom if d.strip()],
    }
    rules["proxy_groups"].append(new_group)

    await _backup_rules()
    async with aiofiles.open(GSG_RULES_FILE, 'w') as f:
        await f.write(json.dumps(rules, indent=4))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
        await f.write("1")
    return new_group

@app.put("/api/groups/{group_id}")
async def update_group(group_id: str, req: ProxyGroupUpdate):
    try:
        async with aiofiles.open(GSG_RULES_FILE, 'r') as f:
            rules = json.loads(await f.read())
    except:
        rules = {}
    rules = _ensure_proxy_groups(rules)

    found = None
    for pg in rules["proxy_groups"]:
        if pg["id"] == group_id:
            found = pg
            break
    if not found:
        raise HTTPException(404, "Group not found")

    if req.name is not None and not found.get("builtin"):
        found["name"] = req.name
    if req.node_filter is not None:
        found["node_filter"] = req.node_filter
    if req.type is not None:
        found["type"] = req.type
    # Поддержка старого поля domains для обратной совместимости
    new_rules = req.rules if req.rules is not None else req.domains
    if new_rules is not None:
        found["rules"] = [d.strip() for d in new_rules if d.strip()]
        found.pop("domains", None)  # убираем старое поле если было
    if req.exclusions is not None:
        found["exclusions"] = [d.strip() for d in req.exclusions if d.strip()]
    if req.exclusions_disabled is not None:
        found["exclusions_disabled"] = [d.strip() for d in req.exclusions_disabled if d.strip()]
    if req.exclusions_custom is not None:
        found["exclusions_custom"] = [d.strip() for d in req.exclusions_custom if d.strip()]

    await _backup_rules()
    async with aiofiles.open(GSG_RULES_FILE, 'w') as f:
        await f.write(json.dumps(rules, indent=4))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
        await f.write("1")
    return found

@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: str):
    try:
        async with aiofiles.open(GSG_RULES_FILE, 'r') as f:
            rules = json.loads(await f.read())
    except:
        rules = {}
    rules = _ensure_proxy_groups(rules)

    idx = None
    for i, pg in enumerate(rules["proxy_groups"]):
        if pg["id"] == group_id:
            if pg.get("builtin"):
                raise HTTPException(400, "Cannot delete built-in group")
            idx = i
            break
    if idx is None:
        raise HTTPException(404, "Group not found")

    rules["proxy_groups"].pop(idx)

    # Очищаем route_overrides с group:{id}
    overrides = rules.get("route_overrides", [])
    rules["route_overrides"] = [o for o in overrides if o.get("target") != f"group:{group_id}"]

    await _backup_rules()
    async with aiofiles.open(GSG_RULES_FILE, 'w') as f:
        await f.write(json.dumps(rules, indent=4))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
        await f.write("1")
    return {"ok": True}

@app.get("/api/dhcp")
async def get_dhcp():
    default = {
        "gateway": GATEWAY_IP,
        "pool_start": os.getenv("GSG_DHCP_START", "10.10.1.100"),
        "pool_end": os.getenv("GSG_DHCP_END", "10.10.1.200"),
        "dns": GATEWAY_IP
    }
    return await read_json(GSG_DHCP_FILE, default)

@app.put("/api/dhcp")
async def update_dhcp(data: DHCPUpdate):
    config = data.model_dump()
    async with aiofiles.open(GSG_DHCP_FILE, 'w') as f:
        await f.write(json.dumps(config, indent=2))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_dhcp", 'w') as f:
        await f.write("1")
    return {"success": True}

@app.get("/api/logs")
async def get_logs():
    if not GSG_LOG_FILE.exists():
        return ["[INFO] Ожидание логов туннеля..."]
    try:
        async with aiofiles.open(GSG_LOG_FILE, 'r') as f:
            lines = await f.readlines()
            return [l.strip() for l in lines[-100:]]
    except Exception:
        return ["[ERROR] Не удалось прочитать лог"]

@app.get("/api/log-level")
async def get_log_level():
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get("http://127.0.0.1:9090/configs", timeout=3)
            return {"level": r.json().get("log-level", "info")}
    except Exception:
        return {"level": "unknown"}

class LogLevelUpdate(BaseModel):
    level: str

@app.put("/api/log-level")
async def set_log_level(req: LogLevelUpdate):
    allowed = {"silent", "error", "warning", "info", "debug"}
    if req.level not in allowed:
        raise HTTPException(400, "Invalid log level")
    try:
        async with httpx.AsyncClient() as client:
            await client.patch(
                "http://127.0.0.1:9090/configs",
                json={"log-level": req.level},
                timeout=3
            )
        return {"level": req.level}
    except Exception as e:
        raise HTTPException(500, str(e))

class FeedbackRequest(BaseModel):
    name: str = ""
    message: str
    telegram: str = ""

@app.post("/api/feedback")
async def post_feedback(req: FeedbackRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Сообщение пустое")
    tg_username = req.telegram.strip().lstrip("@")
    entry = {"ts": datetime.utcnow().isoformat(), "name": req.name.strip(), "message": req.message.strip(), "telegram": tg_username}
    try:
        async with _feedback_lock:
            existing = []
            try:
                async with aiofiles.open(GSG_FEEDBACK_FILE, 'r') as f:
                    existing = json.loads(await f.read())
            except: pass
            existing.append(entry)
            # Retention: keep last 500 records no older than 1 year
            cutoff = (datetime.utcnow() - timedelta(days=365)).isoformat()
            existing = [e for e in existing if e.get("ts", "") >= cutoff]
            if len(existing) > 500:
                existing = existing[-500:]
            async with aiofiles.open(GSG_FEEDBACK_FILE, 'w') as f:
                await f.write(json.dumps(existing, ensure_ascii=False, indent=2))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat  = os.getenv("TELEGRAM_NOTIFY_USERS_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        import platform

        # Железо: модель платы + arch + RAM
        board = "–"
        try:
            board = open("/proc/device-tree/model").read().replace("\x00", "").strip()
        except Exception:
            try:
                for line in open("/proc/cpuinfo"):
                    if line.lower().startswith("model name") or line.lower().startswith("hardware"):
                        board = line.split(":", 1)[1].strip()
                        break
            except Exception:
                pass
        arch = platform.machine()
        ram_gb = "–"
        try:
            for line in open("/proc/meminfo"):
                if line.startswith("MemTotal"):
                    ram_gb = f"{round(int(line.split()[1]) / 1024 / 1024, 1)} GB"
                    break
        except Exception:
            pass

        # Регион и провайдер по внешнему IP (без прокси — нужен реальный IP)
        ext_ip = isp = region = "–"
        try:
            async with httpx.AsyncClient() as geo:
                gr = await geo.get(
                    "http://ip-api.com/json/?fields=query,isp,country,regionName",
                    timeout=4.0
                )
                if gr.status_code == 200:
                    gd = gr.json()
                    ext_ip = gd.get("query", "–")
                    isp    = gd.get("isp", "–")
                    region = f"{gd.get('regionName', '')}, {gd.get('country', '')}".strip(", ")
        except Exception:
            pass

        device_id = "–"
        device_token = ""
        try:
            async with aiofiles.open(GSG_DEVICE_FILE, 'r') as f:
                _dev = json.loads(await f.read())
                device_id    = _dev.get("device_id", "–")
                device_token = _dev.get("device_token", "")
        except Exception:
            pass

        # Пробуем получить Telegram ID пользователя по токену подписки
        tg_user_line = ""
        try:
            sub_data = json.loads(open(GSG_SUBSCRIPTION_FILE).read())
            sub_url = sub_data.get("url", "")
            sub_token = sub_url.rstrip("/").split("/")[-1] if sub_url else ""
            if sub_token:
                async with httpx.AsyncClient(timeout=5.0) as cl:
                    resolve_resp = await cl.get(
                        f"{GLOBALSHIELD_API}/devices/resolve-user",
                        params={"token": sub_token},
                        headers={"X-Device-ID": device_id, "X-Device-Token": device_token},
                    )
                    if resolve_resp.status_code == 200:
                        tg_uid = resolve_resp.json().get("telegram_id")
                        if tg_uid:
                            tg_user_line = f"📱 <a href='tg://user?id={tg_uid}'>Написать в Telegram</a>\n"
        except Exception:
            pass

        name_part = entry['name'] if entry['name'] else "Аноним"
        tg_username_line = ""
        if entry.get('telegram'):
            tg_username_line = f"✉️ <a href='https://t.me/{entry['telegram']}'>@{entry['telegram']}</a>\n"
        text = (
            f"📬 <b>Обратная связь GSG</b>\n"
            f"➖➖➖➖➖➖➖➖➖\n"
            f"👤 {name_part}\n"
            f"{tg_username_line}"
            f"{tg_user_line}"
            f"💬 {entry['message']}\n"
            f"➖➖➖➖➖➖➖➖➖\n"
            f"💻 {board} | {arch} | {ram_gb}\n"
            f"🌐 {ext_ip} — {isp}\n"
            f"📍 {region}\n"
            f"🆔 <code>{device_id}</code>\n"
            f"🕐 {entry['ts'][:16].replace('T', ' ')}\n"
            f"#feedback"
        )
        try:
            # Роутим через локальный Mihomo-прокси (порт 2080) — Telegram заблокирован в РФ
            async with httpx.AsyncClient(proxy="http://127.0.0.1:2080") as client:
                await client.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": tg_chat, "text": text, "parse_mode": "HTML"},
                    timeout=10.0
                )
        except Exception: pass

    return {"ok": True}

UPDATE_STATE_FILE = GSG_CONFIG_DIR / ".update_state.json"

def _read_update_state() -> dict:
    """Читает .update_state.json, возвращает {} при ошибке."""
    try:
        return json.loads(UPDATE_STATE_FILE.read_text())
    except Exception:
        return {}

@app.get("/api/version")
async def get_version():
    state = _read_update_state()
    result: dict = {"version": GSG_VERSION}
    if state:
        post = state.get("post_update") or {}
        pre  = state.get("pre_update") or {}
        result["previous_version"] = pre.get("version")
        result["update_status"]    = post.get("status")
        result["last_rollback"]    = state.get("last_rollback")
    return result

GITHUB_REPO = "GlobalShieldRu/GateWay"
UPDATE_TRIGGER = GSG_CONFIG_DIR / ".update_trigger"
UPDATE_LOG = GSG_CONFIG_DIR / ".update_log"

# ── Tunnel hard-restart (авто-восстановление health-check) ────────────────────
# Внутренний токен для вызова из gsg-tunnel контейнера (watchdog)
INTERNAL_RESTART_TOKEN = os.getenv("GSG_INTERNAL_TOKEN", "gsg-internal-restart-v1")
_last_tunnel_restart: float = 0.0   # timestamp последнего hard-restart
_RESTART_COOLDOWN = 90              # секунд между рестартами (защита от флаппинга)

TUNNEL_RESTART_TRIGGER = GSG_CONFIG_DIR / ".tunnel_restart_request"

async def _do_tunnel_hard_restart(reason: str) -> dict:
    """Запрашивает docker restart gsg-tunnel через файл-триггер (подхватывает update-watcher.sh на хосте)."""
    global _last_tunnel_restart
    now = time.time()
    since_last = now - _last_tunnel_restart
    if since_last < _RESTART_COOLDOWN:
        wait = int(_RESTART_COOLDOWN - since_last)
        return {"ok": False, "error": f"cooldown: подождите ещё {wait}с", "cooldown": True}

    _last_tunnel_restart = now
    logging.info(f"[tunnel-restart] Запрос hard-restart: {reason}")

    # Пишем файл-триггер в shared volume /etc/gsg — update-watcher.sh на хосте подхватит его
    try:
        import json as _json
        TUNNEL_RESTART_TRIGGER.write_text(_json.dumps({
            "reason": reason,
            "requested_at": datetime.now().isoformat()
        }))
    except Exception as e:
        logging.error(f"[tunnel-restart] Не удалось записать триггер: {e}")
        return {"ok": False, "error": str(e)}

    logging.info(f"[tunnel-restart] Триггер записан, ждём выполнения update-watcher.sh")

    # Telegram-уведомление
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat  = os.getenv("TELEGRAM_NOTIFY_USERS_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        try:
            text = (
                f"🔄 <b>Авто-восстановление tunnel</b>\n"
                f"➖➖➖➖➖➖➖➖➖\n"
                f"Причина: {reason}\n"
                f"Контейнер <code>gsg-tunnel</code> перезапущен автоматически."
            )
            async with httpx.AsyncClient(proxy="http://127.0.0.1:2080") as client:
                await client.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": tg_chat, "text": text, "parse_mode": "HTML"},
                    timeout=10.0
                )
        except Exception:
            pass

    return {"ok": True, "reason": reason}

@app.post("/api/tunnel-hard-restart")
async def tunnel_hard_restart(request: Request):
    """Перезапускает gsg-tunnel. Вызывается watchdog'ом внутри tunnel-контейнера."""
    token = request.headers.get("X-Internal-Token", "")
    if token != INTERNAL_RESTART_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    reason = "unknown"
    try:
        body = await request.json()
        reason = body.get("reason", "unknown")
    except Exception:
        pass
    result = await _do_tunnel_hard_restart(reason)
    if not result["ok"] and result.get("cooldown"):
        raise HTTPException(status_code=429, detail=result["error"])
    return result

# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    return await read_json(GSG_SETTINGS_FILE, {"auto_update": False})

@app.patch("/api/settings")
async def patch_settings(data: dict):
    current = await read_json(GSG_SETTINGS_FILE, {"auto_update": False})
    current.update(data)
    async with aiofiles.open(GSG_SETTINGS_FILE, 'w') as f:
        await f.write(json.dumps(current, indent=2))
    return current

@app.get("/api/check-update")
async def check_update():
    """Проверяет наличие новой версии на GitHub."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest")
            r.raise_for_status()
            data = r.json()
            latest = data.get("tag_name", "").lstrip("v")
            return {
                "current": GSG_VERSION,
                "latest": latest,
                "has_update": latest != GSG_VERSION and latest > GSG_VERSION,
                "release_notes": data.get("body", ""),
                "release_url": data.get("html_url", "")
            }
    except Exception as e:
        return {"current": GSG_VERSION, "latest": None, "has_update": False, "error": str(e)}

@app.post("/api/update")
async def trigger_update():
    """Создаёт триггер-файл для обновления на хосте."""
    import os as _os
    try:
        # O_CREAT | O_EXCL гарантирует атомарность: если файл уже существует — FileExistsError.
        # Это исключает race condition при конкурентных запросах.
        fd = _os.open(str(UPDATE_TRIGGER), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY, 0o644)
        with _os.fdopen(fd, 'w') as f:
            f.write(f"update_requested_at={datetime.now().isoformat()}\n")
        return {"ok": True, "message": "Обновление запущено. Система перезапустится через 1-2 минуты."}
    except FileExistsError:
        return {"ok": False, "error": "Обновление или откат уже запущен"}

@app.get("/api/update/status")
async def update_status():
    """Возвращает статус обновления с прогрессом и последними строками лога."""
    pending = UPDATE_TRIGGER.exists()
    log_text = ""
    stage = 0
    total_stages = 6
    stage_label = ""
    status = "idle"

    if UPDATE_LOG.exists():
        log_text = UPDATE_LOG.read_text()
        lines = log_text.strip().split("\n")
        # Парсим последнюю стадию
        for line in reversed(lines):
            if "[STAGE:" in line:
                try:
                    s = line.split("[STAGE:")[1].split("]")[0]
                    stage = int(s.split("/")[0])
                    total_stages = int(s.split("/")[1])
                    stage_label = line.split("] ", 1)[1] if "] " in line else ""
                except:
                    pass
                break
            if "завершено успешно" in line.lower():
                status = "success"
                stage = total_stages
                break
            if "откат" in line.lower() and "запущен" not in line.lower():
                status = "rolled_back"
                break
            if "ошибка" in line.lower():
                status = "error"
                break

        if pending:
            status = "running"
        elif status == "idle" and stage > 0:
            status = "success" if stage >= total_stages else "running"

    # Последние 30 строк лога
    recent_lines = log_text.strip().split("\n")[-30:] if log_text else []

    return {
        "pending": pending,
        "status": status,
        "stage": stage,
        "total_stages": total_stages,
        "stage_label": stage_label,
        "progress": round(stage / total_stages * 100) if total_stages > 0 else 0,
        "log_lines": recent_lines,
    }

@app.get("/api/rollback/state")
async def rollback_state():
    """Возвращает состояние последнего обновления и возможность отката."""
    if not UPDATE_STATE_FILE.exists():
        return {"available": False}
    try:
        data = _read_update_state()
        if not data:
            return {"available": False}
        post = data.get("post_update") or {}
        pre  = data.get("pre_update") or {}
        can_rollback = (
            post.get("status") == "healthy"
            and bool(pre.get("git_hash"))
        )
        return {
            "available":    True,
            "pre_update":   pre,
            "post_update":  post,
            "last_rollback": data.get("last_rollback"),
            "can_rollback": can_rollback,
        }
    except Exception:
        return {"available": False}

@app.post("/api/rollback")
async def trigger_rollback():
    """Запускает ручной откат к предыдущей версии."""
    import os as _os
    if not UPDATE_STATE_FILE.exists():
        raise HTTPException(status_code=400, detail="Нет данных о предыдущем обновлении")
    try:
        fd = _os.open(str(UPDATE_TRIGGER), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY, 0o644)
        with _os.fdopen(fd, 'w') as f:
            f.write(
                f"rollback_requested=true\n"
                f"rollback_requested_at={datetime.now().isoformat()}\n"
            )
        return {"ok": True, "message": "Откат запущен. Система перезапустится через 1-2 минуты."}
    except FileExistsError:
        raise HTTPException(status_code=409, detail="Операция уже выполняется")

@app.get("/api/traffic/device-chains")
async def get_device_chains():
    result = {}
    for ip, chains in monitor.device_chains.items():
        active = {ch: dict(data) for ch, data in chains.items() if data['total_down'] > 0 or data['total_up'] > 0}
        if active:
            result[ip] = active
    return result

@app.get("/api/connections")
async def get_connections():
    conns = []
    for uid, c in monitor.active_conns.items():
        chains = c.get('chains', [])
        conns.append({
            'src': c.get('src', ''),
            'host': c.get('host', ''),
            'dst_port': c.get('dst_port', ''),
            'network': c.get('network', 'TCP'),
            'chain': next((x for x in chains if x not in ('DIRECT','REJECT','GLOBAL','') ), 'DIRECT'),
            'upload': c.get('up', 0),
            'download': c.get('down', 0),
            'start': c.get('start', ''),
            'rule': c.get('rule', ''),
            'rule_payload': c.get('rule_payload', ''),
            'dst_ip': c.get('dst_ip', ''),
        })
    conns.sort(key=lambda x: -(x['upload'] + x['download']))
    return {"connections": conns[:100]}


@app.get("/api/feedback")
async def get_feedback():
    try:
        async with aiofiles.open(GSG_FEEDBACK_FILE, 'r') as f:
            return json.loads(await f.read())
    except: return []

# ── Auth endpoints ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str

class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str

def _make_cookie(resp: JSONResponse, token: str) -> JSONResponse:
    resp.set_cookie("gsg_token", token, httponly=True, samesite="lax", max_age=30*24*3600, path="/")
    return resp

@app.post("/api/auth/setup")
async def auth_setup(req: LoginRequest):
    """First-time password setup. Only works if no password is configured yet."""
    auth = _load_auth()
    if auth.get("hash"):
        raise HTTPException(status_code=403, detail="Already configured")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Пароль должен быть не менее 6 символов")
    salt = secrets.token_hex(16)
    auth = {"salt": salt, "hash": _hash_password(req.password, salt)}
    token = secrets.token_urlsafe(32)
    auth["token"] = token
    _save_auth(auth)
    return _make_cookie(JSONResponse({"ok": True}), token)

@app.get("/api/auth/check")
async def auth_check(request: Request):
    token = request.cookies.get("gsg_token")
    if _verify_token(token):
        return {"authenticated": True}
    return JSONResponse({"authenticated": False}, status_code=401)

@app.post("/api/login")
async def login(req: LoginRequest):
    auth = _load_auth()
    if not auth.get("hash") or not auth.get("salt"):
        raise HTTPException(status_code=500, detail="Auth not configured")
    expected = _hash_password(req.password, auth["salt"])
    if not secrets.compare_digest(expected, auth["hash"]):
        # Миграция: проверяем старый sha256 хэш
        legacy = hashlib.sha256(f"{auth['salt']}{req.password}".encode()).hexdigest()
        if not secrets.compare_digest(legacy, auth["hash"]):
            raise HTTPException(status_code=401, detail="Неверный пароль")
        # Пароль верный (legacy) — мигрируем на pbkdf2
        new_salt = secrets.token_hex(16)
        auth["salt"] = new_salt
        auth["hash"] = _hash_password(req.password, new_salt)
    token = secrets.token_urlsafe(32)
    auth["token"] = token
    _save_auth(auth)
    return _make_cookie(JSONResponse({"ok": True}), token)

@app.post("/api/logout")
async def logout():
    auth = _load_auth()
    auth.pop("token", None)
    _save_auth(auth)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("gsg_token", path="/")
    return resp

@app.post("/api/auth/password")
async def change_password(req: ChangePasswordRequest):
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="Пароль должен быть не менее 6 символов")
    auth = _load_auth()
    expected = _hash_password(req.current_password, auth["salt"])
    if not secrets.compare_digest(expected, auth["hash"]):
        legacy = hashlib.sha256(f"{auth['salt']}{req.current_password}".encode()).hexdigest()
        if not secrets.compare_digest(legacy, auth["hash"]):
            raise HTTPException(status_code=401, detail="Неверный текущий пароль")
    new_salt = secrets.token_hex(16)
    auth["salt"]  = new_salt
    auth["hash"]  = _hash_password(req.new_password, new_salt)
    auth.pop("token", None)
    _save_auth(auth)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("gsg_token", path="/")
    return resp

async def _close_connections_by_domain(domains: list):
    """Закрывает соединения Mihomo для указанных доменов чтобы переподключились по новым правилам."""
    try:
        await asyncio.sleep(3)  # ждём reload конфига
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("http://127.0.0.1:9090/connections")
            if r.status_code != 200:
                return
            conns = r.json().get("connections", [])
            for c in conns:
                host = (c.get("metadata", {}).get("host", "") or "").lower()
                if any(d in host for d in domains):
                    cid = c.get("id")
                    if cid:
                        await client.delete(f"http://127.0.0.1:9090/connections/{cid}")
    except Exception:
        pass

@app.get("/api/rules/overrides")
async def get_route_overrides():
    rules = await read_json(GSG_RULES_FILE, {})
    return rules.get("route_overrides", [])

@app.post("/api/rules/overrides")
async def add_route_overrides(data: List[RouteOverride]):
    await _backup_rules()
    rules = await read_json(GSG_RULES_FILE, {})
    overrides = rules.get("route_overrides", [])

    # Upsert по домену
    existing = {o["domain"]: o for o in overrides}
    for item in data:
        existing[item.domain.lower().strip()] = {"domain": item.domain.lower().strip(), "target": item.target}

    rules["route_overrides"] = list(existing.values())
    async with aiofiles.open(GSG_RULES_FILE, 'w') as f:
        await f.write(json.dumps(rules, indent=2))

    # Trigger reload
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
        await f.write("1")

    # Закрыть затронутые соединения в Mihomo чтобы переподключились по новым правилам
    changed_domains = [item.domain.lower() for item in data]
    asyncio.create_task(_close_connections_by_domain(changed_domains))

    return {"success": True, "count": len(rules["route_overrides"])}

@app.delete("/api/rules/overrides")
async def delete_route_overrides(data: DeleteOverridesRequest):
    domains = data.domains
    await _backup_rules()
    rules = await read_json(GSG_RULES_FILE, {})
    overrides = rules.get("route_overrides", [])
    rules["route_overrides"] = [o for o in overrides if o["domain"] not in [d.lower() for d in domains]]
    async with aiofiles.open(GSG_RULES_FILE, 'w') as f:
        await f.write(json.dumps(rules, indent=2))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
        await f.write("1")
    asyncio.create_task(_close_connections_by_domain([d.lower() for d in domains]))
    return {"success": True}

@app.get("/api/proxies/list")
async def get_proxies_list():
    nodes = []
    groups = []
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get("http://127.0.0.1:9090/proxies")
            if r.status_code == 200:
                for name, proxy in r.json().get("proxies", {}).items():
                    ptype = proxy.get("type", "")
                    if ptype in ("URLTest", "Selector", "Fallback"):
                        if not name.startswith("GSG-ROUTE-"):  # скрываем служебные
                            groups.append(name)
                    elif ptype not in ("Direct", "Reject", "Compatible"):
                        nodes.append(name)
    except Exception:
        pass
    return {"nodes": sorted(nodes), "groups": sorted(groups)}

@app.get("/api/backup")
async def download_backup():
    buf = io.BytesIO()
    files = ["rules.json", "rulesets.json", "devices.json", "subscription.json"]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for fname in files:
            fpath = GSG_CONFIG_DIR / fname
            if fpath.exists():
                async with aiofiles.open(fpath, "r") as f:
                    content = await f.read()
                zf.writestr(fname, content)
        meta = {
            "version": GSG_VERSION,
            "created_at": time.time(),
            "created_at_iso": datetime.now().isoformat()
        }
        zf.writestr("backup_meta.json", json.dumps(meta, indent=2))
    buf.seek(0)
    filename = f"gsg-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.post("/api/restore")
async def restore_backup(file: UploadFile = File(...)):
    if not file.filename.endswith(".zip"):
        raise HTTPException(400, "Нужен .zip файл")
    content = await file.read()
    buf = io.BytesIO(content)
    allowed = {"rules.json", "rulesets.json", "devices.json", "subscription.json"}
    restored = []
    errors = []
    try:
        with zipfile.ZipFile(buf, "r") as zf:
            for name in zf.namelist():
                if name not in allowed:
                    continue
                try:
                    data = zf.read(name).decode("utf-8")
                    json.loads(data)  # валидация JSON
                    fpath = GSG_CONFIG_DIR / name
                    async with aiofiles.open(fpath, "w") as f:
                        await f.write(data)
                    restored.append(name)
                except Exception as e:
                    errors.append(f"{name}: {e}")
    except zipfile.BadZipFile:
        raise HTTPException(400, "Повреждённый ZIP файл")
    (GSG_CONFIG_DIR / ".reload_nftables").touch()
    (GSG_CONFIG_DIR / ".reload_mihomo").touch()
    return {"restored": restored, "errors": errors}


# ── Apps management ──────────────────────────────────────────────────────────

_BUILTIN_APPS_DEFAULT = [
    {"id": "telegram",  "title": "Telegram",           "color": "#229ED9", "ny": False, "alwaysActive": False, "builtin": True,
     "favicon": None,
     "domains": ["telegram.org", "t.me", "telesco.pe", "telegram-cdn.org", "tdesktop.com", "tg.dev", "telegra.ph"]},
    {"id": "tiktok",    "title": "TikTok",             "color": "#ee1d52", "ny": False, "alwaysActive": False, "builtin": True,
     "favicon": None,
     "domains": ["tiktok.com", "tiktokcdn.com", "tiktokv.com", "tiktokcdn-us.com", "tiktok-row.com",
                 "bytedance.com", "byteoversea.com", "bytecdn.cn", "ibyteimg.com",
                 "musical.ly", "muscdn.com", "tiktokstaticb.com", "tiktokstaticcdn.com",
                 "ttwstatic.com", "ttlivecdn.com", "tiktokio.com"]},
    {"id": "youtube",   "title": "YouTube",            "color": "#ff0000", "ny": False, "alwaysActive": False, "builtin": True,
     "favicon": None,
     "domains": ["youtube.com", "youtu.be", "ytimg.com", "yt3.ggpht.com",
                 "googlevideo.com", "youtube-nocookie.com", "youtubei.googleapis.com"]},
    {"id": "instagram", "title": "Instagram",          "color": "#e1306c", "ny": False, "alwaysActive": False, "builtin": True,
     "favicon": None,
     "domains": ["instagram.com", "cdninstagram.com", "fbcdn.net", "facebook.com",
                 "instagram-brand.com", "ig.me"]},
    {"id": "vk",        "title": "VK",                 "color": "#0077ff", "ny": False, "alwaysActive": False, "builtin": True,
     "favicon": "https://vk.com/favicon.ico",
     "domains": ["vk.com", "vk.ru", "userapi.com", "vkuseraudio.com", "vk-cdn.net", "vkontakte.ru"]},
    {"id": "ok",        "title": "OK",                 "color": "#ee8208", "ny": False, "alwaysActive": False, "builtin": True,
     "favicon": "https://ok.ru/favicon.ico",
     "domains": ["ok.ru", "odnoklassniki.ru"]},
    {"id": "kinopoisk", "title": "Kinopoisk",          "color": "#ff6600", "ny": False, "alwaysActive": False, "builtin": True,
     "favicon": "https://www.kinopoisk.ru/favicon.ico",
     "domains": ["kinopoisk.ru", "kinopoisk.com", "hd.kinopoisk.ru", "yastatic.net"]},
    {"id": "yandex",    "title": "Yandex",             "color": "#ff0000", "ny": False, "alwaysActive": False, "builtin": True,
     "favicon": "https://yandex.ru/favicon.ico",
     "domains": ["yandex.ru", "yandex.com", "ya.ru", "yandex.net", "yandexcloud.net"]},
    {"id": "discord",   "title": "Discord",            "color": "#5865f2", "ny": False, "alwaysActive": False, "builtin": True,
     "favicon": "https://discord.com/favicon.ico",
     "domains": ["discord.com", "discord.gg", "discordapp.com", "discordapp.net", "discord.media"]},
    {"id": "spotify",   "title": "Spotify",            "color": "#1db954", "ny": False, "alwaysActive": False, "builtin": True,
     "favicon": "https://open.spotify.com/favicon.ico",
     "domains": ["spotify.com", "scdn.co", "spotifycdn.com", "pscdn.co"]},
    {"id": "twitch",    "title": "Twitch",             "color": "#9146ff", "ny": False, "alwaysActive": False, "builtin": True,
     "favicon": "https://twitch.tv/favicon.ico",
     "domains": ["twitch.tv", "twitchsvc.net", "jtvnw.net", "twitchapps.com"]},
    {"id": "megogo",    "title": "Megogo",             "color": "#1db954", "ny": False, "alwaysActive": False, "builtin": True,
     "favicon": "https://megogo.net/favicon.ico",
     "domains": ["megogo.net"]},
    {"id": "gemini",    "title": "Gemini (только US)", "color": "#8ab4f8", "ny": True,  "alwaysActive": True,  "builtin": True,
     "favicon": None,
     "domains": ["gemini.google.com", "generativelanguage.googleapis.com", "aistudio.google.com"]},
    {"id": "claude",    "title": "Claude (только US)", "color": "#d97706", "ny": True,  "alwaysActive": True,  "builtin": True,
     "favicon": None,
     "domains": ["claude.ai", "claude.com", "anthropic.com", "api.anthropic.com",
                 "a-api.anthropic.com", "s-cdn.anthropic.com", "platform.claude.com",
                 "console.anthropic.com", "statsig-anthropic.com", "sentry-anthropic.io",
                 "stripe.com", "stripe.network", "js.stripe.com", "checkout.stripe.com",
                 "api.stripe.com", "m.stripe.com",
                 "datadoghq.com", "datadoghq.eu", "ddog-gov.com",
                 "nel.cloudflare.com",
                 "160.79.104.0/22", "160.79.108.0/22",
                 "intercom.io", "githubcopilot.com"]},
    {"id": "chatgpt",   "title": "ChatGPT (только US)","color": "#10a37f", "ny": True,  "alwaysActive": True,  "builtin": True,
     "favicon": None,
     "domains": ["chat.openai.com", "openai.com", "chatgpt.com", "api.openai.com", "oaistatic.com"]},
    {"id": "netflix",   "title": "Netflix (только US)", "color": "#E50914", "ny": True,  "alwaysActive": False, "builtin": True,
     "favicon": None,
     "domains": ["netflix.com", "nflxvideo.net", "nflximg.com", "nflxso.net",
                 "nflximg.net", "netflix.net", "nflx.net"]},
]

def _build_app_regex(domains: list) -> str:
    """Строит regex для определения приложения по доменному имени."""
    roots = set()
    for d in domains:
        if "/" in d:
            continue  # IP-CIDR пропускаем
        parts = d.split(".")
        if len(parts) >= 2:
            name = parts[-2].split("-")[0]
            if len(name) >= 4:
                roots.add(name.lower())
    return "|".join(sorted(roots)) if roots else ""

async def _load_apps() -> list:
    """Загружает apps.json; при отсутствии файла создаёт его из дефолтов."""
    if not GSG_APPS_FILE.exists():
        data = {"apps": _BUILTIN_APPS_DEFAULT}
        # добавляем regex для каждого
        for app in data["apps"]:
            app["regex"] = _build_app_regex(app["domains"])
        GSG_APPS_FILE.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(GSG_APPS_FILE, "w") as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))
        return data["apps"]
    raw = await read_json(GSG_APPS_FILE, {"apps": _BUILTIN_APPS_DEFAULT})
    apps = raw.get("apps", _BUILTIN_APPS_DEFAULT)
    # Проверяем наличие builtin-приложений, добавляем отсутствующие
    existing_ids = {a["id"] for a in apps}
    added = False
    for builtin in _BUILTIN_APPS_DEFAULT:
        if builtin["id"] not in existing_ids:
            app_copy = dict(builtin)
            app_copy["regex"] = _build_app_regex(app_copy["domains"])
            apps.append(app_copy)
            added = True
    if added:
        async with aiofiles.open(GSG_APPS_FILE, "w") as f:
            await f.write(json.dumps({"apps": apps}, ensure_ascii=False, indent=2))
    return apps

async def _save_apps(apps: list):
    async with aiofiles.open(GSG_APPS_FILE, "w") as f:
        await f.write(json.dumps({"apps": apps}, ensure_ascii=False, indent=2))

@app.get("/api/apps")
async def get_apps():
    apps = await _load_apps()
    return {"apps": apps}

@app.post("/api/apps")
async def create_app(data: dict):
    import uuid as _uuid
    apps = await _load_apps()
    new_id = data.get("id") or ("custom_" + _uuid.uuid4().hex[:8])
    # проверяем уникальность id
    if any(a["id"] == new_id for a in apps):
        raise HTTPException(400, f"App с id '{new_id}' уже существует")
    new_app = {
        "id":          new_id,
        "title":       data.get("title", "Новое приложение"),
        "color":       data.get("color", "#94a3b8"),
        "ny":          bool(data.get("ny", False)),
        "alwaysActive":bool(data.get("alwaysActive", False)),
        "builtin":     False,
        "favicon":     data.get("favicon") or None,
        "domains":     data.get("domains", []),
    }
    new_app["regex"] = _build_app_regex(new_app["domains"])
    apps.append(new_app)
    await _save_apps(apps)
    return new_app

@app.patch("/api/apps/{app_id}")
async def update_app(app_id: str, data: dict):
    apps = await _load_apps()
    idx = next((i for i, a in enumerate(apps) if a["id"] == app_id), None)
    if idx is None:
        raise HTTPException(404, "App не найден")
    app = dict(apps[idx])
    for field in ("title", "color", "ny", "alwaysActive", "favicon", "domains"):
        if field in data:
            app[field] = data[field]
    app["regex"] = _build_app_regex(app.get("domains", []))
    apps[idx] = app
    await _save_apps(apps)
    return app

@app.delete("/api/apps/{app_id}")
async def delete_app(app_id: str):
    apps = await _load_apps()
    idx = next((i for i, a in enumerate(apps) if a["id"] == app_id), None)
    if idx is None:
        raise HTTPException(404, "App не найден")
    if apps[idx].get("builtin"):
        raise HTTPException(403, "Встроенные приложения нельзя удалить")
    apps.pop(idx)
    await _save_apps(apps)
    return {"ok": True}


app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/install.sh")
async def install_script():
    return FileResponse("static/install.sh", media_type="text/plain")

@app.get("/")
async def index():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-store"})
