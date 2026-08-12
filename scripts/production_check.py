"""Проверка конфигурации и файлов перед запуском Консилиума на сервере."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from backend import database as db  # noqa: E402
from backend.config import settings  # noqa: E402


def check_writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".consilium-write-check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка готовности Консилиума")
    parser.add_argument(
        "--allow-development",
        action="store_true",
        help="не считать локальные настройки блокирующей ошибкой",
    )
    args = parser.parse_args()

    errors: list[str] = []
    warnings: list[str] = []
    passed: list[str] = []

    if sys.version_info >= (3, 11):
        passed.append(f"Python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        errors.append("Нужен Python 3.11 или новее")

    required_files = [
        PROJECT_DIR / "index.html",
        PROJECT_DIR / "dashboard.html",
        PROJECT_DIR / "manager.html",
        PROJECT_DIR / "manifest.webmanifest",
        PROJECT_DIR / "service-worker.js",
        PROJECT_DIR / "static" / "app.js",
        PROJECT_DIR / "static" / "metrika.js",
        PROJECT_DIR / "static" / "styles.css",
        PROJECT_DIR / "static" / "dashboard.js",
        PROJECT_DIR / "static" / "dashboard.css",
        PROJECT_DIR / "static" / "manager.js",
        PROJECT_DIR / "static" / "manager.css",
        PROJECT_DIR / "static" / "icon-192.png",
        PROJECT_DIR / "static" / "icon-512.png",
        PROJECT_DIR / "static" / "icon-maskable-512.png",
        PROJECT_DIR / "static" / "apple-touch-icon.png",
        PROJECT_DIR / "backend" / "main.py",
    ]
    missing = [str(path.relative_to(PROJECT_DIR)) for path in required_files if not path.is_file()]
    if missing:
        errors.append("Не найдены обязательные файлы: " + ", ".join(missing))
    else:
        passed.append("Файлы приложения на месте")

    key = settings.openai_api_key.strip()
    if not key or key in {"sk-your-key-here", "replace-me", "changeme"}:
        errors.append("OPENAI_API_KEY не задан или оставлен шаблонным")
    else:
        passed.append("OPENAI_API_KEY задан (значение скрыто)")

    if not settings.orchestrator_model or not settings.specialist_model:
        errors.append("Не заданы модели оркестратора и специалистов")
    else:
        passed.append("Модели AI заданы")

    production = settings.app_env == "production"
    if production:
        passed.append("APP_ENV=production")
    elif args.allow_development:
        warnings.append(f"APP_ENV={settings.app_env}; допустимо только для локальной проверки")
    else:
        errors.append("На сервере требуется APP_ENV=production")

    running_in_docker = os.getenv("RUNNING_IN_DOCKER", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if settings.host in {"127.0.0.1", "::1", "localhost"}:
        passed.append("Python слушает только локальный интерфейс")
    elif running_in_docker and settings.host == "0.0.0.0":
        passed.append("Python слушает интерфейс контейнера; порт хоста ограничивается Docker")
    elif args.allow_development:
        warnings.append(f"HOST={settings.host}; для сервера за Nginx рекомендуется 127.0.0.1")
    else:
        errors.append(
            "HOST должен быть 127.0.0.1 либо 0.0.0.0 внутри Docker: внешний доступ принимает Nginx"
        )

    if production and settings.auto_open_browser:
        errors.append("На сервере требуется AUTO_OPEN_BROWSER=0")
    else:
        passed.append("Автооткрытие браузера не мешает серверному запуску")

    if production and not settings.cookie_secure:
        errors.append("На HTTPS-сервере требуется COOKIE_SECURE=1")
    else:
        passed.append("Режим Secure для cookie настроен")

    if production and not settings.public_base_url.startswith("https://"):
        errors.append("PUBLIC_BASE_URL в production должен начинаться с https://")
    else:
        passed.append(f"Публичный адрес задан: {settings.public_base_url}")

    metrika_id = settings.yandex_metrika_counter_id
    if metrika_id and not (metrika_id.isdigit() and 5 <= len(metrika_id) <= 12):
        errors.append("YANDEX_METRIKA_COUNTER_ID должен содержать от 5 до 12 цифр")
    elif metrika_id:
        passed.append("Яндекс Метрика настроена (номер счётчика скрыт)")
    else:
        warnings.append("Яндекс Метрика отключена: YANDEX_METRIKA_COUNTER_ID не задан")

    integration_secret = settings.bot_integration_secret.strip()
    if production and (
        len(integration_secret) < 32
        or integration_secret == "replace-with-a-long-random-secret"
    ):
        errors.append("BOT_INTEGRATION_SECRET должен быть случайной строкой длиной не менее 32 символов")
    elif integration_secret:
        passed.append("Секрет интеграции с мессенджерами задан (значение скрыто)")
    else:
        warnings.append("Интеграция с мессенджерами отключена: BOT_INTEGRATION_SECRET не задан")

    dashboard_token = settings.admin_dashboard_token.strip()
    if production and (
        len(dashboard_token) < 32
        or dashboard_token == "replace-with-another-long-random-secret"
    ):
        errors.append(
            "ADMIN_DASHBOARD_TOKEN должен быть отдельной случайной строкой "
            "длиной не менее 32 символов"
        )
    elif dashboard_token:
        passed.append("Токен админ-панели задан (значение скрыто)")
    else:
        warnings.append("Админ-панель отключена: ADMIN_DASHBOARD_TOKEN не задан")

    if not 300 <= settings.auth_link_ttl_seconds <= 2_592_000:
        errors.append("AUTH_LINK_TTL_SECONDS должен быть от 300 секунд до 30 дней")
    else:
        passed.append("Срок одноразовой ссылки настроен")
    if not 300 <= settings.auth_intent_ttl_seconds <= 2_592_000:
        errors.append("AUTH_INTENT_TTL_SECONDS должен быть от 300 секунд до 30 дней")
    else:
        passed.append("Срок запроса привязки мессенджера настроен")

    for provider, template in (
        ("Telegram", settings.telegram_bot_auth_url),
        ("MAX", settings.max_bot_auth_url),
    ):
        if not template:
            warnings.append(f"Бот {provider} пока не подключён")
            continue
        parsed = urlparse(template)
        if "{token}" not in template or parsed.scheme not in {"http", "https"}:
            errors.append(f"Ссылка на бота {provider} должна быть http(s)-адресом с {{token}}")
        elif production and parsed.scheme != "https":
            errors.append(f"Ссылка на бота {provider} в production должна использовать https://")
        else:
            passed.append(f"Ссылка на бота {provider} настроена")
    if not 1 <= settings.session_ttl_days <= 365:
        errors.append("SESSION_TTL_DAYS должен быть от 1 до 365")
    else:
        passed.append("Срок пользовательской сессии настроен")

    if settings.lab_results_enabled:
        credentials = Path(settings.after_tests_google_credentials)
        if not credentials.is_absolute():
            errors.append("AFTER_TESTS_GOOGLE_CREDENTIALS должен быть абсолютным путём внутри контейнера")
        elif not credentials.is_file():
            errors.append(f"Не найден ключ after_tests_db: {credentials}")
        else:
            passed.append("Ключ after_tests_db подключён")
        try:
            import gspread  # noqa: F401
            passed.append("Клиент Google Sheets установлен")
        except ImportError:
            errors.append("Не установлен gspread: пересоберите Docker-образ")
        if not settings.after_tests_spreadsheet or not settings.after_tests_worksheet:
            errors.append("Не заданы AFTER_TESTS_SPREADSHEET и AFTER_TESTS_WORKSHEET")
        else:
            passed.append("Таблица и лист результатов настроены")
        if not 5 <= settings.google_sheets_timeout_seconds <= 60:
            errors.append("GOOGLE_SHEETS_TIMEOUT_SECONDS должен быть от 5 до 60")
        if not 0 <= settings.lab_results_cache_seconds <= 3600:
            errors.append("LAB_RESULTS_CACHE_SECONDS должен быть от 0 до 3600")
    else:
        warnings.append("Получение результатов отключено: LAB_RESULTS_ENABLED=0")

    if production and not settings.database_path.is_absolute():
        errors.append("DATABASE_PATH в production должен быть абсолютным")
    if not check_writable_directory(settings.database_path.parent):
        errors.append(f"Нет права записи в каталог базы: {settings.database_path.parent}")
    else:
        passed.append(f"Каталог базы доступен: {settings.database_path.parent}")

    if production and not settings.log_path.is_absolute():
        errors.append("LOG_PATH в production должен быть абсолютным")
    if not check_writable_directory(settings.log_path.parent):
        errors.append(f"Нет права записи в каталог журналов: {settings.log_path.parent}")
    else:
        passed.append(f"Каталог журналов доступен: {settings.log_path.parent}")

    try:
        db.init_db()
        with db.connection() as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            errors.append(f"SQLite quick_check: {result}")
        else:
            passed.append("База SQLite и схема готовы")
    except (OSError, sqlite3.Error) as exc:
        errors.append(f"База данных не готова: {exc}")

    print("ПРОВЕРКА ГОТОВНОСТИ КОНСИЛИУМА")
    for item in passed:
        print(f"[OK] {item}")
    for item in warnings:
        print(f"[ПРЕДУПРЕЖДЕНИЕ] {item}")
    for item in errors:
        print(f"[ОШИБКА] {item}")
    print(f"Итог: {len(passed)} успешно, {len(warnings)} предупреждений, {len(errors)} ошибок")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
