"""Server-side organisation suggestions without exposing the provider token."""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import settings


class CompanySuggestionsUnavailable(RuntimeError):
    """The external suggestions service cannot answer right now."""


class CompanySuggestionsRateLimited(RuntimeError):
    """One client sent too many suggestion requests."""


_lock = threading.Lock()
_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
_client_requests: dict[str, deque[float]] = defaultdict(deque)
_MAX_REQUESTS_PER_MINUTE = 30
_MAX_RESPONSE_BYTES = 256_000


def configured() -> bool:
    return bool(settings.dadata_api_key)


def _check_rate_limit(client_key: str) -> None:
    now = time.monotonic()
    key = str(client_key or "unknown")[:100]
    with _lock:
        requests = _client_requests[key]
        while requests and now - requests[0] >= 60:
            requests.popleft()
        if len(requests) >= _MAX_REQUESTS_PER_MINUTE:
            raise CompanySuggestionsRateLimited("Слишком много запросов. Попробуйте немного позже")
        requests.append(now)


def _cached(query: str) -> list[dict[str, str]] | None:
    with _lock:
        cached = _cache.get(query)
        if not cached:
            return None
        created_at, suggestions = cached
        if time.monotonic() - created_at > settings.dadata_suggestions_cache_seconds:
            _cache.pop(query, None)
            return None
        return [dict(item) for item in suggestions]


def suggest_companies(query: str, client_key: str = "") -> list[dict[str, str]]:
    digits = "".join(character for character in str(query) if character.isdigit())
    if len(digits) < 4:
        return []
    if len(digits) > 12 or digits != str(query).strip():
        raise ValueError("Введите от 4 до 12 цифр ИНН")
    if not configured():
        return []

    cached = _cached(digits)
    if cached is not None:
        return cached
    _check_rate_limit(client_key)

    body = json.dumps({"query": digits, "count": 7, "status": ["ACTIVE"]}).encode("utf-8")
    request = Request(
        settings.dadata_suggestions_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Token {settings.dadata_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=settings.dadata_timeout_seconds) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise CompanySuggestionsUnavailable("Сервис подсказок вернул слишком большой ответ")
        payload = json.loads(raw.decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompanySuggestionsUnavailable("Подсказки организаций временно недоступны") from exc

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for suggestion in payload.get("suggestions", []):
        if not isinstance(suggestion, dict):
            continue
        data = suggestion.get("data") if isinstance(suggestion.get("data"), dict) else {}
        inn = str(data.get("inn", "")).strip()
        name_data = data.get("name") if isinstance(data.get("name"), dict) else {}
        name = str(suggestion.get("value") or name_data.get("short_with_opf", "")).strip()
        if not inn.startswith(digits) or inn in seen or not name:
            continue
        seen.add(inn)
        result.append({"inn": inn[:12], "name": name[:300]})
        if len(result) >= 7:
            break

    with _lock:
        _cache[digits] = (time.monotonic(), [dict(item) for item in result])
    return result
