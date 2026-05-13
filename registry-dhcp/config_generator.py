import json
from pathlib import Path

GSG_CONFIG_DIR = Path("/etc/gsg")
GSG_SETTINGS_FILE = GSG_CONFIG_DIR / "settings.json"
GSG_DEVICES_FILE = GSG_CONFIG_DIR / "devices.json"


def detect_network():
    """Автодетект iface/GSG-IP через procfs + ioctl SIOCGIFADDR.
    Источник истины — ОС, без env vars и network.json.
    python-slim не содержит `ip` команды → читаем /proc/net/route напрямую."""
    import socket, fcntl, struct
    iface = "end0"  # fallback
    # 1) iface из default route
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if len(fields) >= 8 and fields[1] == "00000000":
                    iface = fields[0]
                    break
    except Exception:
        pass
    # 2) IPv4 этого iface через ioctl
    gsg_ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        iface_b = iface[:15].encode() + b"\0" * (16 - len(iface[:15]))
        res = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", iface_b))  # SIOCGIFADDR
        gsg_ip = socket.inet_ntoa(res[20:24])
        s.close()
    except Exception:
        pass
    return {"iface": iface, "gsg_ip": gsg_ip}


def load_settings():
    """DHCP-пул — единственное что хранится в settings.json (GSG-решение
    «что раздавать клиентам», не системная конфигурация)."""
    defaults = {"dhcp_start": None, "dhcp_end": None}  # вычислим из подсети если нет
    if GSG_SETTINGS_FILE.exists():
        try:
            with open(GSG_SETTINGS_FILE) as f:
                return {**defaults, **json.load(f)}
        except Exception:
            pass
    return defaults


def _default_pool(gsg_ip):
    """Если в settings.json нет dhcp_start/end — берём `<subnet>.100`–`<subnet>.200`."""
    base = ".".join(gsg_ip.split(".")[:3])
    return f"{base}.100", f"{base}.200"


def generate():
    net = detect_network()
    iface = net["iface"]
    gsg_ip = net["gsg_ip"]
    s = load_settings()
    pool_start = s.get("dhcp_start") or _default_pool(gsg_ip)[0]
    pool_end = s.get("dhcp_end") or _default_pool(gsg_ip)[1]

    lines = [
        f"interface={iface}",
        "bind-interfaces",  # СТРОГО ТАК, чтобы не было конфликта с bind-dynamic
        "domain-needed",
        "bogus-priv",
        "no-resolv",
        "server=127.0.0.1#1053",
        f"dhcp-range={pool_start},{pool_end},24h",
        f"dhcp-option=option:router,{gsg_ip}",       # GSG сам — шлюз для LAN
        f"dhcp-option=option:dns-server,{gsg_ip}",   # GSG сам — DNS (через dnsmasq)
        "quiet-dhcp",
    ]

    # Static IP reservations from devices.json (dhcp-host=MAC,IP)
    # Поддерживает оба формата: MAC-ключ (новый) и IP-ключ со static_ip/reserved_ip (старый)
    try:
        def _looks_like_mac(s: str) -> bool:
            parts = s.split(":")
            return len(parts) == 6 and all(len(p) == 2 for p in parts)

        with open(GSG_DEVICES_FILE, "r") as f:
            devices = json.load(f)
        for key, cfg in devices.items():
            if _looks_like_mac(key):
                mac = key
                reserved_ip = cfg.get("reserved_ip") or cfg.get("static_ip", "")
            else:
                mac = cfg.get("mac", "")
                reserved_ip = cfg.get("reserved_ip") or cfg.get("static_ip", "")
            if mac and reserved_ip:
                lines.append(f"dhcp-host={mac},{reserved_ip},24h")
                print(f"[INFO] DHCP резерв: {mac} → {reserved_ip}")
    except Exception:
        pass

    with open("/etc/dnsmasq.conf", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[INFO] Config generated for {iface} (gsg_ip={gsg_ip}, pool={pool_start}..{pool_end})")


if __name__ == "__main__":
    generate()
