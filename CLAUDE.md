# CLAUDE.md — GSG Smart Gateway

## 🎯 Открытые направления / техдолг — BACKLOG

**При старте сессии** прочитать живой документ с приоритетным списком открытого:

`~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Personal/02-Проекты/GlobalShield/GSG/BACKLOG.md`

(Obsidian vault пользователя, кириллический путь — `02-Проекты`, а не `02-Projects`.) Там по 🔴/🟡/🟢 приоритетам разложены архитектурные задачи (`block_vpn_app` сигнатурная фильтрация, TUN-inbound в Mihomo, UI heap leak, дача через 39, …) с оценкой времени. После закрытия пункта — перенести в раздел «История закрытого» в самом BACKLOG.md.

Связанные обсидиановые материалы: `Personal/02-Проекты/GlobalShield/GSG/{README,Research,Decisions,Incidents,Architecture,Monitoring}/...`

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

**Триггер:** пользователь нажимает кнопку в UI → web-orchestrator создаёт файл `.update_trigger` в volume `/var/lib/docker/volumes/gsg_gsg_config/_data/` → `inotifywait` в `update-watcher.sh` реагирует мгновенно.

**6 стадий `update-watcher.sh`:**
1. `git fetch origin main`
2. `git reset --hard origin/main` + `chmod +x *.sh`
3. `docker compose build` (пересборка образов)
4. `docker compose up -d`
5. `sleep 15` — ожидание запуска
6. Healthcheck (3 попытки): `/api/version`, порт 9090 (Mihomo), dnsmasq, интернет

**Автооткат:** если healthcheck провален — `git reset --hard` к предыдущему hash + `docker compose build` + `up`.

**Версия:** `GSG_VERSION` в `web-orchestrator/main.py`. OTA видит обновление по смене версии → **перед OTA нужен релиз**.

**Релиз перед OTA:** `./release.sh X.Y.Z` — обновляет GSG_VERSION, создаёт тег, GitHub Release, уведомляет в Telegram. Без релиза OTA не покажет новую версию.

- Сервис: `gsg-updater.service` (systemd, не cron)
- Прогресс: `/api/update/status` возвращает stage + log_lines
- Healthcheck endpoint: `/api/version` (публичный, без авторизации)

## Установка на устройство

Скрипты установки/удаления GSG на целевом устройстве (OrangePi/NanoPi/Raspberry Pi):

| Скрипт | Назначение |
|--------|-----------|
| `install.sh` | Полная установка: зависимости, Docker, systemd-сервисы, сборка образов, регистрация устройства |
| `uninstall.sh` | Полное удаление GSG, возврат на DHCP |

Пользователи устанавливают через:
```bash
bash <(curl -fsSL https://www.globalshield.ru/install.sh)
```

**install.sh на сайте** — файл должен быть доступен на сервере Stockholm (`194.87.30.15`) по пути `/root/vless_front/www/install.sh`. Caddy отдаёт как `application/x-sh`. Если файл отсутствует — Caddy вернёт `index.html` (SPA-фолбэк), и пользователь получит ошибку `syntax error near unexpected token 'newline'`.

Обновить скрипт на сайте:
```bash
scp GSG/install.sh root@194.87.30.15:/root/vless_front/www/install.sh
```

## Деплой

**Основной процесс:** коммит + пуш в git → пользователь запускает OTA-обновление через веб-интерфейс GSG. Пересборка образов происходит автоматически через `update-watcher.sh`.

**Вручную `docker compose build` не делаем** — только через OTA.

**Экстренный фикс** (до следующего релиза) — патчим файл прямо в работающем контейнере:
```bash
scp tunnel-provider/generate_config.py root@10.10.1.139:/tmp/gen.py
ssh root@10.10.1.139 'docker cp /tmp/gen.py gsg-tunnel:/usr/local/bin/generate_config.py'
```
`entrypoint.sh` вызывает `/usr/local/bin/generate_config.py` из образа, а не из `/app/`.

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