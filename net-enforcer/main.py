import os, sys, json, asyncio, aiofiles
from pathlib import Path

GSG_CONFIG_DIR = Path("/etc/gsg")
GSG_DEVICES_FILE = GSG_CONFIG_DIR / "devices.json"
RELOAD_SIGNAL_FILE = GSG_CONFIG_DIR / ".reload_nftables"
GATEWAY_IP = os.getenv("GSG_GATEWAY_IP", "10.10.1.139")
TPROXY_PORT = int(os.getenv("GSG_TPROXY_PORT", "12345"))

NFT_TEMPLATE = '''#!/usr/sbin/nft -f
table inet gsg {{ }}
delete table inet gsg
table inet gsg {{
    set bypass_devices {{ type ipv4_addr; elements = {{ {bypass_ips} }}; }}

    chain prerouting_nat {{
        type nat hook prerouting priority dstnat; policy accept;
        iif lo return

        # ИСПРАВЛЕНО: Редирект DNS теперь работает для ВСЕХ, включая Bypass клиентов.
        udp dport 53 redirect to :1053
        tcp dport 53 redirect to :1053
    }}

    chain prerouting_mangle {{
        type filter hook prerouting priority mangle; policy accept;
        iif lo return

        udp dport 53 return
        tcp dport 53 return

        # Отсекаем мусорный трафик умного дома (Multicast и Broadcast)
        ip daddr {{ 224.0.0.0/4, 255.255.255.255/32 }} return

        # Игнорируем локальные сети
        ip daddr {{ 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 }} return
        ip saddr @bypass_devices return

        meta l4proto tcp tproxy ip to 127.0.0.1:{tproxy_port} meta mark set 1 accept
        meta l4proto udp tproxy ip to 127.0.0.1:{tproxy_port} meta mark set 1 accept
    }}

    chain forward {{
        type filter hook forward priority -1; policy accept;
    }}

    chain postrouting {{
        type nat hook postrouting priority srcnat; policy accept;
        masquerade
    }}
}}
'''

class NetEnforcer:
    async def setup_os_routing(self):
        os.system("sysctl -w net.ipv4.ip_forward=1")
        # Отключаем ICMP redirect — иначе bypass-клиенты получат редирект и обойдут GSG
        os.system("sysctl -w net.ipv4.conf.all.send_redirects=0")
        os.system("sysctl -w net.ipv4.conf.eth0.send_redirects=0")
        os.system("ip rule del fwmark 1 lookup 100 2>/dev/null || true")
        os.system("ip route flush table 100 2>/dev/null || true")
        os.system("ip rule add fwmark 1 lookup 100")
        os.system("ip route add local 0.0.0.0/0 dev lo table 100")
        # Docker ставит FORWARD policy=DROP; bypass-трафик идёт через kernel forwarding —
        # добавляем явный ACCEPT для LAN, если правила ещё нет
        os.system("iptables -C FORWARD -s 10.0.0.0/8 -j ACCEPT 2>/dev/null || iptables -I FORWARD 1 -s 10.0.0.0/8 -j ACCEPT")
        os.system("iptables -C FORWARD -d 10.0.0.0/8 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || iptables -I FORWARD 2 -d 10.0.0.0/8 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT")

    async def apply(self):
        await self.setup_os_routing()
        try:
            async with aiofiles.open(GSG_DEVICES_FILE, 'r') as f:
                data = json.loads(await f.read())
        except: data = {}

        bp = [ip for ip, i in data.items() if i.get("mode") == "bypass"] or ["127.0.0.99"]
        conf = NFT_TEMPLATE.format(bypass_ips=", ".join(bp), tproxy_port=TPROXY_PORT)

        async with aiofiles.open("/tmp/gsg.nft", 'w') as f: await f.write(conf)
        p = await asyncio.create_subprocess_exec("nft", "-f", "/tmp/gsg.nft", stderr=asyncio.subprocess.PIPE)
        _, err = await p.communicate()
        if p.returncode != 0: print(f"[ERROR] Nftables failed: {err.decode()}")
        else: print("[INFO] Applied nftables successfully")

    async def run(self):
        await self.apply()
        while True:
            if RELOAD_SIGNAL_FILE.exists():
                RELOAD_SIGNAL_FILE.unlink()
                await self.apply()
            await asyncio.sleep(2)

if __name__ == "__main__": asyncio.run(NetEnforcer().run())
