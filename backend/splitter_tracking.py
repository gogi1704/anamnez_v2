"""Reliable, privacy-safe delivery confirmations for the external splitter."""

from __future__ import annotations

import json
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import settings


SERVER_STAGE = "server_reached"
CLIENT_STAGES = {"javascript_started", "welcome_shown"}
ALL_STAGES = {SERVER_STAGE, *CLIENT_STAGES}
_ASYNC_SLOTS = threading.BoundedSemaphore(4)


def _uuid(value: object) -> str:
    try:
        return str(uuid.UUID(str(value or "")))
    except (ValueError, TypeError, AttributeError):
        return ""


def tracking_from_query(query: dict[str, list[str]]) -> dict[str, str] | None:
    return tracking_from_payload({
        "attemptId": query.get("splitter_attempt", [""])[0],
        "visitorId": query.get("splitter_id", [""])[0],
    })


def tracking_from_payload(payload: dict) -> dict[str, str] | None:
    attempt_id = _uuid(payload.get("attemptId"))
    visitor_id = _uuid(payload.get("visitorId"))
    if not attempt_id or not visitor_id:
        return None
    return {"attemptId": attempt_id, "visitorId": visitor_id}


def configured() -> bool:
    return bool(settings.splitter_event_url and settings.splitter_event_secret)


def notify(tracking: dict[str, str], event: str) -> str:
    """Send one idempotent stage. Returns sent, disabled, or failed."""
    if event not in ALL_STAGES or not tracking_from_payload(tracking):
        return "failed"
    if not configured():
        return "disabled"
    body = json.dumps({**tracking, "event": event}).encode("utf-8")
    request = Request(
        settings.splitter_event_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Splitter-Secret": settings.splitter_event_secret,
        },
    )
    try:
        with urlopen(request, timeout=settings.splitter_event_timeout_seconds) as response:
            return "sent" if 200 <= int(response.status) < 300 else "failed"
    except (HTTPError, URLError, OSError, TimeoutError, ValueError):
        return "failed"


def notify_async(tracking: dict[str, str] | None, event: str) -> None:
    """Retry server arrival without delaying or breaking the HTML response."""
    if not tracking or not configured() or event not in ALL_STAGES:
        return
    if not _ASYNC_SLOTS.acquire(blocking=False):
        return

    def worker() -> None:
        try:
            for delay in (0.0, 0.25, 1.0):
                if delay:
                    time.sleep(delay)
                if notify(tracking, event) == "sent":
                    return
        finally:
            _ASYNC_SLOTS.release()

    threading.Thread(target=worker, name="splitter-delivery", daemon=True).start()
