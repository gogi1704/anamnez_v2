"""First-party product analytics without medical content."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings


_write_lock = threading.Lock()

ALLOWED_EVENTS = {
    "landing_viewed", "welcome_viewed", "welcome_continued", "auth_gate_viewed",
    "registration_method_selected", "anonymous_warning_viewed", "anonymous_warning_cancelled",
    "registration_completed", "registration_failed", "messenger_auth_started", "funnel_action",
    "messenger_auth_completed", "messenger_auth_failed", "appearance_viewed",
    "appearance_completed", "questionnaire_started", "question_viewed",
    "question_answered", "question_skipped", "question_back", "question_validation_error",
    "questionnaire_completed", "not_medical_exam_selected", "examinations_offer_viewed",
    "examinations_opened", "examination_selected", "examination_deselected",
    "examinations_skip_clicked", "examinations_objection_viewed", "examinations_skip_recovered",
    "examinations_skipped", "examinations_selection_completed", "payment_viewed",
    "payment_method_selected", "payment_online_unavailable", "payment_completed",
    "onboarding_completed", "completion_viewed", "capabilities_viewed",
    "capabilities_closed", "install_offer_viewed", "install_clicked", "install_dismissed",
    "app_installed", "app_opened", "chat_opened", "conversation_created", "message_sent",
    "first_message_sent", "ai_response_completed", "ai_response_error", "council_started",
    "council_completed", "council_error", "human_requested", "human_channel_selected",
    "manager_joined", "human_request_closed", "lab_results_requested", "lab_results_found",
    "lab_results_not_found", "lab_interpretation_started", "lab_interpretation_completed",
    "lab_interpretation_error", "api_error", "javascript_error", "performance_measured",
}

ALLOWED_PROPERTIES = {
    "question_key", "step_number", "optional", "provider", "method", "result",
    "error_code", "status_code", "exam_id", "recommended", "selected_count",
    "total_price", "install_method", "channel", "agent", "route_action", "duration_ms",
    "response_ms", "screen", "previous_screen", "source", "campaign", "medium",
    "app_mode", "page_version", "connection_type", "conversation_count",
    "document_count", "cached", "reason", "font_size", "stage", "action",
}

FUNNEL_BREAKDOWNS = {
    "registration_completed": [
        ("max", "MAX", "registration_completed", "method", "max"),
        ("telegram", "Telegram", "registration_completed", "method", "telegram"),
        ("anonymous", "Анонимно", "registration_completed", "method", "anonymous"),
        ("registration_back", "Кнопка «Назад»", "anonymous_warning_cancelled", "", ""),
        ("anonymous_button", "Кнопка «Войти анонимно»", "anonymous_warning_viewed", "", ""),
    ],
    "appearance_completed": [
        ("standard", "Маленький", "appearance_completed", "font_size", "standard"),
        ("large", "Большой", "appearance_completed", "font_size", "large"),
        ("extra", "Очень большой", "appearance_completed", "font_size", "extra"),
    ],
    "examinations_offer_viewed": [
        ("edit_questionnaire", "Изменить анкету", "funnel_action", "action", "edit_questionnaire"),
        ("view_options", "Посмотреть варианты", "funnel_action", "action", "view_options"),
        ("skip", "Пропустить", "funnel_action", "action", "skip"),
        ("refuse", "↳ Всё равно отказаться", "funnel_action", "action", "refuse"),
        ("choose", "↳ Выбрать обследования", "funnel_action", "action", "choose_after_objection"),
    ],
    "examinations_opened": [
        ("back", "Кнопка «Назад»", "funnel_action", "action", "options_back"),
        ("pay_online", "Оплатить онлайн", "funnel_action", "action", "pay_online"),
        ("pay_at_exam", "Оплатить на медосмотре", "funnel_action", "action", "pay_at_exam"),
        ("nothing", "Ничего не выбирать", "funnel_action", "action", "nothing_selected"),
    ],
}

FUNNEL_STEPS = [
    ("registration_completed", "Регистрация завершена"),
    ("appearance_completed", "Размер текста выбран"),
    ("questionnaire_started", "Вход в анкету"),
    ("questionnaire_completed", "Выход из анкеты"),
    ("examinations_offer_viewed", "Предложение обследований показано"),
    ("examinations_opened", "Варианты обследований открыты"),
    ("examinations_selection_completed", "Выбор обследований завершён"),
    ("onboarding_completed", "Стартовый путь завершён"),
    ("capabilities_viewed", "Возможности показаны"),
    ("chat_opened", "Чат открыт"),
    ("first_message_sent", "Первое сообщение отправлено"),
    ("human_requested", "Запрошен человек"),
]

QUESTION_LABELS = {
    "company_inn": "ИНН предприятия", "preferred_name": "Имя", "age": "Возраст",
    "sex": "Пол", "height_cm": "Рост", "weight_kg": "Вес", "smoking": "Курение",
    "alcohol": "Алкоголь", "activity": "Активность", "blood_pressure": "Давление",
    "dark_in_eyes": "Потемнение в глазах", "blood_sugar": "Сахар крови",
    "joint_pain": "Суставы", "fatigue": "Усталость",
    "conditions": "Хронические заболевания", "medications": "Лекарства",
    "allergies": "Аллергии",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection():
    settings.analytics_database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.analytics_database_path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    if not settings.analytics_enabled:
        return
    with _write_lock, connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS analytics_sessions (
                session_id TEXT PRIMARY KEY,
                chel_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                entry_source TEXT NOT NULL DEFAULT '',
                campaign TEXT NOT NULL DEFAULT '',
                registration_method TEXT NOT NULL DEFAULT '',
                device_type TEXT NOT NULL DEFAULT 'other',
                operating_system TEXT NOT NULL DEFAULT 'Другое',
                browser TEXT NOT NULL DEFAULT 'Другое',
                app_mode TEXT NOT NULL DEFAULT 'browser',
                event_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS analytics_events (
                event_id TEXT PRIMARY KEY,
                chel_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                event_name TEXT NOT NULL,
                screen TEXT NOT NULL DEFAULT '',
                step_key TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                campaign TEXT NOT NULL DEFAULT '',
                registration_method TEXT NOT NULL DEFAULT '',
                device_type TEXT NOT NULL DEFAULT 'other',
                operating_system TEXT NOT NULL DEFAULT 'Другое',
                browser TEXT NOT NULL DEFAULT 'Другое',
                app_mode TEXT NOT NULL DEFAULT 'browser',
                duration_ms INTEGER,
                properties TEXT NOT NULL DEFAULT '{}',
                client_at TEXT,
                received_at TEXT NOT NULL,
                is_server INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_analytics_events_time ON analytics_events(received_at, event_name);
            CREATE INDEX IF NOT EXISTS idx_analytics_events_user ON analytics_events(chel_id, received_at);
            CREATE INDEX IF NOT EXISTS idx_analytics_events_session ON analytics_events(session_id, received_at);
            CREATE INDEX IF NOT EXISTS idx_analytics_events_step ON analytics_events(event_name, step_key, received_at);
            CREATE INDEX IF NOT EXISTS idx_analytics_sessions_time ON analytics_sessions(started_at, last_seen_at);
            """
        )
        conn.commit()


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if abs(value) <= 100_000_000 else None
    if isinstance(value, str):
        return " ".join(value.split())[:120]
    return None


def sanitize_properties(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, item in value.items():
        if key not in ALLOWED_PROPERTIES:
            continue
        scalar = _safe_scalar(item)
        if scalar is not None:
            result[key] = scalar
    return result


def _device(user_agent: str) -> dict:
    value = " ".join(str(user_agent or "").split()).lower()
    if "android" in value:
        device_type, operating_system = "android", "Android"
    elif any(marker in value for marker in ("iphone", "ipad", "ipod")) or (
        "macintosh" in value and "mobile/" in value
    ):
        device_type, operating_system = "ios", "iOS"
    elif "windows" in value:
        device_type, operating_system = "desktop", "Windows"
    elif "macintosh" in value or "mac os x" in value:
        device_type, operating_system = "desktop", "macOS"
    elif "linux" in value or "x11" in value:
        device_type, operating_system = "desktop", "Linux"
    else:
        device_type, operating_system = "other", "Другое"
    if "samsungbrowser/" in value:
        browser = "Samsung Internet"
    elif any(marker in value for marker in ("edg/", "edga/", "edgios/")):
        browser = "Edge"
    elif "opr/" in value or "opera" in value:
        browser = "Opera"
    elif "crios/" in value or "chrome/" in value:
        browser = "Chrome"
    elif "fxios/" in value or "firefox/" in value:
        browser = "Firefox"
    elif "safari/" in value:
        browser = "Safari"
    else:
        browser = "Другое"
    return {"device_type": device_type, "operating_system": operating_system, "browser": browser}


def record_events(chel_id: str, events: list[dict], *, user_agent: str = "", is_server: bool = False) -> dict:
    if not settings.analytics_enabled or not isinstance(events, list):
        return {"accepted": 0, "duplicates": 0}
    now = _now()
    device = _device(user_agent)
    prepared = []
    for raw in events[:50]:
        if not isinstance(raw, dict):
            continue
        event_name = str(raw.get("event_name", "")).strip()
        if event_name not in ALLOWED_EVENTS:
            continue
        properties = sanitize_properties(raw.get("properties"))
        event_id = str(raw.get("event_id") or uuid.uuid4())[:80]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,80}", event_id):
            event_id = str(uuid.uuid4())
        session_id = str(raw.get("session_id") or f"server-{chel_id}")[:80]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,80}", session_id):
            session_id = str(uuid.uuid4())
        source = str(properties.get("source", raw.get("source", "")))[:80]
        campaign = str(properties.get("campaign", raw.get("campaign", "")))[:100]
        method = str(properties.get("method", properties.get("provider", "")))[:30]
        app_mode = str(properties.get("app_mode", raw.get("app_mode", "browser")))[:20]
        duration = properties.get("duration_ms")
        duration_ms = max(0, min(int(duration), 86_400_000)) if isinstance(duration, (int, float)) else None
        prepared.append({
            "event_id": event_id, "chel_id": str(chel_id)[:80], "session_id": session_id,
            "event_name": event_name, "screen": str(properties.get("screen", raw.get("screen", "")))[:80],
            "step_key": str(properties.get("question_key", raw.get("step_key", "")))[:80],
            "source": source, "campaign": campaign, "registration_method": method,
            "device_type": device["device_type"], "operating_system": device["operating_system"],
            "browser": device["browser"], "app_mode": app_mode, "duration_ms": duration_ms,
            "properties": json.dumps(properties, ensure_ascii=False),
            "client_at": str(raw.get("client_at", ""))[:40] or None,
            "received_at": now, "is_server": int(bool(is_server)),
        })
    if not prepared:
        return {"accepted": 0, "duplicates": 0}
    accepted = 0
    with _write_lock, connection() as conn:
        for event in prepared:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO analytics_events
                (event_id, chel_id, session_id, event_name, screen, step_key, source, campaign,
                 registration_method, device_type, operating_system, browser, app_mode,
                 duration_ms, properties, client_at, received_at, is_server)
                VALUES (:event_id, :chel_id, :session_id, :event_name, :screen, :step_key,
                 :source, :campaign, :registration_method, :device_type, :operating_system,
                 :browser, :app_mode, :duration_ms, :properties, :client_at, :received_at, :is_server)""",
                event,
            )
            if cursor.rowcount:
                accepted += 1
                conn.execute(
                    """INSERT INTO analytics_sessions
                    (session_id, chel_id, started_at, last_seen_at, entry_source, campaign,
                     registration_method, device_type, operating_system, browser, app_mode, event_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    ON CONFLICT(session_id) DO UPDATE SET
                      last_seen_at=excluded.last_seen_at,
                      entry_source=COALESCE(NULLIF(analytics_sessions.entry_source,''), excluded.entry_source),
                      campaign=COALESCE(NULLIF(analytics_sessions.campaign,''), excluded.campaign),
                      registration_method=COALESCE(NULLIF(excluded.registration_method,''), analytics_sessions.registration_method),
                      app_mode=excluded.app_mode, event_count=analytics_sessions.event_count+1""",
                    (event["session_id"], event["chel_id"], now, now, event["source"],
                     event["campaign"], event["registration_method"], event["device_type"],
                     event["operating_system"], event["browser"], event["app_mode"]),
                )
        conn.commit()
    return {"accepted": accepted, "duplicates": len(prepared) - accepted}


def record_server_event(
    chel_id: str, event_name: str, properties: dict | None = None, *,
    user_agent: str = "", session_id: str = "",
) -> None:
    try:
        record_events(chel_id, [{
            "event_id": f"srv-{uuid.uuid4()}", "session_id": session_id or f"server-{chel_id}",
            "event_name": event_name, "properties": properties or {}, "client_at": _now(),
        }], user_agent=user_agent, is_server=True)
    except Exception:
        # Analytics must never break a medical workflow.
        return


def _period_start(period: str) -> str | None:
    if period == "all":
        return None
    days = 1 if period == "today" else max(1, min(int(period or "30"), 3650))
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) if period == "today" else now - timedelta(days=days)
    return start.isoformat()


def _filters(period: str, device: str, method: str, source: str) -> tuple[str, list]:
    clauses, params = [], []
    start = _period_start(period)
    if start:
        clauses.append("e.received_at >= ?")
        params.append(start)
    if device:
        clauses.append("e.device_type = ?")
        params.append(device)
    if method:
        clauses.append("(e.registration_method = ? OR s.registration_method = ?)")
        params.extend([method, method])
    if source:
        clauses.append("(e.source = ? OR s.entry_source = ?)")
        params.extend([source, source])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def admin_report(
    period: str = "30", device: str = "", method: str = "", source: str = "",
    recent_page: int = 1, recent_limit: int = 25,
) -> dict:
    recent_page = max(1, int(recent_page))
    recent_limit = max(10, min(int(recent_limit), 100))
    where, params = _filters(period, device, method, source)
    join = " FROM analytics_events e LEFT JOIN analytics_sessions s ON s.session_id=e.session_id "
    with connection() as conn:
        def scalar(expression: str) -> int:
            return int(conn.execute("SELECT " + expression + join + where, params).fetchone()[0] or 0)

        total_users = scalar("COUNT(DISTINCT e.chel_id)")
        total_sessions = scalar("COUNT(DISTINCT e.session_id)")
        total_events = scalar("COUNT(*)")
        registered_extra = (" AND " if where else " WHERE ") + "e.event_name = 'registration_completed'"
        registered_users = int(conn.execute(
            "SELECT COUNT(DISTINCT e.chel_id)" + join + where + registered_extra, params,
        ).fetchone()[0] or 0)

        def breakdown(event_name: str) -> list[dict]:
            items = []
            for key, label, detail_event, property_name, property_value in FUNNEL_BREAKDOWNS.get(event_name, []):
                detail_where = where + (" AND " if where else " WHERE ") + "e.event_name = ?"
                detail_params = [*params, detail_event]
                if property_name:
                    if property_name == "method":
                        detail_where += " AND COALESCE(NULLIF(e.registration_method,''),json_extract(e.properties,'$.method')) = ?"
                    else:
                        detail_where += f" AND json_extract(e.properties,'$.{property_name}') = ?"
                    detail_params.append(property_value)
                row = conn.execute(
                    "SELECT COUNT(DISTINCT e.chel_id) users, COUNT(*) events" + join + detail_where,
                    detail_params,
                ).fetchone()
                items.append({"key": key, "label": label, "users": int(row[0] or 0), "events": int(row[1] or 0)})
            return items

        funnel, previous, first = [], None, None
        for event_name, label in FUNNEL_STEPS:
            event_where = where + (" AND " if where else " WHERE ") + "e.event_name = ?"
            count = int(conn.execute(
                "SELECT COUNT(DISTINCT e.chel_id)" + join + event_where, [*params, event_name],
            ).fetchone()[0] or 0)
            if first is None:
                first = count
            funnel.append({
                "event_name": event_name, "label": label, "users": count,
                "from_previous": round(count / previous * 100, 1) if previous else (100.0 if count else 0.0),
                "from_start": round(count / first * 100, 1) if first else 0.0,
                "dropoff": max(0, (previous or count) - count),
                "details": breakdown(event_name),
            })
            previous = count

        def grouped(column: str, event_name: str = "") -> list[dict]:
            extra, grouped_params = "", list(params)
            if event_name:
                extra = (" AND " if where else " WHERE ") + "e.event_name = ?"
                grouped_params.append(event_name)
            rows = conn.execute(
                f"SELECT COALESCE(NULLIF({column},''),'Не указано') label, "
                f"COUNT(DISTINCT e.chel_id) users, COUNT(*) events{join}{where}{extra} "
                "GROUP BY label ORDER BY users DESC LIMIT 30", grouped_params,
            ).fetchall()
            return [dict(row) for row in rows]

        question_extra = (" AND " if where else " WHERE ") + "e.event_name IN ('question_viewed','question_answered','question_skipped','question_back','question_validation_error')"
        question_rows = conn.execute(
            """SELECT e.step_key,
             COUNT(DISTINCT CASE WHEN e.event_name='question_viewed' THEN e.chel_id END) viewed,
             COUNT(DISTINCT CASE WHEN e.event_name='question_answered' THEN e.chel_id END) answered,
             COUNT(DISTINCT CASE WHEN e.event_name='question_skipped' THEN e.chel_id END) skipped,
             COUNT(CASE WHEN e.event_name='question_back' THEN 1 END) back_count,
             COUNT(CASE WHEN e.event_name='question_validation_error' THEN 1 END) validation_errors,
             CAST(AVG(CASE WHEN e.event_name IN ('question_answered','question_skipped') THEN e.duration_ms END) AS INTEGER) avg_duration_ms
             """ + join + where + question_extra + " GROUP BY e.step_key", params,
        ).fetchall()
        questions = []
        for row in question_rows:
            item = dict(row)
            item["label"] = QUESTION_LABELS.get(item["step_key"], item["step_key"] or "Неизвестный вопрос")
            item["conversion"] = round(item["answered"] / item["viewed"] * 100, 1) if item["viewed"] else 0.0
            questions.append(item)
        questions.sort(key=lambda item: list(QUESTION_LABELS).index(item["step_key"]) if item["step_key"] in QUESTION_LABELS else 999)

        daily = [dict(row) for row in conn.execute(
            "SELECT SUBSTR(e.received_at,1,10) date, COUNT(DISTINCT e.chel_id) users, "
            "COUNT(DISTINCT e.session_id) sessions, COUNT(*) events" + join + where +
            " GROUP BY date ORDER BY date", params,
        ).fetchall()]
        error_extra = (" AND " if where else " WHERE ") + "(e.event_name LIKE '%_error' OR e.event_name='api_error')"
        errors = [dict(row) for row in conn.execute(
            "SELECT e.event_name label, COUNT(*) events, COUNT(DISTINCT e.chel_id) users" +
            join + where + error_extra + " GROUP BY e.event_name ORDER BY events DESC", params,
        ).fetchall()]
        recent_total = scalar("COUNT(*)")
        recent_pages = max(1, (recent_total + recent_limit - 1) // recent_limit)
        recent_page = min(recent_page, recent_pages)
        recent_offset = (recent_page - 1) * recent_limit
        recent = []
        for row in conn.execute(
            "SELECT e.received_at, e.event_name, e.chel_id, e.session_id, e.screen, e.step_key, "
            "e.device_type, e.browser, e.properties, e.is_server" + join + where +
            " ORDER BY e.received_at DESC LIMIT ? OFFSET ?", [*params, recent_limit, recent_offset],
        ).fetchall():
            item = dict(row)
            try:
                item["properties"] = json.loads(item["properties"] or "{}")
            except json.JSONDecodeError:
                item["properties"] = {}
            recent.append(item)
        filter_options = {
            "devices": [row[0] for row in conn.execute("SELECT DISTINCT device_type FROM analytics_events ORDER BY device_type") if row[0]],
            "methods": [row[0] for row in conn.execute("SELECT DISTINCT registration_method FROM analytics_sessions WHERE registration_method<>'' ORDER BY registration_method")],
            "sources": [row[0] for row in conn.execute("SELECT DISTINCT entry_source FROM analytics_sessions WHERE entry_source<>'' ORDER BY entry_source LIMIT 100")],
        }
        registrations = grouped("COALESCE(NULLIF(e.registration_method,''),s.registration_method)", "registration_completed")
        devices = grouped("e.device_type")
        operating_systems = grouped("e.operating_system")
        browsers = grouped("e.browser")
        sources = grouped("COALESCE(NULLIF(e.source,''),s.entry_source)")
        examinations = grouped("json_extract(e.properties,'$.exam_id')", "examination_selected")
    return {
        "generated_at": _now(), "period": period,
        "summary": {"users": registered_users, "visitors": total_users, "sessions": total_sessions, "events": total_events},
        "funnel": funnel, "daily": daily, "questions": questions,
        "registrations": registrations, "devices": devices,
        "operating_systems": operating_systems, "browsers": browsers,
        "sources": sources, "examinations": examinations,
        "errors": errors, "recent": recent,
        "recent_pagination": {
            "page": recent_page, "limit": recent_limit, "total": recent_total,
            "pages": recent_pages,
        },
        "filter_options": filter_options,
        "privacy": "Медицинские ответы, сообщения, телефоны и номера пробирок не сохраняются.",
    }


def cleanup_old_events() -> int:
    if not settings.analytics_enabled or settings.analytics_retention_days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.analytics_retention_days)).isoformat()
    with _write_lock, connection() as conn:
        cursor = conn.execute("DELETE FROM analytics_events WHERE received_at < ?", (cutoff,))
        conn.execute(
            "DELETE FROM analytics_sessions WHERE last_seen_at < ? AND session_id NOT IN "
            "(SELECT DISTINCT session_id FROM analytics_events)", (cutoff,),
        )
        conn.commit()
        return cursor.rowcount
