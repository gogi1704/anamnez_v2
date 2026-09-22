"""Daily check-up reoffer scheduling for the ordinary (non-marketer) funnel."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from . import analytics, database as db
from .config import settings


# Moscow has used UTC+3 year-round since 2014. A fixed offset also keeps local
# development working on Windows installations without the optional tzdata DB.
MOSCOW = timezone(timedelta(hours=3), name="Europe/Moscow")


def send_due(*, now: datetime | None = None) -> dict:
    local_now = now.astimezone(MOSCOW) if now else datetime.now(MOSCOW)
    examination_date = (local_now.date() + timedelta(days=1))
    if not db.admin_checkup_reoffer_settings()["enabled"]:
        return {
            "status": "disabled", "date": examination_date.isoformat(),
            "queued": 0, "items": [],
        }
    queued = db.queue_due_checkup_reoffers(examination_date, settings.public_base_url)
    for item in queued:
        common = {
            "screen": "reoffer_message",
            "context": "reoffer",
            "source": "checkup_reoffer",
            "reoffer_id": item["id"],
            "duration_bucket": item["duration_bucket"],
            "active_seconds": item["active_seconds"],
            "examination_date": item["examination_date"],
        }
        analytics.record_server_event(
            item["chel_id"], "checkup_reoffer_sent", common,
            session_id=f"reoffer-{item['id']}",
        )
        analytics.record_server_event(
            item["chel_id"], "onboarding_screen_viewed", common,
            session_id=f"reoffer-{item['id']}",
        )
        if item["messenger_count"]:
            analytics.record_server_event(
                item["chel_id"], "checkup_reoffer_messenger_queued",
                {**common, "provider_count": item["messenger_count"]},
                session_id=f"reoffer-{item['id']}",
            )
    return {
        "status": "ok", "date": examination_date.isoformat(),
        "queued": len(queued), "items": queued,
    }


def start_background_scheduler(record_status=None) -> threading.Event:
    """Run shortly after schedule sync and then near 10:00 Moscow every day."""
    stop = threading.Event()

    def worker() -> None:
        # The schedule sync starts after five seconds. Give it time to commit its
        # first snapshot before evaluating tomorrow's examinations.
        if stop.wait(30):
            return
        while not stop.is_set():
            now = datetime.now(MOSCOW)
            if now.hour >= 10:
                try:
                    result = send_due(now=now)
                    if record_status and result["queued"]:
                        record_status(
                            f"Повторные предложения: {result['queued']} отправлено"
                        )
                except Exception as exc:  # keep the web service alive
                    if record_status:
                        record_status(f"Ошибка повторных предложений: {exc}")
            stop.wait(300)

    threading.Thread(target=worker, name="checkup-reoffer-scheduler", daemon=True).start()
    return stop
