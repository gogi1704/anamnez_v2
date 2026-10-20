"""Scheduled, report-only user-data retention audit.

The production switch is deliberately fail-closed: this module never sends
user warnings and never deletes data while ``DATA_RETENTION_DRY_RUN`` is true.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

from . import database as db
from .config import settings


REPORT_BODY_LIMIT = 3200


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_datetime(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def audit_due(now: datetime | None = None) -> bool:
    last = db.last_completed_data_retention_audit()
    if not last:
        return True
    completed = _parse_datetime(last.get("completed_at"))
    if not completed:
        return True
    interval_days = max(1, int(settings.data_retention_interval_days))
    return _utc(now) >= completed + timedelta(days=interval_days)


def _candidate_line(item: dict, grace_days: int) -> str:
    name = str(item.get("preferred_name") or "Без имени").strip()
    providers = ", ".join(item.get("messenger_providers") or []) or "нет мессенджера"
    action = (
        f"предупредить → удалить через {grace_days} дн."
        if item.get("messenger_linked")
        else f"нет канала предупреждения → удалить через {grace_days} дн."
    )
    return (
        f"• {name} · {item.get('chel_id', '—')} · "
        f"активность {str(item.get('last_activity_at') or '—')[:10]} · "
        f"{providers} · {action}"
    )


def _excluded_line(item: dict) -> str:
    reasons = "; ".join(item.get("exclusion_reasons") or ["защищён от удаления"])
    return f"• {item.get('chel_id', '—')} · {reasons}"


def _report_lines(preview: dict, grace_days: int) -> list[str]:
    lines = [
        "Режим: DRY-RUN — сообщения пользователям не отправлялись, данные не удалялись.",
        f"Порог неактивности: {str(preview.get('cutoff_at') or '')[:10]}.",
        f"Кандидатов: {preview.get('candidate_count', 0)}.",
        f"С привязанным мессенджером: {preview.get('messenger_count', 0)}.",
        f"Без мессенджера: {preview.get('no_messenger_count', 0)}.",
        f"Исключено из очистки: {preview.get('excluded_count', 0)}.",
        f"В рабочем режиме удаление планировалось бы через {grace_days} дн. после предупреждения.",
    ]
    candidates = preview.get("candidates") or []
    lines.append("")
    lines.append("Кандидаты:")
    lines.extend(_candidate_line(item, grace_days) for item in candidates)
    if not candidates:
        lines.append("• Нет")
    excluded = preview.get("excluded") or []
    lines.append("")
    lines.append("Исключения:")
    lines.extend(_excluded_line(item) for item in excluded)
    if not excluded:
        lines.append("• Нет")
    return lines


def _chunk_lines(lines: list[str]) -> list[str]:
    chunks: list[str] = []
    current = ""
    for raw in lines:
        line = str(raw or "")[:1000]
        addition = line if not current else f"\n{line}"
        if current and len(current) + len(addition) > REPORT_BODY_LIMIT:
            chunks.append(current)
            current = line
        else:
            current += addition
    if current or not chunks:
        chunks.append(current)
    return chunks


def queue_report(run_id: str, preview: dict) -> int:
    recipients = [
        ("telegram", settings.data_retention_report_telegram_chat_id),
        ("max", settings.data_retention_report_max_chat_id),
    ]
    recipients = [(provider, recipient) for provider, recipient in recipients if recipient]
    if not recipients:
        return 0
    chunks = _chunk_lines(_report_lines(
        preview, max(1, int(settings.data_retention_grace_days)),
    ))
    queued = 0
    total = len(chunks)
    for provider, recipient in recipients:
        for index, body in enumerate(chunks, start=1):
            title = "Проверка очистки данных · DRY-RUN"
            if total > 1:
                title += f" · {index}/{total}"
            queued += int(db.queue_system_notification(
                provider, recipient, "data_retention_dry_run",
                {"title": title, "body": body},
                f"retention:{run_id}:{index}",
            ))
    return queued


def run_dry_run(*, now: datetime | None = None, force: bool = False) -> dict:
    """Build the retention report. This function cannot delete or warn users."""
    current = _utc(now)
    if not settings.data_retention_enabled:
        return {"status": "disabled", "dry_run": True}
    if not settings.data_retention_dry_run:
        return {
            "status": "blocked",
            "dry_run": False,
            "detail": "Реальное удаление ещё не реализовано и заблокировано",
        }
    if not (
        settings.data_retention_report_telegram_chat_id
        or settings.data_retention_report_max_chat_id
    ):
        return {
            "status": "misconfigured",
            "dry_run": True,
            "detail": "Не указан чат для отчёта dry-run",
        }
    if not force and not audit_due(current):
        return {"status": "not_due", "dry_run": True}

    inactive_days = max(1, int(settings.data_retention_inactive_days))
    cutoff_at = (current - timedelta(days=inactive_days)).isoformat()
    run_id = f"retention-{current.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    db.start_data_retention_audit(run_id, cutoff_at)
    try:
        preview = db.data_retention_preview(cutoff_at)
        report_notifications = queue_report(run_id, preview)
        preview["report_notifications"] = report_notifications
        preview["report_recipient_configured"] = bool(
            settings.data_retention_report_telegram_chat_id
            or settings.data_retention_report_max_chat_id
        )
        db.finish_data_retention_audit(run_id, preview)
        return {"status": "completed", "dry_run": True, "run_id": run_id, **preview}
    except Exception as exc:
        db.fail_data_retention_audit(run_id, str(exc))
        raise


def start_background_scheduler(record_status=None) -> threading.Event:
    """Check hourly and execute the report at most once per configured interval."""
    stop = threading.Event()

    def worker() -> None:
        if stop.wait(60):
            return
        while not stop.is_set():
            try:
                result = run_dry_run()
                if record_status and result.get("status") == "completed":
                    record_status(
                        "Dry-run очистки: "
                        f"{result.get('candidate_count', 0)} кандидатов, "
                        f"{result.get('excluded_count', 0)} исключено"
                    )
                elif record_status and result.get("status") == "misconfigured":
                    record_status(f"Dry-run очистки не запущен: {result.get('detail')}")
            except Exception as exc:  # keep the web service alive
                if record_status:
                    record_status(f"Ошибка dry-run очистки данных: {exc}")
            stop.wait(3600)

    threading.Thread(
        target=worker, name="data-retention-dry-run", daemon=True,
    ).start()
    return stop
