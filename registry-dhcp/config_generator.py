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
        "server=127.0.0.1#1053",
        f"dhcp-range={conf['pool_start']},{conf['pool_end']},24h",
        f"dhcp-option=option:router,{conf['gateway']}",
        f"dhcp-option=option:dns-server,{conf['dns']}",
        "quiet-dhcp"
    ]

    # Static IP reservations from devices.json (dhcp-host=MAC,IP)
    # Поддерживает оба формата: MAC-ключ (новый) и IP-ключ со static_ip/reserved_ip (старый)
    try:
        def _looks_like_mac(s: str) -> bool:
            parts = s.split(':')
            return len(parts) == 6 and all(len(p) == 2 for p in parts)

        with open(GSG_DEVICES_FILE, 'r') as f:
            devices = json.load(f)
        for key, cfg in devices.items():
            if _looks_like_mac(key):
                # Новый формат: ключ = MAC
                mac = key
                reserved_ip = cfg.get("reserved_ip") or cfg.get("static_ip", "")
            else:
                # Старый формат: ключ = IP
                mac = cfg.get("mac", "")
                reserved_ip = cfg.get("reserved_ip") or cfg.get("static_ip", "")
            if mac and reserved_ip:
                lines.append(f"dhcp-host={mac},{reserved_ip},24h")
                print(f"[INFO] DHCP резерв: {mac} → {reserved_ip}")
    except Exception:
        pass

    with open("/etc/dnsmasq.conf", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[INFO] Config generated for {iface}")

if __name__ == "__main__":
    generate()
