import os, sys, json, asyncio, aiofiles, socket
from pathlib import Path

GSG_CONFIG_DIR = Path("/etc/gsg")
GSG_DEVICES_FILE = GSG_CONFIG_DIR / "devices.json"
GSG_NODES_FILE  = GSG_CONFIG_DIR / "nodes.json"
RELOAD_SIGNAL_FILE = GSG_CONFIG_DIR / ".reload_nftables"
GATEWAY_IP = os.getenv("GSG_GATEWAY_IP", "10.10.1.139")
TPROXY_PORT = int(os.getenv("GSG_TPROXY_PORT", "12345"))

NFT_TEMPLATE = '''#!/usr/sbin/nft -f
table inet gsg {{ }}
delete table inet gsg
table inet gsg {{
    set bypass_devices {{ type ipv4_addr; elements = {{ {bypass_ips} }}; }}
{node_drop_set}
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

        # WebRTC/VoIP bypass — эти порты не должны идти через прокси (Телемост, FaceTime, STUN/TURN)
        udp dport {{ 3478-3497 }} return
        udp dport {{ 16384-16387 }} return
        tcp dport 5223 return
        udp dport 5223 return

        # Steam/игровой UDP bypass — предотвращаем conntrack flood от игровых клиентов
        udp dport {{ 27000-28000 }} return

        # Отсекаем мусорный трафик умного дома (Multicast и Broadcast)
        ip daddr {{ 224.0.0.0/4, 255.255.255.255/32 }} return

        # Игнорируем локальные сети
        ip daddr {{ 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 }} return
        ip saddr @bypass_devices return

        # Блокируем прямые подключения LAN-устройств к нашим VPN нодам.
        # VPN-приложение на телефоне теряет соединение (timeout) → iOS снимает туннель →
        # трафик идёт через Wi-Fi → GSG перехватывает через TPROXY и маршрутизирует сам.
{node_drop_rule}
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

def _resolve_node_ips(nodes_data: dict) -> list[str]:
    """Резолвим hostname нод в IP-адреса для DROP правила.
    Блокируем только ноды с суффиксом *.nodes.globalshield.ru — это наши
    выделенные серверы с уникальными IP. CDN-ноды (cdn.*, Cloudflare) пропускаем:
    они используют shared IP-адреса за которыми сидят тысячи других сервисов."""
    seen = set()
    ips = []
    for n in nodes_data.get("nodes", []):
        server = n.get("server", "").strip()
        if not server or server in seen:
            continue
        seen.add(server)
        # Только выделенные ноды, не CDN/Cloudflare
        if not server.endswith(".nodes.globalshield.ru"):
            continue
        try:
            socket.inet_aton(server)  # уже IP
            ips.append(server)
        except OSError:
            try:
                ip = socket.gethostbyname(server)
                ips.append(ip)
            except Exception as e:
                print(f"[WARN] Не удалось резолвить {server}: {e}", flush=True)
    return list(dict.fromkeys(ips))  # уникальные, сохраняя порядок

class NetEnforcer:
    async def setup_os_routing(self):
        os.system("sysctl -w net.ipv4.ip_forward=1")
        # Conntrack — применяем значения из install.sh (131072/600s)
        # net-enforcer НЕ переопределяет conntrack_max — оно задаётся через /etc/sysctl.d/99-gsg.conf
        # Здесь только гарантируем минимум на случай если sysctl.d не применился
        os.system("sysctl -w net.netfilter.nf_conntrack_max=131072 2>/dev/null || true")
        os.system("sysctl -w net.netfilter.nf_conntrack_tcp_timeout_established=600 2>/dev/null || true")
        # Swappiness — низкое значение чтобы ядро не свопировало без нужды
        os.system("sysctl -w vm.swappiness=10 2>/dev/null || true")
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

        bp = [i.get("reserved_ip", ip) for ip, i in data.items() if i.get("mode") == "bypass"]
        bp = bp or ["127.0.0.99"]

        node_drop_set  = ""
        node_drop_rule = ""

        conf = NFT_TEMPLATE.format(
            bypass_ips=", ".join(bp),
            node_drop_set=node_drop_set,
            node_drop_rule=node_drop_rule,
            tproxy_port=TPROXY_PORT,
        )

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
