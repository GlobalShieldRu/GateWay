import os
import json
import asyncio
import time
import socket
import psutil
import httpx
import aiofiles
from pathlib import Path
from datetime import datetime
from typing import List, Optional
from collections import defaultdict
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="GSG Smart Gateway API")

GSG_CONFIG_DIR = Path("/etc/gsg")
GSG_DEVICES_FILE = GSG_CONFIG_DIR / "devices.json"
GSG_NODES_FILE = GSG_CONFIG_DIR / "nodes.json"
GSG_SUBSCRIPTION_FILE = GSG_CONFIG_DIR / "subscription.json"
GSG_RULES_FILE = GSG_CONFIG_DIR / "rules.json"
GSG_DHCP_FILE = GSG_CONFIG_DIR / "dhcp.json"
GSG_LOG_FILE = GSG_CONFIG_DIR / "sing-box.log"
GSG_TRAFFIC_HISTORY_FILE = GSG_CONFIG_DIR / "traffic_history.json"
DNSMASQ_LEASES = Path("/var/lib/misc/dnsmasq.leases")

GATEWAY_IP = os.getenv("GSG_GATEWAY_IP", "10.10.1.139")
socket.setdefaulttimeout(0.3)

class TrafficMonitor:
    def __init__(self):
        self.active_conns = {}
        self.stats = defaultdict(lambda: {'total_up': 0, 'total_down': 0, 'speed_up': 0, 'speed_down': 0})
        self.node_stats = defaultdict(lambda: {'total_up': 0, 'total_down': 0, 'speed_up': 0, 'speed_down': 0})

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

                            node = next((c for c in reversed(chains) if c not in ('DIRECT', 'REJECT', 'GLOBAL', '')), None)
                            if node:
                                self.node_stats[node]['total_up'] += delta_up
                                self.node_stats[node]['total_down'] += delta_down
                                self.node_stats[node]['speed_up'] += delta_up
                                self.node_stats[node]['speed_down'] += delta_down

                            self.active_conns[uid] = {'up': up, 'down': down}

                        self.active_conns = {k: v for k, v in self.active_conns.items() if k in current_active_ids}
                except Exception:
                    pass
                await asyncio.sleep(1)

monitor = TrafficMonitor()


class TrafficHistory:
    def __init__(self):
        self.data: dict = {}   # ip -> {alltime_up, alltime_down, yearly, monthly, daily}
        self.nodes: dict = {}  # tag -> {alltime_up, alltime_down, yearly, monthly, daily}
        self.schedule: dict = {"type": "never", "time": "00:00"}
        self._snapshots: dict = {}       # ip -> {up, down}
        self._node_snapshots: dict = {}  # tag -> {up, down}

    async def load(self):
        raw = await read_json(GSG_TRAFFIC_HISTORY_FILE, {})
        self.data = raw.get("devices", {})
        self.nodes = raw.get("nodes", {})
        self.schedule = raw.get("schedule", {"type": "never", "time": "00:00"})
        self._snapshots = {}
        self._node_snapshots = {}

    async def save(self):
        try:
            raw = {"devices": self.data, "nodes": self.nodes, "schedule": self.schedule}
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

    def flush(self, session_stats: dict, node_stats: dict = None):
        for ip, stat in session_stats.items():
            self._flush_bucket(self.data, self._snapshots, ip, stat)
        if node_stats:
            for tag, stat in node_stats.items():
                self._flush_bucket(self.nodes, self._node_snapshots, tag, stat)

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
                self.flush(dict(mon.stats), dict(mon.node_stats))
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

_mac_vendor_cache: dict = {}

@app.get("/api/vendor/{mac}")
async def get_mac_vendor(mac: str):
    oui = mac.replace(':', '').replace('-', '').upper()[:6]
    if oui in _mac_vendor_cache:
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
    return {"vendor": vendor}

@app.on_event("startup")
async def startup_event():
    await traffic_history.load()
    asyncio.create_task(monitor.poll_mihomo())
    asyncio.create_task(traffic_history.run(monitor))

@app.get("/api/traffic")
async def get_traffic():
    return monitor.stats

@app.get("/api/traffic/nodes")
async def get_traffic_nodes():
    return dict(monitor.node_stats)

@app.get("/api/traffic/history")
async def get_traffic_history():
    return {"devices": traffic_history.data, "nodes": traffic_history.nodes, "schedule": traffic_history.schedule}

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
    except Exception:
        return default

async def parse_arp_and_leases():
    devices = {}
    lan_prefix = GATEWAY_IP.rsplit('.', 1)[0] + '.'

    try:
        async with aiofiles.open('/proc/net/arp', 'r') as f:
            lines = await f.readlines()
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00" and parts[2] == "0x2":
                    ip = parts[0]
                    if ip.startswith(lan_prefix) and ip != GATEWAY_IP and not ip.startswith("172."):
                        hostname = "Устройство"
                        try:
                            hostname = socket.gethostbyaddr(ip)[0]
                        except Exception:
                            pass
                        devices[ip] = {"ip": ip, "mac": parts[3], "hostname": hostname}
    except Exception:
        pass

    if DNSMASQ_LEASES.exists():
        try:
            async with aiofiles.open(DNSMASQ_LEASES, 'r') as f:
                async for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 4:
                        ip = parts[2]
                        if ip.startswith(lan_prefix) and not ip.startswith("172."):
                            hostname = parts[3] if parts[3] != "*" else "Unknown"
                            if ip in devices:
                                if devices[ip]["hostname"] in ["Устройство", "Unknown"]:
                                    devices[ip]["hostname"] = hostname
                            else:
                                devices[ip] = {"ip": ip, "mac": parts[1], "hostname": hostname}
        except Exception:
            pass

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

@app.get("/api/network-status")
async def get_network_status():
    direct = {"ip": "Оффлайн", "country": "-", "status": "error"}
    tunnel = {"ip": "Оффлайн", "country": "-", "status": "error"}
    youtube = {"status": "error", "ping": 0}

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get("http://ip-api.com/json")
            if r.status_code == 200:
                d = r.json()
                direct = {"ip": d.get("query"), "country": d.get("countryCode"), "isp": d.get("isp", ""), "org": d.get("org", ""), "status": "ok"}
    except Exception:
        pass

    proxies = {"http://": "http://127.0.0.1:2080", "https://": "http://127.0.0.1:2080"}
    try:
        async with httpx.AsyncClient(proxies=proxies, timeout=5.0) as client:
            r = await client.get("http://ip-api.com/json")
            if r.status_code == 200:
                d = r.json()
                tunnel = {"ip": d.get("query"), "country": d.get("countryCode"), "status": "ok"}

            start = time.time()
            yt = await client.get("https://www.youtube.com/favicon.ico", follow_redirects=True)
            if yt.status_code == 200:
                youtube = {"status": "ok", "ping": int((time.time() - start) * 1000)}
    except Exception:
        pass

    return {"direct": direct, "tunnel": tunnel, "youtube": youtube}

@app.get("/api/nodes/dashboard")
async def get_nodes_dash():
    data = await read_json(GSG_NODES_FILE, {"nodes": []})
    nodes = data.get("nodes", [])
    async def check(n):
        p = await ping_tcp(n['server'], int(n['server_port']))
        n['ping'] = p
        n['status'] = 'online' if p != -1 else 'offline'
        return n
    res = await asyncio.gather(*(check(n) for n in nodes))
    return res

@app.get("/api/devices")
async def get_devices():
    active_devices = await parse_arp_and_leases()
    configs = await read_json(GSG_DEVICES_FILE, {})
    result = []
    for d in active_devices:
        conf = configs.get(d["ip"], {})
        result.append({
            **d,
            "mode": conf.get("mode", "smart"),
            "assigned_node": conf.get("assigned_node", "auto"),
            "tiktok_node": conf.get("tiktok_node", "auto"),
            "custom_name": conf.get("custom_name", "")
        })
    return result

@app.put("/api/devices/{ip}")
async def update_device(ip: str, data: DeviceUpdate):
    configs = await read_json(GSG_DEVICES_FILE, {})
    configs[ip] = {"mode": data.mode, "assigned_node": data.assigned_node, "tiktok_node": data.tiktok_node, "custom_name": data.custom_name}
    async with aiofiles.open(GSG_DEVICES_FILE, 'w') as f:
        await f.write(json.dumps(configs, indent=2))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_nftables", 'w') as f:
        await f.write("1")
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f:
        await f.write("1")
    return {"success": True}

@app.get("/api/nodes")
async def get_nodes():
    data = await read_json(GSG_NODES_FILE, {"nodes": []})
    return data.get("nodes", [])

@app.get("/api/subscription")
async def get_sub():
    return await read_json(GSG_SUBSCRIPTION_FILE, {"url": "", "global_node": "auto", "last_update": None})

@app.put("/api/subscription")
async def update_sub(data: dict):
    url = data.get("url")
    if not url:
        raise HTTPException(400)
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
            return [l.strip() for l in lines[-30:]]
    except Exception:
        return ["[ERROR] Не удалось прочитать лог"]

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index():
    return FileResponse("static/index.html")
