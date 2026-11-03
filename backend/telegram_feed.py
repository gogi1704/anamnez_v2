"""Telegram feed media proxy which never persists media on local disk."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import settings


class TelegramFeedUnavailable(RuntimeError):
    pass


def configured() -> bool:
    return bool(
        settings.telegram_feed_enabled
        and settings.telegram_feed_bot_token
        and (settings.telegram_feed_channel_id or settings.telegram_feed_channel_username)
    )


def _api_url(method: str) -> str:
    token = quote(settings.telegram_feed_bot_token, safe=":")
    return f"{settings.telegram_feed_bot_api_url}/bot{token}/{method}"


def _file_url(file_path: str) -> str:
    clean = str(file_path or "").strip().lstrip("/")
    if not clean or ".." in clean.split("/"):
        raise TelegramFeedUnavailable("Telegram вернул некорректный путь к файлу")
    token = quote(settings.telegram_feed_bot_token, safe=":")
    encoded = "/".join(quote(part, safe="") for part in clean.split("/"))
    return f"{settings.telegram_feed_bot_api_url}/file/bot{token}/{encoded}"


def get_file_path(file_id: str) -> str:
    if not configured():
        raise TelegramFeedUnavailable("Лента Telegram не настроена")
    candidate = str(file_id or "").strip()
    if not candidate or len(candidate) > 512:
        raise TelegramFeedUnavailable("Некорректный идентификатор файла")
    body = json.dumps({"file_id": candidate}).encode("utf-8")
    request = Request(
        _api_url("getFile"), data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urlopen(request, timeout=settings.telegram_feed_timeout_seconds) as response:
            payload = json.loads(response.read(256_000).decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read(32_000).decode("utf-8")).get("description", "")
        except (ValueError, UnicodeDecodeError, AttributeError):
            pass
        if "file is too big" in str(detail).lower():
            raise TelegramFeedUnavailable(
                "Видео слишком большое для облачного Telegram API. "
                "Настройте MTProto-поток для больших файлов."
            ) from exc
        raise TelegramFeedUnavailable(detail or "Telegram не отдал файл") from exc
    except (URLError, TimeoutError, ValueError, UnicodeDecodeError) as exc:
        raise TelegramFeedUnavailable("Telegram временно недоступен") from exc
    if not payload.get("ok") or not isinstance(payload.get("result"), dict):
        raise TelegramFeedUnavailable(str(payload.get("description") or "Telegram не отдал файл"))
    return str(payload["result"].get("file_path") or "")


def open_media(file_id: str, range_header: str = ""):
    """Open Telegram response as a stream. Caller must close it; no disk writes."""
    file_path = get_file_path(file_id)
    headers = {"User-Agent": "Consilium-Telegram-Feed/1.0"}
    if range_header:
        headers["Range"] = str(range_header)[:200]
    try:
        return urlopen(
            Request(_file_url(file_path), headers=headers),
            timeout=settings.telegram_feed_timeout_seconds,
        )
    except HTTPError as exc:
        if exc.code == 416:
            return exc
        raise TelegramFeedUnavailable("Telegram не отдал медиаданные") from exc
    except (URLError, TimeoutError) as exc:
        raise TelegramFeedUnavailable("Telegram временно недоступен") from exc
