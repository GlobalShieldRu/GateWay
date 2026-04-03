# CLAUDE.md — GSG Smart Gateway

## Обзор

GSG Smart Gateway — веб-дашборд для управления VPN-шлюзом (OrangePi/NanoPi). Работает на Mihomo (Clash Meta).

## Архитектура

| Компонент | Каталог | Контейнер | Назначение |
|-----------|---------|-----------|-----------|
| Web UI + API | `web-orchestrator/` | `gsg-web-orchestrator` | FastAPI, дашборд (index.html) |
| VPN-туннель | `tunnel-provider/` | `gsg-tunnel` | Mihomo, generate_config.py |
| DHCP | `registry-dhcp/` | `gsg-dhcp` | dnsmasq |
| Firewall | `net-enforcer/` | `gsg-netenforcer` | iptables, tproxy |

## Маршрутизация

Proxy-группы управляются через вкладку "Маршруты":
- Данные: `/etc/gsg/rules.json` → поле `proxy_groups`
- Встроенные группы: **Auto** (url-test, все узлы), **AI** (fallback, фильтр NY), **Bypass** (DIRECT)
- `generate_config.py` создаёт Mihomo proxy-groups из `proxy_groups`
- Миграция: если `proxy_groups` нет — строится автоматически из `ai_settings`, `custom_groups`, `proxy`

### Порядок правил Mihomo
```
override_rules → domain_rules (из групп) → ip_rules (устройства) → custom_routing_rules → MATCH
```
Доменные правила групп приоритетнее режимов устройств.

## OTA обновления

- Systemd-сервис `gsg-updater.service` — следит за триггером через `inotifywait` (не cron)
- Скрипт: `update-watcher.sh` — git fetch → build → up → healthcheck → автооткат при ошибке
- Прогресс-бар: 6 стадий, `/api/update/status` возвращает stage + log_lines
- `/api/version` — публичный эндпоинт (без авторизации) для healthcheck
- После `git reset --hard` автоматически делается `chmod +x *.sh`
- Релиз: `./release.sh X.Y.Z` — обновляет GSG_VERSION, тег, GitHub Release

## Деплой

**Обязательно пересобирать образы** (docker cp недостаточно!):
```bash
# На хосте GSG (10.10.1.139):
cd /root/GSG
docker compose build tunnel-provider web-orchestrator
docker compose up -d
```
`entrypoint.sh` вызывает `/usr/local/bin/generate_config.py` из образа, а не `/app/`.

## Конфигурация

| Файл | Путь в контейнере | Назначение |
|------|-------------------|-----------|
| `rules.json` | `/etc/gsg/rules.json` | proxy_groups, route_overrides, direct |
| `rulesets.json` | `/etc/gsg/rulesets.json` | rkn_bypass, ru_direct, whitelist_bypass |
| `devices.json` | `/etc/gsg/devices.json` | Режимы устройств (smart/global/bypass/block) |
| `subscription.json` | `/etc/gsg/subscription.json` | URL подписки, global_node |
| `nodes.json` | `/etc/gsg/nodes.json` | Кэш узлов (из подписки) |

## Разработка

Single-file приложение (`index.html`) + FastAPI (`main.py`) + config generator (`generate_config.py`).

- Tailwind CSS через CDN, vanilla JavaScript
- Все тексты на русском
- Проверка JS: `node -e "new Function(...)"`
- Проверка Python: `py_compile.compile(...)`