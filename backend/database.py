import json
import hashlib
import secrets
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

from .config import settings


_write_lock = threading.Lock()
_current_chel_id: ContextVar[str] = ContextVar("current_chel_id", default="chel_test_default")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_current_chel_id(chel_id: str) -> None:
    _current_chel_id.set(chel_id)


def current_chel_id() -> str:
    return _current_chel_id.get()


def ensure_user(chel_id: str) -> dict:
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO users (chel_id, created_at, last_seen_at)
            VALUES (?, ?, ?) ON CONFLICT(chel_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
            (chel_id, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE chel_id = ?", (chel_id,)).fetchone()
    return dict(row)


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
        conn.execute("DELETE FROM user_profile WHERE chel_id = ?", (chel_id,))
        conn.execute("DELETE FROM onboarding_state WHERE chel_id = ?", (chel_id,))
        if not preserve_identity:
            conn.execute("DELETE FROM users WHERE chel_id = ?", (chel_id,))
        conn.commit()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _max_chel_id(legacy_chel_id: int) -> str:
    return f"chel_max_{legacy_chel_id:012d}"


def create_max_login(max_user_id: int, legacy_chel_id: int) -> dict:
    """Create a time-limited one-time login token for a verified MAX user."""
    if max_user_id <= 0 or legacy_chel_id <= 0:
        raise ValueError("MAX ID и chel_id должны быть положительными числами")
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.auth_link_ttl_seconds)
    raw_token = secrets.token_urlsafe(32)
    with _write_lock, connection() as conn:
        identity = conn.execute(
            """SELECT * FROM external_identities
            WHERE provider = 'max' AND provider_user_id = ?""",
            (str(max_user_id),),
        ).fetchone()
        if identity:
            if identity["access_status"] != "active":
                raise PermissionError("Доступ к Консилиуму для пользователя не активен")
            chel_id = identity["chel_id"]
            conn.execute(
                "UPDATE external_identities SET last_login_at = ? WHERE id = ?",
                (now.isoformat(), identity["id"]),
            )
        else:
            chel_id = _max_chel_id(legacy_chel_id)
            collision = conn.execute(
                """SELECT provider_user_id FROM external_identities
                WHERE provider = 'max' AND chel_id = ?""",
                (chel_id,),
            ).fetchone()
            if collision and collision["provider_user_id"] != str(max_user_id):
                raise ValueError("Этот chel_id уже привязан к другому MAX-пользователю")
            conn.execute(
                """INSERT INTO users (chel_id, created_at, last_seen_at)
                VALUES (?, ?, ?) ON CONFLICT(chel_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
                (chel_id, now.isoformat(), now.isoformat()),
            )
            conn.execute(
                """INSERT INTO external_identities
                (provider, provider_user_id, chel_id, legacy_chel_id, access_status, created_at, last_login_at)
                VALUES ('max', ?, ?, ?, 'active', ?, ?)""",
                (str(max_user_id), chel_id, legacy_chel_id, now.isoformat(), now.isoformat()),
            )
        conn.execute(
            """INSERT INTO login_tokens (token_hash, chel_id, expires_at, created_at)
            VALUES (?, ?, ?, ?)""",
            (_token_hash(raw_token), chel_id, expires_at.isoformat(), now.isoformat()),
        )
        conn.execute(
            "DELETE FROM login_tokens WHERE expires_at < ? OR used_at IS NOT NULL",
            ((now - timedelta(days=1)).isoformat(),),
        )
        conn.commit()
    return {"token": raw_token, "chel_id": chel_id, "expires_at": expires_at.isoformat()}


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
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                chel_id TEXT NOT NULL DEFAULT 'chel_legacy',
                title TEXT NOT NULL,
                active_agent TEXT NOT NULL DEFAULT 'manager',
                context_summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
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
                font_size TEXT NOT NULL DEFAULT 'standard',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
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
            """
        )
        now = utc_now()
        conn.execute(
            "INSERT OR IGNORE INTO users (chel_id, created_at, last_seen_at) VALUES ('chel_legacy', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (chel_id, created_at, last_seen_at) VALUES ('chel_test_default', ?, ?)",
            (now, now),
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
                    preferred_name TEXT NOT NULL DEFAULT '', age INTEGER, sex TEXT NOT NULL DEFAULT '',
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
                (chel_id, preferred_name, age, sex, height_cm, weight_kg, pregnancy, conditions,
                 medications, allergies, smoking, alcohol, activity, blood_pressure, blood_sugar,
                 dark_in_eyes, joint_pain, fatigue, notes, updated_at)
                SELECT 'chel_legacy', preferred_name, age, sex, height_cm, weight_kg, pregnancy,
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_chel_id ON memories(chel_id, updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_body_symptoms_chel_id ON body_symptoms(chel_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_external_identities_chel_id ON external_identities(chel_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_login_tokens_chel_id ON login_tokens(chel_id, expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_chel_id ON user_sessions(chel_id, expires_at)")

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
            "preferred_name": "", "age": None, "sex": "", "height_cm": None,
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
        str(profile.get("preferred_name", ""))[:100], profile.get("age"),
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
            (chel_id, preferred_name, age, sex, height_cm, weight_kg, pregnancy, conditions, medications, allergies, smoking, alcohol, activity, blood_pressure, blood_sugar, dark_in_eyes, joint_pain, fatigue, tube_number, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chel_id) DO UPDATE SET preferred_name=excluded.preferred_name, age=excluded.age,
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


def get_onboarding() -> dict:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM onboarding_state WHERE chel_id = ?",
            (current_chel_id(),),
        ).fetchone()
    if not row:
        return {"status": "appearance", "selected_tests": [], "payment_status": "none", "intro_seen": False, "font_size": "standard", "updated_at": None}
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
    size = current.get("font_size", "standard") if font_size is None else font_size
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
