"""Daily end-to-end MAX notification delivery check."""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import database as db
from .config import settings


def configured() -> bool:
    return bool(
        settings.notification_health_enabled
        and re.fullmatch(r"\d+", settings.notification_health_max_chat_id)
    )


def _zone(name: str):
    if name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=3)) if name == "Europe/Moscow" else timezone.utc


def queue_daily_check(*, now: datetime | None = None) -> bool:
    zone = _zone(settings.notification_health_timezone or "Europe/Moscow")
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    snapshot = db.notification_delivery_snapshot("max")
    last_sent = str(snapshot.get("last_sent_at") or "")
    try:
        last_sent_label = datetime.fromisoformat(last_sent).astimezone(zone).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        last_sent_label = "ещё не было"
    body = (
        "Контрольное сообщение прошло через планировщик Консилиума, очередь доставки "
        "и MAX-бота.\n\n"
        f"Ожидают отправки: {snapshot['pending']}.\n"
        f"Сейчас доставляются: {snapshot['delivering']}.\n"
        f"Последняя успешная отправка до этой проверки: {last_sent_label}."
    )
    expiry = local_now + timedelta(
        hours=max(1, int(settings.notification_health_expiry_hours)),
    )
    return db.queue_system_notification(
        "max",
        settings.notification_health_max_chat_id,
        "notification_health",
        {"title": "✅ Уведомления Консилиума работают", "body": body},
        f"notification-health:{local_now.date().isoformat()}",
        expires_at=expiry.astimezone(timezone.utc).isoformat(),
    )


def start_background_scheduler(log) -> threading.Event:
    stop = threading.Event()

    def worker() -> None:
        last_queued_date = ""
        while not stop.is_set():
            if configured():
                local_now = datetime.now(_zone(settings.notification_health_timezone))
                today = local_now.date().isoformat()
                if (
                    local_now.strftime("%H:%M") >= settings.notification_health_send_time
                    and today != last_queued_date
                ):
                    try:
                        queued = queue_daily_check(now=local_now)
                    except Exception as exc:
                        log(f"Контроль уведомлений не поставлен в очередь: {exc}")
                        stop.wait(300)
                    else:
                        last_queued_date = today
                        log(
                            "Ежедневный контроль уведомлений поставлен в очередь"
                            if queued else
                            "Ежедневный контроль уведомлений уже был поставлен в очередь"
                        )
            stop.wait(max(10, int(settings.notification_health_poll_seconds)))

    threading.Thread(target=worker, name="notification-health", daemon=True).start()
    return stop
