import os
import json
from pathlib import Path

GSG_CONFIG_DIR = Path("/etc/gsg")
GSG_DHCP_FILE = GSG_CONFIG_DIR / "dhcp.json"
GSG_DEVICES_FILE = GSG_CONFIG_DIR / "devices.json"

def load_settings():
    """Загружаем из файла или берем из твоих переменных окружения"""
    if GSG_DHCP_FILE.exists():
        try:
            with open(GSG_DHCP_FILE, 'r') as f:
                return json.load(f)
        except: pass

    return {
        "gateway": os.getenv("GATEWAY_IP", "10.10.1.139"),
        "pool_start": os.getenv("DHCP_START", "10.10.1.100"),
        "pool_end": os.getenv("DHCP_END", "10.10.1.200"),
        "dns": os.getenv("DNS_IP", "10.10.1.139")
    }

def generate():
    conf = load_settings()
    iface = os.getenv("LAN_IFACE", "eth0")

    lines = [
        f"interface={iface}",
        "bind-interfaces",  # СТРОГО ТАК, чтобы не было конфликта с bind-dynamic
        "domain-needed",
        "bogus-priv",
        "no-resolv",
        f"server={conf['dns']}",
        f"dhcp-range={conf['pool_start']},{conf['pool_end']},24h",
        f"dhcp-option=option:router,{conf['gateway']}",
        f"dhcp-option=option:dns-server,{conf['dns']}",
        "log-dhcp"
    ]

    # Static IP reservations from devices.json (dhcp-host=MAC,IP)
    try:
        with open(GSG_DEVICES_FILE, 'r') as f:
            devices = json.load(f)
        for ip, cfg in devices.items():
            mac = cfg.get("mac", "")
            static_ip = cfg.get("static_ip", "")
            if mac and static_ip:
                lines.append(f"dhcp-host={mac},{static_ip}")
                print(f"[INFO] Static IP: {mac} → {static_ip}")
    except Exception:
        pass

    with open("/etc/dnsmasq.conf", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[INFO] Config generated for {iface}")

if __name__ == "__main__":
    generate()
