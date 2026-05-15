#!/usr/bin/env python3
"""GSG network auto-detect.

Логика: ARP-probe к probe_gateway (например, 10.10.2.1 = дачный шлюз).
Используется именно ARP (через `arping`), не ICMP ping:
  - ARP работает на L2, не зависит от L3-маршрутизации
  - ICMP ping не подходит: с secondary IP на eth0 directly-connected роут
    может ложно срабатывать (ARP broadcast в home LAN иногда отвечает
    случайный сосед или kernel-side ARP shenanigans — поймали на 2026-05-15)

Если ARP-ответ от probe_gateway получен → устройство в этой LAN → match_mode.
Если ARP-таймаут → устройство в другой LAN → fallback_mode (home).

Запускается systemd-сервисом при старте и таймером раз в минуту.
Защита от flapping: при детекте смены ждём 30с, перепроверяем, и только потом switch.
Idempotent: если netplan уже соответствует — ничего не делаем (не дёргаем netplan apply
впустую, не теряем connection).

Конфиг — /etc/gsg/network-modes.json. Если файл отсутствует или auto_network.enabled=false,
скрипт молча выходит — на 254 (один режим) автодетект не нужен.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CONFIG = Path("/etc/gsg/network-modes.json")
NETPLAN = Path("/etc/netplan/01-gsg-lan.yaml")
RELOAD_TRIGGER = Path("/var/lib/docker/volumes/gsg_gsg_config/_data/.reload_nftables")
GSG_CONTAINERS = ["gsg-netenforcer", "gsg-tunnel", "gsg-dhcp", "gsg-web-orchestrator"]


def log(msg: str) -> None:
    print(f"[gsg-network-detect] {msg}", flush=True)


def detect_iface() -> str:
    """Определяем основной интерфейс через default route."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if len(fields) >= 8 and fields[1] == "00000000":
                    return fields[0]
    except Exception:
        pass
    return "eth0"


def arp_probe(host: str, iface: str, timeout_sec: int = 2) -> bool:
    """ARP-запрос к host через iface. Возвращает True если получен ARP-reply.
    Используем iputils-arping (`arping -c 1 -w {sec} -I {iface} {ip}`) — он
    отправляет ARP-broadcast и ждёт ответа. Не зависит от L3-routing."""
    try:
        r = subprocess.run(
            ["arping", "-c", "1", "-w", str(timeout_sec), "-I", iface, host],
            capture_output=True, text=True, timeout=timeout_sec + 3,
        )
        return r.returncode == 0
    except FileNotFoundError:
        log("arping не установлен — попробуй: apt install -y iputils-arping")
        return False
    except Exception as e:
        log(f"arping {host} ошибка: {e}")
        return False


def render_netplan(mode: dict) -> str:
    ip = mode["ip"]
    gw = mode["gateway"]
    dns = mode.get("dns", ["8.8.8.8", "1.1.1.1"])
    return (
        "network:\n"
        "  version: 2\n"
        "  renderer: networkd\n"
        "  ethernets:\n"
        "    eth0:\n"
        f"      addresses: [{ip}]\n"
        "      dhcp4: false\n"
        f"      gateway4: {gw}\n"
        "      nameservers:\n"
        f"        addresses: [{', '.join(dns)}]\n"
    )


def apply_netplan(content: str) -> bool:
    if NETPLAN.exists():
        backup = NETPLAN.with_name(f"{NETPLAN.name}.bak-{int(time.time())}")
        backup.write_text(NETPLAN.read_text())
        # Чистим старые backup'ы — оставляем 5 последних
        backups = sorted(NETPLAN.parent.glob(f"{NETPLAN.name}.bak-*"))
        for old in backups[:-5]:
            try:
                old.unlink()
            except Exception:
                pass

    NETPLAN.write_text(content)
    os.chmod(NETPLAN, 0o600)

    try:
        subprocess.run(["netplan", "generate"], check=True, capture_output=True, text=True)
        subprocess.run(["netplan", "apply"], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        log(f"netplan apply упал: {e.stderr}")
        return False

    time.sleep(3)
    return True


def reload_containers() -> None:
    try:
        RELOAD_TRIGGER.parent.mkdir(parents=True, exist_ok=True)
        RELOAD_TRIGGER.touch()
    except Exception:
        pass
    try:
        subprocess.run(
            ["docker", "restart"] + GSG_CONTAINERS,
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        log(f"docker restart ошибка: {e}")


def main() -> int:
    if not CONFIG.exists():
        # Нет конфига — этот узел не имеет multi-mode логики (например, 254)
        return 0

    try:
        cfg = json.loads(CONFIG.read_text()).get("auto_network", {})
    except Exception as e:
        log(f"некорректный {CONFIG}: {e}")
        return 1

    if not cfg.get("enabled"):
        return 0

    probe_gw = cfg.get("probe_gateway")
    match_mode = cfg.get("match_mode")
    fallback_mode = cfg.get("fallback_mode")
    if not (probe_gw and match_mode and fallback_mode):
        log("конфиг неполный (нужны probe_gateway/match_mode/fallback_mode)")
        return 1

    iface = detect_iface()
    is_match = arp_probe(probe_gw, iface)
    target = match_mode if is_match else fallback_mode

    desired = render_netplan(target)
    current = NETPLAN.read_text() if NETPLAN.exists() else ""
    if current.strip() == desired.strip():
        # Уже в нужном режиме — выходим без дёргания сети.
        # КРИТИЧНО: ничего не пишем, не дёргаем netplan apply, не рестартим
        # контейнеры. Каждый docker restart рвёт активные TLS-сессии
        # клиентов в LAN (Clash Royale «зависает на 97%» и т.д.).
        return 0

    # Hysteresis: подождать и перепроверить дважды — и arp-probe, и netplan-файл.
    # Защищает от: (1) флапа ARP-ответов; (2) race condition когда netplan apply
    # асинхронно переформатирует файл и мы видим «расхождение» которое сейчас
    # исчезнет.
    log(f"arp-probe {probe_gw}: {'отвечает' if is_match else 'недоступен'} → может потребоваться switch к '{target.get('ip')}'")
    time.sleep(30)
    is_match2 = arp_probe(probe_gw, iface)
    if is_match2 != is_match:
        log(f"flapping ({is_match} → {is_match2}), отменяю switch")
        return 0

    # Перечитываем netplan ПОСЛЕ sleep — за 30с он мог стабилизироваться.
    current_after = NETPLAN.read_text() if NETPLAN.exists() else ""
    if current_after.strip() == desired.strip():
        log("netplan уже совпадает после стабилизации — switch не нужен")
        return 0

    log(f"применяю netplan: ip={target['ip']} gateway={target['gateway']}")
    if not apply_netplan(desired):
        log("apply failed — откат")
        return 1

    # Validation: проверяем что новый gateway достижим. Если нет — rollback
    # на предыдущий netplan (он в backup'е).
    time.sleep(5)
    new_gw = target["gateway"]
    if not arp_probe(new_gw, iface, timeout_sec=3):
        log(f"новый gateway {new_gw} НЕ отвечает — rollback на backup")
        backups = sorted(NETPLAN.parent.glob(f"{NETPLAN.name}.bak-*"))
        if backups:
            NETPLAN.write_text(backups[-1].read_text())
            try:
                subprocess.run(["netplan", "apply"], capture_output=True, text=True, check=True)
                log("rollback применён, контейнеры НЕ рестартим (не было реального switch)")
            except subprocess.CalledProcessError as e:
                log(f"rollback netplan apply упал: {e.stderr}")
        return 1

    log("новый gateway отвечает — перезапускаю GSG контейнеры для подхвата новой подсети")
    reload_containers()
    log("switch завершён")
    return 0


if __name__ == "__main__":
    sys.exit(main())
