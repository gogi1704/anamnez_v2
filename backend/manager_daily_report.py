"""Daily privacy-safe report of new registrations attributed to managers."""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import database as db
from .config import settings


class ManagerDailyReportUnavailable(RuntimeError):
    pass


_send_lock = threading.Lock()


def configured() -> bool:
    return bool(
        settings.bitrix_connector_url
        and settings.bitrix_metrics_secret
        and re.fullmatch(r"(?:chat|sg)?\d+", settings.manager_daily_report_dialog_id)
    )


def _zone(name: str):
    if name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=3)) if name == "Europe/Moscow" else timezone.utc


def _manager_sort_key(item: dict) -> tuple[int, str]:
    marker = str(item.get("from_manager") or "")
    match = re.fullmatch(r"manager_(\d+)", marker)
    return (int(match.group(1)) if match else 1_000_000, marker)


def build_report(*, report_date: date | None = None, test: bool = False) -> dict:
    timezone_name = settings.manager_daily_report_timezone or "Europe/Moscow"
    local_now = datetime.now(_zone(timezone_name))
    target_date = report_date or (local_now.date() - timedelta(days=1))
    source = db.admin_manager_first_arrivals(
        target_date.isoformat(), target_date.isoformat(),
    )
    by_marker = {
        str(item.get("from_manager") or ""): dict(item)
        for item in source.get("managers", [])
    }
    managers = []
    for marker, name in db.FROM_MANAGER_NAMES.items():
        item = by_marker.pop(marker, {})
        managers.append({
            "from_manager": marker,
            "manager_name": name,
            "users": int(item.get("users", 0) or 0),
        })
    for marker, item in by_marker.items():
        managers.append({
            "from_manager": marker,
            "manager_name": str(item.get("manager_name") or marker)[:100],
            "users": int(item.get("users", 0) or 0),
        })
    managers.sort(key=_manager_sort_key)
    suffix = uuid.uuid4().hex[:12] if test else target_date.isoformat()
    total_users = int(source.get("total_users", 0) or 0)
    attributed_users = int(source.get("attributed_users", 0) or 0)
    return {
        "schema_version": 1,
        "report_id": f"consilium-manager-users-{suffix}",
        "test": bool(test),
        "date": target_date.isoformat(),
        "timezone": timezone_name,
        "dialog_id": settings.manager_daily_report_dialog_id,
        "total_users": total_users,
        "attributed_users": attributed_users,
        "unattributed_users": max(0, total_users - attributed_users),
        "managers": managers,
        "privacy": (
            "Только агрегаты новых посетителей первого посадочного экрана "
            "по менеджерам, без данных пользователей."
        ),
    }


def send_report(*, report_date: date | None = None, test: bool = False) -> dict:
    if not configured():
        raise ManagerDailyReportUnavailable(
            "Ежедневный отчёт по менеджерам или его чат Bitrix24 не настроен"
        )
    with _send_lock:
        payload = build_report(report_date=report_date, test=test)
        request = Request(
            f"{settings.bitrix_connector_url}/bitrix/reports/manager-users",
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Consilium-Metrics-Secret": settings.bitrix_metrics_secret,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=settings.bitrix_connector_timeout_seconds) as response:
                result = json.loads(response.read(64_000).decode("utf-8"))
        except HTTPError as exc:
            raise ManagerDailyReportUnavailable(
                f"Bitrix Connector вернул HTTP {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ManagerDailyReportUnavailable(
                "Bitrix Connector временно недоступен"
            ) from exc
        if not isinstance(result, dict) or result.get("status") not in {"queued", "duplicate"}:
            raise ManagerDailyReportUnavailable("Bitrix Connector не подтвердил приём отчёта")
        return {"payload": payload, "connector": result}


def start_background_scheduler(log) -> threading.Event:
    stop = threading.Event()

    def worker() -> None:
        last_accepted_date = ""
        while not stop.is_set():
            if settings.manager_daily_report_enabled and configured():
                local_now = datetime.now(_zone(settings.manager_daily_report_timezone))
                report_date = (local_now.date() - timedelta(days=1)).isoformat()
                if (
                    local_now.strftime("%H:%M") >= settings.manager_daily_report_send_time
                    and report_date != last_accepted_date
                ):
                    try:
                        send_report(report_date=date.fromisoformat(report_date))
                    except ManagerDailyReportUnavailable as exc:
                        log(f"Отчёт по новым пользователям менеджеров не отправлен: {exc}")
                        stop.wait(300)
                    except Exception:
                        log("Отчёт по новым пользователям менеджеров не отправлен: неожиданная ошибка")
                        stop.wait(300)
                    else:
                        last_accepted_date = report_date
                        log("Ежедневный отчёт по новым пользователям менеджеров передан в Bitrix24")
            stop.wait(max(10, int(settings.manager_daily_report_poll_seconds)))

    threading.Thread(target=worker, name="manager-daily-report", daemon=True).start()
    return stop
