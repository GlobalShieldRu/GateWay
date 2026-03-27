import os
import json
import asyncio
import time
import socket
import psutil
import httpx
import aiofiles
import hashlib
import secrets
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Optional
from collections import defaultdict, OrderedDict
from fastapi import FastAPI, HTTPException, Request, Response, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

GSG_VERSION = "1.1.0"

app = FastAPI(title="GSG Smart Gateway API")

GSG_CONFIG_DIR = Path("/etc/gsg")
GSG_DEVICES_FILE = GSG_CONFIG_DIR / "devices.json"
GSG_NODES_FILE = GSG_CONFIG_DIR / "nodes.json"
GSG_SUBSCRIPTION_FILE = GSG_CONFIG_DIR / "subscription.json"
GSG_RULES_FILE = GSG_CONFIG_DIR / "rules.json"
GSG_DHCP_FILE = GSG_CONFIG_DIR / "dhcp.json"
GSG_LOG_FILE = GSG_CONFIG_DIR / "sing-box.log"
GSG_TRAFFIC_HISTORY_FILE = GSG_CONFIG_DIR / "traffic_history.json"
GSG_FEEDBACK_FILE = GSG_CONFIG_DIR / "feedback.json"
GSG_DEVICE_FILE = GSG_CONFIG_DIR / "device.json"
GSG_AUTH_FILE   = GSG_CONFIG_DIR / "auth.json"
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
    return hashlib.sha256(f"{salt}{password}".encode()).hexdigest()

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
_PUBLIC = {"/api/login", "/api/auth/check", "/api/auth/setup"}

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
                                'network': meta.get('network', 'tcp').upper(),
                                'chains': chains,
                                'start': conn.get('start', ''),
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
    await traffic_history.load()
    asyncio.create_task(monitor.poll_mihomo())
    asyncio.create_task(traffic_history.run(monitor))
    asyncio.create_task(_rotate_log())

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
                        devices[ip] = {"ip": ip, "mac": parts[3].lower(), "hostname": hostname}
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

class RulesUpdate(BaseModel):
    direct: List[str]
    proxy: List[str]

class DHCPUpdate(BaseModel):
    gateway: str
    pool_start: str
    pool_end: str
    dns: str

class GlobalNodeUpdate(BaseModel):
    global_node: str

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
    """Принудительно запускает тест задержек в Mihomo и возвращает результаты."""
    data = await read_json(GSG_NODES_FILE, {"nodes": []})
    nodes = data.get("nodes", [])
    results = {}
    async def measure(n):
        tag = n.get("tag", "")
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                r = await client.get(
                    f"http://127.0.0.1:9090/proxies/{tag}/delay",
                    params={"url": "https://www.google.com/", "timeout": 5000}
                )
                if r.status_code == 200:
                    results[tag] = r.json().get("delay", -1)
                else:
                    results[tag] = -1
        except Exception:
            results[tag] = -1
    await asyncio.gather(*(measure(n) for n in nodes))
    return results

@app.get("/api/nodes/dashboard")
async def get_nodes_dash():
    data = await read_json(GSG_NODES_FILE, {"nodes": []})
    nodes = data.get("nodes", [])

    # Получаем задержки из Mihomo (реальный VLESS-латентность)
    mihomo_delays = {}
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

    async def check(n):
        tag = n.get("tag", "")
        if tag in mihomo_delays:
            p = mihomo_delays[tag]
        else:
            p = await ping_tcp(n['server'], int(n['server_port']))
        n['ping'] = p
        n['status'] = 'online' if p > 0 else 'offline'
        return n

    res = await asyncio.gather(*(check(n) for n in nodes))
    return res

@app.get("/api/devices")
async def get_devices():
    active_ips = set(monitor.stats.keys())
    active_devices = await parse_arp_and_leases(active_ips)
    configs = await read_json(GSG_DEVICES_FILE, {})
    new_devices = [d for d in active_devices if d["ip"] not in configs]
    if new_devices:
        for d in new_devices:
            configs[d["ip"]] = {"mode": "smart", "assigned_node": "auto", "tiktok_node": "auto", "custom_name": "", "static_ip": "", "mac": d.get("mac", "")}
        async with _devices_lock:
            async with aiofiles.open(GSG_DEVICES_FILE, 'w') as f:
                await f.write(json.dumps(configs, indent=2))
        async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
            await f.write("1")
    result = []
    for d in active_devices:
        conf = configs.get(d["ip"], {})
        # Keep mac in sync
        if d.get("mac") and not conf.get("mac"):
            conf["mac"] = d["mac"]
        result.append({
            **d,
            "mode": conf.get("mode", "smart"),
            "assigned_node": conf.get("assigned_node", "auto"),
            "tiktok_node": conf.get("tiktok_node", "auto"),
            "custom_name": conf.get("custom_name", ""),
            "static_ip": conf.get("static_ip", ""),
        })
    return result

@app.put("/api/devices/{ip}")
async def update_device(ip: str, data: DeviceUpdate):
    async with _devices_lock:
        configs = await read_json(GSG_DEVICES_FILE, {})
        existing = configs.get(ip, {})
        new_mac = data.mac or existing.get("mac", "")
        configs[ip] = {
            "mode": data.mode,
            "assigned_node": data.assigned_node,
            "tiktok_node": data.tiktok_node,
            "custom_name": data.custom_name,
            "static_ip": data.static_ip,
            "mac": new_mac,
        }
        async with aiofiles.open(GSG_DEVICES_FILE, 'w') as f:
            await f.write(json.dumps(configs, indent=2))
    # Reload nftables only if routing mode changed
    routing_changed = (
        data.mode != existing.get("mode") or
        data.assigned_node != existing.get("assigned_node", "auto") or
        data.tiktok_node != existing.get("tiktok_node", "auto")
    )
    if routing_changed:
        async with aiofiles.open(GSG_CONFIG_DIR / ".reload_nftables", 'w') as f:
            await f.write("1")
        async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
            await f.write("1")
    # Trigger dnsmasq reload if static IP or MAC changed
    if data.static_ip != "" or data.mac:
        async with aiofiles.open(GSG_CONFIG_DIR / ".reload_dhcp", 'w') as f:
            await f.write("1")
    return {"success": True}

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
    rules = {
        "direct": [r.strip() for r in data.direct if r.strip()],
        "proxy": [r.strip() for r in data.proxy if r.strip()]
    }
    async with aiofiles.open(GSG_RULES_FILE, 'w') as f:
        await f.write(json.dumps(rules, indent=2))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
        await f.write("1")
    return {"success": True}

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

@app.get("/api/version")
async def get_version():
    return {"version": GSG_VERSION}

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
        raise HTTPException(status_code=401, detail="Неверный пароль")
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
        raise HTTPException(status_code=401, detail="Неверный текущий пароль")
    new_salt = secrets.token_hex(16)
    auth["salt"]  = new_salt
    auth["hash"]  = _hash_password(req.new_password, new_salt)
    auth.pop("token", None)
    _save_auth(auth)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("gsg_token", path="/")
    return resp

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/install.sh")
async def install_script():
    return FileResponse("static/install.sh", media_type="text/plain")

@app.get("/")
async def index():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-store"})
