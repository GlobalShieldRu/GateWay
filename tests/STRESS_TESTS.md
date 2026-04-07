# GSG OTA Update Stress Tests

Стресс-тесты проверяют отказоустойчивость механизма OTA-обновлений (`update-watcher.sh` + `gsg-updater.service`).

## Запуск

```bash
cd GSG
pip install pytest requests

# Только smoke-тесты (безопасны, можно на prod):
pytest tests/test_update_stress.py -m smoke -v

# Все live-тесты (деструктивны — только на dev-устройстве!):
pytest tests/test_update_stress.py --live -v

# Конкретное устройство:
GSG_HOST=192.168.2.254 pytest tests/test_update_stress.py --live -v
```

## Переменные окружения

| Переменная | По умолчанию | Описание |
|------------|-------------|----------|
| `GSG_HOST` | `192.168.2.254` | IP тестового устройства |
| `GSG_PORT` | `8080` | Порт web-orchestrator |
| `GSG_TOKEN` | *(читается с устройства по SSH)* | Токен авторизации |

## Маркеры

| Маркер | Описание |
|--------|----------|
| `smoke` | Безопасные тесты, не трогают live-процессы. Можно запускать на prod. |
| `live` | Деструктивные тесты симуляции отказов. Только на dev-устройстве. Требуют флага `--live`. |
| `slow` | Медленные сквозные тесты (полный update+rollback цикл, ~5-10 мин). |

---

## Группы тестов

### TestAPISmoke (9 тестов) `@smoke`

Базовые проверки API без побочных эффектов. Безопасны для prod.

| Тест | Что проверяет |
|------|--------------|
| `test_version_endpoint_responds` | `GET /api/version` возвращает поле `version` |
| `test_check_update_responds` | `GET /api/check-update` возвращает `current`, `has_update` |
| `test_update_status_idle_when_no_trigger` | Без триггера статус не `running` |
| `test_rollback_state_responds` | `GET /api/rollback/state` не возвращает 500 |
| `test_version_endpoint_no_auth` | `/api/version` доступен без авторизации (healthcheck endpoint) |
| `test_protected_endpoints_require_auth` | Защищённые endpoints возвращают 401/403 без токена |
| `test_double_update_trigger_returns_error` | Повторный `POST /api/update` при активном триггере → ошибка |
| `test_concurrent_status_polls` | 10 параллельных опросов статуса не вызывают 500 |
| `test_rapid_status_polling` | 50 последовательных запросов: avg < 500ms, p95 < 1s |

---

### TestStateMachine (7 тестов) `@smoke`

Манипуляции с `state.json` через API — проверка корректности чтения/записи состояний.

| Тест | Что проверяет |
|------|--------------|
| `test_corrupted_state_file_api_stable` | Повреждённый `state.json` не роняет `/api/update/status` |
| `test_missing_state_file_rollback_unavailable` | Без `state.json` откат недоступен |
| `test_empty_pre_hash_rollback_unavailable` | Пустой `pre_update.git_hash` → `can_rollback=false` |
| `test_rollback_available_after_healthy_update` | `status=healthy` + валидный hash → `can_rollback=true` |
| `test_rollback_unavailable_during_pending` | `status=pending` → `can_rollback=false` |
| `test_rollback_api_400_no_state` | `POST /api/rollback` без `state.json` → 400 |
| `test_double_rollback_trigger_rejected` | Повторный rollback при активном триггере → 409 |

---

### TestStage1GitFetchFail (1 тест) `@live`

Симуляция падения этапа 1 (git fetch).

**Метод:** подмена `remote origin` на несуществующий хост (`BLOCKED.invalid`).

| Тест | Ожидаемое поведение |
|------|---------------------|
| `test_git_fetch_failure_no_rollback` | Обновление прерывается. Откат **НЕ** запускается. Триггер удаляется. `status=failed_fetch`. Git hash не меняется. |

---

### TestStage3DockerBuildFail (1 тест) `@live`

Симуляция падения этапа 3 (docker compose build).

**Метод:** создаётся `Dockerfile.STRESS_TEST` с `RUN false` и `docker-compose.override.yml`. Оба файла untracked — `git reset --hard` их не удаляет.

| Тест | Ожидаемое поведение |
|------|---------------------|
| `test_build_failure_status_failed_build` | `status=failed_build`. Откат **НЕ** запускается. Контейнеры остаются на старой версии. |

---

### TestStage6HealthcheckFails (4 теста) `@live`

Симуляция падения healthcheck (этап 6) при недоступности отдельных сервисов.

**Общий сценарий:** обновление доходит до этапа 5 (ожидание запуска), затем убивается нужный сервис → healthcheck падает → автооткат.

| Тест | Метод симуляции | Ожидаемое поведение |
|------|----------------|---------------------|
| `test_web_orchestrator_down_triggers_rollback` | `docker stop gsg-web-orchestrator` | Автооткат, `status=rolled_back` |
| `test_mihomo_down_triggers_rollback` | `docker stop gsg-tunnel` | Автооткат, `status=rolled_back` |
| `test_dhcp_down_triggers_rollback` | `docker stop gsg-dhcp` | Автооткат, `status=rolled_back` |
| `test_no_internet_triggers_rollback` | `iptables DROP connectivitycheck.gstatic.com` | Автооткат, `status=rolled_back` |

---

### TestRollbackEdgeCases (2 теста) `@live`

Граничные случаи механизма отката.

| Тест | Сценарий | Ожидаемое поведение |
|------|---------|---------------------|
| `test_rollback_no_pre_hash_graceful_error` | В `state.json` пустой `pre_update.git_hash`, запускается откат | Лог: "нет hash для отката". Зависания нет. |
| `test_rollback_to_invalid_hash_handled` | В `state.json` несуществующий git hash | Лог: "ОШИБКА: git reset не удался". Триггер удаляется. |

---

### TestConcurrency (3 теста) `@live`

Гонки и параллельные запросы.

| Тест | Сценарий | Ожидаемое поведение |
|------|---------|---------------------|
| `test_concurrent_update_requests_one_wins` | 10 одновременных `POST /api/update` | Ровно 1 принят (`ok=true`), 9 отклонены |
| `test_update_while_rollback_trigger_exists` | `POST /api/update` при активном rollback-триггере | 400/409 или `ok=false` |
| `test_rollback_while_update_trigger_exists` | `POST /api/rollback` при активном update-триггере | 400/409 |

---

### TestWatcherResilience (3 теста) `@live`

Устойчивость демона `gsg-updater.service`.

| Тест | Сценарий | Ожидаемое поведение |
|------|---------|---------------------|
| `test_trigger_processed_after_watcher_restart` | Триггер создан пока watcher остановлен | При старте watcher обрабатывает триггер |
| `test_watcher_auto_restarts_on_crash` | `kill -9 <watcher_pid>` | systemd перезапускает сервис (RestartSec=10) |
| `test_trigger_file_idempotent` | Два trigger-файла подряд | Второй запуск отклоняется API |

---

### TestHappyPath (1 тест) `@live @slow`

Полный сквозной тест: успешное обновление → ручной откат.

| Тест | Сценарий |
|------|---------|
| `test_full_update_and_manual_rollback` | Обновление до `healthy` → `can_rollback=true` → ручной откат → возврат на pre_hash |

> На тестовом устройстве (192.168.2.254) dnsmasq падает с `unknown interface eth0`, вызывая авто-откат. Тест делает `pytest.skip` с пояснением — это не баг системы.

---

## Архитектура тестов

### Инфраструктура

```
SSH                 — subprocess-обёртка для команд на устройстве
API                 — requests-обёртка для HTTP-запросов к web-orchestrator
clean_trigger       — autouse-фикстура: изоляция тестов, ожидание settle до/после
log_baseline        — autouse-фикстура: фиксирует позицию в логе до теста
_wait_for_update_to_settle() — ключевая функция изоляции
```

### `_wait_for_update_to_settle`

Надёжнее `api.wait_for_idle` — читает `state.json` напрямую по SSH вместо парсинга лога API.

Ждёт пока:
1. `/api/version` доступен
2. Trigger-файл отсутствует
3. `state.post_update.status` не в (`pending`, `failed_healthcheck`)
4. В последних 500 строках лога нет незавершённого отката (`===== ОТКАТ` без `Откат завершён`)
5. Если откат только что завершился — ждёт 3 стабильных ответа API (контейнеры поднимаются асинхронно)

### Нетривиальные решения

| Проблема | Решение |
|---------|--------|
| `set -euo pipefail` роняет watcher при RC≠0 от `docker compose build` | `set +e` перед build, `set -e` после. Проверка `PIPESTATUS[0]` |
| BuildKit возвращает RC=0 при ошибке build | Grep вывода на паттерны `failed to solve`, `^ERROR:` |
| `inotifywait` пропускает триггеры созданные во время обработки | Проверка файла в начале каждой итерации главного цикла |
| API статус возвращает "success" по старым записям лога | `_wait_for_update_to_settle` читает state.json, не API |
| `docker compose up -d` запускает контейнеры асинхронно | После детекции завершённого отката — ожидание 3 стабильных ответов API |
