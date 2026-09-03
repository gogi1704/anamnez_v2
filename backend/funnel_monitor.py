"""Daily privacy-safe sales-funnel reports delivered through Bitrix Connector."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import analytics, database as db
from .config import settings


class FunnelMonitorUnavailable(RuntimeError):
    pass


_send_lock = threading.Lock()

MANUAL_ANALYSES = {
    "all": ("Полный отчёт", True, True, True, True),
    "standard": ("Обычная воронка", True, False, False, False),
    "result": ("Путь /result", False, True, False, False),
    "payments": ("Оплаты", False, False, True, False),
    "errors": ("Технические ошибки", False, False, False, True),
}

ANALYSIS_INSTRUCTIONS = {
    "all": "Проанализируй воронки, оплаты и технические ошибки в совокупности. Найди главные отклонения и предложи приоритетные проверки и действия.",
    "standard": "Проанализируй обычную воронку анкетирования: найди экраны с наибольшим падением конверсии и предложи проверяемые причины и действия.",
    "result": "Проанализируй путь /result: найди потери при поиске, получении и расшифровке результатов и предложи проверяемые улучшения.",
    "payments": "Проанализируй попытки и успешность оплат, конверсию, выручку и неуспешные статусы. Укажи, какие причины можно подтвердить данными и что следует проверить.",
    "errors": "Проанализируй динамику технических ошибок, выдели наиболее массовые и предложи порядок технической проверки по влиянию на пользователей.",
}


def configured() -> bool:
    return bool(settings.bitrix_connector_url and settings.bitrix_metrics_secret)


def _zone(name: str):
    if name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows installations may not ship the optional tzdata package.
        # Moscow has used a fixed UTC+3 offset since 2014.
        return timezone(timedelta(hours=3)) if name == "Europe/Moscow" else timezone.utc


def _windows(period_days: int, local_today: date) -> tuple[dict, dict]:
    current_end = local_today - timedelta(days=1)
    current_start = current_end - timedelta(days=period_days - 1)
    comparison_end = current_start - timedelta(days=1)
    comparison_start = comparison_end - timedelta(days=period_days - 1)
    return (
        {"date_from": current_start.isoformat(), "date_to": current_end.isoformat()},
        {"date_from": comparison_start.isoformat(), "date_to": comparison_end.isoformat()},
    )


def _distribution(items: list[dict], limit: int = 10) -> list[dict]:
    return [
        {"label": str(item.get("label", ""))[:100], "users": int(item.get("users", 0) or 0)}
        for item in items[:limit]
    ]


def _compact_admin(report: dict, config: dict) -> dict:
    result = {
        "summary": {
            key: int(report.get("summary", {}).get(key, 0) or 0)
            for key in ("users", "visitors", "sessions", "events")
        },
        "funnel": [{
            "id": str(item.get("event_name", ""))[:80],
            "title": str(item.get("label", ""))[:160],
            "users": int(item.get("users", 0) or 0),
            "from_previous": float(item.get("from_previous", 0) or 0),
            "from_start": float(item.get("from_start", 0) or 0),
            "dropoff": int(item.get("dropoff", 0) or 0),
        } for item in report.get("funnel", [])],
        "devices": _distribution(report.get("devices", [])),
        "registrations": _distribution(report.get("registrations", [])),
        "sources": _distribution(report.get("sources", [])),
    }
    if config.get("include_errors"):
        result["errors"] = [{
            "label": str(item.get("label", ""))[:100],
            "events": int(item.get("events", 0) or 0),
            "users": int(item.get("users", 0) or 0),
        } for item in report.get("errors", [])[:20]]
    if config.get("include_payments"):
        payment = report.get("payments", {}).get("summary", {})
        result["payments"] = {
            key: payment.get(key, 0)
            for key in (
                "attempts", "users", "succeeded", "successful_users", "conversion",
                "revenue_kopecks", "pending", "unsuccessful", "at_exam_users", "test_attempts",
            )
        }
    return result


def _compact_flow(current: dict, comparison: dict, config: dict) -> dict:
    previous = {str(item.get("id")): item for item in comparison.get("screens", [])}
    screens = []
    alerts = []
    threshold = float(config["alert_threshold_pp"])
    minimum_users = int(config["minimum_users"])
    for item in current.get("screens", []):
        screen_id = str(item.get("id", ""))
        old = previous.get(screen_id, {})
        current_conversion = float(item.get("percent_of_parent", 0) or 0)
        previous_conversion = float(old.get("percent_of_parent", 0) or 0)
        delta = round(current_conversion - previous_conversion, 1)
        compact = {
            "id": screen_id[:80],
            "title": str(item.get("title", ""))[:160],
            "users": int(item.get("users", 0) or 0),
            "percent_of_start": float(item.get("percent_of_start", 0) or 0),
            "percent_of_parent": current_conversion,
            "previous_percent_of_parent": previous_conversion,
            "change_pp": delta,
            "terminal": bool(item.get("terminal")),
            "comparison_users": int(item.get("comparison_users", 0) or 0),
            "previous_comparison_users": int(old.get("comparison_users", 0) or 0),
            "actual_dropoff_users": int(item.get("actual_dropoff_users", 0) or 0),
            "stopped_users": int(item.get("stopped_users", 0) or 0),
            "incomplete_transition_users": int(item.get("incomplete_transition_users", 0) or 0),
            "data_quality": "incomplete" if item.get("data_quality") == "incomplete" else "complete",
        }
        screens.append(compact)
        comparison_users = int(item.get("comparison_users", 0) or 0)
        previous_comparison_users = int(old.get("comparison_users", 0) or 0)
        if (
            min(comparison_users, previous_comparison_users) >= minimum_users
            and compact["data_quality"] == "complete"
            and old.get("data_quality") != "incomplete"
            and delta <= -threshold
        ):
            alerts.append({
                "screen_id": compact["id"], "title": compact["title"],
                "change_pp": delta, "current_percent": current_conversion,
                "previous_percent": previous_conversion,
                "users": compact["users"], "comparison_users": comparison_users,
            })
    alerts.sort(key=lambda item: item["change_pp"])
    return {
        "label": str(current.get("flow_label", ""))[:100],
        "summary": {
            "start_users": int(current.get("summary", {}).get("start_users", 0) or 0),
            "reached_completion": int(current.get("summary", {}).get("reached_completion", 0) or 0),
        },
        "screens": screens,
        "alerts": alerts[:10],
        "sample_sufficient": min(
            int(current.get("summary", {}).get("start_users", 0) or 0),
            int(comparison.get("summary", {}).get("start_users", 0) or 0),
        ) >= minimum_users,
    }


def manual_analysis_config(config: dict, analysis: str) -> dict:
    preset = MANUAL_ANALYSES.get(str(analysis))
    if not preset:
        raise ValueError("Неизвестный вид анализа")
    label, standard, result, payments, errors = preset
    return {
        **config,
        "include_standard": standard,
        "include_result": result,
        "include_payments": payments,
        "include_errors": errors,
        "analysis": str(analysis),
        "analysis_label": label,
    }


def build_report(
    config: dict | None = None, *, test: bool = False,
    now: datetime | None = None, analysis: str | None = None,
) -> dict:
    config = config or db.admin_funnel_monitor_settings()
    if analysis is not None:
        config = manual_analysis_config(config, analysis)
    local_now = now or datetime.now(_zone(str(config["timezone"])))
    current_window, comparison_window = _windows(int(config["period_days"]), local_now.date())
    current_admin = analytics.admin_report(
        "all", recent_limit=10, **current_window,
    )
    comparison_admin = analytics.admin_report(
        "all", recent_limit=10, **comparison_window,
    )
    flows = []
    for flow, enabled_key in (("standard", "include_standard"), ("result", "include_result")):
        if not config.get(enabled_key):
            continue
        current_flow = analytics.metric2_report("all", flow=flow, **current_window)
        comparison_flow = analytics.metric2_report("all", flow=flow, **comparison_window)
        flows.append({"id": flow, **_compact_flow(current_flow, comparison_flow, config)})
    suffix = uuid.uuid4().hex[:12] if test else current_window["date_to"]
    return {
        "schema_version": 1,
        "report_id": f"consilium-funnel-{int(config['period_days'])}d-{suffix}",
        "test": bool(test),
        "analysis": str(config.get("analysis") or "scheduled"),
        "analysis_label": str(config.get("analysis_label") or "Отчёт по расписанию"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dialog_id": str(config["dialog_id"]),
        "period_days": int(config["period_days"]),
        "current_period": current_window,
        "comparison_period": comparison_window,
        "minimum_users": int(config["minimum_users"]),
        "alert_threshold_pp": float(config["alert_threshold_pp"]),
        "current": _compact_admin(current_admin, config),
        "comparison": _compact_admin(comparison_admin, config),
        "flows": flows,
        "ai_instruction": (
            f"{ANALYSIS_INSTRUCTIONS.get(str(config.get('analysis')), 'Проанализируй изменения воронки.')} "
            "Отдели подтверждённые фактами выводы "
            "от гипотез. Укажи критические отклонения, возможные причины, необходимые "
            "проверки и приоритетные действия. Не делай уверенных выводов при "
            "недостаточной выборке или неполных связях данных."
        ),
        "privacy": "Передаются только обезличенные агрегаты без анкет, сообщений и идентификаторов пользователей.",
    }


def send_report(
    config: dict | None = None, *, test: bool = False,
    now: datetime | None = None, analysis: str | None = None,
) -> dict:
    if not configured():
        raise FunnelMonitorUnavailable("Интеграция метрик с Bitrix Connector не настроена")
    with _send_lock:
        payload = build_report(config, test=test, now=now, analysis=analysis)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{settings.bitrix_connector_url}/bitrix/metrics/consilium",
            data=body,
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
            raise FunnelMonitorUnavailable(f"Bitrix Connector вернул HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise FunnelMonitorUnavailable("Bitrix Connector временно недоступен") from exc
        if not isinstance(result, dict) or result.get("status") not in {"queued", "duplicate"}:
            raise FunnelMonitorUnavailable("Bitrix Connector не подтвердил приём отчёта")
        return {"payload": payload, "connector": result}


def start_background_monitor(log) -> threading.Event:
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                config = db.admin_funnel_monitor_settings()
            except Exception:
                log("Не удалось прочитать настройки мониторинга; повтор через 5 минут")
                stop.wait(300)
                continue
            if config.get("enabled") and configured():
                local_now = datetime.now(_zone(str(config["timezone"])))
                if (
                    local_now.strftime("%H:%M") >= str(config["send_time"])
                    and str(config.get("last_sent_date") or "") != local_now.date().isoformat()
                ):
                    try:
                        send_report(config, now=local_now)
                    except FunnelMonitorUnavailable as exc:
                        db.record_funnel_monitor_delivery(status="failed", error=str(exc))
                        log(f"Отчёт по воронке не отправлен: {exc}")
                        stop.wait(300)
                    except Exception:
                        db.record_funnel_monitor_delivery(status="failed", error="Неожиданная ошибка")
                        log("Отчёт по воронке не отправлен: неожиданная ошибка")
                        stop.wait(300)
                    else:
                        db.record_funnel_monitor_delivery(
                            status="sent", sent_date=local_now.date().isoformat(),
                        )
                        log("Ежедневный отчёт по воронке передан в Bitrix24")
            stop.wait(max(10, int(settings.funnel_monitor_poll_seconds)))

    threading.Thread(target=worker, name="funnel-monitor", daemon=True).start()
    return stop
