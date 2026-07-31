"""Cross-project preflight for Consilium and its Telegram/MAX login bots."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from urllib.parse import urlparse


PLACEHOLDERS = {"", "changeme", "replace-me"}


def dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def placeholder(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in PLACEHOLDERS or normalized.startswith("replace-with-")


def main() -> int:
    project_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Проверка трёх проектов перед публикацией")
    parser.add_argument("--consilium-dir", type=Path, default=project_dir)
    parser.add_argument(
        "--telegram-dir", type=Path,
        default=project_dir.parent / "tg_to_consillium",
    )
    parser.add_argument(
        "--max-dir", type=Path,
        default=project_dir.parent / "max_to_consilium",
    )
    parser.add_argument(
        "--check-env", action="store_true",
        help="проверить рабочие .env как production-конфигурацию",
    )
    args = parser.parse_args()

    projects = {
        "Консилиум": args.consilium_dir.resolve(),
        "Telegram-бот": args.telegram_dir.resolve(),
        "MAX-бот": args.max_dir.resolve(),
    }
    errors: list[str] = []
    passed: list[str] = []

    compose_texts: dict[str, str] = {}
    container_names: dict[str, str] = {}
    expected_container_names = {
        "Консилиум": "consilium",
        "Telegram-бот": "consilium-telegram-bot",
        "MAX-бот": "consilium-max-bot",
    }
    for label, directory in projects.items():
        for filename in ("Dockerfile", "docker-compose.yml", ".dockerignore", ".gitignore", "requirements.txt"):
            if not (directory / filename).is_file():
                errors.append(f"{label}: отсутствует {filename}")
        compose_path = directory / "docker-compose.yml"
        compose = compose_path.read_text(encoding="utf-8") if compose_path.is_file() else ""
        compose_texts[label] = compose
        match = re.search(r"(?m)^\s*container_name:\s*([^\s#]+)", compose)
        if not match:
            errors.append(f"{label}: не задано уникальное container_name")
        else:
            container_names[label] = match.group(1)
            if match.group(1) != expected_container_names[label]:
                errors.append(
                    f"{label}: ожидается container_name {expected_container_names[label]}"
                )
        if "external: true" not in compose or "name: consilium-internal" not in compose:
            errors.append(f"{label}: сеть consilium-internal должна быть внешней")
        for required in ("restart: unless-stopped", "read_only: true", "no-new-privileges:true"):
            if required not in compose:
                errors.append(f"{label}: в Compose отсутствует защитная настройка {required}")
        dockerfile_path = directory / "Dockerfile"
        dockerfile = dockerfile_path.read_text(encoding="utf-8") if dockerfile_path.is_file() else ""
        if not re.search(r"(?m)^USER\s+(?!root\b)\S+", dockerfile):
            errors.append(f"{label}: контейнер должен запускаться не от root")
        if label != "Консилиум" and "HEALTHCHECK" not in dockerfile:
            errors.append(f"{label}: в Dockerfile отсутствует HEALTHCHECK")
        ignore = (directory / ".gitignore").read_text(encoding="utf-8") if (directory / ".gitignore").is_file() else ""
        if not re.search(r"(?m)^\.env/?$", ignore):
            errors.append(f"{label}: .env не защищён правилом .gitignore")

    if len(set(container_names.values())) != len(container_names):
        errors.append("Имена контейнеров трёх проектов должны быть уникальными")
    elif len(container_names) == 3:
        passed.append("Имена контейнеров уникальны")
    if not errors:
        passed.append("Docker-контейнеры ограничены и запускаются не от root")

    consilium_compose = compose_texts.get("Консилиум", "")
    if "127.0.0.1:${CONSILIUM_HOST_PORT:-8002}:8000" not in consilium_compose:
        errors.append("Консилиум должен публиковаться только на 127.0.0.1:8002")
    else:
        passed.append("Порт Консилиума изолирован на 127.0.0.1:8002")
    for label in ("Telegram-бот", "MAX-бот"):
        if re.search(r"(?m)^\s*ports:\s*$", compose_texts.get(label, "")):
            errors.append(f"{label}: бот не должен публиковать порты хоста")
        else:
            passed.append(f"{label}: внешние порты не публикуются")

    expected_examples = {
        "Консилиум": ".env.docker.example",
        "Telegram-бот": ".env.production.example",
        "MAX-бот": ".env.production.example",
    }
    for label, filename in expected_examples.items():
        if not (projects[label] / filename).is_file():
            errors.append(f"{label}: отсутствует {filename}")

    if args.check_env:
        envs = {label: dotenv(directory / ".env") for label, directory in projects.items()}
        for label, values in envs.items():
            if not values:
                errors.append(f"{label}: рабочий .env не найден или пуст")
        consilium = envs["Консилиум"]
        telegram = envs["Telegram-бот"]
        max_bot = envs["MAX-бот"]
        secret = consilium.get("BOT_INTEGRATION_SECRET", "")
        if placeholder(secret) or len(secret) < 32:
            errors.append("Консилиум: BOT_INTEGRATION_SECRET должен содержать минимум 32 случайных символа")
        elif telegram.get("BOT_INTEGRATION_SECRET") != secret or max_bot.get("BOT_INTEGRATION_SECRET") != secret:
            errors.append("BOT_INTEGRATION_SECRET не совпадает во всех трёх проектах")
        else:
            passed.append("Общий секрет интеграции совпадает во всех проектах")
        for label, values, token_name in (
            ("Консилиум", consilium, "OPENAI_API_KEY"),
            ("Telegram-бот", telegram, "TELEGRAM_BOT_TOKEN"),
            ("MAX-бот", max_bot, "MAX_BOT_TOKEN"),
        ):
            if placeholder(values.get(token_name, "")):
                errors.append(f"{label}: не задан {token_name}")
            else:
                passed.append(f"{label}: {token_name} задан (значение скрыто)")
        if consilium.get("APP_ENV") != "production":
            errors.append("Консилиум: требуется APP_ENV=production")
        if consilium.get("PUBLIC_BASE_URL", "").startswith("https://"):
            passed.append("Публичный адрес Консилиума использует HTTPS")
        else:
            errors.append("Консилиум: PUBLIC_BASE_URL должен начинаться с https://")
        if consilium.get("COOKIE_SECURE") not in {"1", "true", "yes", "on"}:
            errors.append("Консилиум: для HTTPS требуется COOKIE_SECURE=1")
        for label, values in (("Telegram-бот", telegram), ("MAX-бот", max_bot)):
            if values.get("CONSILIUM_API_URL") != "http://consilium:8000":
                errors.append(f"{label}: в Docker требуется CONSILIUM_API_URL=http://consilium:8000")
            else:
                passed.append(f"{label}: используется внутренний адрес Консилиума")
        for provider, key in (("Telegram", "TELEGRAM_BOT_AUTH_URL"), ("MAX", "MAX_BOT_AUTH_URL")):
            template = consilium.get(key, "")
            parsed = urlparse(template)
            if parsed.scheme != "https" or "{token}" not in template:
                errors.append(f"Консилиум: {key} должен быть HTTPS-ссылкой с {{token}}")
            else:
                passed.append(f"Deep-link {provider} настроен")

    print("ПРОВЕРКА КОМПЛЕКТА ИЗ ТРЁХ ПРОЕКТОВ")
    for item in passed:
        print(f"[OK] {item}")
    for item in errors:
        print(f"[ОШИБКА] {item}")
    print(f"Итог: {len(passed)} успешно, {len(errors)} ошибок")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
