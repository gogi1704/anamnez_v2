"""Background delivery for recurring reminders in «Контроль питания»."""

import threading

from . import database as db
from .config import settings
from .llm import llm_service


def dispatch_due_conclusions(report=None) -> int:
    """Queue end-of-program notices; the user explicitly starts the AI analysis."""
    queued = db.queue_due_weight_control_completions(settings.public_base_url)
    if queued and report:
        report(f"Завершения «Контроль питания»: подготовлено {queued}")
    return queued


def generate_conclusion_now(conversation_id: str) -> dict:
    """Generate a diary conclusion only after the owner presses the analysis button."""
    reservation = db.begin_weight_control_conclusion(conversation_id)
    if reservation["status"] == "generating":
        return {"status": "generating", "conversation_id": conversation_id}
    if reservation["status"] == "ready":
        message_id = int(reservation["message_id"])
        message = next(
            (item for item in db.list_messages(conversation_id) if int(item["id"]) == message_id),
            None,
        )
        return {
            "status": "ready", "conversation_id": conversation_id,
            "assistant_message": message,
        }
    try:
        state = db.get_weight_control_state(conversation_id)
        diary = db.weight_control_diary(conversation_id)
        started_at = str(diary.get("started_at") or "")
        history = [
            message for message in db.list_messages(conversation_id)
            if not started_at or str(message.get("created_at") or "") >= started_at
        ]
        conclusion = llm_service.weight_control_conclusion(
            db.get_profile(), state or {}, diary, history,
        )
        completed = db.complete_weight_control_conclusion(conversation_id, conclusion)
        if not completed:
            raise RuntimeError("Не удалось сохранить анализ питания")
        message_id = int(completed["message_id"])
        message = next(
            (item for item in db.list_messages(conversation_id) if int(item["id"]) == message_id),
            None,
        )
        return {
            "status": "ready", "conversation_id": conversation_id,
            "assistant_message": message,
        }
    except Exception as exc:
        db.fail_weight_control_conclusion(conversation_id, str(exc))
        raise


def start_background_scheduler(report=None) -> threading.Event:
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            try:
                dispatch_due_conclusions(report)
                delivered = db.dispatch_due_weight_control_reminders(settings.public_base_url)
                if delivered and report:
                    report(f"Напоминания «Контроль питания»: отправлено {delivered}")
            except Exception as exc:  # scheduler must not stop the web server
                if report:
                    report(f"Ошибка напоминаний «Контроль питания»: {exc}")
            stop.wait(30)

    thread = threading.Thread(target=run, name="weight-reminders", daemon=True)
    thread.start()
    return stop
