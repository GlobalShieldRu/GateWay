# Changelog

Все значимые изменения в проекте GSG Smart Gateway.

## [Unreleased]

## [1.13.4] — 2026-05-13

### Изменено
- **Критично: устройство-специфичная конфигурация вынесена в `.env`** (`docker-compose.yml`, `install.sh`, `update-watcher.sh`, `network-watcher.sh`). Раньше `GSG_GATEWAY_IP`, `GSG_LAN_INTERFACE`, `GSG_DHCP_START/END` хардкодились в `docker-compose.yml` (дефолт `10.10.1.139/eth0` — это OrangePi) и патчились `sed`-ом в `install.sh` под конкретное устройство. При OTA `git reset --hard origin/main` затирал эти патчи. На NanoPi с интерфейсом `end0` контейнер `gsg-dhcp` падал в crash loop с `dnsmasq: unknown interface eth0` → клиенты теряли DNS → весь интернет ложился. Фикс: `compose.yml` теперь использует `${GSG_GATEWAY_IP:?требуется .env}` — переменные **обязаны** быть в `.env` (создаётся `install.sh`, в `.gitignore`, не трогается `git reset`). `network-watcher.sh` пишет в `.env` вместо `compose.yml`. `update-watcher.sh` при OTA восстанавливает `.env` из `/etc/gsg/network.json` если потерян (миграция со старой версии). Fail-fast вместо дефолта: лучше явная ошибка `docker compose` чем «GSG поднялся с настройками другого устройства».

## [1.13.3] — 2026-05-13

### Исправлено
- **Критично: узлы массово помечались как ERR, клиенты теряли связь** (`tunnel-provider/generate_config.py`). В v1.13.0 включили `lazy: false` для групп `auto`/`ai`, чтобы устранить cold-start. Но Mihomo health-check ходит на `http://www.gstatic.com/generate_204` через каждый узел, и VLESS Reality узлы с SNI `kinopoisk.ru` этот конкретный URL **не пропускают** — Mihomo помечает их мёртвыми, fallback переключается на медленные CDN-узлы (`*|Обход блокировок`) или вообще теряет связь. Видно в UI: 4-5 узлов в ERR. На самих VPN-серверах узлы работают, проблема была на стороне GSG в логике health-check. Откатил `lazy: true` для всех групп — узлы проверяются только при реальном трафике через настоящие домены, ложных ERR нет. Cold-start фикс остаётся в виде prewarm при старте/reload через `entrypoint.sh` (он использует прямые `/group/*/delay` вызовы — однократный прогрев истории). Долгосрочное решение — сменить health-check URL на тот что проходит через Reality-узлы (см. `Decisions/2026-05-13-mihomo-cold-start-warmup.md`).

## [1.13.2] — 2026-05-13

### Исправлено
- **Критично: на Armbian Trixie (NanoPi и более новые образы) DNS ломался у хост-приложений** (`net-enforcer/main.py`). `git fetch`, `apt`, `curl` от лица root зависали с «Could not resolve host». Симптом возникал только на Debian 13 с активным `systemd-resolved` stub-listener'ом на `127.0.0.53`. Корневая причина: `postrouting` chain в `net-enforcer` содержал безусловный `masquerade` — он применялся ко **всем** исходящим пакетам, включая loopback к `127.0.0.53`. Ядро подменяло src IP с `127.0.0.x` на адрес LAN-интерфейса, после чего systemd-resolved отбрасывал пакеты с логом «Got packet on unexpected (i.e. non-localhost) IP range, ignoring». На OrangePi (Armbian 22.02, Debian 11) эта проблема не проявлялась — там NetworkManager пишет статичный `/etc/resolv.conf` с прямым `nameserver 8.8.8.8`, минуя stub. Фикс — добавить `oif "lo" return` перед `masquerade`: loopback-трафик больше не маскарадится. ОТА после v1.13.1 на устройствах с Trixie ломался первым же `git fetch`.

## [1.13.1] — 2026-05-13

### Изменено
- **`release.sh` автоматически переименовывает `[Unreleased]` → `[X.Y.Z] — DATE`** в CHANGELOG перед коммитом версии. Раньше требовалось вручную поправить CHANGELOG до запуска `release.sh`, иначе GitHub Release заполнялся дефолтным git-log (так и случилось с v1.13.0). Теперь release.sh сам делает рерайт.

### Исправлено
- **OTA автоматически устанавливает новые systemd-юниты GSG** (`update-watcher.sh`). До этой правки появление в репо нового `gsg-*.service` (как `gsg-network-watcher` в 1.13.0) НЕ приводило к его установке на устройстве — пользователю надо было руками `cp service && systemctl enable`, что в модели «GSG автономен, открывают раз в год» означало мёртвую фичу. Теперь `update-watcher.sh` после `git reset` сканирует `/root/GSG/gsg-*.service`, сравнивает с `/etc/systemd/system/`, копирует изменённые/новые, делает `daemon-reload + enable + restart`. Дополнительно: если сам `update-watcher.sh` обновился — после healthcheck рестартим `gsg-updater.service --no-block`, чтобы systemd подхватил новый код (иначе старая копия живёт в памяти до reboot).

## [1.13.0] — 2026-05-13

### Изменено
- **Mihomo prewarm + RU-DNS-policy для устранения «холодных» 5-15с задержек первого открытия** (`tunnel-provider/generate_config.py`). Раньше критичные fallback-группы (`auto`, `ai`) шли с `lazy: true, timeout: 15000` — Mihomo не прогревал узлы проактивно, и любой reload `rules.json` (drag, override, добавление в группу) обнулял history. Первый клиентский запрос триггерил url-test и ждал до 15 с — пользователь видел «Потерялся интернет», «Не удалось подключиться», виснущие страницы Яндекса. Теперь для `auto`/`ai`: `lazy: false, interval: 60` — постоянный прогрев, history всегда свежая, cold-start устранён. Plus `nameserver-policy`: RU-домены (`+.ru`, `+.рф`, `+.su` + `geosite:cn,private`) идут к `77.88.8.8` (Yandex DNS, local-edge IP в ответе) и `1.1.1.1`; глобальные — `77.88.8.8/1.1.1.1/8.8.8.8`. Дефолтный nameserver сменён с `8.8.8.8` на `77.88.8.8` — быстрее из РФ. **NB**: пробовали и `enhanced-mode: fake-ip` — откатили: сломало `GEOIP,ru,DIRECT,no-resolve` (см. `Decisions/2026-05-13-mihomo-cold-start-warmup.md`).

### Добавлено
- **Перенастройка сети GSG из UI** (раньше задавалась только в `install.sh`). В вкладке «Настройки DHCP» появился блок «Сеть GSG в LAN» с полями: IP GSG, длина префикса, шлюз провайдера, DNS провайдера. После нажатия «Применить сеть» UI показывает confirm-диалог с предупреждением о потере сеанса, бэкенд пишет `/etc/gsg/network.json` и маркер `.network_reconfig_request`. Новый host-сервис `gsg-network-watcher.service` (`network-watcher.sh`) подхватывает маркер, регенерирует `/etc/netplan/01-gsg-lan.yaml`, обновляет env-vars в `docker-compose.yml`, пересоздаёт контейнеры `gsg-dhcp`/`gsg-netenforcer` с новыми параметрами и применяет `netplan apply`. Сценарий «перенос на дачу»: задал новую сеть → переподключился к новому IP через 30-60 с, без переустановки. `install.sh` создаёт `network.json` при первой установке и регистрирует новый systemd-сервис.

### Исправлено
- **Форма обратной связи: «Ошибка, попробуйте снова», хотя сообщение в Telegram доезжает** (`web-orchestrator/main.py`). `/api/feedback` синхронно ждал три внешних запроса (`ip-api.com` геолокация ~4 с + `resolve-user` к GlobalShield API ~5 с + `sendMessage` через Mihomo до 10 с — итого до ~19 с), а фронтенд `utils.apiCall` обрывает ожидание на 8 с и показывает «Ошибка». Telegram-нотификация вынесена в `asyncio.create_task` — ответ отдаётся сразу после сохранения в `feedback.json`, доставка в Telegram продолжается в фоне.
- **Drag-and-drop приложения на узел NY/Auto не работал, если домены ранее были в `route_overrides → DIRECT`** (`web-orchestrator/static/index.html`). Например, после Apple→Bypass в `route_overrides` оставались `apple.com→DIRECT, icloud.com→DIRECT, …`, и Mihomo вычисляет `override_rules` до `domain_rules` группы — drag визуально срабатывал (домены попадали в group `ai`), но трафик всё равно шёл DIRECT. `addAppToGroup` теперь дополнительно дёргает `DELETE /api/rules/overrides` для своих доменов перед добавлением в группу (кроме случая drag в Bypass — там DIRECT и нужен).
- **`DELETE /api/rules/overrides` падал `KeyError: 'domain'` на записях с `ip-cidr`** (`web-orchestrator/main.py`). Использован `.get("domain", "")` — теперь фильтрация безопасно пропускает ip-cidr-записи.

## [1.12.0] — 2026-05-12

### Добавлено
- **DHCP-toggle: gateway-only режим** (новая фича). В UI на вкладке «Настройки DHCP» появился переключатель «GSG раздаёт IP-адреса (DHCP-сервер)». Когда выключен — контейнер `gsg-dhcp` (dnsmasq) останавливается, GSG **не** отвечает на DHCP-запросы. Раздачей IP занимается основной роутер пользователя, а на устройствах, которые должны идти через GSG, нужно **вручную** указать шлюз = IP GSG (DNS — этот же IP). Полезно для гибридных установок где GSG ставится поверх существующей сети без замены DHCP. Реализация: флаг `dhcp_enabled` в `settings.json`, `entrypoint.sh` контейнера `gsg-dhcp` проверяет флаг при старте, при изменении флага в UI создаётся `.dhcp_restart_request` маркер + entrypoint реагирует через `inotifywait` и выходит → `restart: always` перезапускает контейнер в нужном режиме. В UI добавлен confirm-диалог при выключении (предупреждение о ручной настройке устройств) и inline-warning когда DHCP неактивен.
- **DEFAULT_DIRECT_DOMAINS / DEFAULT_DIRECT_IP_CIDRS в `tunnel-provider/generate_config.py`**: список «накопленных уроков» — 16 доменов RU-сервисов (HeadHunter `hh.ru`/`hhcdn.ru`, Мособлеирц, VK CDN `vkuseraudio.*`/`vkuservideo.*`/`vkuserphoto.*`/`vk-portal.net`/`vk-cdn.net`/`vk.me`/`vk-apps.com`/`userapi.com`/`mycdn.me`/`apptracer.ru`/`vkvideo.ru`) и 3 IP-CIDR (Telegram fronting `194.221.250.0/24`, официальные DC `149.154.160.0/20`, `91.108.0.0/16`). Применяются ПОСЛЕ пользовательских `route_overrides` (user правила приоритетнее) и ПЕРЕД `domain_rules`/GeoIP/catch-all. Новые установки GSG из коробки получают правильную маршрутизацию для сервисов, которые ранее ломались (VK Music CDN через VPN, Telegram-fronting перегружал Stockholm-узел, и т.д.). Закрывает класс инцидентов, где раньше каждый случай ловился вручную через runtime `route_overrides`.
- **Auto-update по умолчанию включён** (`web-orchestrator/main.py`): дефолт `_SETTINGS_DEFAULTS = {"auto_update": True, "dhcp_enabled": True}`. Раньше было `False` — для автономной модели GSG это неправильно: пользователь заходит в UI редко, без явного включения версия устаревает. Цикл `_auto_update_loop` (раз в 6 часов проверяет latest на GitHub) уже был реализован, но без включения был бесполезен. Кто не хочет — выключает явно через тоггл в UI.
- **GeoIP-fallback `GEOIP,ru,DIRECT` перед catch-all** (`tunnel-provider/generate_config.py`): закрывает класс «нишевый RU-сервис в catch-all → Stockholm → блок не-RU IP». До этого фикса каждый случай (hh.ru, Мособлеирц, VK Music CDN, Yandex Market CDN, …) приходилось ловить вручную через `route_overrides`. С `GEOIP,ru` любой IP в RU-AS попадает в DIRECT автоматически — без поддержки списка. Использует встроенный `geoip.dat` от runetfreedom/russia-v2ray-rules-dat, оптимизированный под РФ. Стоит **после** явных `domain_rules`/`override_rules`/`geoip_ai_rules` (AI и пользовательские правила приоритетнее) и **перед** `MATCH,auto`.
- **`route_overrides` теперь поддерживает `ip-cidr`** (`tunnel-provider/generate_config.py`): раньше парсился только `domain` — мой собственный фикс Telegram-fronting (`{"ip-cidr": "194.221.250.0/24", "target": "DIRECT"}`) тихо игнорировался. Теперь `ip-cidr`-записи корректно превращаются в `IP-CIDR,<cidr>,<target>,no-resolve` Mihomo-правила. Закрывает кейс «легитимный сервис в анонимном CIDR-блоке должен идти DIRECT».
- **Видимый JS heap-индикатор в UI** (правый нижний угол): показывает текущее потребление памяти вкладкой GSG в МБ, цвет индикатора меняется (серый → жёлтый > 400 МБ → красный > 800 МБ). Клик перезагружает вкладку с подтверждением. Toast-предупреждение при превышении 600 МБ (раз в час). Авто-reload фоновой вкладки если heap > 1 ГБ и вкладка скрыта 60+ сек — защита от падений UI при долгой утечке памяти. Не устраняет утечку, но даёт visibility до её локализации через DevTools Heap Snapshot.

### Исправлено
- **QUIC blackhole-fix: расширенный whitelist `quic_bypass_nets`** (`net-enforcer/main.py`): набор включает Yandex AS13238 + Yandex.Cloud AS208722 (Telemost media), Fastly AS54113 (yastatic, Telemost-чат CDN), Sberbank AS35237, плюс RU гос/телеком префиксы (Минцифры/Госуслуги 91.103.0.0/16, 109.207.0.0/16, Ростелеком 213.59.0.0/16, Связьинвест 176.211.0.0/16, МосОблЭлектро 94.79.0.0/16, прочие). Архитектурный контекст: UDP TPROXY на текущем ядре blackhole'ит ответные пакеты, и без `reject UDP/443 from LAN` (с whitelist'ом для критичных сервисов) YouTube и др. зависают. TODO: заменить на динамический GeoIP RU через RIPE delegated + cron-обновление.

## [1.11.0] — 2026-04-22

Крупный UI-релиз: новая страница «Приложения» с полноценным редактором (вместо хардкода) + унификация защищённых сервисов (DIRECT) для всех proxy-групп + переход маршрутизации на MAC-привязку + расширение Claude/Anthropic стека (Stripe/Datadog/Vercel/Cloudflare NEL) и связанные routing-фиксы.

### Добавлено
- **Apps Management (страница «Приложения»)**: APPS/APP_DOMAINS перенесены в `apps.json` на сервере. Новый пункт «Приложения» в боковом меню (Expert режим) — таблица всех приложений с поиском, бейджами (US, всегда, 🔒 builtin) и expand-редактором каждого приложения: название, цвет, favicon, флаги (US-нода, alwaysActive), список доменов в chip-форме с автоформатом DOMAIN-SUFFIX/KEYWORD/IP-CIDR (иконки 🌐/🔍). Создание кастомных приложений и удаление не-builtin (builtin защищены от удаления). Хранилище `/etc/gsg/apps.json`, автомиграция 16 встроенных при первом запуске. REST API: `GET/POST /api/apps`, `PATCH/DELETE /api/apps/{id}`. Regex для подсветки активного приложения в трафике генерируется автоматически из доменов. Frontend читает данные из API при старте, fallback на хардкод при недоступности API.
- **Чип-пресеты приложений в редакторе proxy-группы**: над полем правил блок с кнопками TikTok, Instagram, Gemini, Claude — клик добавляет/убирает все домены пресета разом. Кнопка Claude (оранжевая) вставляет полный Claude/Anthropic стек: anthropic.com, claude.ai, claude.com, statsig-anthropic.com, sentry-anthropic.io, IP-подсети Anthropic (160.79.104.0/22, 160.79.108.0/22). Работает в форме создания группы и в редакторе существующей.
- **Унифицированный chip-блок «Защищённые сервисы (DIRECT)»** для всех proxy-групп (включая форму создания новой) — пресет 13 RU-сервисов (Госуслуги, Сбер, ВТБ, Тинькофф, Альфа, Райффайзен, Газпромбанк, mos.ru, nalog.ru, gov.ru, cbr.ru, mil.ru) подключён по умолчанию, чипы можно убирать (✕ → `exclusions_disabled`) и добавлять свои (`exclusions_custom`). Custom-добавление с автоформатом: 🌐 IP-CIDR, 🔍 KEYWORD, без префикса DOMAIN-SUFFIX.
- **Поля `exclusions_disabled` / `exclusions_custom`** в схемах ProxyGroup{Create,Update} (`web-orchestrator/main.py`); legacy-поле `exclusions` поддерживается и мигрируется как `custom`.
- **Секция «Группы»** в chip-блоке exclusions: dropdown «+ Группа» позволяет подключить любую existing proxy-группу как DIRECT-исключение (фиолетовый chip 📦 Имя). Защита от циклических ссылок group:A → group:B → group:A в `_get_effective_exclusions` через `_visited: set`.
- **Pre-set Claude расширен с 7 до 22 элементов**: anthropic stack (10) + Stripe (6: stripe.com, stripe.network, js./checkout./api./m.stripe.com) + Datadog (3: datadoghq.com/.eu, ddog-gov.com) + Cloudflare NEL (nel.cloudflare.com) + IP-CIDR Anthropic ASN (160.79.104.0/22, 160.79.108.0/22). Закрывает leaks биллинга/метрик/error-reporting Claude через RU-IP.
- **Sniffer skip-domain** расширен: anthropic.com, claude.ai, claude.com, stripe.com, stripe.network, vercel.com, vercel.app, cloudflare.com, openai.com, chatgpt.com — sniffer override-destination ломал TLS fingerprint, и Anthropic/Stripe/Vercel считали трафик подозрительным. Через HAPP (без Mihomo sniffer) сайты работают, через GSG падали — теперь Mihomo не парсит ClientHello, TLS handshake идёт как от браузера.
- **Claude/Anthropic auto-route watcher** (`gsg-claude-watcher.sh`): whitelist расширен — Stripe, Datadog, ChatGPT/OpenAI, Vercel автоматически направляются через NY при детекции трафика мимо VPN.
- **Per-device monitor** (`gsg-monitor-100.sh` + `gsg-monitor-100.service`): постоянный мониторинг `/connections` Mihomo для конкретного IP, фиксирует уникальные пары (chain, host) в `/var/log/gsg-monitor-{N}.log` с пометкой 🟡 DIRECT-LEAK? для DIRECT-цепочек. Помогает отлавливать leak'и в реальном времени.

### Изменено
- **MAC-based device routing**: правила Mihomo для устройств теперь генерируются по `current_ip` (актуальный IP из dnsmasq), а не по `reserved_ip`. Настройки (mode/assigned_node/tiktok_node) хранятся под MAC-ключом и автоматически следуют за устройством при смене IP. `reserved_ip` больше не возвращается в API `/api/devices` и скрыт из UI — поле остаётся в `devices.json` только для DHCP (dnsmasq). Конфликт двух MAC с одним current_ip разрешается по `last_seen` (новейший побеждает). Устройства без активности 24+ часов исключаются из правил.
- **Stripe-домены теперь идут через NY** (биллинг claude.ai требует совпадения IP с Anthropic): добавлены в группу AI вместе с Datadog/Vercel/Cloudflare NEL.
- **mcpmarket.com через NY**: Vercel блокирует RU-IP — добавлен в AI группу.
- **Supercell games (Clash Royale) через NY**: gameplay-серверы географически ограничены, RU-узлы получали высокий ping/disconnect.
- **komanda.fit через Stockholm**: RU-сайт фитнес-сервиса блокирует трафик с не-RU IP — направлен на RU-ноду через отдельную группу.

### Исправлено
- **Race-condition при выборе режима + узла для устройства**: при быстром клике «Весь трафик» → chip узла второй клик читал устаревший `mode` из `state.devices` (ещё не обновлённый с сервера) и отправлял `mode=smart` вместо `mode=global`. Исправлено оптимистичным update `state.devices` сразу при клике на pill до вызова `updateDeviceSettings`, плюс debounce 400ms для коалесцинга быстрых смен.
- **Docker cp в `/app/entrypoint.sh`**: путь раньше летел в `/entrypoint.sh` корня — экстренные patch-команды не применялись. Поправлен реальный путь в образе.

## [1.10.0] — 2026-04-22

UI/UX-релиз: унифицированный chip-блок исключений DIRECT во всех proxy-группах + ряд stability-фиксов (отключение шумного node watchdog, плановой 6-часовой переподписки) + host-level watchdog для Claude/Anthropic.

### Добавлено
- **Унифицированные исключения DIRECT (chip-style)** для всех proxy-групп: вместо textarea — chip-блок с дефолтным пресетом (госсервисы, банки, group:Bypass). Чипы пресета можно убрать (✕ → попадают в `exclusions_disabled`), можно добавить свои домены (+Добавить → `exclusions_custom`). Работает для всех групп включая форму создания новой. Группы Bypass/direct — без блока.
- **DEFAULT_EXCLUSIONS в generate_config.py**: все не-direct proxy-группы автоматически получают прямой маршрут для 13 RU-сервисов (Госуслуги, Сбер, ВТБ, Тинькофф, Альфа, Райффайзен, Газпромбанк, mos.ru, nalog.ru, gov.ru, cbr.ru, mil.ru) + `group:Bypass`, если явно не отключены пользователем (через `exclusions_disabled`).
- **Поля `exclusions_disabled` / `exclusions_custom`** в схемах `ProxyGroupCreate` / `ProxyGroupUpdate` (`web-orchestrator/main.py`). Legacy-поле `exclusions` сохранено для обратной совместимости — мигрируется как `custom`.
- **Универсальный выбор групп-exclusions**: toggle "Все из Bypass" заменён на полноценную секцию "Группы" в chip-блоке exclusions. Dropdown "+ Группа" позволяет подключить любую существующую proxy-группу (Auto, AI, Биржи, Мой IP и свои custom) — её домены будут идти DIRECT. Выбранные группы отображаются фиолетовыми chips (📦 Имя), ✕ убирает. `group:Bypass` по-прежнему включена по умолчанию.
- **Защита от рекурсии в `_get_effective_exclusions`** (`generate_config.py`): добавлен `_visited: set` — циклические ссылки group:A → group:B → group:A не вызывают бесконечную рекурсию.
- **Autoformat визуализация exclusions**: кастомные exclusions и дефолтные домены отображают тип правила через префикс в chip — 🌐 для IP-CIDR, 🔍 для ключевого слова (без точки), без префикса для DOMAIN-SUFFIX. Custom-добавление автоматически распознаёт формат: `domain.com` → DOMAIN-SUFFIX, `keyword` → DOMAIN-KEYWORD, `1.2.3.0/24` → IP-CIDR (с no-resolve).
- **Подсказка в поле добавления exclusion**: placeholder и hint-строка под input поясняют форматы: с точкой → DOMAIN-SUFFIX, без точки → DOMAIN-KEYWORD, X.X.X.X/N → IP-CIDR.
- **Claude/Anthropic auto-route watcher** (`gsg-claude-watcher.sh` + `gsg-claude-watcher.service`): host-level systemd-watchdog, который автоматически детектирует Claude/Anthropic-трафик, идущий мимо VPN, и направляет его через NY (через правки `rules.json` + Mihomo reload). Защита от breaking-changes Anthropic — при появлении новых доменов/IP трафик не «провалится» в DIRECT и не попадёт под РКН/региональные блокировки.

### Изменено
- **Node watchdog отключён** (`tunnel-provider/entrypoint.sh`, `if false; ...; fi`): давал ложные срабатывания (`alive=True`, но `history` пустая сразу после reload) и приводил к Telegram-спаму вида «subscription refresh: 3 мёртвых нод» каждые 5 минут + 15-секундным обрывам трафика на каждой переподписке. Будет переписан на event-based триггер от backend.
- **Плановое 6-часовое обновление подписки убрано**: оно создавало 15-секундные обрывы у всех клиентов 4 раза в сутки без необходимости. Backend сам уведомляет об изменениях подписки — тихий polling больше не нужен.

## [1.9.1] — 2026-04-22

Хотфикс для устранения ложных срабатываний health-check и node watchdog, которые приводили к обрывам трафика и Telegram-спаму переподписок.

### Исправлено
- **Health-check timeout 5000ms → 15000ms** для всех proxy-групп (`tunnel-provider/generate_config.py`). CDN WebSocket узлы (VLESS поверх WS через Cloudflare, например `NY | Обход блокировок`) делают handshake 5–8 секунд из-за цепочки CF → WS upgrade → VLESS inner handshake → HTTP GET `generate_204`. Прежний timeout 5000ms отсекал живой узел как мёртвый → fallback-группа `ai` ошибочно переключалась на резервный узел → пользователи видели обрывы при работе с Meta/Gemini/Discord. При `timeout=15000` узел корректно показывает `delay=156ms`.
- **Node watchdog корректно различает «мёртвый» vs «ещё не проверен»** (`tunnel-provider/entrypoint.sh`). Было: `if not p.get('alive', True) or last == 0 → dead` — ложно считал мёртвыми любые ещё-не-проверенные узлы сразу после reload (у них `history` пуста, `alive` отсутствует), из-за чего запускался circle-of-death: watchdog триггерил переподписку → переподписка делала reload → свежие узлы опять без history → watchdog опять считал их мёртвыми → повторная переподписка и Telegram-спам. Стало: skip если `history` пустая (узел ещё не проверен); `dead` только если `alive == False` И `delay == 0`.

## [1.9.0] — 2026-04-22

Обновление Mihomo на 14 минорных релизов вперёд ради стабильности fallback/url-test групп после API reload и свежих фиксов DNS/fake-ip/sniffer/TLS.

### Изменено
- **Mihomo v1.18.10 → v1.19.24** (`tunnel-provider/Dockerfile`): 14 промежуточных upstream-релизов с багфиксами proxy reload, DNS, fake-ip, sniffer, TLS. В v1.18 фиксировался баг, из-за которого после API reload (`PUT /configs`) портился internal state proxy instances — fallback-группы начинали считать живые узлы мёртвыми. В v1.19 зашёл ряд связанных фиксов. Upstream issue #2588 (fallback + health-check) всё ещё open, поэтому гарантий нет, но апгрейд даёт новейшие исправления и снижает вероятность «призрачных» мёртвых узлов.

### Добавлено
- **Host-level watchdog (подготовка)**: файлы `gsg-watchdog.sh` и `gsg-watchdog.service` добавлены в репозиторий для будущего использования. В `install.sh` оставлен NOTE-комментарий — watchdog **не устанавливается** в этом релизе, требует отладки логики `check_group` для fallback-групп с `lazy: true` (в первой версии был circle-of-death: watchdog сам триггерил рестарты, которые триггерили следующий watchdog-цикл). Будет включён в следующем релизе.

## [1.8.0] — 2026-04-22

Крупный пакет стабильности: proactive-мониторинг туннеля, kernel/TCP tuning для TPROXY под нагрузкой, IPv6 protection против Happy Eyeballs таймаутов.

### Добавлено
- **Health-watchdog** в `tunnel-provider/entrypoint.sh`: каждые 30с опрашивает группы `auto` и `ai` через Mihomo API (`/group/{name}/delay`). При 2 подряд провалах (60с «мертвы» все узлы) запрашивает hard-restart контейнера через `POST /api/tunnel-hard-restart`.
- **Endpoint `POST /api/tunnel-hard-restart`** в `web-orchestrator/main.py`: принимает вызовы от внутреннего watchdog по токену `X-Internal-Token` (env `GSG_INTERNAL_TOKEN`), cooldown 90с, пишет файл-триггер `.tunnel_restart_request` в shared volume (подхватывает `update-watcher.sh` на хосте), отправляет Telegram-уведомление об авто-восстановлении.
- **Post-reload force health-check**: после каждого `PUT /configs` (hot-reload) forced-опрос всех fallback/url-test/loadbalance групп через `/group/*/delay`. Заполняет history сразу — устраняет «призрачные» health-check состояния, когда fallback-группы первые секунды после reload думают, что все узлы мертвы.
- **Startup health-check**: прогрев всех fallback/url-test групп сразу после старта Mihomo — устройства получают живой proxy без задержки первого запроса.
- **IPv6 FORWARD REJECT** в `net-enforcer/main.py`: `ip6tables -I FORWARD 1 -j REJECT --reject-with icmp6-no-route`. Клиенты с чужим DNS (например Mac с 8.8.8.8) получают AAAA-записи и пытаются IPv6-коннект; без REJECT пакеты тихо дропались → Safari Happy Eyeballs ждал 60-70с таймаута. Теперь мгновенный ICMPv6 unreach — клиент сразу откатывается на IPv4.

### Изменено
- **Hard-restart после переподписки**: node watchdog и плановое обновление (6ч) теперь запрашивают hard-restart контейнера вместо API reload (`PUT /configs`). Избавляет от «испорченного state» в Mihomo после смены списка нод. Fallback на API reload при недоступности `/api/tunnel-hard-restart`.
- **Debounce hot-reload**: минимальный интервал между reload повышен с 1с до **30с** — частые inotify-события (пачка записей rules.json/devices.json) объединяются в один reload. Предотвращает гонку состояний в Mihomo.
- **Группа `myip`**: тип изменён с `fallback` на `url-test`, интервал health-check 600с — устраняет IP-flapping в MenuBar когда несколько нод чередуются.

### Kernel/TCP tuning (net-enforcer/main.py + install.sh)
Долгоживущие idle SSE/WebSocket переставали работать; SYN/burst терялись при нагрузке; UDP-паузы рвали TikTok/QUIC сессии. Применяется при старте net-enforcer и при установке GSG.
- `net.ipv4.tcp_keepalive_time`: 7200 → **120с** (+ `intvl=15`, `probes=3`) — idle SSE/WebSocket больше не протухают через NAT/conntrack
- `net.ipv4.tcp_max_syn_backlog`: 128 → **4096** — нет дропа SYN при burst трафика
- `net.ipv4.tcp_max_tw_buckets`: 4096 → **65536** — TIME_WAIT flood больше не бьёт по портам
- `net.ipv4.tcp_mtu_probing`: 0 → **1** — PMTU discovery при потерях больших пакетов
- `net.ipv4.tcp_slow_start_after_idle`: 1 → **0** — SSE/WebSocket после паузы не стартуют заново с нуля
- `net.ipv4.tcp_retries2`: 15 → **8** — быстрее отваливаемся от мёртвых соединений
- `net.ipv4.tcp_fin_timeout`: 60 → **15** — экономия портов при активной работе
- `net.netfilter.nf_conntrack_udp_timeout`: 30 → **180с** — TikTok/QUIC паузы теперь держат NAT
- `net.netfilter.nf_conntrack_udp_timeout_stream`: 120 → **600с** — длинные voice/видео звонки не рвутся
- `net.netfilter.nf_conntrack_generic_timeout`: 600 → **300с** — быстрее освобождаем мёртвые entry
- `net.netfilter.nf_conntrack_buckets` (hashsize): 8192 → **32768** — меньше collisions при 131k conntrack_max
- `net.core.netdev_max_backlog`: 5000 → **10000** — RX queue при burst трафике
- `net.core.somaxconn`: → **4096** (install.sh)

## [1.7.9] — 2026-04-20

### Исправлено
- WhatsApp Web QR-код не загружался на устройствах: Meta CDN-трафик шёл через DIRECT без SNI → РКН блокировал. Добавлены в группу `ai` (NY-узел) в начало rules списка: `whatsapp.net`, `whatsapp.com`, `wa.me`, `fbcdn.net`, `cdninstagram.com` и 6 IP-CIDR блоков Meta (`31.13.24.0/21`, `31.13.64.0/18`, `157.240.0.0/16`, `57.144.0.0/14`, `179.60.192.0/22`, `185.60.216.0/22`). Правила встают до rkn-domains и GEOSITE,ru-available-only-inside.

### Добавлено
- Node watchdog в tunnel-provider: `entrypoint.sh` каждые 60 секунд опрашивает `/proxies` Mihomo и считает мёртвые прямые VLESS-ноды (delay=0, без учёта групп). Если 3+ нод подряд помечены мёртвыми — немедленно триггерится переподписка (`fetch_subscription.sh` + reload Mihomo), не дожидаясь планового обновления. Плановая переподписка каждые 6 часов сохранена как fallback.
- Sniffer `skip-domain`: 19 российских доменов (`+.yandex.ru`, `+.yandex.net`, `+.yandex.com`, `+.yandex.kz`, `+.ya.ru`, `+.yastatic.net`, `+.wildberries.ru`, `+.wb.ru`, `+.wbbasket.ru`, `+.ozon.ru`, `+.ozone.ru`, `+.sber.ru`, `+.sberbank.ru`, `+.vk.com`, `+.vk.ru`, `+.mail.ru`, `+.avito.ru`, `+.dzen.ru`, `+.gosuslugi.ru`) исключены из TLS/QUIC sniff. Устраняет задержку «кружок загрузки» при первом подключении к российским сервисам — Mihomo больше не парсит TLS ClientHello для доменов, которые и так идут DIRECT.

### Исправлено
- Discord WebSocket обрывался каждые ~2 минуты: отключён `_stale_connection_cleaner` в web-orchestrator — он закрывал keep-alive соединения с нулевым счётчиком трафика старше 120 с, к которым относятся Discord/Telegram WebSocket gateway heartbeat-сессии. Функция сохранена в коде, только вызов закомментирован.
- Долгоживущие WebSocket-соединения (Discord, Telegram) рвались из-за истечения conntrack: `nf_conntrack_tcp_timeout_established` увеличен с 600 с (10 мин) до 7200 с (2 ч) в net-enforcer/main.py и install.sh.
- Реверт агрессивного health-check: параметры proxy-groups возвращены к мягким значениям (`interval: 120`, `lazy: true`, `timeout: 5000ms`, `max-failed-times: 3`) для всех групп без исключений. Предыдущие значения (`interval: 30` для auto, `lazy: false`, `timeout: 3000ms`, `max-failed-times: 1`) разрывали долгие сессии Gemini, Discord и Telegram из-за частых переключений активного узла.
- IPv6 Happy Eyeballs таймаут: браузеры устройств делали IPv6-коннект (Happy Eyeballs), ждали 60-70 секунд таймаута и лишь потом откатывались на IPv4 — из-за того что Mihomo DNS возвращал AAAA-записи, хотя IPv6 в GSG не настроен. Добавлено `ipv6: false` в блок `dns:` конфига (плюс глобальный `ipv6: false` на уровне Mihomo). Теперь DNS не возвращает AAAA-записи → браузеры знают только IPv4 → Happy Eyeballs не инициируется → исчезают задержки 60-70 сек при первом обращении к сайту.
- Яндекс RTC (Телемост + Яндекс.Мессенджер звонки): iOS-клиенты использовали UDP на нестандартных портах к Яндекс TURN-серверам без SNI → трафик попадал в VPN → ICE negotiation зависал. Добавлены в Bypass (DIRECT, no-resolve) 8 IP-диапазонов инфраструктуры Яндекса: `77.88.0.0/18`, `5.255.192.0/18`, `37.9.64.0/18`, `213.180.192.0/19`, `87.250.224.0/19`, `93.158.128.0/17`, `141.8.128.0/18`, `178.154.128.0/18`. **[REVERTED]** — 8 широких Яндекс-CIDR удалены из Bypass: предположительно перекрывали трафик Ozon и других российских сервисов, что приводило к зависаниям; домены Яндекса (yastatic.net, telemost.yandex.net и др.) остаются в Bypass.
- Телемост desktop-приложение рвало соединение во время звонков: медиа-трафик идёт через QUIC (UDP 443) на Akamai `172.224.171.0/24`, Mihomo не проксирует QUIC через TPROXY. Добавлены в Bypass (DIRECT): домены `voicemaster.yandex.net`, `janus.yandex.net` (TURN/Janus медиа-серверы) и IP-CIDR `172.224.171.0/24` (Akamai media CDN).
- Телемост (telemost.yandex.ru) не подключался к встречам: `yastatic.net` (Яндекс CDN) попадал в группу AI (узел NY), из-за чего TURN-серверы Яндекса видели нероссийский IP и блокировали WebRTC-рукопожатие. Домен убран из группы AI и добавлен в группу Bypass (DIRECT) вместе с `strm.yandex.net` и `telemost.yandex.net`.

### Добавлено
- RU CDN fast-path расширен: 29 дополнительных доменов добавлены в группу Bypass для прямого DIRECT-матчинга без медленного GEOSITE lookup. Добавлены: Wildberries CDN (`wbbasket.ru`, `wbstatic.net`), Ozon short (`o3.ru`), Mail.ru CDN (`mradx.net`, `imgsmail.ru`, `mycdn.me`, `my.com`), VK CDN (`userapi.com`, `vk-cdn.net`, `vkuser.net`, `vkuseraudio.net`, `vkuservideo.net`), Одноклассники (`ok.ru`, `odnoklassniki.ru`), банки (`tinkoff.com`, `cdn-tinkoff.ru`, `alfabank.ru`, `raiffeisen.ru`, `gazprombank.ru`, `sbermegamarket.ru`, `sberbank-online.ru`), `yadi.sk`, `ngenix.net`, операторы (`megafon.ru`, `beeline.ru`, `mts.ru`, `tele2.ru`), `2gis.ru`, `2gis.com`. Правила на строках ~301-330, до `GEOSITE,ru-available-only-inside` (строка ~386).
- RU sites DIRECT fast-path: 24 популярных российских домена и IP-CIDR `185.73.192.0/22` (Ozon) добавлены в группу Bypass с явными `DOMAIN-SUFFIX` правилами. Матчинг происходит на строках ~286-310 конфига — до `GEOSITE,ru-available-only-inside` (строка 365). Устраняет задержку при открытии ozon.ru, wildberries.ru, sberbank.ru, vk.com, avito.ru, gosuslugi.ru и других.
- Авто-установка обновлений: кнопка в шапке сайдбара включает/выключает автоматический OTA-апдейт; состояние сохраняется в `/etc/gsg/settings.json`, проверка каждые 6 часов

## [1.7.5] — 2026-04-15

### Добавлено
- App-to-node routing visualization: динамические бейджи в колонке ПРИЛОЖЕНИЯ — показывают через какой узел идёт каждое активное приложение устройства
- Бэкап и восстановление конфигурации: `GET /api/backup` (ZIP-архив с rules.json, rulesets.json, devices.json, subscription.json) и `POST /api/restore`; кнопки в шапке сайдбара
- Иконки доменов в разделе трафика берутся с реального сайта (Google S2 Favicon API)

### Исправлено
- Кэш вендоров MAC: пустые результаты больше не кешируются — устройства без вендора (Samsung и другие) снова определяются при следующем запросе
- Выравнивание колонки ПРИЛОЖЕНИЯ: `minmax(0, fr)` в grid-template-columns гарантирует одинаковую ширину колонок во всех строках таблицы
- Steam/игровой UDP (27000–28000) выводится из tproxy — предотвращает conntrack flood от игровых клиентов

### Изменено
- Колонка ПРИЛОЖЕНИЯ расширена за счёт сужения МАРШРУТ для лучшего отображения чипов
- DNS-сервер в dnsmasq переведён на 127.0.0.1#1053 (локальный resolver Mihomo)
- Таймаут загрузки подписки VPN сокращён с 15 до 5 секунд

## [1.7.4] — 2026-04-09

### Добавлено
- Connection watchdog — каждые 2 минуты проверяет таблицу Mihomo и убивает соединения устройств, превысивших порог 500 (защита от зависших VPN-клиентов)
- cerebras.ai добавлен в AI группу по умолчанию
- pg_type_override: типы proxy-groups из rules.json перезаписывают типы из подписки

### Исправлено
- Auto группа переведена на тип fallback — при падении узла Mihomo автоматически переключается на следующий живой
- Краш tunnel-контейнера каждые 2-3 минуты: UnboundLocalError в generate_config.py (pg_type_override обращался к proxy_groups до его определения), вызывавший перезапуск контейнера и пропадание интернета
- WebRTC/Телемост: L3 nftables bypass портов STUN/RTP/APNs + L7 DST-PORT DIRECT в Mihomo

## [1.7.3] — 2026-04-09

### Добавлено
- Авто-удаление устройств без активности более 30 дней (guest eviction)
- Трекинг last_seen для каждого устройства — обновляется при каждом появлении в сети
- cerebras.ai добавлен в AI группу по умолчанию

### Исправлено
- WebRTC bypass: net-enforcer пропускает UDP 3478-3497, 16384-16387, TCP/UDP 5223 напрямую — Телемост и FaceTime работают в любом режиме
- generate_config.py: те же порты прописаны первыми правилами DST-PORT DIRECT в Mihomo (L7)
- Откат rkn-domains на community список (полный antifilter 1.3M доменов убивал 1GB RAM)

### Удалено
- Кнопка "Закрепить IP" (pin) и три соответствующих endpoint-а — IP резервируется автоматически при появлении устройства
- Индикатор несовпадения зарезервированного и текущего IP (всегда совпадают)

## [1.7.2] — 2026-04-09

### Исправлено
- Crash loop Mihomo при недоступной подписке: при каждом успешном fetch теперь сохраняется полный список VLESS-конфигов в `/etc/gsg/proxies_backup.yaml` (персистентный volume). При недоступной подписке конфиги восстанавливаются из backup вместо config.yaml, который содержал только запись GSG-FALLBACK и приводил к краш-лупу из-за несуществующих нод в proxy-groups.

## [1.7.1] — 2026-04-09

### Добавлено
- Привязка правил устройств к MAC-адресу (DHCP-резервация)

### Изменено
- Детальное описание OTA процесса в CLAUDE.md

## [1.7.0] — 2026-04-09

### Добавлено
- App myip — сервисы определения внешнего IP
- App-бейджи в группах маршрутов
- Hint в попапе app-chip для утилитарных приложений

### Исправлено
- myip — расширен список сервисов определения IP, исправлен маршрут
- Устранение утечки памяти и ошибки initAiSettings

### Изменено
- Оптимизация Mihomo — keepalive, performance, UI улучшения

## [1.6.3] — 2026-04-07

### Добавлено
- ru_direct через runetfreedom geosite (1449 звёзд, обновление каждые 6 часов)
- Tooltip-система + пауза polling при скрытой вкладке
- Стресс-тесты OTA — все 31 теста проходят (30 pass + 1 skip)
- Документация стресс-тестов OTA

### Исправлено
- Утечки памяти браузера — ограничения буферов, очистка состояния, оптимизация DOM

## [1.3.0] — 2026-04-01

### Добавлено
- GSG Fleet: heartbeat при чтении/обновлении подписки — версия, клиенты, CPU, RAM, Mihomo, трафик
- Telegram `/gsg_stats` — сводка флота (версии, клиенты, проблемные устройства)
- Telegram `/gsg_device <id>` — полная диагностика конкретного устройства
- Реферальный trial: landing `globalshield.ru/ref/` с QR-кодом, таймером, инструкцией
- Trial backend: `POST /v1/trial/create`, таблица trial_keys, rate limit, дедупликация
- Конверсия trial при оплате: PATCH существующего аккаунта (subscription URL сохраняется)
- Универсальная реферальная ссылка `globalshield.ru/ref/ref_{id}` вместо t.me/bot
- Уведомление реферу в Telegram при активации trial
- Trial-статистика в боте, callbacks и Mini App
- Защита от абьюза: проверка существующего пользователя + повторного trial
- Скрипт uninstall.sh — деинсталляция GSG
- Telegram-пост при релизе с черновиком и редактором

### Исправлено
- install.sh — определение netplan renderer, удаление dhcp-restore
- Uptime: от boot_time (не от старта контейнера)
- intel.py — исправлен путь к competitor_intel модулю

## [1.2.1] — 2026-03-31

### Исправлено
- Мобильная адаптация: sidebar не приглушается backdrop (fix z-index stacking)
- Горизонтальный скролл на мобильных устройствах
- Поиск устройств по static_ip и vendor, работа на скрытых табах
- Редактирование имени и IP не сбрасывается при автообновлении
- Легенда: цвета согласованы с режимами (Smart/Global/Bypass/Block)
- Sparkline активности: двусторонний (входящий вверх, исходящий вниз)

### Добавлено
- DHCP force renew: автоматическое обновление IP после назначения static IP
- Отображение обоих IP (static + текущий) в таблице устройств

---

## [1.2.0] — 2026-03-30

### Добавлено
- Domain pills в визуализации маршрутов — топ-5 доменов по каждой ветке (Auto/NY/Direct)
- AI-группы в правилах маршрутизации: парсинг доменов и ключевых слов
- Миграция конфига устройств по MAC-адресу при смене IP (static IP, DHCP renew)
- Telegram CIDR правила применяются всегда (не только при rkn_bypass)

### Изменено
- Визуализация маршрутов: линии выровнены относительно GSG, убраны Hulu и X
- Сортировка: устройства в маршрутах по имени, в таблице — онлайн первыми
- Sniffing: `override-destination: False` для TLS/QUIC (сохранение оригинального IP)
- DHCP: `quiet-dhcp` вместо `log-dhcp`

### Исправлено
- Потеря настроек устройства (mode, custom_name, static_ip) при смене IP
- Глобальные правила перетирали пользовательские AI-правила и наоборот
- Визуальный бейдж режима устройства, cron-очистка Mihomo, перегрев CPU

---

## [1.1.0] — 2026-03-27

### Добавлено
- Geo-группы в Mihomo: правила ссылаются на `gsg-us`, `gsg-<keyword>` вместо конкретных узлов — автоматический failover, устойчивость к переименованию узлов
- Визуализация трафика: точки генерируются по спайкам (1 точка = 1 всплеск на графике), не непрерывно
- Скрипт `speedtest.sh` для тестирования визуализации через реальный трафик
- Onboarding-модал для новых пользователей (объяснение схемы маршрутизации)
- Тултипы: режимы устройства, карточка "Авто", заголовок "Активные соединения" (QUIC-пояснение)
- Бейдж "Шлюз поднимается…" с обратным отсчётом после подтверждённого даунтайма

### Исправлено
- Атрибуция трафика по узлам: `chains[0]` вместо `reversed(chains)` — NY-трафик правильно маркируется
- Отображение маршрутов: только активные соединения (speed > 0), не исторические
- Размер точек в визуализации: стабильный `globalSz` вместо меняющегося `branchSz`
- Таймер прогрева шлюза не срабатывает при первой загрузке страницы

---

## [1.0.0] — 2026-02-01

Первый рабочий релиз GSG Smart Gateway.
