"""Background delivery for recurring reminders in «Контроль питания»."""

import threading

from . import database as db
from .config import settings
from .llm import llm_service


def dispatch_due_conclusions(report=None) -> int:
    """Generate final diary conclusions after the persisted fourteen-day deadline."""
    completed = 0
    previous_chel_id = db.current_chel_id()
    try:
        for item in db.due_weight_control_conclusions(limit=3):
            try:
                db.set_current_chel_id(item["chel_id"])
                state = db.get_weight_control_state(item["conversation_id"])
                if not state:
                    continue
                diary = db.weight_control_diary(
                    item["conversation_id"], require_owner=False,
                )
                started_at = str(diary.get("started_at") or "")
                history = [
                    message for message in db.list_messages(item["conversation_id"])
                    if not started_at or str(message.get("created_at") or "") >= started_at
                ]
                conclusion = llm_service.weight_control_conclusion(
                    db.get_profile(), state, diary, history,
                )
                if db.complete_weight_control_conclusion(
                    item["conversation_id"], conclusion,
                ):
                    completed += 1
            except Exception as exc:
                if report:
                    report(
                        f"Ошибка итогового заключения «Контроль питания» "
                        f"для {item.get('conversation_id')}: {exc}"
                    )
    finally:
        db.set_current_chel_id(previous_chel_id)
    return completed


def start_background_scheduler(report=None) -> threading.Event:
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            try:
                delivered = db.dispatch_due_weight_control_reminders(settings.public_base_url)
                if delivered and report:
                    report(f"Напоминания «Контроль питания»: отправлено {delivered}")
                conclusions = dispatch_due_conclusions(report)
                if conclusions and report:
                    report(f"Итоги «Контроль питания»: подготовлено {conclusions}")
            except Exception as exc:  # scheduler must not stop the web server
                if report:
                    report(f"Ошибка напоминаний «Контроль питания»: {exc}")
            stop.wait(30)

    thread = threading.Thread(target=run, name="weight-reminders", daemon=True)
    thread.start()
    return stop
