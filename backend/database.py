import json
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone

from .config import settings


_write_lock = threading.Lock()
_current_chel_id: ContextVar[str] = ContextVar("current_chel_id", default="chel_test_default")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_current_chel_id(chel_id: str) -> None:
    _current_chel_id.set(chel_id)


def current_chel_id() -> str:
    return _current_chel_id.get()


def ensure_user(chel_id: str, *, pending: bool = False) -> dict:
    now = utc_now()
    with _write_lock, connection() as conn:
        if pending:
            conn.execute(
                """INSERT INTO users (chel_id, created_at, last_seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chel_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                (chel_id, now, now),
            )
        else:
            conn.execute(
                """INSERT INTO users
                (chel_id, created_at, last_seen_at, registered_at, registration_method)
                VALUES (?, ?, ?, ?, 'internal')
                ON CONFLICT(chel_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    registered_at=COALESCE(users.registered_at, excluded.registered_at),
                    registration_method=COALESCE(users.registration_method, excluded.registration_method)""",
                (chel_id, now, now, now),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE chel_id = ?", (chel_id,)).fetchone()
    return dict(row)


def mark_current_user_registered(method: str) -> dict:
    """Count a user only after an explicit access choice or consumed messenger login."""
    method = str(method or "").strip().lower()
    if method not in {"anonymous", "telegram", "max"}:
        raise ValueError("Неизвестный способ регистрации")
    now = utc_now()
    chel_id = current_chel_id()
    with _write_lock, connection() as conn:
        conn.execute(
            """UPDATE users SET
                registered_at=COALESCE(registered_at, ?),
                registration_method=COALESCE(registration_method, ?),
                last_seen_at=?
            WHERE chel_id=?""",
            (now, method, now, chel_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE chel_id = ?", (chel_id,)).fetchone()
    if not row:
        raise ValueError("Пользователь не найден")
    return dict(row)


def classify_user_agent(user_agent: str) -> dict:
    """Turn a browser User-Agent into coarse, privacy-conscious audience fields."""
    raw = " ".join(str(user_agent or "").split())[:500]
    value = raw.lower()
    if any(marker in value for marker in (
        "bot", "crawler", "spider", "slurp", "headlesschrome", "lighthouse",
    )):
        device_type = "bot"
        operating_system = "Bot"
    elif "android" in value:
        device_type = "android"
        operating_system = "Android"
    elif any(marker in value for marker in ("iphone", "ipad", "ipod")) or (
        "macintosh" in value and "mobile/" in value
    ):
        device_type = "ios"
        operating_system = "iOS"
    elif "windows" in value:
        device_type = "desktop"
        operating_system = "Windows"
    elif "cros" in value:
        device_type = "desktop"
        operating_system = "ChromeOS"
    elif "macintosh" in value or "mac os x" in value:
        device_type = "desktop"
        operating_system = "macOS"
    elif "linux" in value or "x11" in value:
        device_type = "desktop"
        operating_system = "Linux"
    else:
        device_type = "other"
        operating_system = "Другое"

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
    return {
        "device_type": device_type,
        "operating_system": operating_system,
        "browser": browser,
        "user_agent": raw,
    }


def record_device_access(user_agent: str) -> dict | None:
    """Count one application page opening for the current user and device."""
    classified = classify_user_agent(user_agent)
    if classified["device_type"] == "bot":
        return None
    chel_id = current_chel_id()
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO user_device_stats
            (chel_id, device_type, operating_system, browser, user_agent,
             first_seen_at, last_seen_at, visit_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(chel_id, device_type, operating_system, browser)
            DO UPDATE SET user_agent = excluded.user_agent,
                last_seen_at = excluded.last_seen_at,
                visit_count = user_device_stats.visit_count + 1""",
            (
                chel_id,
                classified["device_type"],
                classified["operating_system"],
                classified["browser"],
                classified["user_agent"],
                now,
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            """SELECT * FROM user_device_stats
            WHERE chel_id = ? AND device_type = ? AND operating_system = ? AND browser = ?""",
            (
                chel_id,
                classified["device_type"],
                classified["operating_system"],
                classified["browser"],
            ),
        ).fetchone()
    return dict(row)


def current_device() -> dict:
    """Return the most recently used device without exposing the raw User-Agent."""
    with connection() as conn:
        row = conn.execute(
            """SELECT device_type, operating_system, browser, last_seen_at
            FROM user_device_stats WHERE chel_id = ?
            ORDER BY last_seen_at DESC LIMIT 1""",
            (current_chel_id(),),
        ).fetchone()
    return dict(row) if row else {
        "device_type": "other",
        "operating_system": "Другое",
        "browser": "Другое",
        "last_seen_at": None,
    }


def user_exists(chel_id: str) -> bool:
    with connection() as conn:
        return bool(conn.execute("SELECT 1 FROM users WHERE chel_id = ?", (chel_id,)).fetchone())


def reset_current_user(preserve_identity: bool = False) -> None:
    """Remove user content; optionally preserve a verified external identity."""
    chel_id = current_chel_id()
    with _write_lock, connection() as conn:
        # Messages and handoffs are removed by the conversation foreign keys.
        conn.execute("DELETE FROM conversations WHERE chel_id = ?", (chel_id,))
        conn.execute("DELETE FROM memories WHERE chel_id = ?", (chel_id,))
        conn.execute("DELETE FROM body_symptoms WHERE chel_id = ?", (chel_id,))
        conn.execute("DELETE FROM lab_interpretations WHERE chel_id = ?", (chel_id,))
        conn.execute("DELETE FROM user_profile WHERE chel_id = ?", (chel_id,))
        conn.execute("DELETE FROM onboarding_state WHERE chel_id = ?", (chel_id,))
        if not preserve_identity:
            conn.execute("DELETE FROM users WHERE chel_id = ?", (chel_id,))
        conn.commit()


def admin_delete_user_data(chel_id: str) -> dict:
    """Permanently remove every main-database record owned by one user."""
    chel_id = str(chel_id or "").strip()
    if not re.fullmatch(r"chel_[A-Za-z0-9_-]{8,64}", chel_id):
        raise ValueError("Укажите корректный chel_id пользователя")
    if chel_id in {"chel_legacy", "chel_test_default"}:
        raise ValueError("Системного пользователя удалять нельзя")

    # Keep this list explicit and ordered: conversations must be removed before
    # the user because their dependent messages, handoffs and notifications use
    # cascading foreign keys.
    owned_tables = (
        "conversations", "memories", "body_symptoms", "lab_interpretations",
        "user_profile", "onboarding_state", "user_device_stats", "ai_usage",
        "login_tokens", "auth_intents", "user_sessions", "external_identities",
        "users",
    )
    deleted_by_table: dict[str, int] = {}
    with _write_lock, connection() as conn:
        for table in owned_tables:
            cursor = conn.execute(f"DELETE FROM {table} WHERE chel_id = ?", (chel_id,))
            deleted_by_table[table] = max(0, int(cursor.rowcount or 0))
        conn.commit()
    return {
        "chel_id": chel_id,
        "deleted": sum(deleted_by_table.values()),
        "deleted_by_table": deleted_by_table,
        "user_found": bool(deleted_by_table["users"]),
    }


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _staff_login(value: str) -> str:
    login = str(value or "").strip().lower()
    if not 4 <= len(login) <= 64 or any(
        char not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for char in login
    ):
        raise ValueError("Логин: 4–64 символа, латинские буквы, цифры, точка, дефис или _")
    return login


def _password_hash(password: str) -> str:
    password = str(password or "")
    if len(password) < 6 or len(password) > 256:
        raise ValueError("Пароль должен содержать от 6 до 256 символов")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32,
    )
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def _password_valid(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            str(password or "").encode("utf-8"), salt=bytes.fromhex(salt),
            n=int(n), r=int(r), p=int(p), dklen=32,
        )
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def _max_chel_id(legacy_chel_id: int) -> str:
    return f"chel_max_{legacy_chel_id:012d}"


def _messenger_provider(value: str) -> str:
    provider = str(value or "").strip().lower()
    if provider not in {"telegram", "max"}:
        raise ValueError("Поддерживаются только Telegram и MAX")
    return provider


def create_auth_intent(provider: str) -> dict:
    """Create a short-lived token that lets a messenger bind the current browser user."""
    provider = _messenger_provider(provider)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.auth_intent_ttl_seconds)
    raw_token = secrets.token_urlsafe(32)
    chel_id = current_chel_id()
    with _write_lock, connection() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE chel_id = ?", (chel_id,)).fetchone():
            raise ValueError("Пользователь не найден")
        conn.execute(
            """INSERT INTO auth_intents
            (token_hash, chel_id, provider, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (_token_hash(raw_token), chel_id, provider, expires_at.isoformat(), now.isoformat()),
        )
        conn.execute(
            "DELETE FROM auth_intents WHERE expires_at < ? OR used_at IS NOT NULL",
            ((now - timedelta(days=1)).isoformat(),),
        )
        conn.commit()
    return {
        "token": raw_token,
        "chel_id": chel_id,
        "provider": provider,
        "expires_at": expires_at.isoformat(),
    }


def create_messenger_login(
    provider: str,
    provider_user_id: str | int,
    intent_token: str = "",
    legacy_chel_id: int | None = None,
) -> dict:
    """Bind a verified messenger identity and issue a one-time Consilium login."""
    provider = _messenger_provider(provider)
    external_id = str(provider_user_id or "").strip()
    if not external_id or len(external_id) > 128:
        raise ValueError("Не указан идентификатор пользователя мессенджера")
    if legacy_chel_id is not None and int(legacy_chel_id) <= 0:
        raise ValueError("chel_id должен быть положительным числом")

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.auth_link_ttl_seconds)
    raw_token = secrets.token_urlsafe(32)
    with _write_lock, connection() as conn:
        intent = None
        if intent_token:
            intent = conn.execute(
                "SELECT * FROM auth_intents WHERE token_hash = ?",
                (_token_hash(intent_token),),
            ).fetchone()
            if (
                not intent
                or intent["used_at"]
                or intent["provider"] != provider
                or datetime.fromisoformat(intent["expires_at"]) <= now
            ):
                raise ValueError("Запрос авторизации недействителен или устарел")
        identity = conn.execute(
            """SELECT * FROM external_identities
            WHERE provider = ? AND provider_user_id = ?""",
            (provider, external_id),
        ).fetchone()
        if identity:
            if identity["access_status"] != "active":
                raise PermissionError("Доступ к Консилиуму для этого пользователя не активен")
            chel_id = identity["chel_id"]
            conn.execute(
                "UPDATE external_identities SET last_login_at = ? WHERE id = ?",
                (now.isoformat(), identity["id"]),
            )
        else:
            if intent:
                chel_id = intent["chel_id"]
            elif provider == "max" and legacy_chel_id is not None:
                chel_id = _max_chel_id(int(legacy_chel_id))
                collision = conn.execute(
                    """SELECT provider_user_id FROM external_identities
                    WHERE provider = 'max' AND chel_id = ?""",
                    (chel_id,),
                ).fetchone()
                if collision and collision["provider_user_id"] != external_id:
                    raise ValueError("Этот chel_id уже привязан к другому MAX-пользователю")
            else:
                chel_id = f"chel_{secrets.token_hex(16)}"
            conn.execute(
                """INSERT INTO users (chel_id, created_at, last_seen_at)
                VALUES (?, ?, ?) ON CONFLICT(chel_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                (chel_id, now.isoformat(), now.isoformat()),
            )
            conn.execute(
                """INSERT INTO external_identities
                (provider, provider_user_id, chel_id, legacy_chel_id, access_status, created_at, last_login_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                (
                    provider, external_id, chel_id, legacy_chel_id,
                    now.isoformat(), now.isoformat(),
                ),
            )
        if intent:
            updated = conn.execute(
                """UPDATE auth_intents SET used_at = ?
                WHERE token_hash = ? AND used_at IS NULL""",
                (now.isoformat(), intent["token_hash"]),
            )
            if updated.rowcount != 1:
                raise ValueError("Запрос авторизации уже использован")
        conn.execute(
            """INSERT INTO login_tokens (token_hash, chel_id, expires_at, created_at)
            VALUES (?, ?, ?, ?)""",
            (_token_hash(raw_token), chel_id, expires_at.isoformat(), now.isoformat()),
        )
        conn.execute(
            """DELETE FROM login_tokens
            WHERE expires_at < ? AND used_at IS NULL""",
            ((now - timedelta(days=1)).isoformat(),),
        )
        conn.commit()
    return {"token": raw_token, "chel_id": chel_id, "expires_at": expires_at.isoformat()}


def create_max_login(max_user_id: int, legacy_chel_id: int) -> dict:
    """Backward-compatible login API for the existing MAX integration."""
    if max_user_id <= 0 or legacy_chel_id <= 0:
        raise ValueError("MAX ID и chel_id должны быть положительными числами")
    return create_messenger_login("max", max_user_id, legacy_chel_id=legacy_chel_id)


def consume_login_token(raw_token: str) -> dict | None:
    """Consume a one-time login token and create a durable browser session."""
    if not raw_token:
        return None
    now = datetime.now(timezone.utc)
    session_value = secrets.token_urlsafe(32)
    session_expires = now + timedelta(days=settings.session_ttl_days)
    with _write_lock, connection() as conn:
        token = conn.execute(
            "SELECT * FROM login_tokens WHERE token_hash = ?",
            (_token_hash(raw_token),),
        ).fetchone()
        if not token or token["used_at"] or datetime.fromisoformat(token["expires_at"]) <= now:
            return None
        updated = conn.execute(
            """UPDATE login_tokens SET used_at = ?
            WHERE token_hash = ? AND used_at IS NULL""",
            (now.isoformat(), token["token_hash"]),
        )
        if updated.rowcount != 1:
            return None
        conn.execute(
            """INSERT INTO user_sessions
            (session_hash, chel_id, created_at, last_seen_at, expires_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                _token_hash(session_value), token["chel_id"], now.isoformat(),
                now.isoformat(), session_expires.isoformat(),
            ),
        )
        conn.commit()
    return {
        "session": session_value,
        "chel_id": token["chel_id"],
        "expires_at": session_expires.isoformat(),
    }


def get_consumed_login_owner(raw_token: str) -> str | None:
    """Return the owner of an already-used link without authenticating anyone."""
    if not raw_token:
        return None
    with connection() as conn:
        token = conn.execute(
            """SELECT chel_id FROM login_tokens
            WHERE token_hash = ? AND used_at IS NOT NULL""",
            (_token_hash(raw_token),),
        ).fetchone()
    return token["chel_id"] if token else None


def get_session_chel_id(session_value: str) -> str | None:
    if not session_value:
        return None
    now = datetime.now(timezone.utc)
    with _write_lock, connection() as conn:
        session = conn.execute(
            """SELECT s.* FROM user_sessions s
            WHERE s.session_hash = ? AND s.revoked_at IS NULL
              AND EXISTS (
                SELECT 1 FROM external_identities e
                WHERE e.chel_id = s.chel_id AND e.access_status = 'active'
              )""",
            (_token_hash(session_value),),
        ).fetchone()
        if not session or datetime.fromisoformat(session["expires_at"]) <= now:
            return None
        conn.execute(
            "UPDATE user_sessions SET last_seen_at = ? WHERE session_hash = ?",
            (now.isoformat(), session["session_hash"]),
        )
        conn.execute(
            "UPDATE users SET last_seen_at = ? WHERE chel_id = ?",
            (now.isoformat(), session["chel_id"]),
        )
        conn.commit()
    return session["chel_id"]


def current_external_identity() -> dict | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM external_identities WHERE chel_id = ? ORDER BY id LIMIT 1",
            (current_chel_id(),),
        ).fetchone()
    return dict(row) if row else None


def current_external_identities() -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT provider, provider_user_id, access_status, created_at, last_login_at
            FROM external_identities
            WHERE chel_id = ? AND access_status = 'active' ORDER BY id""",
            (current_chel_id(),),
        ).fetchall()
    return [dict(row) for row in rows]


def record_ai_usage(item: dict) -> int:
    """Persist privacy-safe token counters and the cost-rate snapshot for one call."""
    integer_fields = (
        "input_tokens", "cached_input_tokens", "output_tokens",
        "reasoning_tokens", "total_tokens",
    )
    float_fields = (
        "input_rate", "cached_input_rate", "output_rate", "input_cost_usd",
        "cached_input_cost_usd", "output_cost_usd", "total_cost_usd",
    )
    values = {
        "chel_id": str(item.get("chel_id") or "")[:80],
        "operation": str(item.get("operation") or "other")[:60],
        "model": str(item.get("model") or "unknown")[:120],
        "pricing_key": str(item.get("pricing_key") or "")[:120],
        "pricing_known": 1 if item.get("pricing_known") else 0,
        "long_context": 1 if item.get("long_context") else 0,
    }
    for field in integer_fields:
        values[field] = max(0, int(item.get(field) or 0))
    for field in float_fields:
        values[field] = max(0.0, float(item.get(field) or 0))
    now = utc_now()
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            """INSERT INTO ai_usage (
                chel_id, operation, model, pricing_key, pricing_known, long_context,
                input_tokens, cached_input_tokens, output_tokens, reasoning_tokens,
                total_tokens, input_rate, cached_input_rate, output_rate,
                input_cost_usd, cached_input_cost_usd, output_cost_usd,
                total_cost_usd, created_at
            ) VALUES (
                :chel_id, :operation, :model, :pricing_key, :pricing_known, :long_context,
                :input_tokens, :cached_input_tokens, :output_tokens, :reasoning_tokens,
                :total_tokens, :input_rate, :cached_input_rate, :output_rate,
                :input_cost_usd, :cached_input_cost_usd, :output_cost_usd,
                :total_cost_usd, :created_at
            )""",
            {**values, "created_at": now},
        )
        conn.commit()
        return int(cursor.lastrowid)


def admin_ai_costs(period: str = "30", recent_limit: int = 100) -> dict:
    """Aggregate AI token usage without exposing prompts, replies, or medical data."""
    period = str(period or "30").strip().lower()
    if period not in {"today", "7", "30", "90", "all"}:
        raise ValueError("Неизвестный период расходов")
    recent_limit = max(10, min(250, int(recent_limit)))
    now = datetime.now(timezone.utc)
    if period == "today":
        started_at = now.date().isoformat()
        chart_days = 1
    elif period == "all":
        started_at = ""
        chart_days = 90
    else:
        days = int(period)
        started_at = (now - timedelta(days=days - 1)).date().isoformat()
        chart_days = days
    where = "created_at >= ?" if started_at else "1=1"
    params = (started_at,) if started_at else ()

    summary_sql = """SELECT COUNT(*) AS requests,
        COALESCE(SUM(input_tokens), 0) AS input_tokens,
        COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
        COALESCE(SUM(output_tokens), 0) AS output_tokens,
        COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
        COALESCE(SUM(total_tokens), 0) AS total_tokens,
        COALESCE(SUM(input_cost_usd), 0) AS input_cost_usd,
        COALESCE(SUM(cached_input_cost_usd), 0) AS cached_input_cost_usd,
        COALESCE(SUM(output_cost_usd), 0) AS output_cost_usd,
        COALESCE(SUM(total_cost_usd), 0) AS total_cost_usd,
        COALESCE(SUM(CASE WHEN pricing_known = 0 THEN 1 ELSE 0 END), 0) AS unpriced_requests
        FROM ai_usage"""

    def normalized_summary(row: sqlite3.Row) -> dict:
        result = dict(row)
        for key in (
            "requests", "input_tokens", "cached_input_tokens", "output_tokens",
            "reasoning_tokens", "total_tokens", "unpriced_requests",
        ):
            result[key] = int(result[key] or 0)
        for key in (
            "input_cost_usd", "cached_input_cost_usd", "output_cost_usd", "total_cost_usd",
        ):
            result[key] = round(float(result[key] or 0), 9)
        return result

    with connection() as conn:
        summary = normalized_summary(conn.execute(
            f"{summary_sql} WHERE {where}", params,
        ).fetchone())
        all_time = normalized_summary(conn.execute(summary_sql).fetchone())
        by_model = [dict(row) for row in conn.execute(
            f"""SELECT model, pricing_key, pricing_known, COUNT(*) AS requests,
                SUM(input_tokens) AS input_tokens,
                SUM(cached_input_tokens) AS cached_input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(total_tokens) AS total_tokens,
                SUM(total_cost_usd) AS total_cost_usd
            FROM ai_usage WHERE {where}
            GROUP BY model, pricing_key, pricing_known
            ORDER BY total_cost_usd DESC, total_tokens DESC""",
            params,
        ).fetchall()]
        by_operation = [dict(row) for row in conn.execute(
            f"""SELECT operation, COUNT(*) AS requests,
                SUM(total_tokens) AS total_tokens, SUM(total_cost_usd) AS total_cost_usd
            FROM ai_usage WHERE {where}
            GROUP BY operation ORDER BY total_cost_usd DESC, total_tokens DESC""",
            params,
        ).fetchall()]
        chart_start = (now - timedelta(days=chart_days - 1)).date().isoformat()
        daily_rows = conn.execute(
            """SELECT SUBSTR(created_at, 1, 10) AS day, COUNT(*) AS requests,
                SUM(total_tokens) AS total_tokens, SUM(total_cost_usd) AS total_cost_usd
            FROM ai_usage WHERE created_at >= ? GROUP BY day ORDER BY day""",
            (chart_start,),
        ).fetchall()
        daily_map = {row["day"]: dict(row) for row in daily_rows}
        daily = []
        first_day = date.fromisoformat(chart_start)
        for offset in range(chart_days):
            day = (first_day + timedelta(days=offset)).isoformat()
            item = daily_map.get(day, {})
            daily.append({
                "date": day,
                "requests": int(item.get("requests") or 0),
                "total_tokens": int(item.get("total_tokens") or 0),
                "total_cost_usd": round(float(item.get("total_cost_usd") or 0), 9),
            })
        recent = [dict(row) for row in conn.execute(
            f"""SELECT id, chel_id, operation, model, pricing_known, long_context,
                input_tokens, cached_input_tokens, output_tokens, reasoning_tokens,
                total_tokens, total_cost_usd, created_at
            FROM ai_usage WHERE {where} ORDER BY id DESC LIMIT ?""",
            (*params, recent_limit),
        ).fetchall()]

    from .ai_costs import public_pricing_catalog
    return {
        "generated_at": now.isoformat(),
        "period": period,
        "period_started_at": started_at or None,
        "chart_days": chart_days,
        "summary": summary,
        "all_time": all_time,
        "by_model": by_model,
        "by_operation": by_operation,
        "daily": daily,
        "recent": recent,
        "pricing": public_pricing_catalog(),
        "notice": (
            "Расчёт по токенам из ответов OpenAI API и сохранённым тарифам. "
            "Окончательная сумма определяется биллингом OpenAI."
        ),
    }


def admin_dashboard(days: int = 14, limit: int = 100) -> dict:
    """Return privacy-conscious aggregate analytics without message or profile text."""
    days = max(7, min(90, int(days)))
    limit = max(10, min(250, int(limit)))
    now = datetime.now(timezone.utc)
    first_day = (now - timedelta(days=days - 1)).date()
    real_users = "chel_id NOT IN ('chel_legacy', 'chel_test_default') AND registered_at IS NOT NULL"

    with connection() as conn:
        def scalar(query: str, params: tuple = ()) -> int:
            return int(conn.execute(query, params).fetchone()[0] or 0)

        summary = {
            "users_total": scalar(f"SELECT COUNT(*) FROM users WHERE {real_users}"),
            "users_active_7d": scalar(
                f"SELECT COUNT(*) FROM users WHERE {real_users} AND last_seen_at >= ?",
                ((now - timedelta(days=7)).isoformat(),),
            ),
            "messenger_users": scalar(
                """SELECT COUNT(DISTINCT e.chel_id) FROM external_identities e
                JOIN users u ON u.chel_id=e.chel_id
                WHERE e.access_status = 'active'
                  AND u.registered_at IS NOT NULL
                  AND e.chel_id NOT IN ('chel_legacy', 'chel_test_default')"""
            ),
            "onboarding_complete": scalar(
                """SELECT COUNT(*) FROM onboarding_state
                WHERE status = 'complete'
                  AND chel_id NOT IN ('chel_legacy', 'chel_test_default')"""
            ),
            "profiles_with_tube": scalar(
                """SELECT COUNT(*) FROM user_profile
                WHERE TRIM(tube_number) <> ''
                  AND chel_id NOT IN ('chel_legacy', 'chel_test_default')"""
            ),
            "conversations_total": scalar(
                """SELECT COUNT(*) FROM conversations
                WHERE chel_id NOT IN ('chel_legacy', 'chel_test_default')"""
            ),
            "messages_total": scalar(
                """SELECT COUNT(*) FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE c.chel_id NOT IN ('chel_legacy', 'chel_test_default')"""
            ),
            "human_requests": scalar(
                """SELECT COUNT(*) FROM conversations
                WHERE human_ticket_id IS NOT NULL
                  AND chel_id NOT IN ('chel_legacy', 'chel_test_default')"""
            ),
            "human_pending": scalar(
                """SELECT COUNT(*) FROM conversations
                WHERE human_status = 'pending'
                  AND chel_id NOT IN ('chel_legacy', 'chel_test_default')"""
            ),
        }

        created_rows = conn.execute(
            f"""SELECT SUBSTR(registered_at, 1, 10) AS day, COUNT(*) AS value
            FROM users WHERE {real_users} AND registered_at >= ?
            GROUP BY day""",
            (first_day.isoformat(),),
        ).fetchall()
        message_rows = conn.execute(
            """SELECT SUBSTR(m.created_at, 1, 10) AS day, COUNT(*) AS value
            FROM messages m JOIN conversations c ON c.id = m.conversation_id
            WHERE c.chel_id NOT IN ('chel_legacy', 'chel_test_default')
              AND m.created_at >= ? GROUP BY day""",
            (first_day.isoformat(),),
        ).fetchall()
        conversation_rows = conn.execute(
            """SELECT SUBSTR(created_at, 1, 10) AS day, COUNT(*) AS value
            FROM conversations
            WHERE chel_id NOT IN ('chel_legacy', 'chel_test_default')
              AND created_at >= ? GROUP BY day""",
            (first_day.isoformat(),),
        ).fetchall()
        created_by_day = {row["day"]: int(row["value"]) for row in created_rows}
        messages_by_day = {row["day"]: int(row["value"]) for row in message_rows}
        conversations_by_day = {row["day"]: int(row["value"]) for row in conversation_rows}
        activity = []
        for offset in range(days):
            day = (first_day + timedelta(days=offset)).isoformat()
            activity.append({
                "date": day,
                "new_users": created_by_day.get(day, 0),
                "conversations": conversations_by_day.get(day, 0),
                "messages": messages_by_day.get(day, 0),
            })

        agents = [
            {"agent": row["agent_id"] or "manager", "messages": int(row["value"])}
            for row in conn.execute(
                """SELECT m.agent_id, COUNT(*) AS value
                FROM messages m JOIN conversations c ON c.id = m.conversation_id
                WHERE m.role = 'assistant'
                  AND c.chel_id NOT IN ('chel_legacy', 'chel_test_default')
                GROUP BY m.agent_id ORDER BY value DESC"""
            ).fetchall()
        ]
        human_channels = [
            {"channel": row["channel"], "requests": int(row["value"])}
            for row in conn.execute(
                """SELECT COALESCE(human_channel, 'not_selected') AS channel, COUNT(*) AS value
                FROM conversations WHERE human_ticket_id IS NOT NULL
                  AND chel_id NOT IN ('chel_legacy', 'chel_test_default')
                GROUP BY channel ORDER BY value DESC"""
            ).fetchall()
        ]
        devices = [
            {
                "device_type": row["device_type"],
                "users": int(row["users"]),
                "visits": int(row["visits"]),
            }
            for row in conn.execute(
                """SELECT d.device_type, COUNT(DISTINCT d.chel_id) AS users,
                    SUM(d.visit_count) AS visits
                FROM user_device_stats d JOIN users u ON u.chel_id=d.chel_id
                WHERE d.chel_id NOT IN ('chel_legacy', 'chel_test_default')
                  AND u.registered_at IS NOT NULL
                GROUP BY d.device_type ORDER BY users DESC, visits DESC"""
            ).fetchall()
        ]
        operating_systems = [
            {
                "operating_system": row["operating_system"],
                "users": int(row["users"]),
                "visits": int(row["visits"]),
            }
            for row in conn.execute(
                """SELECT d.operating_system, COUNT(DISTINCT d.chel_id) AS users,
                    SUM(d.visit_count) AS visits
                FROM user_device_stats d JOIN users u ON u.chel_id=d.chel_id
                WHERE d.chel_id NOT IN ('chel_legacy', 'chel_test_default')
                  AND u.registered_at IS NOT NULL
                GROUP BY d.operating_system ORDER BY users DESC, visits DESC"""
            ).fetchall()
        ]
        browsers = [
            {
                "browser": row["browser"],
                "users": int(row["users"]),
                "visits": int(row["visits"]),
            }
            for row in conn.execute(
                """SELECT d.browser, COUNT(DISTINCT d.chel_id) AS users,
                    SUM(d.visit_count) AS visits
                FROM user_device_stats d JOIN users u ON u.chel_id=d.chel_id
                WHERE d.chel_id NOT IN ('chel_legacy', 'chel_test_default')
                  AND u.registered_at IS NOT NULL
                GROUP BY d.browser ORDER BY users DESC, visits DESC"""
            ).fetchall()
        ]
        summary["tracked_devices"] = scalar(
            """SELECT COUNT(DISTINCT d.chel_id) FROM user_device_stats d
            JOIN users u ON u.chel_id=d.chel_id
            WHERE d.chel_id NOT IN ('chel_legacy', 'chel_test_default')
              AND u.registered_at IS NOT NULL"""
        )

        users = [
            dict(row) for row in conn.execute(
                f"""SELECT u.chel_id, u.registered_at AS created_at, u.last_seen_at,
                    COALESCE(o.status, 'not_started') AS onboarding_status,
                    COALESCE(GROUP_CONCAT(DISTINCT e.provider), '') AS messengers,
                    COUNT(DISTINCT c.id) AS conversations,
                    COUNT(DISTINCT m.id) AS messages
                FROM users u
                LEFT JOIN onboarding_state o ON o.chel_id = u.chel_id
                LEFT JOIN external_identities e
                  ON e.chel_id = u.chel_id AND e.access_status = 'active'
                LEFT JOIN conversations c ON c.chel_id = u.chel_id
                LEFT JOIN messages m ON m.conversation_id = c.id
                WHERE u.{real_users}
                GROUP BY u.chel_id ORDER BY u.last_seen_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        ]
        conversations = [
            dict(row) for row in conn.execute(
                """SELECT c.id, c.chel_id, c.active_agent, c.status,
                    c.human_status, COALESCE(c.human_channel, '') AS human_channel,
                    c.created_at, c.updated_at, COUNT(m.id) AS messages
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                WHERE c.chel_id NOT IN ('chel_legacy', 'chel_test_default')
                GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        ]
        requests = []
        for row in conn.execute(
            """SELECT human_ticket_id AS ticket_id, chel_id, human_status,
                COALESCE(human_channel, '') AS channel, human_phone,
                created_at, updated_at
            FROM conversations WHERE human_ticket_id IS NOT NULL
              AND chel_id NOT IN ('chel_legacy', 'chel_test_default')
            ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall():
            item = dict(row)
            phone = str(item.pop("human_phone") or "")
            item["phone"] = f"•••• {phone[-4:]}" if len(phone) >= 4 else ""
            requests.append(item)

    return {
        "generated_at": now.isoformat(),
        "period_days": days,
        "summary": summary,
        "activity": activity,
        "agents": agents,
        "human_channels": human_channels,
        "devices": devices,
        "operating_systems": operating_systems,
        "browsers": browsers,
        "tables": {
            "users": users,
            "conversations": conversations,
            "human_requests": requests,
        },
        "privacy": {
            "message_content_included": False,
            "medical_profile_content_included": False,
        },
    }


def _admin_date(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field_name} должна быть датой в формате ГГГГ-ММ-ДД") from exc


def admin_table(
    name: str,
    query: str = "",
    limit: int = 25,
    offset: int = 0,
    created_from: str = "",
    created_to: str = "",
) -> dict:
    """Search and paginate a strict allowlist of non-medical admin table views."""
    name = str(name or "").strip()
    if name not in {"users", "conversations", "human_requests", "devices"}:
        raise ValueError("Неизвестная таблица дашборда")
    query = " ".join(str(query or "").split())[:120]
    limit = max(10, min(100, int(limit)))
    offset = max(0, min(1_000_000, int(offset)))
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    created_from = _admin_date(created_from, "Начальная дата")
    created_to = _admin_date(created_to, "Конечная дата")
    if created_from and created_to and created_from > created_to:
        raise ValueError("Начальная дата не может быть позже конечной")
    overall_total = None
    period_total = None

    with connection() as conn:
        if name == "users":
            where = """u.chel_id NOT IN ('chel_legacy', 'chel_test_default')
                AND u.registered_at IS NOT NULL"""
            params: list = []
            period_where = where
            period_params: list = []
            if created_from:
                period_where += " AND SUBSTR(u.registered_at, 1, 10) >= ?"
                period_params.append(created_from)
            if created_to:
                period_where += " AND SUBSTR(u.registered_at, 1, 10) <= ?"
                period_params.append(created_to)
            where = period_where
            params.extend(period_params)
            if query:
                where += """ AND (
                    u.chel_id LIKE ? ESCAPE '\\'
                    OR COALESCE(o.status, 'not_started') LIKE ? ESCAPE '\\'
                    OR COALESCE(e.provider, 'нет') LIKE ? ESCAPE '\\'
                )"""
                params.extend([pattern, pattern, pattern])
            overall_total = conn.execute(
                """SELECT COUNT(*) FROM users
                WHERE chel_id NOT IN ('chel_legacy', 'chel_test_default')
                  AND registered_at IS NOT NULL"""
            ).fetchone()[0]
            period_total = conn.execute(
                f"SELECT COUNT(*) FROM users u WHERE {period_where}",
                tuple(period_params),
            ).fetchone()[0]
            total = conn.execute(
                f"""SELECT COUNT(DISTINCT u.chel_id)
                FROM users u
                LEFT JOIN onboarding_state o ON o.chel_id = u.chel_id
                LEFT JOIN external_identities e
                  ON e.chel_id = u.chel_id AND e.access_status = 'active'
                WHERE {where}""",
                tuple(params),
            ).fetchone()[0]
            rows = [
                dict(row) for row in conn.execute(
                    f"""SELECT u.chel_id, u.registered_at AS created_at, u.last_seen_at,
                        COALESCE(o.status, 'not_started') AS onboarding_status,
                        COALESCE(GROUP_CONCAT(DISTINCT e.provider), '') AS messengers,
                        COUNT(DISTINCT c.id) AS conversations,
                        COUNT(DISTINCT m.id) AS messages
                    FROM users u
                    LEFT JOIN onboarding_state o ON o.chel_id = u.chel_id
                    LEFT JOIN external_identities e
                      ON e.chel_id = u.chel_id AND e.access_status = 'active'
                    LEFT JOIN conversations c ON c.chel_id = u.chel_id
                    LEFT JOIN messages m ON m.conversation_id = c.id
                    WHERE {where}
                    GROUP BY u.chel_id ORDER BY u.last_seen_at DESC LIMIT ? OFFSET ?""",
                    tuple(params + [limit, offset]),
                ).fetchall()
            ]
        elif name == "conversations":
            where = """c.chel_id NOT IN ('chel_legacy', 'chel_test_default')"""
            params = []
            if query:
                where += """ AND (
                    c.id LIKE ? ESCAPE '\\' OR c.chel_id LIKE ? ESCAPE '\\'
                    OR c.active_agent LIKE ? ESCAPE '\\' OR c.status LIKE ? ESCAPE '\\'
                    OR c.human_status LIKE ? ESCAPE '\\'
                    OR COALESCE(c.human_channel, '') LIKE ? ESCAPE '\\'
                    OR COALESCE(c.human_ticket_id, '') LIKE ? ESCAPE '\\'
                )"""
                params.extend([pattern] * 7)
            total = conn.execute(
                f"SELECT COUNT(*) FROM conversations c WHERE {where}",
                tuple(params),
            ).fetchone()[0]
            rows = [
                dict(row) for row in conn.execute(
                    f"""SELECT c.id, c.chel_id, c.active_agent, c.status,
                        c.human_status, COALESCE(c.human_channel, '') AS human_channel,
                        c.created_at, c.updated_at, COUNT(m.id) AS messages
                    FROM conversations c
                    LEFT JOIN messages m ON m.conversation_id = c.id
                    WHERE {where}
                    GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ? OFFSET ?""",
                    tuple(params + [limit, offset]),
                ).fetchall()
            ]
        elif name == "human_requests":
            where = """c.human_ticket_id IS NOT NULL
                AND c.chel_id NOT IN ('chel_legacy', 'chel_test_default')"""
            params = []
            if query:
                where += """ AND (
                    c.human_ticket_id LIKE ? ESCAPE '\\' OR c.chel_id LIKE ? ESCAPE '\\'
                    OR c.human_status LIKE ? ESCAPE '\\'
                    OR COALESCE(c.human_channel, '') LIKE ? ESCAPE '\\'
                    OR COALESCE(c.human_phone, '') LIKE ? ESCAPE '\\'
                )"""
                params.extend([pattern] * 5)
            total = conn.execute(
                f"SELECT COUNT(*) FROM conversations c WHERE {where}",
                tuple(params),
            ).fetchone()[0]
            rows = []
            for row in conn.execute(
                f"""SELECT c.human_ticket_id AS ticket_id, c.chel_id,
                    c.human_status, COALESCE(c.human_channel, '') AS channel,
                    c.human_phone, c.created_at, c.updated_at
                FROM conversations c WHERE {where}
                ORDER BY c.updated_at DESC LIMIT ? OFFSET ?""",
                tuple(params + [limit, offset]),
            ).fetchall():
                item = dict(row)
                phone = str(item.pop("human_phone") or "")
                item["phone"] = f"•••• {phone[-4:]}" if len(phone) >= 4 else ""
                rows.append(item)
        else:
            where = """d.chel_id NOT IN ('chel_legacy', 'chel_test_default')
                AND EXISTS (
                    SELECT 1 FROM users u
                    WHERE u.chel_id=d.chel_id AND u.registered_at IS NOT NULL
                )"""
            params = []
            if query:
                where += """ AND (
                    d.chel_id LIKE ? ESCAPE '\\'
                    OR d.device_type LIKE ? ESCAPE '\\'
                    OR d.operating_system LIKE ? ESCAPE '\\'
                    OR d.browser LIKE ? ESCAPE '\\'
                )"""
                params.extend([pattern] * 4)
            total = conn.execute(
                f"SELECT COUNT(*) FROM user_device_stats d WHERE {where}",
                tuple(params),
            ).fetchone()[0]
            rows = [
                dict(row) for row in conn.execute(
                    f"""SELECT d.chel_id, d.device_type, d.operating_system,
                        d.browser, d.first_seen_at, d.last_seen_at, d.visit_count
                    FROM user_device_stats d WHERE {where}
                    ORDER BY d.last_seen_at DESC LIMIT ? OFFSET ?""",
                    tuple(params + [limit, offset]),
                ).fetchall()
            ]

    return {
        "table": name,
        "query": query,
        "limit": limit,
        "offset": offset,
        "total": int(total or 0),
        "overall_total": int(overall_total or 0) if overall_total is not None else None,
        "period_total": int(period_total or 0) if period_total is not None else None,
        "created_from": created_from,
        "created_to": created_to,
        "rows": rows,
    }


def _manager_message(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        item["metadata"] = json.loads(item.get("metadata") or "{}")
    except json.JSONDecodeError:
        item["metadata"] = {}
    return item


def _manager_profile(conn: sqlite3.Connection, chel_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM user_profile WHERE chel_id = ?", (chel_id,),
    ).fetchone()
    if not row:
        return {
            "chel_id": chel_id, "preferred_name": "", "company_inn": "", "age": None, "sex": "",
            "height_cm": None, "weight_kg": None, "pregnancy": "not_applicable",
            "conditions": [], "medications": [], "allergies": [],
            "smoking": "unknown", "alcohol": "unknown", "activity": "unknown",
            "blood_pressure": "unknown", "blood_sugar": "unknown",
            "dark_in_eyes": "unknown", "joint_pain": "unknown", "fatigue": "unknown",
            "tube_number": "", "notes": "", "updated_at": None,
        }
    result = dict(row)
    for key in ("conditions", "medications", "allergies"):
        try:
            result[key] = json.loads(result.get(key) or "[]")
        except json.JSONDecodeError:
            result[key] = []
    return result


def _staff_messenger_id(value, label: str) -> str:
    value = str(value or "").strip()
    if value and (not value.isdigit() or len(value) > 30):
        raise ValueError(f"{label} должен состоять только из цифр")
    return value


def admin_list_staff() -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT id, display_name, login, is_active, telegram_id, max_id,
            notify_new_requests, notify_new_messages, created_at, updated_at,
            last_login_at FROM staff_users ORDER BY is_active DESC, display_name"""
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            linked_ids = conn.execute(
                """SELECT provider, chel_id FROM external_identities
                WHERE (? <> '' AND provider = 'telegram' AND provider_user_id = ?)
                   OR (? <> '' AND provider = 'max' AND provider_user_id = ?)
                ORDER BY provider""",
                (
                    item["telegram_id"], item["telegram_id"],
                    item["max_id"], item["max_id"],
                ),
            ).fetchall()
            item["user_identities"] = [dict(identity) for identity in linked_ids]
            item["user_chel_ids"] = list(dict.fromkeys(
                identity["chel_id"] for identity in linked_ids
            ))
            item["is_active"] = bool(item["is_active"])
            item["notify_new_requests"] = bool(item["notify_new_requests"])
            item["notify_new_messages"] = bool(item["notify_new_messages"])
            result.append(item)
    return result


def admin_create_staff(
    display_name: str, login: str, password: str, *, telegram_id="", max_id="",
    notify_new_requests: bool = True, notify_new_messages: bool = True,
) -> dict:
    display_name = " ".join(str(display_name or "").split())[:80]
    if len(display_name) < 2:
        raise ValueError("Укажите имя менеджера")
    login = _staff_login(login)
    encoded = _password_hash(password)
    telegram_id = _staff_messenger_id(telegram_id, "Telegram ID")
    max_id = _staff_messenger_id(max_id, "MAX ID")
    now = utc_now()
    try:
        with _write_lock, connection() as conn:
            cursor = conn.execute(
                """INSERT INTO staff_users
                (display_name, login, password_hash, is_active, telegram_id,
                 telegram_chat_id, max_id, max_chat_id, notify_new_requests,
                 notify_new_messages, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    display_name, login, encoded, telegram_id, telegram_id,
                    max_id, max_id, int(bool(notify_new_requests)),
                    int(bool(notify_new_messages)), now, now,
                ),
            )
            conn.commit()
            staff_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        details = str(exc).lower()
        if "telegram_id" in details:
            raise ValueError("Этот Telegram ID уже привязан к другому менеджеру") from exc
        if "max_id" in details:
            raise ValueError("Этот MAX ID уже привязан к другому менеджеру") from exc
        raise ValueError("Менеджер с таким логином уже существует") from exc
    return next(item for item in admin_list_staff() if item["id"] == staff_id)


def admin_update_staff(
    staff_id: int, *, display_name: str | None = None,
    password: str | None = None, is_active: bool | None = None,
    telegram_id: str | None = None, max_id: str | None = None,
    notify_new_requests: bool | None = None,
    notify_new_messages: bool | None = None,
) -> dict | None:
    updates: list[str] = []
    params: list = []
    if display_name is not None:
        name = " ".join(str(display_name).split())[:80]
        if len(name) < 2:
            raise ValueError("Укажите имя менеджера")
        updates.append("display_name = ?")
        params.append(name)
    if password is not None and str(password):
        updates.append("password_hash = ?")
        params.append(_password_hash(password))
    if is_active is not None:
        updates.append("is_active = ?")
        params.append(int(bool(is_active)))
    for column, value, label in (
        ("telegram_id", telegram_id, "Telegram ID"),
        ("max_id", max_id, "MAX ID"),
    ):
        if value is not None:
            messenger_id = _staff_messenger_id(value, label)
            updates.extend([f"{column} = ?", f"{column.removesuffix('_id')}_chat_id = ?"])
            params.extend([messenger_id, messenger_id])
    if notify_new_requests is not None:
        updates.append("notify_new_requests = ?")
        params.append(int(bool(notify_new_requests)))
    if notify_new_messages is not None:
        updates.append("notify_new_messages = ?")
        params.append(int(bool(notify_new_messages)))
    if not updates:
        raise ValueError("Нет изменений")
    updates.append("updated_at = ?")
    params.append(utc_now())
    params.append(int(staff_id))
    with _write_lock, connection() as conn:
        for column, value, label in (
            ("telegram_id", telegram_id, "Telegram ID"),
            ("max_id", max_id, "MAX ID"),
        ):
            normalized = _staff_messenger_id(value, label) if value is not None else ""
            if normalized and conn.execute(
                f"SELECT 1 FROM staff_users WHERE {column} = ? AND id <> ?",
                (normalized, int(staff_id)),
            ).fetchone():
                raise ValueError(f"Этот {label} уже привязан к другому менеджеру")
        if telegram_id is not None:
            conn.execute(
                "DELETE FROM manager_notification_outbox WHERE staff_user_id = ? AND provider = 'telegram' AND status <> 'sent'",
                (int(staff_id),),
            )
        if max_id is not None:
            conn.execute(
                "DELETE FROM manager_notification_outbox WHERE staff_user_id = ? AND provider = 'max' AND status <> 'sent'",
                (int(staff_id),),
            )
        cursor = conn.execute(
            f"UPDATE staff_users SET {', '.join(updates)} WHERE id = ?", tuple(params),
        )
        if not cursor.rowcount:
            return None
        if is_active is False or password:
            conn.execute(
                "UPDATE staff_sessions SET revoked_at = ? WHERE staff_user_id = ? AND revoked_at IS NULL",
                (utc_now(), int(staff_id)),
            )
        conn.commit()
    return next(item for item in admin_list_staff() if item["id"] == int(staff_id))


def admin_delete_staff(staff_id: int) -> bool:
    """Permanently remove a manager account and all of its active sessions."""
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            "DELETE FROM staff_users WHERE id = ?", (int(staff_id),),
        )
        conn.commit()
    return bool(cursor.rowcount)


def create_staff_messenger_token(staff_id: int, provider: str) -> dict:
    provider = str(provider or "").strip().lower()
    if provider not in {"telegram", "max"}:
        raise ValueError("Выберите Telegram или MAX")
    token = f"mgr_{secrets.token_urlsafe(24)}"
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=7)
    with _write_lock, connection() as conn:
        staff = conn.execute(
            "SELECT id, display_name FROM staff_users WHERE id = ? AND is_active = 1",
            (int(staff_id),),
        ).fetchone()
        if not staff:
            raise ValueError("Активный менеджер не найден")
        conn.execute(
            "DELETE FROM staff_messenger_tokens WHERE staff_user_id = ? AND provider = ? AND used_at IS NULL",
            (int(staff_id), provider),
        )
        conn.execute(
            """INSERT INTO staff_messenger_tokens
            (token_hash, staff_user_id, provider, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (_token_hash(token), int(staff_id), provider, expires_at.isoformat(), now.isoformat()),
        )
        conn.commit()
    return {
        "token": token, "provider": provider, "staff_id": int(staff_id),
        "display_name": staff["display_name"], "expires_at": expires_at.isoformat(),
    }


def bind_staff_messenger(
    token: str, provider: str, provider_user_id, chat_id=None,
) -> dict:
    provider = str(provider or "").strip().lower()
    if provider not in {"telegram", "max"}:
        raise ValueError("Неизвестный мессенджер")
    provider_user_id = _staff_messenger_id(provider_user_id, f"{provider.upper()} ID")
    chat_id = _staff_messenger_id(chat_id or provider_user_id, "ID чата")
    now = datetime.now(timezone.utc)
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT t.token_hash, t.staff_user_id, t.provider, t.expires_at,
                t.used_at, s.display_name, s.is_active
            FROM staff_messenger_tokens t
            JOIN staff_users s ON s.id = t.staff_user_id
            WHERE t.token_hash = ?""",
            (_token_hash(str(token or "")),),
        ).fetchone()
        if not row or row["provider"] != provider:
            raise ValueError("Ссылка привязки недействительна")
        if row["used_at"]:
            raise ValueError("Ссылка привязки уже использована")
        if datetime.fromisoformat(row["expires_at"]) < now:
            raise ValueError("Срок действия ссылки привязки истёк")
        if not row["is_active"]:
            raise ValueError("Учётная запись менеджера отключена")
        id_column = "telegram_id" if provider == "telegram" else "max_id"
        chat_column = "telegram_chat_id" if provider == "telegram" else "max_chat_id"
        duplicate = conn.execute(
            f"SELECT id FROM staff_users WHERE {id_column} = ? AND id <> ?",
            (provider_user_id, row["staff_user_id"]),
        ).fetchone()
        if duplicate:
            raise ValueError("Этот аккаунт мессенджера уже привязан к другому менеджеру")
        conn.execute(
            f"UPDATE staff_users SET {id_column} = ?, {chat_column} = ?, updated_at = ? WHERE id = ?",
            (provider_user_id, chat_id, now.isoformat(), row["staff_user_id"]),
        )
        conn.execute(
            "DELETE FROM manager_notification_outbox WHERE staff_user_id = ? AND provider = ? AND status <> 'sent'",
            (row["staff_user_id"], provider),
        )
        conn.execute(
            "UPDATE staff_messenger_tokens SET used_at = ? WHERE token_hash = ?",
            (now.isoformat(), row["token_hash"]),
        )
        conn.commit()
    return {
        "staff_id": row["staff_user_id"], "display_name": row["display_name"],
        "provider": provider, "provider_user_id": provider_user_id,
    }


def enqueue_manager_notifications(
    event_type: str, conversation_id: str, *, message_id: int = 0,
    message_text: str = "",
) -> int:
    if event_type not in {"new_request", "new_message"}:
        raise ValueError("Неизвестный тип уведомления")
    now = utc_now()
    with _write_lock, connection() as conn:
        conversation = conn.execute(
            """SELECT c.id, c.chel_id, c.human_ticket_id, c.human_channel,
                COALESCE(p.preferred_name, '') AS preferred_name
            FROM conversations c LEFT JOIN user_profile p ON p.chel_id = c.chel_id
            WHERE c.id = ?""",
            (conversation_id,),
        ).fetchone()
        if not conversation:
            return 0
        manager_url = f"{settings.public_base_url}/manager?conversation={conversation_id}"
        name = conversation["preferred_name"] or f"Пользователь {conversation['chel_id'][-6:]}"
        if event_type == "new_request":
            title = "Новое обращение в Консилиуме"
            body = (
                f"{name} просит подключить человека. "
                f"Обращение {conversation['human_ticket_id'] or conversation_id[:8]}."
            )
            preference = "notify_new_requests"
        else:
            title = "Новое сообщение пользователя"
            body = f"{name} отправил новое сообщение. Откройте защищённую панель менеджера."
            preference = "notify_new_messages"
        recipients = conn.execute(
            f"""SELECT id, telegram_chat_id, max_chat_id FROM staff_users
            WHERE is_active = 1 AND {preference} = 1"""
        ).fetchall()
        inserted = 0
        payload = json.dumps({
            "title": title, "body": body, "manager_url": manager_url,
            "conversation_id": conversation_id,
            "ticket_id": conversation["human_ticket_id"] or "",
        }, ensure_ascii=False)
        for staff in recipients:
            for provider, recipient in (
                ("telegram", staff["telegram_chat_id"]), ("max", staff["max_chat_id"]),
            ):
                if not recipient:
                    continue
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO manager_notification_outbox
                    (staff_user_id, provider, recipient_id, event_type,
                     conversation_id, source_message_id, payload, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    (
                        staff["id"], provider, recipient, event_type, conversation_id,
                        int(message_id or 0), payload, now,
                    ),
                )
                inserted += int(bool(cursor.rowcount))
        conn.commit()
    return inserted


def claim_manager_notifications(provider: str, limit: int = 20) -> list[dict]:
    provider = str(provider or "").strip().lower()
    if provider not in {"telegram", "max"}:
        raise ValueError("Неизвестный мессенджер")
    limit = max(1, min(50, int(limit)))
    now = datetime.now(timezone.utc)
    stale_before = (now - timedelta(minutes=2)).isoformat()
    result = []
    with _write_lock, connection() as conn:
        rows = conn.execute(
            """SELECT id, recipient_id, event_type, conversation_id, payload, attempts
            FROM manager_notification_outbox
            WHERE provider = ? AND attempts < 100
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
              AND (
                status = 'pending' OR (status = 'delivering' AND leased_at < ?)
            ) ORDER BY id LIMIT ?""",
            (provider, now.isoformat(), stale_before, limit),
        ).fetchall()
        for row in rows:
            lease_token = secrets.token_urlsafe(18)
            conn.execute(
                """UPDATE manager_notification_outbox
                SET status = 'delivering', lease_token = ?, leased_at = ?, attempts = attempts + 1
                WHERE id = ?""",
                (lease_token, now.isoformat(), row["id"]),
            )
            item = dict(row)
            item["lease_token"] = lease_token
            item["payload"] = json.loads(item["payload"] or "{}")
            result.append(item)
        conn.commit()
    return result


def acknowledge_manager_notification(
    notification_id: int, lease_token: str, success: bool, error: str = "",
) -> bool:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT attempts FROM manager_notification_outbox
            WHERE id = ? AND lease_token = ? AND status = 'delivering'""",
            (int(notification_id), str(lease_token or "")),
        ).fetchone()
        if not row:
            return False
        retry_delay = min(3600, 5 * (2 ** min(int(row["attempts"]), 10)))
        next_attempt_at = None if success else (now_dt + timedelta(seconds=retry_delay)).isoformat()
        cursor = conn.execute(
            """UPDATE manager_notification_outbox SET status = ?, sent_at = ?,
                last_error = ?, next_attempt_at = ?, lease_token = NULL, leased_at = NULL
            WHERE id = ? AND lease_token = ? AND status = 'delivering'""",
            (
                "sent" if success else "pending", now if success else None,
                str(error or "")[:500], next_attempt_at,
                int(notification_id), str(lease_token or ""),
            ),
        )
        conn.commit()
    return bool(cursor.rowcount)


def list_examinations() -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT id, name, description, includes, price, created_at, updated_at
            FROM examination_catalog ORDER BY created_at, name"""
        ).fetchall()
    return [dict(row) for row in rows]


def _validate_examination(
    name: str, description: str, includes: str, price,
) -> tuple[str, str, str, int]:
    name = " ".join(str(name or "").split())[:140]
    description = " ".join(str(description or "").split())[:1200]
    includes = " ".join(str(includes or "").split())[:1200]
    if len(name) < 2:
        raise ValueError("Название должно содержать минимум 2 символа")
    if len(description) < 5:
        raise ValueError("Добавьте понятное описание обследования")
    try:
        normalized_price = int(price)
    except (ValueError, TypeError) as exc:
        raise ValueError("Цена должна быть целым числом") from exc
    if normalized_price < 0 or normalized_price > 10_000_000:
        raise ValueError("Цена должна быть от 0 до 10 000 000 рублей")
    return name, description, includes, normalized_price


def admin_create_examination(
    name: str, description: str, includes: str, price,
) -> dict:
    name, description, includes, price = _validate_examination(
        name, description, includes, price,
    )
    examination_id = f"exam_{secrets.token_hex(8)}"
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO examination_catalog
            (id, name, description, includes, price, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (examination_id, name, description, includes, price, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM examination_catalog WHERE id = ?", (examination_id,),
        ).fetchone()
    return dict(row)


def admin_update_examination(
    examination_id: str, name: str, description: str, includes: str, price,
) -> dict | None:
    examination_id = str(examination_id or "").strip()
    name, description, includes, price = _validate_examination(
        name, description, includes, price,
    )
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            """UPDATE examination_catalog
            SET name = ?, description = ?, includes = ?, price = ?, updated_at = ?
            WHERE id = ?""",
            (name, description, includes, price, utc_now(), examination_id),
        )
        if not cursor.rowcount:
            return None
        conn.commit()
        row = conn.execute(
            "SELECT * FROM examination_catalog WHERE id = ?", (examination_id,),
        ).fetchone()
    return dict(row)


def admin_delete_examination(examination_id: str) -> bool:
    examination_id = str(examination_id or "").strip()
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            "DELETE FROM examination_catalog WHERE id = ?", (examination_id,),
        )
        conn.commit()
    return bool(cursor.rowcount)


def authenticate_staff(login: str, password: str) -> dict | None:
    try:
        normalized = _staff_login(login)
    except ValueError:
        normalized = ""
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM staff_users WHERE login = ?", (normalized,),
        ).fetchone()
    encoded = row["password_hash"] if row else _password_hash("dummy-password-value")
    valid = _password_valid(password, encoded)
    if not row or not valid or not row["is_active"]:
        return None
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=12)
    token = secrets.token_urlsafe(32)
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO staff_sessions
            (token_hash, staff_user_id, created_at, last_seen_at, expires_at)
            VALUES (?, ?, ?, ?, ?)""",
            (_token_hash(token), row["id"], now.isoformat(), now.isoformat(), expires_at.isoformat()),
        )
        conn.execute(
            "UPDATE staff_users SET last_login_at = ?, updated_at = ? WHERE id = ?",
            (now.isoformat(), now.isoformat(), row["id"]),
        )
        conn.commit()
    return {
        "token": token, "expires_at": expires_at.isoformat(),
        "user": {"id": row["id"], "display_name": row["display_name"], "login": row["login"]},
    }


def get_staff_session(token: str) -> dict | None:
    if not token:
        return None
    now = datetime.now(timezone.utc)
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT s.token_hash, s.expires_at, s.revoked_at, u.id, u.display_name,
            u.login, u.is_active FROM staff_sessions s
            JOIN staff_users u ON u.id = s.staff_user_id WHERE s.token_hash = ?""",
            (_token_hash(token),),
        ).fetchone()
        if (
            not row or row["revoked_at"] or not row["is_active"]
            or datetime.fromisoformat(row["expires_at"]) <= now
        ):
            return None
        conn.execute(
            "UPDATE staff_sessions SET last_seen_at = ? WHERE token_hash = ?",
            (now.isoformat(), row["token_hash"]),
        )
        conn.commit()
    return {"id": row["id"], "display_name": row["display_name"], "login": row["login"]}


def revoke_staff_session(token: str) -> None:
    if not token:
        return
    with _write_lock, connection() as conn:
        conn.execute(
            "UPDATE staff_sessions SET revoked_at = ? WHERE token_hash = ?",
            (utc_now(), _token_hash(token)),
        )
        conn.commit()


def manager_list_conversations(
    query: str = "", queue: str = "open", limit: int = 100,
) -> list[dict]:
    """Return the human-support queue. This deliberately contains sensitive data."""
    query = " ".join(str(query or "").split())[:120]
    queue = str(queue or "open")
    if queue not in {"open", "all", "ai_off"}:
        raise ValueError("Неизвестный фильтр очереди")
    limit = max(10, min(250, int(limit)))
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    where = ["c.chel_id NOT IN ('chel_legacy', 'chel_test_default')"]
    params: list = []
    if queue == "open":
        where.append("(c.human_ticket_id IS NOT NULL OR c.ai_enabled = 0)")
        where.append("c.human_status IN ('pending', 'connected')")
    elif queue == "ai_off":
        where.append("c.ai_enabled = 0")
    if query:
        where.append("""(
            c.id LIKE ? ESCAPE '\\' OR c.chel_id LIKE ? ESCAPE '\\'
            OR COALESCE(c.human_ticket_id, '') LIKE ? ESCAPE '\\'
            OR COALESCE(p.preferred_name, '') LIKE ? ESCAPE '\\'
            OR COALESCE(p.company_inn, '') LIKE ? ESCAPE '\\'
            OR COALESCE(c.human_phone, '') LIKE ? ESCAPE '\\'
        )""")
        params.extend([pattern] * 6)
    where_sql = " AND ".join(where)
    with connection() as conn:
        rows = conn.execute(
            f"""SELECT c.id, c.chel_id, c.title, c.active_agent, c.status,
                c.ai_enabled, c.human_status, c.human_ticket_id,
                COALESCE(c.human_channel, '') AS human_channel,
                c.human_phone, c.created_at, c.updated_at,
                COALESCE(p.preferred_name, '') AS preferred_name,
                (SELECT content FROM messages lm
                 WHERE lm.conversation_id = c.id ORDER BY lm.id DESC LIMIT 1) AS last_message,
                (SELECT COUNT(*) FROM messages um
                 WHERE um.conversation_id = c.id AND um.role = 'user'
                   AND um.id > COALESCE((
                     SELECT MAX(hm.id) FROM messages hm
                     WHERE hm.conversation_id = c.id
                       AND json_extract(hm.metadata, '$.sender_type') = 'human_manager'
                   ), 0)) AS unanswered_user_messages
            FROM conversations c
            LEFT JOIN user_profile p ON p.chel_id = c.chel_id
            WHERE {where_sql}
            ORDER BY CASE WHEN c.ai_enabled = 0 THEN 0 ELSE 1 END,
                     c.updated_at DESC LIMIT ?""",
            tuple(params + [limit]),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["ai_enabled"] = bool(item["ai_enabled"])
        item["last_message"] = str(item.get("last_message") or "")[:180]
        result.append(item)
    return result


def manager_conversation_detail(conversation_id: str) -> dict | None:
    """Return a complete manager view for one conversation and its owner."""
    with connection() as conn:
        conversation_row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,),
        ).fetchone()
        if not conversation_row:
            return None
        conversation = dict(conversation_row)
        conversation["ai_enabled"] = bool(conversation.get("ai_enabled", 1))
        chel_id = conversation["chel_id"]
        messages = [
            _manager_message(row) for row in conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        ]
        symptoms = [
            dict(row) for row in conn.execute(
                "SELECT * FROM body_symptoms WHERE chel_id = ? ORDER BY created_at DESC LIMIT 100",
                (chel_id,),
            ).fetchall()
        ]
        memories = [
            dict(row) for row in conn.execute(
                "SELECT * FROM memories WHERE chel_id = ? ORDER BY updated_at DESC LIMIT 100",
                (chel_id,),
            ).fetchall()
        ]
        onboarding_row = conn.execute(
            "SELECT * FROM onboarding_state WHERE chel_id = ?", (chel_id,),
        ).fetchone()
        onboarding = dict(onboarding_row) if onboarding_row else {}
        try:
            onboarding["selected_tests"] = json.loads(onboarding.get("selected_tests") or "[]")
        except json.JSONDecodeError:
            onboarding["selected_tests"] = []
        interpretations = []
        for row in conn.execute(
            """SELECT med_id, scope_key, source_urls, interpretation, agent_id,
                created_at, updated_at FROM lab_interpretations
            WHERE chel_id = ? ORDER BY updated_at DESC LIMIT 50""",
            (chel_id,),
        ).fetchall():
            item = dict(row)
            try:
                item["source_urls"] = json.loads(item.get("source_urls") or "[]")
            except json.JSONDecodeError:
                item["source_urls"] = []
            interpretations.append(item)
        identities = [
            dict(row) for row in conn.execute(
                """SELECT provider, provider_user_id, access_status, last_login_at
                FROM external_identities WHERE chel_id = ? ORDER BY provider""",
                (chel_id,),
            ).fetchall()
        ]
        actions = []
        for row in conn.execute(
            """SELECT manager_name, action, details, created_at FROM manager_actions
            WHERE conversation_id = ? ORDER BY id DESC LIMIT 50""",
            (conversation_id,),
        ).fetchall():
            item = dict(row)
            try:
                item["details"] = json.loads(item.get("details") or "{}")
            except json.JSONDecodeError:
                item["details"] = {}
            actions.append(item)

    lab_documents: list[dict] = []
    seen_urls: set[str] = set()
    for message in messages:
        metadata = message.get("metadata") or {}
        for document in metadata.get("lab_result_documents", []) or []:
            url = str(document.get("url") or "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                lab_documents.append(document)
    profile = _manager_profile_from_value(conversation["chel_id"])
    return {
        "conversation": conversation,
        "messages": messages,
        "profile": profile,
        "symptoms": symptoms,
        "memories": memories,
        "onboarding": onboarding,
        "lab": {
            "tube_number": profile.get("tube_number", ""),
            "documents": lab_documents,
            "interpretations": interpretations,
        },
        "identities": identities,
        "manager_actions": actions,
    }


def _manager_profile_from_value(chel_id: str) -> dict:
    with connection() as conn:
        return _manager_profile(conn, chel_id)


def manager_add_reply(
    conversation_id: str, content: str, manager_name: str,
) -> dict:
    content = str(content or "").strip()
    manager_name = " ".join(str(manager_name or "").split())[:80] or "Менеджер"
    if not content or len(content) > 12_000:
        raise ValueError("Ответ должен содержать от 1 до 12000 символов")
    now = utc_now()
    metadata = {
        "sender_type": "human_manager",
        "manager_name": manager_name,
        "action": "manager_reply",
    }
    with _write_lock, connection() as conn:
        conversation = conn.execute(
            "SELECT id FROM conversations WHERE id = ?", (conversation_id,),
        ).fetchone()
        if not conversation:
            raise ValueError("Диалог не найден")
        cursor = conn.execute(
            """INSERT INTO messages
            (conversation_id, role, agent_id, content, metadata, created_at)
            VALUES (?, 'assistant', 'manager', ?, ?, ?)""",
            (conversation_id, content, json.dumps(metadata, ensure_ascii=False), now),
        )
        conn.execute(
            """UPDATE conversations SET human_status = 'connected',
            status = 'waiting_human', updated_at = ? WHERE id = ?""",
            (now, conversation_id),
        )
        conn.execute(
            """INSERT INTO manager_actions
            (conversation_id, manager_name, action, details, created_at)
            VALUES (?, ?, 'reply', ?, ?)""",
            (conversation_id, manager_name, json.dumps({"message_id": cursor.lastrowid}), now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,),
        ).fetchone()
    return _manager_message(row)


def manager_set_ai_enabled(
    conversation_id: str, enabled: bool, manager_name: str,
) -> dict | None:
    manager_name = " ".join(str(manager_name or "").split())[:80] or "Менеджер"
    now = utc_now()
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            """UPDATE conversations SET ai_enabled = ?, human_status = CASE
                WHEN ? = 0 THEN 'connected'
                WHEN human_ticket_id IS NOT NULL THEN 'pending'
                ELSE human_status END,
                status = CASE WHEN ? = 0 THEN 'waiting_human' ELSE 'active' END,
                updated_at = ? WHERE id = ?""",
            (int(bool(enabled)), int(bool(enabled)), int(bool(enabled)), now, conversation_id),
        )
        if not cursor.rowcount:
            return None
        conn.execute(
            """INSERT INTO manager_actions
            (conversation_id, manager_name, action, details, created_at)
            VALUES (?, ?, 'ai_mode', ?, ?)""",
            (
                conversation_id, manager_name,
                json.dumps({"ai_enabled": bool(enabled)}, ensure_ascii=False), now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,),
        ).fetchone()
    result = dict(row)
    result["ai_enabled"] = bool(result["ai_enabled"])
    return result


def manager_close_conversation(
    conversation_id: str, manager_name: str,
) -> dict | None:
    manager_name = " ".join(str(manager_name or "").split())[:80] or "Менеджер"
    now = utc_now()
    with _write_lock, connection() as conn:
        previous = conn.execute(
            """SELECT human_status, human_ticket_id, human_channel, ai_enabled
            FROM conversations WHERE id = ?""",
            (conversation_id,),
        ).fetchone()
        if not previous:
            return None
        conn.execute(
            """UPDATE conversations SET ai_enabled = 1, human_status = 'closed',
            status = 'active', updated_at = ? WHERE id = ?""",
            (now, conversation_id),
        )
        conn.execute(
            """INSERT INTO manager_actions
            (conversation_id, manager_name, action, details, created_at)
            VALUES (?, ?, 'close', ?, ?)""",
            (
                conversation_id, manager_name,
                json.dumps({
                    "human_status_before": previous["human_status"],
                    "human_ticket_id": previous["human_ticket_id"],
                    "human_channel": previous["human_channel"],
                    "ai_enabled_before": bool(previous["ai_enabled"]),
                }, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,),
        ).fetchone()
    result = dict(row)
    result["ai_enabled"] = bool(result["ai_enabled"])
    return result


def add_user_message_waiting_for_manager(
    conversation_id: str, content: str, attachments: list[dict] | None = None,
) -> tuple[dict, dict]:
    """Persist a user message without invoking AI when a manager paused it."""
    conversation = get_conversation(conversation_id)
    if not conversation:
        raise ValueError("Диалог не найден")
    if bool(conversation.get("ai_enabled", 1)):
        raise ValueError("ИИ включён для этого диалога")
    attachment_meta = [
        {"name": item.get("name"), "type": item.get("type")}
        for item in (attachments or [])
    ]
    message = add_message(
        conversation_id, "user", content,
        metadata={"attachments": attachment_meta, "awaiting_manager": True},
    )
    updated = get_conversation(conversation_id)
    return message, updated


@contextmanager
def connection():
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.database_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with _write_lock, connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                chel_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                registered_at TEXT,
                registration_method TEXT
            );

            CREATE TABLE IF NOT EXISTS user_device_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chel_id TEXT NOT NULL,
                device_type TEXT NOT NULL,
                operating_system TEXT NOT NULL,
                browser TEXT NOT NULL,
                user_agent TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                visit_count INTEGER NOT NULL DEFAULT 1,
                UNIQUE(chel_id, device_type, operating_system, browser),
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                chel_id TEXT NOT NULL DEFAULT 'chel_legacy',
                title TEXT NOT NULL,
                active_agent TEXT NOT NULL DEFAULT 'manager',
                context_summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                ai_enabled INTEGER NOT NULL DEFAULT 1,
                human_status TEXT NOT NULL DEFAULT 'none',
                human_ticket_id TEXT,
                human_channel TEXT,
                human_phone TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                agent_id TEXT,
                content TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS handoffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                from_agent TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS manager_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                manager_name TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS staff_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL,
                login TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                telegram_id TEXT NOT NULL DEFAULT '',
                telegram_chat_id TEXT NOT NULL DEFAULT '',
                max_id TEXT NOT NULL DEFAULT '',
                max_chat_id TEXT NOT NULL DEFAULT '',
                notify_new_requests INTEGER NOT NULL DEFAULT 1,
                notify_new_messages INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            );

            CREATE TABLE IF NOT EXISTS staff_sessions (
                token_hash TEXT PRIMARY KEY,
                staff_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                FOREIGN KEY(staff_user_id) REFERENCES staff_users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS staff_messenger_tokens (
                token_hash TEXT PRIMARY KEY,
                staff_user_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(staff_user_id) REFERENCES staff_users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS manager_notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                staff_user_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                source_message_id INTEGER NOT NULL DEFAULT 0,
                payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                lease_token TEXT,
                leased_at TEXT,
                sent_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(staff_user_id, provider, event_type, conversation_id, source_message_id),
                FOREIGN KEY(staff_user_id) REFERENCES staff_users(id) ON DELETE CASCADE,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chel_id TEXT NOT NULL DEFAULT 'chel_legacy',
                category TEXT NOT NULL DEFAULT 'preference',
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_profile (
                chel_id TEXT PRIMARY KEY,
                preferred_name TEXT NOT NULL DEFAULT '',
                company_inn TEXT NOT NULL DEFAULT '',
                age INTEGER,
                sex TEXT NOT NULL DEFAULT '',
                height_cm REAL,
                weight_kg REAL,
                pregnancy TEXT NOT NULL DEFAULT 'not_applicable',
                conditions TEXT NOT NULL DEFAULT '[]',
                medications TEXT NOT NULL DEFAULT '[]',
                allergies TEXT NOT NULL DEFAULT '[]',
                smoking TEXT NOT NULL DEFAULT 'unknown',
                alcohol TEXT NOT NULL DEFAULT 'unknown',
                activity TEXT NOT NULL DEFAULT 'unknown',
                blood_pressure TEXT NOT NULL DEFAULT 'unknown',
                blood_sugar TEXT NOT NULL DEFAULT 'unknown',
                dark_in_eyes TEXT NOT NULL DEFAULT 'unknown',
                joint_pain TEXT NOT NULL DEFAULT 'unknown',
                fatigue TEXT NOT NULL DEFAULT 'unknown',
                tube_number TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS onboarding_state (
                chel_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'questionnaire',
                selected_tests TEXT NOT NULL DEFAULT '[]',
                payment_status TEXT NOT NULL DEFAULT 'none',
                intro_seen INTEGER NOT NULL DEFAULT 0,
                font_size TEXT NOT NULL DEFAULT 'extra',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS examination_catalog (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                includes TEXT NOT NULL DEFAULT '',
                price INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS external_identities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                provider_user_id TEXT NOT NULL,
                chel_id TEXT NOT NULL,
                legacy_chel_id INTEGER,
                access_status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                last_login_at TEXT NOT NULL,
                UNIQUE(provider, provider_user_id),
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS login_tokens (
                token_hash TEXT PRIMARY KEY,
                chel_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS auth_intents (
                token_hash TEXT PRIMARY KEY,
                chel_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_sessions (
                session_hash TEXT PRIMARY KEY,
                chel_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS body_symptoms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chel_id TEXT NOT NULL DEFAULT 'chel_legacy',
                region TEXT NOT NULL,
                view TEXT NOT NULL DEFAULT 'front',
                symptom_type TEXT NOT NULL,
                intensity INTEGER NOT NULL,
                started_at TEXT,
                duration TEXT NOT NULL DEFAULT '',
                pattern TEXT NOT NULL DEFAULT 'constant',
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lab_interpretations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chel_id TEXT NOT NULL,
                med_id TEXT NOT NULL,
                scope_key TEXT NOT NULL,
                source_urls TEXT NOT NULL DEFAULT '[]',
                profile_fingerprint TEXT NOT NULL,
                interpretation TEXT NOT NULL,
                agent_id TEXT NOT NULL DEFAULT 'therapist',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(chel_id, med_id, scope_key, profile_fingerprint),
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chel_id TEXT NOT NULL DEFAULT '',
                operation TEXT NOT NULL DEFAULT 'other',
                model TEXT NOT NULL,
                pricing_key TEXT NOT NULL DEFAULT '',
                pricing_known INTEGER NOT NULL DEFAULT 0,
                long_context INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                input_rate REAL NOT NULL DEFAULT 0,
                cached_input_rate REAL NOT NULL DEFAULT 0,
                output_rate REAL NOT NULL DEFAULT 0,
                input_cost_usd REAL NOT NULL DEFAULT 0,
                cached_input_cost_usd REAL NOT NULL DEFAULT 0,
                output_cost_usd REAL NOT NULL DEFAULT 0,
                total_cost_usd REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )
        now = utc_now()
        users_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        registration_columns_added = False
        if "registered_at" not in users_columns:
            conn.execute("ALTER TABLE users ADD COLUMN registered_at TEXT")
            registration_columns_added = True
        if "registration_method" not in users_columns:
            conn.execute("ALTER TABLE users ADD COLUMN registration_method TEXT")
            registration_columns_added = True
        if registration_columns_added:
            # Historical installations did not record the first access choice. Preserve their
            # existing analytics; the stricter rule applies to records created after migration.
            conn.execute(
                """UPDATE users SET registered_at=created_at,
                    registration_method=CASE
                        WHEN EXISTS (
                            SELECT 1 FROM external_identities e WHERE e.chel_id=users.chel_id
                        ) THEN 'messenger'
                        ELSE 'legacy'
                    END
                WHERE registered_at IS NULL"""
            )
        conn.execute(
            "INSERT OR IGNORE INTO users (chel_id, created_at, last_seen_at) VALUES ('chel_legacy', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (chel_id, created_at, last_seen_at) VALUES ('chel_test_default', ?, ?)",
            (now, now),
        )
        from .onboarding import TEST_CATALOG
        for examination in TEST_CATALOG:
            conn.execute(
                """INSERT OR IGNORE INTO examination_catalog
                (id, name, description, includes, price, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    examination["id"], examination["name"], examination["description"],
                    examination.get("includes", ""), int(examination["price"]), now, now,
                ),
            )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
        if "chel_id" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN chel_id TEXT NOT NULL DEFAULT 'chel_legacy'")
        if "human_status" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN human_status TEXT NOT NULL DEFAULT 'none'")
        if "human_ticket_id" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN human_ticket_id TEXT")
        if "human_channel" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN human_channel TEXT")
        if "human_phone" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN human_phone TEXT")
        if "ai_enabled" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN ai_enabled INTEGER NOT NULL DEFAULT 1")

        staff_columns = {row[1] for row in conn.execute("PRAGMA table_info(staff_users)").fetchall()}
        for name in ("telegram_id", "telegram_chat_id", "max_id", "max_chat_id"):
            if name not in staff_columns:
                conn.execute(f"ALTER TABLE staff_users ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        for name in ("notify_new_requests", "notify_new_messages"):
            if name not in staff_columns:
                conn.execute(f"ALTER TABLE staff_users ADD COLUMN {name} INTEGER NOT NULL DEFAULT 1")
        outbox_columns = {row[1] for row in conn.execute("PRAGMA table_info(manager_notification_outbox)").fetchall()}
        if "next_attempt_at" not in outbox_columns:
            conn.execute("ALTER TABLE manager_notification_outbox ADD COLUMN next_attempt_at TEXT")

        memory_columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
        if "chel_id" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN chel_id TEXT NOT NULL DEFAULT 'chel_legacy'")

        symptom_columns = {row[1] for row in conn.execute("PRAGMA table_info(body_symptoms)").fetchall()}
        if "chel_id" not in symptom_columns:
            conn.execute("ALTER TABLE body_symptoms ADD COLUMN chel_id TEXT NOT NULL DEFAULT 'chel_legacy'")

        profile_columns = {row[1] for row in conn.execute("PRAGMA table_info(user_profile)").fetchall()}
        if "chel_id" not in profile_columns:
            conn.execute("ALTER TABLE user_profile RENAME TO user_profile_legacy")
            conn.execute(
                """CREATE TABLE user_profile (
                    chel_id TEXT PRIMARY KEY,
                    preferred_name TEXT NOT NULL DEFAULT '', company_inn TEXT NOT NULL DEFAULT '', age INTEGER, sex TEXT NOT NULL DEFAULT '',
                    height_cm REAL, weight_kg REAL, pregnancy TEXT NOT NULL DEFAULT 'not_applicable',
                    conditions TEXT NOT NULL DEFAULT '[]', medications TEXT NOT NULL DEFAULT '[]',
                    allergies TEXT NOT NULL DEFAULT '[]', smoking TEXT NOT NULL DEFAULT 'unknown',
                    alcohol TEXT NOT NULL DEFAULT 'unknown', activity TEXT NOT NULL DEFAULT 'unknown',
                    blood_pressure TEXT NOT NULL DEFAULT 'unknown', blood_sugar TEXT NOT NULL DEFAULT 'unknown',
                    dark_in_eyes TEXT NOT NULL DEFAULT 'unknown', joint_pain TEXT NOT NULL DEFAULT 'unknown',
                    fatigue TEXT NOT NULL DEFAULT 'unknown', tube_number TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
                )"""
            )
            conn.execute(
                """INSERT INTO user_profile
                (chel_id, preferred_name, company_inn, age, sex, height_cm, weight_kg, pregnancy, conditions,
                 medications, allergies, smoking, alcohol, activity, blood_pressure, blood_sugar,
                 dark_in_eyes, joint_pain, fatigue, notes, updated_at)
                SELECT 'chel_legacy', preferred_name, '', age, sex, height_cm, weight_kg, pregnancy,
                 conditions, medications, allergies, smoking, alcohol, activity, blood_pressure,
                 blood_sugar, dark_in_eyes, joint_pain, fatigue, notes, updated_at
                FROM user_profile_legacy WHERE id = 1"""
            )
            conn.execute("DROP TABLE user_profile_legacy")
            profile_columns = {row[1] for row in conn.execute("PRAGMA table_info(user_profile)").fetchall()}
        for name in ("alcohol", "activity", "blood_pressure", "blood_sugar", "dark_in_eyes", "joint_pain", "fatigue"):
            if name not in profile_columns:
                conn.execute(f"ALTER TABLE user_profile ADD COLUMN {name} TEXT NOT NULL DEFAULT 'unknown'")
        if "tube_number" not in profile_columns:
            conn.execute("ALTER TABLE user_profile ADD COLUMN tube_number TEXT NOT NULL DEFAULT ''")
        if "company_inn" not in profile_columns:
            conn.execute("ALTER TABLE user_profile ADD COLUMN company_inn TEXT NOT NULL DEFAULT ''")

        onboarding_columns = {row[1] for row in conn.execute("PRAGMA table_info(onboarding_state)").fetchall()}
        if "chel_id" not in onboarding_columns:
            conn.execute("ALTER TABLE onboarding_state RENAME TO onboarding_state_legacy")
            conn.execute(
                """CREATE TABLE onboarding_state (
                    chel_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'questionnaire',
                    selected_tests TEXT NOT NULL DEFAULT '[]',
                    payment_status TEXT NOT NULL DEFAULT 'none',
                    intro_seen INTEGER NOT NULL DEFAULT 0,
                    font_size TEXT NOT NULL DEFAULT 'standard',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
                )"""
            )
            conn.execute(
                """INSERT INTO onboarding_state
                (chel_id, status, selected_tests, payment_status, intro_seen, font_size, updated_at)
                SELECT 'chel_legacy', status, selected_tests, payment_status, intro_seen, font_size, updated_at
                FROM onboarding_state_legacy WHERE id = 1"""
            )
            conn.execute("DROP TABLE onboarding_state_legacy")
            onboarding_columns = {row[1] for row in conn.execute("PRAGMA table_info(onboarding_state)").fetchall()}
        if "intro_seen" not in onboarding_columns:
            conn.execute("ALTER TABLE onboarding_state ADD COLUMN intro_seen INTEGER NOT NULL DEFAULT 0")
        if "font_size" not in onboarding_columns:
            conn.execute("ALTER TABLE onboarding_state ADD COLUMN font_size TEXT NOT NULL DEFAULT 'standard'")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_chel_id ON conversations(chel_id, updated_at)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_device_stats_audience "
            "ON user_device_stats(device_type, operating_system, browser, last_seen_at)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_human_queue ON conversations(human_status, updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_manager_actions_conversation_id ON manager_actions(conversation_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_sessions_user ON staff_sessions(staff_user_id, expires_at)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_telegram_id ON staff_users(telegram_id) WHERE telegram_id <> ''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_max_id ON staff_users(max_id) WHERE max_id <> ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_messenger_tokens ON staff_messenger_tokens(staff_user_id, provider, expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_manager_notification_delivery ON manager_notification_outbox(provider, status, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_chel_id ON memories(chel_id, updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_body_symptoms_chel_id ON body_symptoms(chel_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_external_identities_chel_id ON external_identities(chel_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_tokens_chel_id ON login_tokens(chel_id, expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_intents_chel_id ON auth_intents(chel_id, expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_chel_id ON user_sessions(chel_id, expires_at)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lab_interpretations_lookup "
            "ON lab_interpretations(chel_id, med_id, scope_key, profile_fingerprint)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_created_at ON ai_usage(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_model ON ai_usage(model, created_at)")

        # Preserve tickets created by earlier versions without rewriting messages.
        conversations = conn.execute("SELECT id, status, human_status, human_ticket_id FROM conversations").fetchall()
        for conversation in conversations:
            if conversation["human_ticket_id"]:
                continue
            messages = conn.execute(
                "SELECT metadata FROM messages WHERE conversation_id = ? AND role = 'assistant' ORDER BY id DESC",
                (conversation["id"],),
            ).fetchall()
            ticket_id = None
            for message in messages:
                try:
                    ticket_id = json.loads(message["metadata"] or "{}").get("human_ticket_id")
                except json.JSONDecodeError:
                    continue
                if ticket_id:
                    break
            if ticket_id:
                conn.execute(
                    "UPDATE conversations SET human_status = 'pending', human_ticket_id = ? WHERE id = ?",
                    (ticket_id, conversation["id"]),
                )
        conn.commit()


def create_conversation(title: str = "Новый диалог") -> dict:
    conversation_id = str(uuid.uuid4())
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            "INSERT INTO conversations (id, chel_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, current_chel_id(), title[:80], now, now),
        )
        conn.commit()
    return get_conversation(conversation_id)


def get_conversation(conversation_id: str) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND chel_id = ?",
            (conversation_id, current_chel_id()),
        ).fetchone()
    return dict(row) if row else None


def list_conversations(limit: int = 50) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE chel_id = ? ORDER BY updated_at DESC LIMIT ?",
            (current_chel_id(), limit),
        ).fetchall()
    return [dict(row) for row in rows]


def list_messages(conversation_id: str, limit: int | None = None) -> list[dict]:
    query = """SELECT m.* FROM messages m JOIN conversations c ON c.id = m.conversation_id
        WHERE m.conversation_id = ? AND c.chel_id = ? ORDER BY m.id ASC"""
    params: tuple = (conversation_id, current_chel_id())
    if limit:
        query = """SELECT * FROM (
            SELECT m.* FROM messages m JOIN conversations c ON c.id = m.conversation_id
            WHERE m.conversation_id = ? AND c.chel_id = ? ORDER BY m.id DESC LIMIT ?
        ) ORDER BY id ASC"""
        params = (conversation_id, current_chel_id(), limit)
    with connection() as conn:
        rows = conn.execute(query, params).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item["metadata"] or "{}")
        result.append(item)
    return result


def list_messages_after(conversation_id: str, after_id: int = 0) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT m.* FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.conversation_id = ? AND c.chel_id = ? AND m.id > ?
            ORDER BY m.id ASC LIMIT 200""",
            (conversation_id, current_chel_id(), max(0, int(after_id))),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item["metadata"] or "{}")
        except json.JSONDecodeError:
            item["metadata"] = {}
        result.append(item)
    return result


def add_message(conversation_id: str, role: str, content: str, agent_id: str | None = None, metadata: dict | None = None) -> dict:
    if not get_conversation(conversation_id):
        raise ValueError("Диалог не найден")
    now = utc_now()
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            "INSERT INTO messages (conversation_id, role, agent_id, content, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, role, agent_id, content, json.dumps(metadata or {}, ensure_ascii=False), now),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ? AND chel_id = ?",
            (now, conversation_id, current_chel_id()),
        )
        conn.commit()
        message_id = cursor.lastrowid
    return next(message for message in list_messages(conversation_id) if message["id"] == message_id)


def update_conversation(
    conversation_id: str, *, active_agent: str, context_summary: str,
    status: str = "active", human_status: str = "none", human_ticket_id: str | None = None,
    human_channel: str | None = None,
) -> None:
    with _write_lock, connection() as conn:
        conn.execute(
            "UPDATE conversations SET active_agent = ?, context_summary = ?, status = ?, human_status = ?, human_ticket_id = ?, human_channel = ?, updated_at = ? WHERE id = ? AND chel_id = ?",
            (active_agent, context_summary, status, human_status, human_ticket_id, human_channel, utc_now(), conversation_id, current_chel_id()),
        )
        conn.commit()


def set_human_channel(conversation_id: str, channel: str, phone: str | None = None) -> dict | None:
    if channel not in {"chat", "call"}:
        raise ValueError("Неизвестный способ связи")
    with _write_lock, connection() as conn:
        conn.execute(
            "UPDATE conversations SET human_channel = ?, human_phone = ?, status = 'waiting_human', updated_at = ? WHERE id = ? AND chel_id = ?",
            (channel, phone if channel == "call" else None, utc_now(), conversation_id, current_chel_id()),
        )
        conn.commit()
    return get_conversation(conversation_id)


def add_handoff(conversation_id: str, from_agent: str, to_agent: str, reason: str) -> None:
    if not get_conversation(conversation_id):
        raise ValueError("Диалог не найден")
    with _write_lock, connection() as conn:
        conn.execute(
            "INSERT INTO handoffs (conversation_id, from_agent, to_agent, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (conversation_id, from_agent, to_agent, reason, utc_now()),
        )
        conn.commit()


def list_handoffs(conversation_id: str) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT h.* FROM handoffs h JOIN conversations c ON c.id = h.conversation_id
            WHERE h.conversation_id = ? AND c.chel_id = ? ORDER BY h.id ASC""",
            (conversation_id, current_chel_id()),
        ).fetchall()
    return [dict(row) for row in rows]


def list_memories() -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM memories WHERE chel_id = ? ORDER BY updated_at DESC, id DESC",
            (current_chel_id(),),
        ).fetchall()
    return [dict(row) for row in rows]


def add_memory(content: str, category: str = "preference") -> dict:
    content = " ".join(content.split())[:500]
    if not content:
        raise ValueError("Память не может быть пустой")
    if category not in {"preference", "health", "constraint", "goal"}:
        category = "preference"
    now = utc_now()
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            "INSERT INTO memories (chel_id, category, content, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (current_chel_id(), category, content, now, now),
        )
        conn.commit()
        memory_id = cursor.lastrowid
    return next(item for item in list_memories() if item["id"] == memory_id)


def delete_memory(memory_id: int) -> bool:
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            "DELETE FROM memories WHERE id = ? AND chel_id = ?",
            (memory_id, current_chel_id()),
        )
        conn.commit()
    return cursor.rowcount > 0


def list_body_symptoms(status: str | None = None, limit: int = 100) -> list[dict]:
    query = "SELECT * FROM body_symptoms WHERE chel_id = ?"
    params: list = [current_chel_id()]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with connection() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def add_body_symptom(payload: dict) -> dict:
    now = utc_now()
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            """INSERT INTO body_symptoms
            (chel_id, region, view, symptom_type, intensity, started_at, duration, pattern, notes, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (
                current_chel_id(), payload["region"], payload.get("view", "front"), payload["symptom_type"],
                int(payload["intensity"]), payload.get("started_at") or None,
                payload.get("duration", ""), payload.get("pattern", "constant"),
                payload.get("notes", ""), now, now,
            ),
        )
        conn.commit()
        symptom_id = cursor.lastrowid
    return next(item for item in list_body_symptoms(limit=500) if item["id"] == symptom_id)


def set_body_symptom_status(symptom_id: int, status: str) -> dict | None:
    if status not in {"active", "resolved"}:
        raise ValueError("Неизвестный статус симптома")
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            "UPDATE body_symptoms SET status = ?, updated_at = ? WHERE id = ? AND chel_id = ?",
            (status, utc_now(), symptom_id, current_chel_id()),
        )
        conn.commit()
    if not cursor.rowcount:
        return None
    return next((item for item in list_body_symptoms(limit=500) if item["id"] == symptom_id), None)


def delete_body_symptom(symptom_id: int) -> bool:
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            "DELETE FROM body_symptoms WHERE id = ? AND chel_id = ?",
            (symptom_id, current_chel_id()),
        )
        conn.commit()
    return cursor.rowcount > 0


def list_health_history(limit: int = 150) -> list[dict]:
    events: list[dict] = []
    for item in list_body_symptoms(limit=limit):
        events.append({
            "id": f"symptom-{item['id']}", "type": "symptom",
            "title": item["symptom_type"], "summary": item["region"],
            "occurred_at": item.get("started_at") or item["created_at"],
            "status": item["status"], "details": item,
        })

    with connection() as conn:
        conversations = conn.execute(
            """SELECT DISTINCT c.* FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            WHERE c.chel_id = ? AND (
               c.active_agent IN ('therapist','cardiologist','neurologist','dermatologist','pediatrician','psychologist','safety')
               OR m.agent_id IN ('therapist','cardiologist','neurologist','dermatologist','pediatrician','psychologist','safety'))
            ORDER BY c.updated_at DESC LIMIT ?""",
            (current_chel_id(), limit),
        ).fetchall()
        messages = conn.execute(
            """SELECT m.* FROM messages m JOIN conversations c ON c.id = m.conversation_id
            WHERE c.chel_id = ? ORDER BY m.created_at DESC LIMIT ?""",
            (current_chel_id(), limit * 4),
        ).fetchall()

    medical_conversation_ids = {row["id"] for row in conversations}
    for row in conversations:
        item = dict(row)
        events.append({
            "id": f"conversation-{item['id']}", "type": "consultation",
            "title": item["title"], "summary": item["active_agent"],
            "occurred_at": item["updated_at"], "status": item["status"],
            "details": {"conversation_id": item["id"], "agent_id": item["active_agent"]},
        })

    seen_documents: set[tuple[str, str]] = set()
    for row in messages:
        item = dict(row)
        try:
            metadata = json.loads(item.get("metadata") or "{}")
        except json.JSONDecodeError:
            metadata = {}
        if metadata.get("action") == "council":
            events.append({
                "id": f"council-{item['id']}", "type": "council",
                "title": "Заключение консилиума", "summary": item["content"][:280],
                "occurred_at": item["created_at"], "status": "complete",
                "details": {"conversation_id": item["conversation_id"], "agents": metadata.get("agents", [])},
            })
        attachments = metadata.get("attachments") or [] if item["conversation_id"] in medical_conversation_ids else []
        for index, attachment in enumerate(attachments):
            name = str(attachment.get("name") or "Медицинский документ")
            key = (item["conversation_id"], name)
            if key in seen_documents:
                continue
            seen_documents.add(key)
            events.append({
                "id": f"document-{item['id']}-{index}", "type": "document",
                "title": name, "summary": str(attachment.get("type") or "Файл"),
                "occurred_at": item["created_at"], "status": "added",
                "details": {"conversation_id": item["conversation_id"]},
            })

    profile = get_profile()
    if profile.get("updated_at"):
        events.append({
            "id": "profile", "type": "profile", "title": "Обновлена медицинская анкета",
            "summary": "Базовые данные, заболевания, лекарства и аллергии",
            "occurred_at": profile["updated_at"], "status": "complete", "details": {},
        })
    onboarding = get_onboarding()
    if onboarding.get("selected_tests") and onboarding.get("updated_at"):
        events.append({
            "id": "tests", "type": "tests", "title": "Выбраны дополнительные обследования",
            "summary": f"Наборов: {len(onboarding['selected_tests'])}",
            "occurred_at": onboarding["updated_at"], "status": onboarding.get("payment_status", "pending"),
            "details": {"test_ids": onboarding["selected_tests"]},
        })

    events.sort(key=lambda item: item.get("occurred_at") or "", reverse=True)
    return events[:max(1, min(int(limit), 500))]


def update_context(conversation_id: str, context: dict) -> dict | None:
    with _write_lock, connection() as conn:
        conn.execute(
            "UPDATE conversations SET context_summary = ?, updated_at = ? WHERE id = ? AND chel_id = ?",
            (json.dumps(context, ensure_ascii=False), utc_now(), conversation_id, current_chel_id()),
        )
        conn.commit()
    return get_conversation(conversation_id)


def get_profile() -> dict:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM user_profile WHERE chel_id = ?",
            (current_chel_id(),),
        ).fetchone()
    if not row:
        return {
            "chel_id": current_chel_id(),
            "preferred_name": "", "company_inn": "", "age": None, "sex": "", "height_cm": None,
            "weight_kg": None, "pregnancy": "not_applicable", "conditions": [],
            "medications": [], "allergies": [], "smoking": "unknown", "notes": "",
            "alcohol": "unknown", "activity": "unknown", "blood_pressure": "unknown",
            "blood_sugar": "unknown", "dark_in_eyes": "unknown", "joint_pain": "unknown",
            "fatigue": "unknown", "tube_number": "",
            "updated_at": None,
        }
    result = dict(row)
    for key in ("conditions", "medications", "allergies"):
        try:
            result[key] = json.loads(result[key] or "[]")
        except json.JSONDecodeError:
            result[key] = []
    return result


def save_profile(profile: dict) -> dict:
    now = utc_now()
    values = (
        str(profile.get("preferred_name", ""))[:100], str(profile.get("company_inn", ""))[:12], profile.get("age"),
        str(profile.get("sex", ""))[:30], profile.get("height_cm"), profile.get("weight_kg"),
        str(profile.get("pregnancy", "not_applicable"))[:30],
        json.dumps(profile.get("conditions", []), ensure_ascii=False),
        json.dumps(profile.get("medications", []), ensure_ascii=False),
        json.dumps(profile.get("allergies", []), ensure_ascii=False),
        str(profile.get("smoking", "unknown"))[:30],
        str(profile.get("alcohol", "unknown"))[:30], str(profile.get("activity", "unknown"))[:30],
        str(profile.get("blood_pressure", "unknown"))[:30], str(profile.get("blood_sugar", "unknown"))[:30],
        str(profile.get("dark_in_eyes", "unknown"))[:30], str(profile.get("joint_pain", "unknown"))[:30],
        str(profile.get("fatigue", "unknown"))[:30],
        str(profile.get("tube_number", ""))[:80],
        str(profile.get("notes", ""))[:1000], now,
    )
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO user_profile
            (chel_id, preferred_name, company_inn, age, sex, height_cm, weight_kg, pregnancy, conditions, medications, allergies, smoking, alcohol, activity, blood_pressure, blood_sugar, dark_in_eyes, joint_pain, fatigue, tube_number, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chel_id) DO UPDATE SET preferred_name=excluded.preferred_name, company_inn=excluded.company_inn, age=excluded.age,
            sex=excluded.sex, height_cm=excluded.height_cm, weight_kg=excluded.weight_kg,
            pregnancy=excluded.pregnancy, conditions=excluded.conditions, medications=excluded.medications,
            allergies=excluded.allergies, smoking=excluded.smoking, alcohol=excluded.alcohol,
            activity=excluded.activity, blood_pressure=excluded.blood_pressure, blood_sugar=excluded.blood_sugar,
            dark_in_eyes=excluded.dark_in_eyes, joint_pain=excluded.joint_pain, fatigue=excluded.fatigue,
            tube_number=excluded.tube_number, notes=excluded.notes, updated_at=excluded.updated_at""",
            (current_chel_id(), *values),
        )
        conn.commit()
    return get_profile()


def profile_fingerprint(profile: dict | None = None) -> str:
    """Stable cache key for profile fields that can change lab interpretation."""
    source = dict(profile or get_profile())
    for key in ("chel_id", "company_inn", "tube_number", "updated_at"):
        source.pop(key, None)
    payload = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_lab_interpretation(
    med_id: str,
    scope_key: str,
    profile_hash: str,
) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            """SELECT * FROM lab_interpretations
            WHERE chel_id = ? AND med_id = ? AND scope_key = ? AND profile_fingerprint = ?""",
            (current_chel_id(), med_id, scope_key, profile_hash),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        result["source_urls"] = json.loads(result["source_urls"] or "[]")
    except json.JSONDecodeError:
        result["source_urls"] = []
    return result


def save_lab_interpretation(
    med_id: str,
    scope_key: str,
    source_urls: list[str],
    profile_hash: str,
    interpretation: str,
    agent_id: str = "therapist",
) -> dict:
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO lab_interpretations
            (chel_id, med_id, scope_key, source_urls, profile_fingerprint,
             interpretation, agent_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chel_id, med_id, scope_key, profile_fingerprint)
            DO UPDATE SET source_urls=excluded.source_urls,
              interpretation=excluded.interpretation, agent_id=excluded.agent_id,
              updated_at=excluded.updated_at""",
            (
                current_chel_id(), med_id, scope_key,
                json.dumps(source_urls, ensure_ascii=False), profile_hash,
                interpretation[:30000], agent_id, now, now,
            ),
        )
        conn.commit()
    return get_lab_interpretation(med_id, scope_key, profile_hash)


def get_onboarding() -> dict:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM onboarding_state WHERE chel_id = ?",
            (current_chel_id(),),
        ).fetchone()
    if not row:
        return {"status": "appearance", "selected_tests": [], "payment_status": "none", "intro_seen": False, "font_size": "extra", "updated_at": None}
    result = dict(row)
    try:
        result["selected_tests"] = json.loads(result["selected_tests"] or "[]")
    except json.JSONDecodeError:
        result["selected_tests"] = []
    result["intro_seen"] = bool(result.get("intro_seen"))
    if result.get("font_size") not in {"standard", "large", "extra"}:
        result["font_size"] = "standard"
    return result


def save_onboarding(
    *, status: str, selected_tests: list[str] | None = None,
    payment_status: str | None = None, intro_seen: bool | None = None,
    font_size: str | None = None,
) -> dict:
    current = get_onboarding()
    selected = current["selected_tests"] if selected_tests is None else selected_tests
    payment = current["payment_status"] if payment_status is None else payment_status
    seen = current.get("intro_seen", False) if intro_seen is None else intro_seen
    size = current.get("font_size", "extra") if font_size is None else font_size
    if size not in {"standard", "large", "extra"}:
        raise ValueError("Некорректный размер текста")
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO onboarding_state (chel_id, status, selected_tests, payment_status, intro_seen, font_size, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(chel_id) DO UPDATE SET status=excluded.status,
            selected_tests=excluded.selected_tests, payment_status=excluded.payment_status,
            intro_seen=excluded.intro_seen, font_size=excluded.font_size, updated_at=excluded.updated_at""",
            (current_chel_id(), status, json.dumps(selected, ensure_ascii=False), payment, int(bool(seen)), size, utc_now()),
        )
        conn.commit()
    return get_onboarding()
