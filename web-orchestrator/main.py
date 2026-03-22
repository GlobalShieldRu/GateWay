import os, json, asyncio, aiofiles, psutil, time, socket
import httpx
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI(title="GSG Smart Gateway API")

GSG_CONFIG_DIR = Path("/etc/gsg")
GSG_DEVICES_FILE = GSG_CONFIG_DIR / "devices.json"
GSG_NODES_FILE = GSG_CONFIG_DIR / "nodes.json"
GSG_SUBSCRIPTION_FILE = GSG_CONFIG_DIR / "subscription.json"
GSG_RULES_FILE = GSG_CONFIG_DIR / "rules.json"
GSG_DHCP_FILE = GSG_CONFIG_DIR / "dhcp.json"
GSG_LOG_FILE = GSG_CONFIG_DIR / "sing-box.log"
DNSMASQ_LEASES = Path("/var/lib/misc/dnsmasq.leases")

socket.setdefaulttimeout(0.3)

async def read_json(path: Path, default):
    if not path.exists(): return default
    try:
        async with aiofiles.open(path, 'r') as f: return json.loads(await f.read())
    except: return default

async def parse_arp_and_leases():
    devices = {}
    gateway_ip = os.getenv("GSG_GATEWAY_IP", "10.10.1.139")
    lan_prefix = gateway_ip.rsplit('.', 1)[0] + '.'

    try:
        async with aiofiles.open('/proc/net/arp', 'r') as f:
            lines = await f.readlines()
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00" and parts[2] == "0x2":
                    ip = parts[0]
                    if ip.startswith(lan_prefix) and ip != gateway_ip and not ip.startswith("172."):
                        hostname = "Устройство"
                        try:
                            hostname = socket.gethostbyaddr(ip)[0]
                        except: pass
                        devices[ip] = {"ip": ip, "mac": parts[3], "hostname": hostname}
    except: pass

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
                            else: devices[ip] = {"ip": ip, "mac": parts[1], "hostname": hostname}
        except: pass

    return list(devices.values())

async def ping_tcp(host: str, port: int, timeout: float = 1.0):
    try:
        start = time.time()
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        await writer.wait_closed()
        return int((time.time() - start) * 1000)
    except: return -1

class DeviceUpdate(BaseModel): mode: str; assigned_node: str
class RulesUpdate(BaseModel): direct: List[str]; proxy: List[str]
class DHCPUpdate(BaseModel): gateway: str; pool_start: str; pool_end: str; dns: str
class GlobalNodeUpdate(BaseModel): global_node: str

@app.get("/api/status")
async def get_status():
    temp = 0
    try:
        if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
            with open("/sys/class/thermal/thermal_zone0/temp") as f: temp = int(f.read()) / 1000
    except: pass
    return {"cpu_percent": psutil.cpu_percent(), "memory_used": psutil.virtual_memory().used, "memory_total": psutil.virtual_memory().total, "temperature": round(temp, 1), "uptime": int(psutil.boot_time())}

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
                direct = {"ip": d.get("query"), "country": d.get("countryCode"), "status": "ok"}
    except: pass

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
                youtube = {"status": "ok", "ping": int((time.time() - start)*1000)}
    except: pass

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
        result.append({**d, "mode": conf.get("mode", "smart"), "assigned_node": conf.get("assigned_node", "auto")})
    return result

@app.put("/api/devices/{ip}")
async def update_device(ip: str, data: DeviceUpdate):
    configs = await read_json(GSG_DEVICES_FILE, {})
    configs[ip] = {"mode": data.mode, "assigned_node": data.assigned_node}
    async with aiofiles.open(GSG_DEVICES_FILE, 'w') as f: await f.write(json.dumps(configs, indent=2))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_nftables", 'w') as f: await f.write("1")
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f: await f.write("1")
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
    if not url: raise HTTPException(400)
    sub = await read_json(GSG_SUBSCRIPTION_FILE, {})
    sub["url"] = url
    sub["last_update"] = datetime.now().isoformat()
    async with aiofiles.open(GSG_SUBSCRIPTION_FILE, 'w') as f: await f.write(json.dumps(sub))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f: await f.write("1")
    return {"success": True}

@app.put("/api/subscription/node")
async def update_global_node(data: GlobalNodeUpdate):
    sub = await read_json(GSG_SUBSCRIPTION_FILE, {"url": "", "global_node": "auto"})
    sub["global_node"] = data.global_node
    async with aiofiles.open(GSG_SUBSCRIPTION_FILE, 'w') as f: await f.write(json.dumps(sub))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f: await f.write("1")
    return {"success": True}

@app.get("/api/rules")
async def get_rules(): return await read_json(GSG_RULES_FILE, {"direct": [], "proxy": []})

@app.put("/api/rules")
async def update_rules(data: RulesUpdate):
    rules = {"direct": [r.strip() for r in data.direct if r.strip()], "proxy": [r.strip() for r in data.proxy if r.strip()]}
    async with aiofiles.open(GSG_RULES_FILE, 'w') as f: await f.write(json.dumps(rules, indent=2))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_singbox", 'w') as f: await f.write("1")
    return {"success": True}

@app.get("/api/dhcp")
async def get_dhcp():
    default = {"gateway": os.getenv("GSG_GATEWAY_IP", "10.10.1.139"), "pool_start": os.getenv("GSG_DHCP_START", "10.10.1.100"), "pool_end": os.getenv("GSG_DHCP_END", "10.10.1.200"), "dns": os.getenv("GSG_GATEWAY_IP", "10.10.1.139")}
    return await read_json(GSG_DHCP_FILE, default)

@app.put("/api/dhcp")
async def update_dhcp(data: DHCPUpdate):
    config = data.model_dump()
    async with aiofiles.open(GSG_DHCP_FILE, 'w') as f: await f.write(json.dumps(config, indent=2))
    async with aiofiles.open(GSG_CONFIG_DIR / ".reload_dhcp", 'w') as f: await f.write("1")
    return {"success": True}

@app.get("/api/logs")
async def get_logs():
    if not GSG_LOG_FILE.exists(): return ["[INFO] Ожидание логов туннеля..."]
    try:
        async with aiofiles.open(GSG_LOG_FILE, 'r') as f:
            lines = await f.readlines()
            return [l.strip() for l in lines[-30:]]
    except: return ["[ERROR] Не удалось прочитать лог"]

app.mount("/static", StaticFiles(directory="static"), name="static")
@app.get("/")
async def index(): return FileResponse("static/index.html")
