"""
Конфигурация pytest для GSG stress-тестов.

Маркеры:
    smoke  — безопасные тесты (нет деструктивных действий), можно запускать всегда
    live   — тесты симуляции отказов (деструктивные, только на dev-устройстве)
    slow   — долгие тесты (полный update+rollback цикл, 5-10 минут)
"""
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--live", action="store_true", default=False,
        help="Запустить деструктивные live-тесты (симуляция отказов)"
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "smoke: безопасные smoke-тесты")
    config.addinivalue_line("markers", "live: деструктивные тесты на live-устройстве")
    config.addinivalue_line("markers", "slow: медленные сквозные тесты (5-10 мин)")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--live"):
        skip_live = pytest.mark.skip(reason="Нужен флаг --live для деструктивных тестов")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)
