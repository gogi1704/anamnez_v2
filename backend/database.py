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
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from .config import settings
from .schemas import normalize_psychology_dialog_state


_write_lock = threading.Lock()
_current_chel_id: ContextVar[str] = ContextVar("current_chel_id", default="chel_test_default")
MOSCOW_TZ = timezone(timedelta(hours=3), "Europe/Moscow")
TEST_COMPANY_INN = "123123"
WEIGHT_ANALYSIS_MIN_CONFIRMED_MEALS = 20
WEIGHT_CONTROL_PERSONALITY_MODES = {"friendly", "balanced", "strict"}
WEIGHT_CONTROL_ADDRESS_MODES = {"formal", "informal"}
MASTERCLASS_PSYCHOLOGY_WELCOME = (
    "Здравствуйте! Я Вита — ИИ-помощник команды психологов. Я не человек и "
    "не ставлю диагнозов, но помогу разобраться, что происходит, и подберу "
    "простую технику на сейчас.\n\n"
    "Напишите как есть, без правильных формулировок: что сейчас беспокоит "
    "больше всего?\n\n"
    "Если захочется поговорить с психологом — первая 30-минутная сессия "
    "бесплатна.\n\n"
    "Если вам прямо сейчас очень плохо или появились мысли причинить себе вред "
    "— напишите об этом, я сразу подскажу, куда обратиться."
)


def normalize_tube_number(value: object) -> str:
    """Return the numeric laboratory identifier or an empty value.

    Historical profiles may contain a one-letter laboratory prefix (for
    example ``B4433354`` or ``С 607408``).  Those prefixes and visual
    separators are safe to remove.  Free-form answers and internal IDs must
    not be converted into plausible-looking tube numbers merely because they
    happen to contain a few digits.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"[0-9\s-]+", raw):
        return re.sub(r"[^0-9]", "", raw)[:80]
    prefixed = re.fullmatch(
        r"[A-Za-zА-Яа-яЁё]\s*[-–—]?\s*([0-9][0-9\s-]*)",
        raw,
    )
    if prefixed:
        return re.sub(r"[^0-9]", "", prefixed.group(1))[:80]
    return ""


def _test_company_inn_chel_ids(conn: sqlite3.Connection) -> set[str]:
    """Return identities that use the dedicated testing INN."""
    try:
        return {
            str(row[0]) for row in conn.execute(
                "SELECT chel_id FROM user_profile WHERE TRIM(company_inn) = ?",
                (TEST_COMPANY_INN,),
            ).fetchall()
        }
    except sqlite3.OperationalError:
        # The profile table does not exist during the first database bootstrap.
        return set()


def _statistics_excluded_chel_ids(conn: sqlite3.Connection) -> set[str]:
    """Return identities that must never contribute to product statistics."""
    excluded = {"chel_legacy", "chel_test_default"} | _test_company_inn_chel_ids(conn)
    try:
        excluded.update(
            str(row[0]) for row in conn.execute(
                "SELECT DISTINCT chel_id FROM ikp_access_users"
            ).fetchall()
        )
    except sqlite3.OperationalError:
        pass
    return excluded


def is_test_user(chel_id: str | None = None) -> bool:
    """Return True only when the user entered the dedicated testing INN."""
    candidate = str(chel_id or current_chel_id()).strip()
    if not candidate:
        return False
    with connection() as conn:
        return candidate in _test_company_inn_chel_ids(conn)


def _sqlite_casefold(value: object) -> str:
    """Unicode-aware normalization for case-insensitive SQLite searches."""
    return str(value or "").casefold()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


AI_BRANCH_MAIN = "main"
AI_BRANCH_TEST = "deepseek_test"

TEST_BRANCH_MODEL_OPTIONS = {
    "deepseek-v4.1-flash": {
        "label": "DeepSeek V4.1 Flash",
        "description": "Быстрая модель DeepSeek через Yandex AI Studio.",
    },
    "qwen3.6-35b-a3b": {
        "label": "Qwen 3.6",
        "description": "Qwen 3.6-35B-A3B через Yandex AI Studio.",
    },
    "gpt-oss-120b": {
        "label": "OpenAI GPT-OSS 120B",
        "description": "Открытая модель OpenAI, размещённая в Yandex AI Studio.",
    },
}

CHAT_PROVIDER_OPTIONS = {
    "yandex": {
        "label": "Yandex AI Studio",
        "description": "Использует общую основную и резервную модели Яндекса.",
    },
    "openai": {
        "label": "OpenAI напрямую",
        "description": "Отправляет запросы напрямую в OpenAI, без Yandex AI Studio.",
    },
}

OPENAI_CHAT_MODEL_OPTIONS = {
    "gpt-5.6-luna": {
        "label": "GPT-5.6 Luna",
        "description": "Экономичная модель для простых диалогов.",
    },
    "gpt-5.6-terra": {
        "label": "GPT-5.6 Terra",
        "description": "Баланс стоимости и качества.",
    },
    "gpt-5.6-sol": {
        "label": "GPT-5.6 Sol",
        "description": "Максимальное качество для сложных диалогов.",
    },
}

ASSISTANT_PERSONALITY_DEFAULTS = {
    "preset": "balanced",
    "apply_to_all": False,
    "address_mode": "formal",
    "formality": 35,
    "warmth": 80,
    "sociability": 70,
    "supportiveness": 85,
    "humor": 15,
    "emoji": 5,
    "initiative": 60,
    "provider": "openai",
    "openai_model": "gpt-5.6-sol",
    "test_branch_model": "deepseek-v4.1-flash",
    "test_branch_fallback_model": "qwen3.6-35b-a3b",
}
ASSISTANT_PERSONALITY_PROFILE_KEYS = {
    "general", "weight_control", "masterclass_psychology",
}
ASSISTANT_PERSONALITY_PROFILE_DEFAULTS = {
    "general": {},
    "weight_control": {},
    "masterclass_psychology": {
        "preset": "balanced",
        "address_mode": "contextual",
        "formality": 20,
        "warmth": 90,
        "sociability": 55,
        "supportiveness": 90,
        "humor": 5,
        "emoji": 5,
        "initiative": 25,
    },
}
PSYCHOLOGY_DIALOG_SETTING_DEFAULTS = {
    "duration_trigger_days": 14,
    "impact_trigger_score": 6,
    "technique_shift_trigger": 2,
    "max_messages_before_offer": 10,
    "offer_cooldown_messages": 3,
}
ASSISTANT_PERSONALITY_PRESETS = {
    "professional": {
        "address_mode": "formal", "formality": 75, "warmth": 55,
        "sociability": 35, "supportiveness": 65, "humor": 0, "emoji": 0,
        "initiative": 40,
    },
    "balanced": {
        "address_mode": "formal", "formality": 35, "warmth": 80,
        "sociability": 70, "supportiveness": 85, "humor": 15, "emoji": 5,
        "initiative": 60,
    },
    "friendly": {
        "address_mode": "contextual", "formality": 15, "warmth": 95,
        "sociability": 85, "supportiveness": 95, "humor": 45, "emoji": 15,
        "initiative": 80,
    },
    "companion": {
        "address_mode": "informal", "formality": 5, "warmth": 95,
        "sociability": 95, "supportiveness": 95, "humor": 70, "emoji": 25,
        "initiative": 90,
    },
}


def current_ai_branch() -> str:
    with connection() as conn:
        row = conn.execute(
            "SELECT branch FROM ai_branch WHERE chel_id = ?", (current_chel_id(),),
        ).fetchone()
    return row["branch"] if row else AI_BRANCH_MAIN


def set_ai_branch(chel_id: str, branch: str) -> None:
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO ai_branch (chel_id, branch, updated_at) VALUES (?,?,?)
               ON CONFLICT(chel_id) DO UPDATE SET branch=excluded.branch, updated_at=excluded.updated_at""",
            (chel_id, branch, utc_now()),
        )
        conn.commit()


def apply_ai_branch_link(token: str) -> None:
    """Switch the current user's AI branch only for the secret link (or back to main)."""
    if token == "main":
        set_ai_branch(current_chel_id(), AI_BRANCH_MAIN)
    elif settings.test_branch_token and hmac.compare_digest(token, settings.test_branch_token):
        set_ai_branch(current_chel_id(), AI_BRANCH_TEST)


def _personality_profile_key(value: object) -> str:
    key = str(value or "general").strip().lower()
    if key not in ASSISTANT_PERSONALITY_PROFILE_KEYS:
        raise ValueError("Неизвестный тип диалога для характера Ольги")
    return key


def admin_assistant_personality_settings(profile_key: str = "general") -> dict:
    """Return the safe, admin-controlled communication style for Olga."""
    profile_key = _personality_profile_key(profile_key)
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM assistant_personality_settings WHERE id = 1"
        ).fetchone()
        profile_row = None
        if profile_key != "general":
            profile_row = conn.execute(
                "SELECT * FROM assistant_personality_profiles WHERE profile_key = ?",
                (profile_key,),
            ).fetchone()
        psychology_row = None
        if profile_key == "masterclass_psychology":
            try:
                psychology_row = conn.execute(
                    "SELECT * FROM psychology_dialog_settings WHERE id=1"
                ).fetchone()
            except sqlite3.OperationalError:
                psychology_row = None
    global_item = dict(row) if row else {}
    profile_defaults = ASSISTANT_PERSONALITY_PROFILE_DEFAULTS.get(profile_key, {})
    style_item = global_item if profile_key == "general" else dict(profile_row or {})
    result = {
        **ASSISTANT_PERSONALITY_DEFAULTS,
        **profile_defaults,
        **style_item,
        "apply_to_all": global_item.get(
            "apply_to_all", ASSISTANT_PERSONALITY_DEFAULTS["apply_to_all"],
        ),
        "test_branch_model": global_item.get(
            "test_branch_model", ASSISTANT_PERSONALITY_DEFAULTS["test_branch_model"],
        ),
        "test_branch_fallback_model": global_item.get(
            "test_branch_fallback_model",
            ASSISTANT_PERSONALITY_DEFAULTS["test_branch_fallback_model"],
        ),
        "profile_key": profile_key,
    }
    if profile_key == "masterclass_psychology":
        result.update(PSYCHOLOGY_DIALOG_SETTING_DEFAULTS)
        if psychology_row:
            result.update(dict(psychology_row))
    result["apply_to_all"] = bool(result.get("apply_to_all"))
    if result.get("provider") not in CHAT_PROVIDER_OPTIONS:
        result["provider"] = ASSISTANT_PERSONALITY_DEFAULTS["provider"]
    if result.get("openai_model") not in OPENAI_CHAT_MODEL_OPTIONS:
        result["openai_model"] = ASSISTANT_PERSONALITY_DEFAULTS["openai_model"]
    for key in (
        "formality", "warmth", "sociability", "supportiveness",
        "humor", "emoji", "initiative",
    ):
        result[key] = max(0, min(100, int(result.get(key, ASSISTANT_PERSONALITY_DEFAULTS[key]))))
    if profile_key == "masterclass_psychology":
        for key, default in PSYCHOLOGY_DIALOG_SETTING_DEFAULTS.items():
            maximum = 10 if key in {"impact_trigger_score", "technique_shift_trigger"} else 100
            result[key] = max(1, min(maximum, int(result.get(key, default))))
    if result.get("address_mode") not in {"formal", "contextual", "informal"}:
        result["address_mode"] = ASSISTANT_PERSONALITY_DEFAULTS["address_mode"]
    if result.get("test_branch_model") not in TEST_BRANCH_MODEL_OPTIONS:
        result["test_branch_model"] = ASSISTANT_PERSONALITY_DEFAULTS["test_branch_model"]
    if (
        result.get("test_branch_fallback_model") not in TEST_BRANCH_MODEL_OPTIONS
        or result["test_branch_fallback_model"] == result["test_branch_model"]
    ):
        preferred = ASSISTANT_PERSONALITY_DEFAULTS["test_branch_fallback_model"]
        result["test_branch_fallback_model"] = next(
            model for model in (preferred, *TEST_BRANCH_MODEL_OPTIONS)
            if model != result["test_branch_model"]
        )
    return result


def admin_assistant_personality_profiles() -> dict[str, dict]:
    return {
        key: admin_assistant_personality_settings(key)
        for key in ("general", "weight_control", "masterclass_psychology")
    }


def admin_update_assistant_personality_settings(payload: dict) -> dict:
    """Validate and persist personality controls without accepting raw prompts."""
    if not isinstance(payload, dict):
        raise ValueError("Ожидается объект настроек характера")
    profile_key = _personality_profile_key(payload.get("profile_key", "general"))
    current = admin_assistant_personality_settings(profile_key)
    global_current = admin_assistant_personality_settings("general")
    preset = str(payload.get("preset", current["preset"])).strip().lower()
    if preset not in {*ASSISTANT_PERSONALITY_PRESETS, "custom"}:
        raise ValueError("Неизвестный пресет характера")
    address_mode = str(payload.get("address_mode", current["address_mode"])).strip().lower()
    if address_mode not in {"formal", "contextual", "informal"}:
        raise ValueError("Неизвестный формат обращения")
    provider = str(payload.get("provider", current["provider"])).strip().lower()
    if provider not in CHAT_PROVIDER_OPTIONS:
        raise ValueError("Неизвестный провайдер модели для диалога")
    openai_model = str(
        payload.get("openai_model", current["openai_model"])
    ).strip().lower()
    if openai_model not in OPENAI_CHAT_MODEL_OPTIONS:
        raise ValueError("Неизвестная модель OpenAI для диалога")
    test_branch_model = str(
        payload.get("test_branch_model", global_current["test_branch_model"])
    ).strip().lower()
    if test_branch_model not in TEST_BRANCH_MODEL_OPTIONS:
        raise ValueError("Неизвестная модель секретной ветки")
    fallback_explicit = "test_branch_fallback_model" in payload
    test_branch_fallback_model = str(
        payload.get(
            "test_branch_fallback_model",
            global_current["test_branch_fallback_model"],
        )
    ).strip().lower()
    if test_branch_fallback_model not in TEST_BRANCH_MODEL_OPTIONS:
        raise ValueError("Неизвестная резервная модель секретной ветки")
    if test_branch_fallback_model == test_branch_model:
        if not fallback_explicit and global_current["test_branch_model"] != test_branch_model:
            test_branch_fallback_model = global_current["test_branch_model"]
        else:
            raise ValueError("Основная и резервная модели должны различаться")
    apply_to_all_raw = payload.get("apply_to_all", global_current["apply_to_all"])
    if apply_to_all_raw in (True, 1, "1"):
        apply_to_all = True
    elif apply_to_all_raw in (False, 0, "0"):
        apply_to_all = False
    else:
        raise ValueError("Некорректная область применения характера")
    values = {}
    for key in (
        "formality", "warmth", "sociability", "supportiveness",
        "humor", "emoji", "initiative",
    ):
        try:
            value = int(payload.get(key, current[key]))
        except (TypeError, ValueError):
            raise ValueError("Параметры характера должны быть целыми числами") from None
        if not 0 <= value <= 100:
            raise ValueError("Параметры характера должны быть от 0 до 100")
        values[key] = value
    psychology_values = {}
    if profile_key == "masterclass_psychology":
        for key, default in PSYCHOLOGY_DIALOG_SETTING_DEFAULTS.items():
            try:
                value = int(payload.get(key, current.get(key, default)))
            except (TypeError, ValueError):
                raise ValueError("Пороги сценария Виты должны быть целыми числами") from None
            maximum = 10 if key in {"impact_trigger_score", "technique_shift_trigger"} else 100
            if not 1 <= value <= maximum:
                raise ValueError(
                    f"Параметр {key} должен быть от 1 до {maximum}"
                )
            psychology_values[key] = value
    now = utc_now()
    with _write_lock, connection() as conn:
        if profile_key == "general":
            conn.execute(
                """INSERT INTO assistant_personality_settings
                   (id,preset,address_mode,formality,warmth,sociability,supportiveness,
                    humor,emoji,initiative,provider,openai_model,apply_to_all,test_branch_model,
                    test_branch_fallback_model,updated_at)
                   VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                    preset=excluded.preset,address_mode=excluded.address_mode,
                    formality=excluded.formality,
                    warmth=excluded.warmth,sociability=excluded.sociability,
                    supportiveness=excluded.supportiveness,humor=excluded.humor,
                    emoji=excluded.emoji,initiative=excluded.initiative,
                    provider=excluded.provider,openai_model=excluded.openai_model,
                    apply_to_all=excluded.apply_to_all,
                    test_branch_model=excluded.test_branch_model,
                    test_branch_fallback_model=excluded.test_branch_fallback_model,
                    updated_at=excluded.updated_at""",
                (
                    preset, address_mode, values["formality"], values["warmth"],
                    values["sociability"], values["supportiveness"],
                    values["humor"], values["emoji"], values["initiative"],
                    provider, openai_model, int(apply_to_all), test_branch_model,
                    test_branch_fallback_model, now,
                ),
            )
        else:
            conn.execute(
                """INSERT INTO assistant_personality_profiles
                   (profile_key,preset,address_mode,formality,warmth,sociability,
                    supportiveness,humor,emoji,initiative,provider,openai_model,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(profile_key) DO UPDATE SET
                    preset=excluded.preset,address_mode=excluded.address_mode,
                    formality=excluded.formality,warmth=excluded.warmth,
                    sociability=excluded.sociability,
                    supportiveness=excluded.supportiveness,humor=excluded.humor,
                    emoji=excluded.emoji,initiative=excluded.initiative,
                    provider=excluded.provider,openai_model=excluded.openai_model,
                    updated_at=excluded.updated_at""",
                (
                    profile_key, preset, address_mode, values["formality"],
                    values["warmth"], values["sociability"],
                    values["supportiveness"], values["humor"], values["emoji"],
                    values["initiative"], provider, openai_model, now,
                ),
            )
            if profile_key == "masterclass_psychology":
                conn.execute(
                    """INSERT INTO psychology_dialog_settings
                       (id,duration_trigger_days,impact_trigger_score,
                        technique_shift_trigger,max_messages_before_offer,
                        offer_cooldown_messages,updated_at)
                       VALUES (1,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                        duration_trigger_days=excluded.duration_trigger_days,
                        impact_trigger_score=excluded.impact_trigger_score,
                        technique_shift_trigger=excluded.technique_shift_trigger,
                        max_messages_before_offer=excluded.max_messages_before_offer,
                        offer_cooldown_messages=excluded.offer_cooldown_messages,
                        updated_at=excluded.updated_at""",
                    (
                        psychology_values["duration_trigger_days"],
                        psychology_values["impact_trigger_score"],
                        psychology_values["technique_shift_trigger"],
                        psychology_values["max_messages_before_offer"],
                        psychology_values["offer_cooldown_messages"], now,
                    ),
                )
            conn.execute(
                """UPDATE assistant_personality_settings
                   SET apply_to_all=?,test_branch_model=?,
                       test_branch_fallback_model=?,updated_at=? WHERE id=1""",
                (
                    int(apply_to_all), test_branch_model,
                    test_branch_fallback_model, now,
                ),
            )
        conn.commit()
    return admin_assistant_personality_settings(profile_key)


def admin_update_yandex_model_settings(payload: dict) -> dict:
    """Update the shared Yandex primary/fallback pair without changing chat profiles."""
    if not isinstance(payload, dict):
        raise ValueError("Ожидается объект настроек моделей Яндекса")
    current = admin_assistant_personality_settings("general")
    primary = str(
        payload.get("test_branch_model", current["test_branch_model"])
    ).strip().lower()
    fallback = str(
        payload.get(
            "test_branch_fallback_model",
            current["test_branch_fallback_model"],
        )
    ).strip().lower()
    if primary not in TEST_BRANCH_MODEL_OPTIONS:
        raise ValueError("Неизвестная основная модель Яндекса")
    if fallback not in TEST_BRANCH_MODEL_OPTIONS:
        raise ValueError("Неизвестная резервная модель Яндекса")
    if primary == fallback:
        raise ValueError("Основная и резервная модели Яндекса должны различаться")
    with _write_lock, connection() as conn:
        conn.execute(
            """UPDATE assistant_personality_settings
               SET test_branch_model=?,test_branch_fallback_model=?,updated_at=?
               WHERE id=1""",
            (primary, fallback, utc_now()),
        )
        conn.commit()
    return admin_assistant_personality_settings("general")


def assistant_personality_prompt(item: dict | None = None) -> str:
    """Translate numeric controls into bounded dialogue guidance for user-facing replies."""
    item = item or admin_assistant_personality_settings()

    def level(value: int, low: str, middle: str, high: str) -> str:
        return low if value <= 32 else high if value >= 68 else middle

    formality = level(
        item["formality"],
        "говори просто и естественно, без канцелярита",
        "сочетай живой разговорный язык с профессиональной точностью",
        "сохраняй сдержанный профессиональный стиль, но не пиши как официальный документ",
    )
    address = {
        "formal": "обязательно обращайся к пользователю на «вы» во всём ответе",
        "contextual": "начинай с «вы», но обязательно переходи на «ты», если пользователь сам общается на «ты» или просит об этом",
        "informal": "обязательно общайся с пользователем на «ты» во всём ответе; используй формы «тебе», «твой», «давай», а не «вам», «ваш», «давайте». Вернись к «вы» только по прямой просьбе пользователя",
    }[item["address_mode"]]
    warmth = level(
        item["warmth"],
        "держи нейтральный спокойный тон без холодности",
        "отвечай доброжелательно и по-человечески",
        "отвечай особенно тепло и бережно, но без сюсюканья и навязчивой похвалы",
    )
    sociability = level(
        item["sociability"],
        "будь лаконичной и сразу переходи к сути",
        "поддерживай естественный диалог и используй короткие живые переходы",
        "будь общительной: поддерживай ощущение живого разговора, но не добавляй пустую болтовню",
    )
    supportiveness = level(
        item["supportiveness"],
        "не добавляй отдельную поддержку без повода",
        "при сложностях коротко поддержи и предложи выполнимый следующий шаг",
        "замечай эмоции и усилия пользователя, мягко поддерживай и предлагай небольшой реалистичный шаг",
    )
    humor_value = item["humor"]
    if humor_value == 0:
        humor = "не добавляй шутки"
    elif humor_value <= 32:
        humor = "редко добавляй короткую добрую шутку в нейтральной теме"
    elif humor_value <= 67:
        humor = "регулярно используй лёгкий добрый юмор в нейтральных и бытовых темах"
    else:
        humor = "заметно оживляй нейтральный разговор добрыми шутками, но не превращай ответ в выступление"
    emoji_value = item["emoji"]
    if emoji_value == 0:
        emoji = "не используй эмодзи"
    elif emoji_value <= 32:
        emoji = "в каждом обычном безопасном ответе добавляй ровно один уместный эмодзи"
    elif emoji_value <= 67:
        emoji = "в большинстве обычных безопасных ответов используй один-два уместных эмодзи"
    else:
        emoji = "используй один-два уместных эмодзи в каждом обычном безопасном ответе, но не ставь их в каждом предложении"
    initiative = level(
        item["initiative"],
        "отвечай на вопрос и не расширяй тему без необходимости",
        "предлагай один полезный следующий шаг, когда он уместен",
        "проявляй инициативу: задавай уместный уточняющий вопрос и предлагай практичный следующий шаг, не перегружая пользователя",
    )
    return f"""Настройка характера Ольги, заданная администратором:
- Выбранный профиль: {item['preset']}.
- Обращение — обязательное правило: {address}.
- Формальность {item['formality']}/100: {formality}.
- Теплота {item['warmth']}/100: {warmth}.
- Общительность {item['sociability']}/100: {sociability}.
- Поддержка {item['supportiveness']}/100: {supportiveness}.
- Юмор {item['humor']}/100: {humor}.
- Эмодзи {item['emoji']}/100: {emoji}.
- Инициативность {item['initiative']}/100: {initiative}.
Одновременно соблюдай каждый параметр выше. Не выбирай только часть настроек. Если общие
инструкции о тоне противоречат этому блоку, этот блок имеет приоритет, кроме правил безопасности.
Эта настройка меняет только подачу. Она не отменяет медицинскую точность, правила
безопасности, срочность предупреждений, запрет выдумывать факты и требуемый формат ответа.
Никогда не шути и не используй игривый тон при красных флагах, срочных рекомендациях,
тяжёлых симптомах, горе, страхе пользователя или сообщении потенциально опасной новости.
Юмор не должен быть направлен на пользователя, его тело, симптомы, диагнозы или привычки.
Не имитируй близкого друга и не допускай фамильярности даже при обращении на «ты».
Подстраивайся под явный стиль самого пользователя, если это безопасно."""


def set_current_chel_id(chel_id: str) -> None:
    _current_chel_id.set(chel_id)


def current_chel_id() -> str:
    return _current_chel_id.get()


FUNNEL_MONITOR_DEFAULTS = {
    "enabled": False,
    "dialog_id": "",
    "send_time": "09:00",
    "timezone": "Europe/Moscow",
    "period_days": 1,
    "include_standard": True,
    "include_result": True,
    "include_payments": True,
    "include_errors": True,
    "minimum_users": 20,
    "alert_threshold_pp": 10.0,
}

EXPERIMENT_DEFAULTS = {
    "enabled": False,
    "experiment_key": "marketer_funnel_2026",
    "name": "Воронка маркетолога",
    "marketer_percent": 25,
    "control_version": "main_v1",
    "marketer_version": "marketer_v1",
    "yandex_enabled": True,
    "yandex_goal_prefix": "consilium_marketer",
    "assignment_counter": 0,
}

MARKETER_EXAMINATION_TEXT_DEFAULTS = {
    "result_days": 14,
    "progress_label": "Шаг 20 из 20 · последний шаг",
    "stage_label": "Обследования",
    "headline": "Кровь у вас возьмут в любом случае. Из той же пробирки можно проверить то, что медосмотр не покажет",
    "zero_effort": "Без нового укола и записи — из той же пробы, что возьмут на медосмотре",
    "recommended_badge": "Рекомендуем по вашим ответам",
    "package_add_label": "+ добавить",
    "price_caption_template": "При оплате онлайн",
    "all_packages_title": "Все остальные чек-апы",
    "show_all_packages": "Показать все чек-апы",
    "hide_all_packages": "Скрыть остальные чек-апы",
    "benefit_results": "Результаты и ИИ-расшифровка появятся в личном кабинете",
    "benefit_doctor": "Врач высшей категории Татьяна Витальевна бесплатно поможет разобраться в результатах",
    "benefit_visit": "Всё за один визит — без отдельной записи и нового укола",
    "benefits_link": "Посмотреть пример расшифровки →",
    "total_template": "Выбрано: {count} · {amount} ₽ (экономия {discount} ₽)",
    "primary_cta_template": "Добавить к медосмотру — {amount} ₽",
    "empty_cta": "Продолжить без обследований",
    "decline_link": "Продолжить без дополнительных обследований",
    "empty_warning": "Вы теряете скидку 10 % на онлайн-оплату",
    "discount_note": "Скидка 10 % действует при оплате онлайн. На медосмотре — полная цена",
    "fine_print_template": "Дополнительные обследования добровольны. Результаты через {days} дней в личном кабинете, расшифровка врача бесплатно",
    "retention_kicker": "Перед тем как продолжить",
    "retention_title": "После обследований вы получите больше, чем результаты",
    "retention_body_1": "Врач высшей категории Татьяна Витальевна подготовит подробную расшифровку сложных показателей.",
    "retention_body_2": "И самое главное — вы получите бесплатную консультацию по результатам.",
    "retention_body_3": "Всё будет доступно в этом сервисе — без очередей и доплат за расшифровку.",
    "retention_benefit_1_title": "Ничего дополнительно делать не нужно",
    "retention_benefit_1_body": "Выберите обследования сейчас, а в день медосмотра сдайте всё вместе.",
    "retention_benefit_2_title": "Один визит вместо отдельной поездки",
    "retention_benefit_2_body": "Вы уже будете на осмотре — дополнительные анализы можно сдать за один раз.",
    "retention_benefit_3_title": "Бесплатная консультация специалиста",
    "retention_benefit_3_body": "После готовности дополнительных анализов врач высшей категории поможет разобраться в результатах.",
    "retention_benefit_4_title": "Не придётся записываться отдельно",
    "retention_benefit_4_body": "Если отложить обследования, позже могут потребоваться отдельная запись и поездка.",
    "retention_voluntary_note": "Дополнительные обследования добровольны — окончательное решение остаётся за вами.",
    "retention_discount_note": "Скидка 10 % на онлайн-оплату действует только до дня медосмотра — в клинике по полной цене",
    "retention_back_template": "Вернуться и добавить «{package}» — {price} ₽",
    "retention_decline": "Всё равно отказаться",
    "personal_rule_2": "{имя}, давление бывает повышенным не само по себе — оно может быть связано с работой почек, щитовидной железы и обменом холестерина. Важно вовремя увидеть причины и не пропустить риски для сосудов и сердца",
    "personal_rule_3": "{имя}, по сочетанию роста и веса организму сейчас непросто справляться с обменной нагрузкой. Это не про внешность, а про здоровье: важно понять причины и проверить, не страдают ли уже органы",
    "personal_rule_4": "{имя}, даже если пока ничего не беспокоит, алкоголь часто отражается на функции печени и почек. Многие изменения долго никак себя не проявляют — анализы помогают увидеть их раньше симптомов",
    "personal_rule_5": "{имя}, у женщин слабость и снижение энергии очень часто связаны с дефицитом железа. Это частая находка, и хорошая новость в том, что она обычно корректируется",
    "personal_rule_6": "{имя}, длительная усталость часто связана с дефицитом витамина D и работой щитовидной железы. Это частая и корректируемая причина, её удобно исключить одной проверкой",
    "personal_rule_7": "{имя}, боль в суставах бывает по разным причинам: иногда это возрастные изменения, иногда воспалительный процесс. Проверка помогает не гадать и понять, нужен ли ревматолог",
    "personal_rule_8": "{имя}, после 45 самочувствие часто меняется из-за гормонального фона. Лучше увидеть картину целиком, чем списывать всё на возраст",
    "personal_rule_9": "{имя}, после 40 заболевания простаты встречаются часто, а на ранних стадиях проявляются слабо. Чем раньше контролируются такие показатели, тем спокойнее и понятнее ситуация",
    "personal_rule_10": "{имя}, жалоб нет — это как раз хорошо. Многие изменения долго не дают ярких симптомов, поэтому профилактическая проверка и важна. Можно начать с самого базового",
    "result_signal_priority": "1,2,3,4,5,6,7,8,9,10",
    "result_progress_label": "Анкета заполнена · спасибо, что уделили время",
    "result_title_template": "{имя}, вот что показали ваши ответы",
    "result_title_without_name": "Вот что показали ваши ответы",
    "result_bridge": "Анкета — не формальность: по ответам видно, на что врач обратил бы внимание в первую очередь.",
    "result_detected_title": "Вы отметили",
    "result_explanation_title": "С чем это часто связано",
    "result_prevention_explanation_title": "Что это значит",
    "result_checks_title": "Что стоит проверить",
    "result_prevention_checks_title": "Что обычно проверяют для профилактики",
    "result_doctor_initials": "ТВ",
    "result_doctor_signature": "Татьяна Витальевна, врач высшей категории",
    "result_decision_title": "Это можно сдать прямо на медосмотре",
    "result_decision_body": "Из той же пробы, что возьмут на осмотре — без записи и отдельной поездки.",
    "result_privacy": "Конфиденциально: ваш выбор и результаты анализов видите только вы.",
    "result_primary_cta": "Выбрать анализы",
    "result_decline_link": "Завершить без дополнительных анализов",
    "result_disclaimer": "Это не диагноз, а то, на что стоит обратить внимание. Добавлять анализы не обязательно.",
    "selection_title": "Анализы к медосмотру",
    "selection_personal_template": "{имя}, эти анализы покажут, {phrase1}{phrase2_part}.",
    "result_rule_1_chip": "усталость, которая не проходит после отдыха",
    "result_rule_1_explanation": "У женщин слабость и снижение энергии очень часто связаны с дефицитом железа. Это частая находка, и хорошая новость в том, что она обычно корректируется.",
    "result_rule_1_checks": "Запасы железа · ферритин, железо\nЩитовидную железу · ТТГ\nВитамин D",
    "result_rule_1_phrase": "связана ли ваша усталость с нехваткой железа",
    "result_rule_2_chip": "усталость, которая не проходит после отдыха",
    "result_rule_2_explanation": "Длительная усталость часто связана с дефицитом витамина D и работой щитовидной железы. Это частая и корректируемая причина, её удобно исключить одной проверкой.",
    "result_rule_2_checks": "Витамин D\nЩитовидную железу · ТТГ\nЗапасы железа · ферритин",
    "result_rule_2_phrase": "связана ли ваша усталость с нехваткой витамина D",
    "result_rule_3_chip": "давление бывает повышенным",
    "result_rule_3_explanation": "Повышенное давление бывает не само по себе — оно может быть связано с работой почек и щитовидной железы. Важно вовремя увидеть причины и не пропустить риски для сосудов и сердца.",
    "result_rule_3_checks": "Работу почек · креатинин, мочевина\nЩитовидную железу · ТТГ\nХолестерин · ЛПНП, ЛПВП",
    "result_rule_3_phrase": "связано ли давление с почками и щитовидной железой",
    "result_rule_4_chip": "вес выше нормы для вашего роста",
    "result_rule_4_explanation": "По сочетанию роста и веса организму сейчас непросто справляться с обменной нагрузкой. Это не про внешность, а про здоровье: важно понять причины и проверить, не страдают ли уже органы.",
    "result_rule_4_checks": "Щитовидную железу · ТТГ, Т3, Т4\nПечень · АЛТ, АСТ\nХолестерин · ЛПНП, ЛПВП",
    "result_rule_4_phrase": "есть ли обменные причины набора веса",
    "result_rule_5_chip": "алкоголь чаще раза в неделю",
    "result_rule_5_explanation": "Даже если пока ничего не беспокоит, алкоголь часто отражается на функции печени и почек. Многие изменения долго никак себя не проявляют — анализы помогают увидеть их раньше симптомов.",
    "result_rule_5_checks": "Печень · АЛТ, АСТ, билирубин\nПоджелудочную · альфа-амилаза\nРаботу почек · креатинин",
    "result_rule_5_phrase": "как справляются печень и почки",
    "result_rule_6_chip": "боли или отёчность суставов",
    "result_rule_6_explanation": "Боль в суставах бывает по разным причинам: иногда это возрастные изменения, иногда воспалительный процесс. Проверка помогает не гадать и понять, нужен ли ревматолог.",
    "result_rule_6_checks": "Признаки воспаления · СРБ\nМочевую кислоту\nРевматоидный фактор",
    "result_rule_6_phrase": "есть ли воспалительный процесс в суставах",
    "result_rule_7_chip": "{возраст} лет",
    "result_rule_7_explanation": "После 45 самочувствие часто меняется из-за гормонального фона. Лучше увидеть картину целиком, чем списывать всё на возраст.",
    "result_rule_7_checks": "Гормональный фон · ФСГ, ЛГ, эстрадиол\nЩитовидную железу · ТТГ",
    "result_rule_7_phrase": "в порядке ли гормональный фон",
    "result_rule_8_chip": "{возраст} лет",
    "result_rule_8_explanation": "После 40 заболевания простаты встречаются часто, а на ранних стадиях проявляются слабо. Чем раньше контролируются такие показатели, тем спокойнее и понятнее ситуация.",
    "result_rule_8_checks": "Мужское здоровье · тестостерон, ПСА\nХолестерин · ЛПНП, ЛПВП",
    "result_rule_8_phrase": "в порядке ли мужское здоровье",
    "result_rule_9_chip": "есть жалобы",
    "result_rule_9_explanation": "Вы описали жалобы — по ним есть обследования, которые помогут найти причину. Это поможет не гадать, а понимать, с чего начать.",
    "result_rule_9_checks": "Показатели рекомендуемого пакета",
    "result_rule_9_phrase": "что стоит за вашими жалобами",
    "result_rule_10_chip": "жалоб нет",
    "result_rule_10_chip_pressure": "давление в норме",
    "result_rule_10_chip_no_smoking": "не курите",
    "result_rule_10_explanation": "Тревожных сигналов в ответах нет — и это хорошо. Многие изменения долго не дают ярких симптомов, поэтому профилактическая проверка и важна: по самочувствию не всегда видно реальную картину.",
    "result_rule_10_checks": "Работу печени · АЛТ, АСТ, билирубин\nРаботу почек · креатинин, мочевина\nВитамин D",
    "result_rule_10_phrase": "всё ли в порядке по базовым показателям",
}


def admin_marketer_examination_texts() -> dict:
    """Return editable copy for the marketer examination-offer screen."""
    with connection() as conn:
        row = conn.execute(
            "SELECT value_json FROM content_text_settings WHERE section_id=?",
            ("marketer_examinations",),
        ).fetchone()
    saved = {}
    if row:
        try:
            saved = json.loads(row["value_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            saved = {}
    if not isinstance(saved, dict):
        saved = {}
    result = {**MARKETER_EXAMINATION_TEXT_DEFAULTS}
    for key in result:
        if key in saved:
            result[key] = saved[key]
    # Preserve genuinely edited copy, but migrate values that were saved from
    # the previous default form so an ordinary deployment receives the fixed UI.
    if result["price_caption_template"] == "{online} ₽ онлайн · {full} ₽ на медосмотре":
        result["price_caption_template"] = MARKETER_EXAMINATION_TEXT_DEFAULTS["price_caption_template"]
    if result["retention_body_1"] == "Врач высшей категории Татьяна Витальевна подготовит понятную расшифровку сложных показателей.":
        result["retention_body_1"] = MARKETER_EXAMINATION_TEXT_DEFAULTS["retention_body_1"]
    if result["retention_body_2"] == "Дополнительные чек-апы можно сдать вместе с обязательным анализом — без нового укола и отдельной записи.":
        result["retention_body_2"] = MARKETER_EXAMINATION_TEXT_DEFAULTS["retention_body_2"]
    if result["personal_rule_10"] == "{имя}, по ответам анкеты не выделяется одно приоритетное направление. Для профилактической проверки можно начать с базового чек-апа":
        result["personal_rule_10"] = MARKETER_EXAMINATION_TEXT_DEFAULTS["personal_rule_10"]
    try:
        result["result_days"] = max(1, min(90, int(result["result_days"])))
    except (TypeError, ValueError):
        result["result_days"] = MARKETER_EXAMINATION_TEXT_DEFAULTS["result_days"]
    return result


def admin_update_marketer_examination_texts(payload: dict) -> dict:
    """Validate and persist one future-proof content-settings section."""
    if not isinstance(payload, dict):
        raise ValueError("Ожидается объект с текстами")
    unknown = set(payload) - set(MARKETER_EXAMINATION_TEXT_DEFAULTS)
    if unknown:
        raise ValueError(f"Неизвестные поля настроек: {', '.join(sorted(unknown))}")
    current = admin_marketer_examination_texts()
    for key in MARKETER_EXAMINATION_TEXT_DEFAULTS:
        if key not in payload:
            continue
        if key == "result_days":
            try:
                value = int(payload[key])
            except (TypeError, ValueError):
                raise ValueError("Срок готовности должен быть целым числом") from None
            if not 1 <= value <= 90:
                raise ValueError("Срок готовности должен быть от 1 до 90 дней")
            current[key] = value
            continue
        value = str(payload[key] or "").strip()
        if not value:
            raise ValueError(f"Поле {key} не может быть пустым")
        if len(value) > 2000:
            raise ValueError(f"Поле {key} слишком длинное")
        if key == "result_signal_priority":
            try:
                priority = [int(item.strip()) for item in value.split(",")]
            except ValueError:
                raise ValueError("Приоритет сигналов должен содержать номера через запятую") from None
            if sorted(priority) != list(range(1, 11)):
                raise ValueError("В приоритете должны быть все номера от 1 до 10 без повторов")
        current[key] = value
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO content_text_settings (section_id,value_json,updated_at)
               VALUES (?,?,?) ON CONFLICT(section_id) DO UPDATE SET
               value_json=excluded.value_json,updated_at=excluded.updated_at""",
            ("marketer_examinations", json.dumps(current, ensure_ascii=False), now),
        )
        conn.commit()
    return admin_marketer_examination_texts()


def admin_experiment_settings() -> dict:
    """Return the singleton A/B experiment configuration."""
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM experiment_settings WHERE id = 1"
        ).fetchone()
    result = {**EXPERIMENT_DEFAULTS, **(dict(row) if row else {})}
    result["enabled"] = bool(result.get("enabled"))
    result["yandex_enabled"] = bool(result.get("yandex_enabled"))
    result["marketer_percent"] = int(result.get("marketer_percent") or 0)
    result["assignment_counter"] = int(result.get("assignment_counter") or 0)
    return result


def admin_update_experiment_settings(payload: dict) -> dict:
    """Validate and persist the active A/B experiment settings."""
    if not isinstance(payload, dict):
        raise ValueError("Ожидается объект настроек эксперимента")
    current = admin_experiment_settings()
    for key in ("enabled", "yandex_enabled"):
        if key in payload:
            if not isinstance(payload[key], bool):
                raise ValueError(f"{key} должен быть true или false")
            current[key] = payload[key]
    experiment_key = str(payload.get("experiment_key", current["experiment_key"])).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,49}", experiment_key):
        raise ValueError("Ключ эксперимента: 3–50 латинских букв, цифр, _ или -")
    name = " ".join(str(payload.get("name", current["name"])).split())
    if not 3 <= len(name) <= 100:
        raise ValueError("Название эксперимента должно содержать от 3 до 100 символов")
    versions = {}
    for key in ("control_version", "marketer_version"):
        value = str(payload.get(key, current[key])).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,39}", value):
            raise ValueError("Версия воронки должна содержать 2–40 латинских символов")
        versions[key] = value
    goal_prefix = str(payload.get("yandex_goal_prefix", current["yandex_goal_prefix"])).strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{2,39}", goal_prefix):
        raise ValueError("Префикс цели Метрики: 3–40 латинских букв, цифр, _ или -")
    try:
        marketer_percent = int(payload.get("marketer_percent", current["marketer_percent"]))
    except (TypeError, ValueError):
        raise ValueError("Доля маркетинговой ветки должна быть целым числом") from None
    if marketer_percent not in (0, 25, 50, 75, 100):
        raise ValueError("Доля маркетинговой ветки: только 0, 25, 50, 75 или 100%")
    # Смена ключа эксперимента — это новый эксперимент, счётчик распределения стартует заново.
    assignment_counter = 0 if experiment_key != current["experiment_key"] else current["assignment_counter"]
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO experiment_settings
               (id,enabled,experiment_key,name,marketer_percent,control_version,
                marketer_version,yandex_enabled,yandex_goal_prefix,assignment_counter,updated_at)
               VALUES (1,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                enabled=excluded.enabled,experiment_key=excluded.experiment_key,
                name=excluded.name,marketer_percent=excluded.marketer_percent,
                control_version=excluded.control_version,
                marketer_version=excluded.marketer_version,
                yandex_enabled=excluded.yandex_enabled,
                yandex_goal_prefix=excluded.yandex_goal_prefix,
                assignment_counter=excluded.assignment_counter,
                updated_at=excluded.updated_at""",
            (
                int(current["enabled"]), experiment_key, name, marketer_percent,
                versions["control_version"], versions["marketer_version"],
                int(current["yandex_enabled"]), goal_prefix, assignment_counter, now,
            ),
        )
        conn.commit()
    return admin_experiment_settings()


def current_experiment_assignment() -> dict:
    """Return a stable server-side assignment for the current user.

    New users are assigned in strict round-robin order (e.g. exactly every
    4th user for marketer_percent=25), via an accumulator that carries the
    remainder between assignments — not a random/hash sample.
    """
    config = admin_experiment_settings()
    if not config["enabled"]:
        return {
            "enabled": False, "key": config["experiment_key"],
            "name": config["name"], "variant": "off", "version": "",
            "marketer_percent": config["marketer_percent"],
            "yandex_enabled": config["yandex_enabled"],
            "yandex_goal_prefix": config["yandex_goal_prefix"],
        }
    chel_id = current_chel_id()
    key = config["experiment_key"]
    with _write_lock, connection() as conn:
        row = conn.execute(
            "SELECT variant,version,assigned_at FROM experiment_assignments "
            "WHERE experiment_key=? AND chel_id=?",
            (key, chel_id),
        ).fetchone()
        if row:
            variant, version, assigned_at = row["variant"], row["version"], row["assigned_at"]
        else:
            counter_row = conn.execute(
                "SELECT assignment_counter FROM experiment_settings WHERE id=1"
            ).fetchone()
            accumulator = int(counter_row["assignment_counter"] or 0) + config["marketer_percent"]
            if accumulator >= 100:
                variant = "marketer"
                accumulator -= 100
            else:
                variant = "control"
            conn.execute(
                "UPDATE experiment_settings SET assignment_counter=? WHERE id=1",
                (accumulator,),
            )
            version = (
                config["marketer_version"] if variant == "marketer"
                else config["control_version"]
            )
            assigned_at = utc_now()
            conn.execute(
                """INSERT INTO experiment_assignments
                   (experiment_key,chel_id,variant,version,assigned_at)
                   VALUES (?,?,?,?,?)""",
                (key, chel_id, variant, version, assigned_at),
            )
            conn.commit()
    result = {
        "enabled": True, "key": key, "name": config["name"],
        "variant": variant, "version": version,
        "assigned_at": assigned_at,
        "marketer_percent": config["marketer_percent"],
        "yandex_enabled": config["yandex_enabled"],
        "yandex_goal_prefix": config["yandex_goal_prefix"],
    }
    if variant == "marketer":
        result.update(current_marketer_variant_assignment(key))
    return result


def current_marketer_variant_assignment(experiment_key: str) -> dict:
    """Split the marketer cohort evenly and persist the B/C assignment."""
    chel_id = current_chel_id()
    with _write_lock, connection() as conn:
        row = conn.execute(
            "SELECT variant,assigned_at FROM marketer_variant_assignments "
            "WHERE experiment_key=? AND chel_id=?",
            (experiment_key, chel_id),
        ).fetchone()
        if row:
            variant, assigned_at = row["variant"], row["assigned_at"]
        else:
            assigned_count = conn.execute(
                "SELECT COUNT(*) FROM marketer_variant_assignments WHERE experiment_key=?",
                (experiment_key,),
            ).fetchone()[0]
            variant = "b" if int(assigned_count or 0) % 2 == 0 else "c"
            assigned_at = utc_now()
            conn.execute(
                """INSERT INTO marketer_variant_assignments
                   (experiment_key,chel_id,variant,assigned_at) VALUES (?,?,?,?)""",
                (experiment_key, chel_id, variant, assigned_at),
            )
            conn.commit()
    return {
        "marketer_variant": variant,
        "marketer_variant_version": f"marketer_{variant}_v1",
        "marketer_variant_assigned_at": assigned_at,
    }


def experiment_preview_assignment(variant: str, marketer_variant: str = "") -> dict:
    """Force a specific experiment variant for a demo/QA link.

    Never touches experiment_assignments, so opening the link repeatedly
    cannot skew the real A/B split or the admin report's cohort counts.
    """
    config = admin_experiment_settings()
    variant = variant if variant in ("control", "marketer") else "control"
    version = config["marketer_version"] if variant == "marketer" else config["control_version"]
    result = {
        "enabled": True, "key": config["experiment_key"], "name": config["name"],
        "variant": variant, "version": version, "preview": True,
        "marketer_percent": config["marketer_percent"],
        "yandex_enabled": config["yandex_enabled"],
        "yandex_goal_prefix": config["yandex_goal_prefix"],
    }
    if variant == "marketer":
        nested = marketer_variant if marketer_variant in {"b", "c"} else "b"
        result.update({
            "marketer_variant": nested,
            "marketer_variant_version": f"marketer_{nested}_v1",
        })
    return result


def admin_experiment_report(period: str = "30") -> dict:
    """Build a privacy-safe A/B summary for the active experiment cohort."""
    config = admin_experiment_settings()
    if period not in {"today", "7", "30", "90", "all"}:
        raise ValueError("Неизвестный период")
    cutoff = None
    if period != "all":
        days = 1 if period == "today" else int(period)
        local_now = datetime.now(timezone(timedelta(hours=3)))
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        if days > 1:
            local_start -= timedelta(days=days - 1)
        cutoff = local_start.astimezone(timezone.utc).isoformat()
    with connection() as conn:
        clauses = ["experiment_key=?", "IS_STATS_USER(chel_id)=1"]
        params: list[object] = [config["experiment_key"]]
        if cutoff:
            clauses.append("assigned_at>=?")
            params.append(cutoff)
        rows = conn.execute(
            "SELECT chel_id,variant,version,assigned_at FROM experiment_assignments WHERE "
            + " AND ".join(clauses), params,
        ).fetchall()
        payment_rows = conn.execute(
            """SELECT po.chel_id,po.amount_kopecks,po.paid_at,po.updated_at
               FROM payment_orders po
               INNER JOIN experiment_assignments ea ON ea.chel_id=po.chel_id
                 AND ea.experiment_key=?
               WHERE po.paid=1 AND po.test=0 AND po.order_type='examinations'""",
            (config["experiment_key"],),
        ).fetchall()
    cohort = {str(row["chel_id"]): dict(row) for row in rows}
    stage_events = {
        "assigned": {"experiment_assigned"},
        "questionnaire": {"questionnaire_completed"},
        "application": {"examinations_selection_completed"},
        "online_payment": {"payment_created", "payment_redirected"},
        "payment_success": {"payment_succeeded"},
        "completed": {"onboarding_completed"},
    }
    reached = {
        variant: {stage: set() for stage in stage_events}
        for variant in ("control", "marketer")
    }
    selected_amounts = {"control": {}, "marketer": {}}
    online_paid_amounts = {"control": 0, "marketer": 0}
    # Assignment itself is authoritative even if the browser blocks analytics.
    for chel_id, item in cohort.items():
        reached[item["variant"]]["assigned"].add(chel_id)
    if cohort and settings.analytics_database_path.exists():
        analytics_conn = sqlite3.connect(settings.analytics_database_path, timeout=5)
        analytics_conn.row_factory = sqlite3.Row
        try:
            placeholders = ",".join("?" for _ in cohort)
            wanted = sorted(set().union(*stage_events.values()))
            event_placeholders = ",".join("?" for _ in wanted)
            event_rows = analytics_conn.execute(
                f"SELECT event_id,chel_id,event_name,properties,received_at FROM analytics_events "
                f"WHERE chel_id IN ({placeholders}) AND event_name IN ({event_placeholders}) "
                f"ORDER BY received_at,event_id",
                [*cohort.keys(), *wanted],
            ).fetchall()
            for row in event_rows:
                chel_id = str(row["chel_id"])
                assignment = cohort.get(chel_id)
                if not assignment or str(row["received_at"] or "") < assignment["assigned_at"]:
                    continue
                try:
                    properties = json.loads(row["properties"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    properties = {}
                event_name = str(row["event_name"])
                if event_name == "examinations_selection_completed":
                    try:
                        selected_count = max(0, int(properties.get("selected_count") or 0))
                    except (TypeError, ValueError):
                        selected_count = 0
                    try:
                        total_price = max(0, int(round(float(properties.get("total_price") or 0))))
                    except (TypeError, ValueError):
                        total_price = 0
                    # A user can revise the selection. Keep only the last confirmed
                    # state so repeated submissions do not inflate the experiment total.
                    selected_amounts[assignment["variant"]][chel_id] = (
                        total_price * 100 if selected_count else 0
                    )
                if event_name == "examinations_selection_completed" and selected_count < 1:
                    continue
                for stage, events in stage_events.items():
                    if event_name in events:
                        reached[assignment["variant"]][stage].add(chel_id)
        finally:
            analytics_conn.close()
    for row in payment_rows:
        chel_id = str(row["chel_id"])
        assignment = cohort.get(chel_id)
        payment_at = str(row["paid_at"] or row["updated_at"] or "")
        if not assignment or payment_at < assignment["assigned_at"]:
            continue
        online_paid_amounts[assignment["variant"]] += max(0, int(row["amount_kopecks"] or 0))
        # The paid order is the authoritative source. Browser analytics can be
        # blocked or lost after the external payment redirect, so deriving the
        # counter only from payment_succeeded made it disagree with the amount.
        reached[assignment["variant"]]["payment_success"].add(chel_id)
    labels = {
        "assigned": "Получили вариант", "questionnaire": "Завершили анкету",
        "application": "Выбрали обследования", "online_payment": "Начали онлайн-оплату",
        "payment_success": "Успешно оплатили", "completed": "Завершили стартовый путь",
    }
    variants = []
    for variant, label in (("control", "Контрольная воронка"), ("marketer", "Воронка маркетолога")):
        assigned = len(reached[variant]["assigned"])
        variants.append({
            "variant": variant, "label": label,
            "version": config[f"{variant}_version"] if variant == "marketer" else config["control_version"],
            "users": assigned,
            "selected_amount_kopecks": sum(selected_amounts[variant].values()),
            "online_paid_amount_kopecks": online_paid_amounts[variant],
            "stages": [{
                "key": stage, "label": labels[stage], "users": len(users),
                "percent": round(len(users) / assigned * 100, 1) if assigned else 0.0,
            } for stage, users in reached[variant].items()],
        })
    return {
        "generated_at": utc_now(), "period": period, "settings": config,
        "total_users": len(cohort), "variants": variants,
        "yandex_goal": f"{config['yandex_goal_prefix']}_event",
    }


def admin_funnel_monitor_settings() -> dict:
    """Return the singleton daily funnel-monitor configuration."""
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM funnel_monitor_settings WHERE id = 1"
        ).fetchone()
    item = dict(row) if row else {}
    result = {**FUNNEL_MONITOR_DEFAULTS, **item}
    for key in ("enabled", "include_standard", "include_result", "include_payments", "include_errors"):
        result[key] = bool(result.get(key))
    return result


def admin_update_funnel_monitor_settings(payload: dict) -> dict:
    """Validate and persist the singleton funnel-monitor configuration."""
    if not isinstance(payload, dict):
        raise ValueError("Ожидается объект настроек")
    current = admin_funnel_monitor_settings()
    boolean_fields = (
        "enabled", "include_standard", "include_result",
        "include_payments", "include_errors",
    )
    for key in boolean_fields:
        if key in payload:
            if not isinstance(payload[key], bool):
                raise ValueError(f"{key} должен быть true или false")
            current[key] = payload[key]
    dialog_id = str(payload.get("dialog_id", current["dialog_id"])).strip()
    if len(dialog_id) > 100 or (dialog_id and not re.fullmatch(r"(?:chat|sg)?\d+", dialog_id)):
        raise ValueError("Укажите ID пользователя, chat123 или sg123")
    send_time = str(payload.get("send_time", current["send_time"])).strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", send_time):
        raise ValueError("Время отправки должно быть в формате ЧЧ:ММ")
    timezone_name = str(payload.get("timezone", current["timezone"])).strip()
    if timezone_name not in {"Europe/Moscow", "UTC"}:
        raise ValueError("Поддерживаются часовые пояса Europe/Moscow и UTC")
    try:
        period_days = int(payload.get("period_days", current["period_days"]))
        minimum_users = int(payload.get("minimum_users", current["minimum_users"]))
        alert_threshold_pp = float(payload.get("alert_threshold_pp", current["alert_threshold_pp"]))
    except (TypeError, ValueError):
        raise ValueError("Проверьте числовые параметры мониторинга") from None
    if period_days not in {1, 7, 30}:
        raise ValueError("Период отчёта должен составлять 1, 7 или 30 дней")
    if not 1 <= minimum_users <= 1_000_000:
        raise ValueError("Минимальная выборка должна быть от 1 до 1 000 000")
    if not 0.1 <= alert_threshold_pp <= 100:
        raise ValueError("Порог отклонения должен быть от 0,1 до 100 п.п.")
    if current["enabled"] and not dialog_id:
        raise ValueError("Для включения мониторинга укажите чат Bitrix24")
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO funnel_monitor_settings
               (id,enabled,dialog_id,send_time,timezone,period_days,
                include_standard,include_result,include_payments,include_errors,
                minimum_users,alert_threshold_pp,updated_at)
               VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                enabled=excluded.enabled,dialog_id=excluded.dialog_id,
                send_time=excluded.send_time,timezone=excluded.timezone,
                period_days=excluded.period_days,
                include_standard=excluded.include_standard,
                include_result=excluded.include_result,
                include_payments=excluded.include_payments,
                include_errors=excluded.include_errors,
                minimum_users=excluded.minimum_users,
                alert_threshold_pp=excluded.alert_threshold_pp,
                updated_at=excluded.updated_at""",
            (
                int(current["enabled"]), dialog_id, send_time, timezone_name,
                period_days, int(current["include_standard"]),
                int(current["include_result"]), int(current["include_payments"]),
                int(current["include_errors"]), minimum_users,
                alert_threshold_pp, now,
            ),
        )
        conn.commit()
    return admin_funnel_monitor_settings()


def record_funnel_monitor_delivery(*, status: str, error: str = "", sent_date: str = "") -> None:
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """UPDATE funnel_monitor_settings SET
               last_status=?,last_error=?,last_attempt_at=?,
               last_sent_at=CASE WHEN ?='sent' THEN ? ELSE last_sent_at END,
               last_sent_date=CASE WHEN ?='sent' THEN ? ELSE last_sent_date END
               WHERE id=1""",
            (status[:30], str(error)[:500], now, status, now, status, sent_date),
        )
        conn.commit()


def normalize_from_manager(value: str | None) -> str:
    """Return a safe attribution label suitable for permanent user storage."""
    normalized = str(value or "").strip()[:80]
    if not normalized or normalized.lower() == "direct":
        return ""
    return normalized if re.fullmatch(r"[\w.-]+", normalized, re.UNICODE) else ""


FROM_MANAGER_NAMES = {
    "manager_1": "Красильникова",
    "manager_2": "Аминева",
    "manager_3": "Левина",
    "manager_4": "Бухарова",
    "manager_5": "Сарасон",
    "manager_6": "Пятакова",
    "manager_7": "Зельцер",
    "manager_8": "Богданова",
    "manager_9": "Семененко",
    "manager_10": "Черниговцев",
}


def from_manager_name(value: str | None) -> str:
    """Return the human-readable name for a traffic attribution marker."""
    marker = normalize_from_manager(value)
    return FROM_MANAGER_NAMES.get(marker.casefold(), marker)


def ensure_user(
    chel_id: str, *, pending: bool = False, from_manager: str = "",
) -> dict:
    now = utc_now()
    manager = normalize_from_manager(from_manager)
    with _write_lock, connection() as conn:
        if pending:
            conn.execute(
                """INSERT INTO users (chel_id, created_at, last_seen_at, from_manager)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chel_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    from_manager=CASE
                        WHEN users.registered_at IS NULL AND users.from_manager = ''
                        THEN excluded.from_manager
                        ELSE users.from_manager
                    END""",
                (chel_id, now, now, manager),
            )
        else:
            conn.execute(
                """INSERT INTO users
                (chel_id, created_at, last_seen_at, registered_at, registration_method, from_manager)
                VALUES (?, ?, ?, ?, 'internal', ?)
                ON CONFLICT(chel_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    registered_at=COALESCE(users.registered_at, excluded.registered_at),
                    registration_method=COALESCE(users.registration_method, excluded.registration_method),
                    from_manager=CASE
                        WHEN users.registered_at IS NULL AND users.from_manager = ''
                        THEN excluded.from_manager
                        ELSE users.from_manager
                    END""",
                (chel_id, now, now, now, manager),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE chel_id = ?", (chel_id,)).fetchone()
    return dict(row)


def touch_user_activity(chel_id: str | None = None) -> None:
    """Record activity without turning frequent chat polling into constant writes."""
    candidate = str(chel_id or current_chel_id()).strip()
    if not candidate:
        return
    now_dt = datetime.now(timezone.utc)
    stale_before = (now_dt - timedelta(minutes=5)).isoformat()
    with _write_lock, connection() as conn:
        conn.execute(
            """UPDATE users SET last_seen_at=?
               WHERE chel_id=? AND (last_seen_at IS NULL OR last_seen_at<?)""",
            (now_dt.isoformat(), candidate, stale_before),
        )
        conn.commit()


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


def mark_current_user_result_entry() -> dict:
    """Mark the dedicated laboratory-results journey without rewriting old attribution."""
    now = utc_now()
    chel_id = current_chel_id()
    with _write_lock, connection() as conn:
        conn.execute(
            """UPDATE users SET
                registered_at=COALESCE(registered_at, ?),
                registration_method=COALESCE(registration_method, 'result'),
                result_entry_at=COALESCE(result_entry_at, ?),
                last_seen_at=?
            WHERE chel_id=?""",
            (now, now, now, chel_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE chel_id = ?", (chel_id,)).fetchone()
    if not row:
        raise ValueError("Пользователь не найден")
    return dict(row)


def current_user_has_result_entry() -> bool:
    with connection() as conn:
        row = conn.execute(
            "SELECT result_entry_at FROM users WHERE chel_id = ?",
            (current_chel_id(),),
        ).fetchone()
    return bool(row and str(row["result_entry_at"] or "").strip())


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


def normalize_ikp_identity(inn: str, company: str) -> tuple[str, str, str]:
    normalized_inn = "".join(str(inn or "").split())
    normalized_company = " ".join(str(company or "").split())[:300]
    if normalized_inn:
        if not normalized_inn.isdigit() or len(normalized_inn) not in {10, 12}:
            raise ValueError("ИНН должен состоять из 10 или 12 цифр")
        return f"inn:{normalized_inn}", normalized_inn, normalized_company
    if not normalized_company:
        raise ValueError("Укажите ИНН или название предприятия")
    company_digest = hashlib.sha256(normalized_company.casefold().encode("utf-8")).hexdigest()
    return f"company:{company_digest}", "", normalized_company


def provision_ikp_access(inn: str, company: str, access_token: str) -> dict:
    source_key, normalized_inn, normalized_company = normalize_ikp_identity(inn, company)
    if not access_token or len(access_token) < 32:
        raise ValueError("Некорректный токен ссылки")
    now = utc_now()
    with _write_lock, connection() as conn:
        existing = conn.execute(
            "SELECT id,created_at,access_token FROM ikp_access_links WHERE source_key=?", (source_key,),
        ).fetchone()
        link_id = existing["id"] if existing else str(uuid.uuid4())
        created_at = existing["created_at"] if existing else now
        stored_token = existing["access_token"] if existing else access_token
        stored_hash = hashlib.sha256(stored_token.encode("utf-8")).hexdigest()
        conn.execute(
            """INSERT INTO ikp_access_links
               (id,source_key,inn,company,access_token,token_hash,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(source_key) DO UPDATE SET
                 inn=excluded.inn,company=excluded.company,updated_at=excluded.updated_at""",
            (link_id, source_key, normalized_inn, normalized_company, stored_token, stored_hash, created_at, now),
        )
        conn.commit()
    return {"id": link_id, "inn": normalized_inn, "company": normalized_company, "access_token": stored_token}


def record_ikp_access(access_token: str, chel_id: str) -> dict:
    token_hash = hashlib.sha256(str(access_token or "").encode("utf-8")).hexdigest()
    now = utc_now()
    with _write_lock, connection() as conn:
        link = conn.execute(
            "SELECT * FROM ikp_access_links WHERE token_hash=?", (token_hash,),
        ).fetchone()
        if not link:
            raise ValueError("Ссылка ИКП не найдена")
        conn.execute(
            """UPDATE ikp_access_links SET visit_count=visit_count+1,
               last_accessed_at=? WHERE id=?""", (now, link["id"]),
        )
        conn.execute(
            """INSERT INTO ikp_access_users
               (link_id,chel_id,first_seen_at,last_seen_at,visit_count)
               VALUES (?,?,?,?,1)
               ON CONFLICT(link_id,chel_id) DO UPDATE SET
                 last_seen_at=excluded.last_seen_at,
                 visit_count=ikp_access_users.visit_count+1""",
            (link["id"], chel_id, now, now),
        )
        conn.commit()
    return dict(link)


def current_user_is_ikp() -> bool:
    with connection() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM ikp_access_users WHERE chel_id=? LIMIT 1",
            (current_chel_id(),),
        ).fetchone())


MASTERCLASS_OPTIONS = (
    ("masterclass_1", "Мастер-класс 1"),
    ("masterclass_2", "Мастер-класс 2"),
    ("masterclass_3", "Мастер-класс 3"),
    ("masterclass_4", "Мастер-класс 4"),
    ("masterclass_5", "Мастер-класс 5"),
)
MASTERCLASS_LABELS = dict(MASTERCLASS_OPTIONS)


def normalize_masterclass_identity(
    inn: str, company: str, masterclass_code: str,
) -> tuple[str, str, str, str, str]:
    _, normalized_inn, normalized_company = normalize_ikp_identity(inn, company)
    code = str(masterclass_code or "").strip().lower()
    if code not in MASTERCLASS_LABELS:
        raise ValueError("Выберите мастер-класс из списка")
    source_key = f"inn:{normalized_inn}:masterclass:{code}"
    return source_key, normalized_inn, normalized_company, code, MASTERCLASS_LABELS[code]


def provision_masterclass_access(
    inn: str, company: str, masterclass_code: str, access_token: str,
    *, is_test: bool = False,
) -> dict:
    source_key, normalized_inn, normalized_company, code, label = (
        normalize_masterclass_identity(inn, company, masterclass_code)
    )
    if not access_token or len(access_token) < 32:
        raise ValueError("Некорректный токен ссылки")
    now = utc_now()
    with _write_lock, connection() as conn:
        existing = conn.execute(
            "SELECT id,created_at,access_token FROM masterclass_access_links WHERE source_key=?",
            (source_key,),
        ).fetchone()
        link_id = existing["id"] if existing else str(uuid.uuid4())
        created_at = existing["created_at"] if existing else now
        stored_token = existing["access_token"] if existing else access_token
        stored_hash = hashlib.sha256(stored_token.encode("utf-8")).hexdigest()
        conn.execute(
            """INSERT INTO masterclass_access_links
               (id,source_key,inn,company,masterclass_code,masterclass_name,
                access_token,token_hash,created_at,updated_at,is_test)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_key) DO UPDATE SET
                 company=excluded.company,
                 masterclass_name=excluded.masterclass_name,
                 is_test=excluded.is_test,
                 updated_at=excluded.updated_at""",
            (
                link_id, source_key, normalized_inn, normalized_company, code,
                label, stored_token, stored_hash, created_at, now, int(bool(is_test)),
            ),
        )
        conn.commit()
    return {
        "id": link_id, "inn": normalized_inn, "company": normalized_company,
        "masterclass_code": code, "masterclass_name": label,
        "access_token": stored_token, "is_test": bool(is_test),
    }


def record_masterclass_access(access_token: str, chel_id: str) -> dict:
    token_hash = hashlib.sha256(str(access_token or "").encode("utf-8")).hexdigest()
    now = utc_now()
    with _write_lock, connection() as conn:
        link = conn.execute(
            "SELECT * FROM masterclass_access_links WHERE token_hash=?", (token_hash,),
        ).fetchone()
        if not link:
            raise ValueError("Ссылка мастер-класса не найдена")
        conn.execute(
            """UPDATE masterclass_access_links SET visit_count=visit_count+1,
               last_accessed_at=? WHERE id=?""", (now, link["id"]),
        )
        conn.execute(
            """INSERT INTO masterclass_access_users
               (link_id,chel_id,first_seen_at,last_seen_at,visit_count)
               VALUES (?,?,?,?,1)
               ON CONFLICT(link_id,chel_id) DO UPDATE SET
                 last_seen_at=excluded.last_seen_at,
                 visit_count=masterclass_access_users.visit_count+1""",
            (link["id"], chel_id, now, now),
        )
        conn.commit()
    return dict(link)


def current_masterclass_access() -> dict | None:
    with connection() as conn:
        row = conn.execute(
            """SELECT l.id,l.inn,l.company,l.masterclass_code,l.masterclass_name,l.is_test,
                      l.access_token,
                      u.first_seen_at,u.last_seen_at,u.continued_at,
                      u.profile_completed_at,
                      EXISTS(
                        SELECT 1 FROM conversations c
                        WHERE c.chel_id=u.chel_id
                          AND c.dialog_type='masterclass_psychology'
                      ) AS psychology_chat_started
               FROM masterclass_access_users u
               JOIN masterclass_access_links l ON l.id=u.link_id
               WHERE u.chel_id=? ORDER BY u.last_seen_at DESC LIMIT 1""",
            (current_chel_id(),),
        ).fetchone()
    return dict(row) if row else None


def current_masterclass_psychology_conversation_id() -> str:
    with connection() as conn:
        row = conn.execute(
            """SELECT id FROM conversations
               WHERE chel_id=? AND dialog_type='masterclass_psychology'
               ORDER BY updated_at DESC LIMIT 1""",
            (current_chel_id(),),
        ).fetchone()
    return str(row["id"]) if row else ""


def masterclass_test_payment_token(entry: dict | None = None) -> str:
    entry = entry or current_masterclass_access()
    if not entry or not entry.get("is_test"):
        return ""
    issued_at = int(datetime.now(timezone.utc).timestamp())
    payload = f"{current_chel_id()}:{entry['id']}:{issued_at}"
    signature = hmac.new(
        settings.ikp_integration_secret.encode("utf-8"),
        f"masterclass-test-payment:{payload}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{issued_at}.{signature}"


def verify_masterclass_test_payment_token(raw_token: str) -> bool:
    try:
        issued_raw, supplied = str(raw_token or "").split(".", 1)
        issued_at = int(issued_raw)
    except (TypeError, ValueError):
        return False
    now = int(datetime.now(timezone.utc).timestamp())
    if issued_at > now + 60 or now - issued_at > 2 * 60 * 60:
        return False
    entry = current_masterclass_access()
    if not entry or not entry.get("is_test"):
        return False
    payload = f"{current_chel_id()}:{entry['id']}:{issued_at}"
    expected = hmac.new(
        settings.ikp_integration_secret.encode("utf-8"),
        f"masterclass-test-payment:{payload}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(supplied, expected)


def mark_current_masterclass_continued() -> dict:
    now = utc_now()
    chel_id = current_chel_id()
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT link_id FROM masterclass_access_users
               WHERE chel_id=? ORDER BY last_seen_at DESC LIMIT 1""", (chel_id,),
        ).fetchone()
        if not row:
            raise ValueError("Сначала откройте ссылку мастер-класса")
        conn.execute(
            """UPDATE masterclass_access_users
               SET continued_at=COALESCE(continued_at,?),last_seen_at=?
               WHERE link_id=? AND chel_id=?""",
            (now, now, row["link_id"], chel_id),
        )
        conn.commit()
    return current_masterclass_access() or {}


def mark_current_masterclass_profile_completed() -> dict:
    """Persist completion of the short psychology master-class profile."""
    now = utc_now()
    chel_id = current_chel_id()
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT link_id FROM masterclass_access_users
               WHERE chel_id=? ORDER BY last_seen_at DESC LIMIT 1""", (chel_id,),
        ).fetchone()
        if not row:
            raise ValueError("Сначала откройте ссылку мастер-класса")
        conn.execute(
            """UPDATE masterclass_access_users
               SET continued_at=COALESCE(continued_at,?),
                   profile_completed_at=COALESCE(profile_completed_at,?),
                   last_seen_at=?
               WHERE link_id=? AND chel_id=?""",
            (now, now, now, row["link_id"], chel_id),
        )
        conn.commit()
    return current_masterclass_access() or {}


def admin_masterclass_report() -> dict:
    with connection() as conn:
        links = [dict(row) for row in conn.execute(
            """SELECT l.id,l.inn,l.company,l.masterclass_code,l.masterclass_name,
                      l.created_at,l.last_accessed_at,l.visit_count,
                      COUNT(DISTINCT u.chel_id) AS users,
                      COUNT(DISTINCT CASE WHEN u.continued_at IS NOT NULL THEN u.chel_id END) AS continued_users
               FROM masterclass_access_links l
               LEFT JOIN masterclass_access_users u ON u.link_id=l.id
               GROUP BY l.id ORDER BY COALESCE(l.last_accessed_at,l.created_at) DESC"""
        ).fetchall()]
        totals = conn.execute(
            """SELECT COUNT(DISTINCT chel_id) AS users,
                      COUNT(DISTINCT CASE WHEN continued_at IS NOT NULL THEN chel_id END) AS continued_users
               FROM masterclass_access_users"""
        ).fetchone()
    return {
        "summary": {
            "links": len(links),
            "visits": sum(int(item["visit_count"] or 0) for item in links),
            "users": int(totals["users"] or 0),
            "continued_users": int(totals["continued_users"] or 0),
        },
        "enterprises": links,
        "generated_at": utc_now(),
    }


def admin_ikp_report() -> dict:
    with connection() as conn:
        links = [dict(row) for row in conn.execute(
            """SELECT l.id,l.inn,l.company,l.created_at,l.updated_at,
                      l.last_accessed_at,l.visit_count,
                      COUNT(DISTINCT iu.chel_id) AS users,
                      COUNT(DISTINCT CASE WHEN u.registered_at IS NOT NULL THEN iu.chel_id END) AS registered_users,
                      COUNT(DISTINCT c.id) AS conversations,
                      COUNT(DISTINCT m.id) AS messages
               FROM ikp_access_links l
               LEFT JOIN ikp_access_users iu ON iu.link_id=l.id
               LEFT JOIN users u ON u.chel_id=iu.chel_id
               LEFT JOIN conversations c ON c.chel_id=iu.chel_id
               LEFT JOIN messages m ON m.conversation_id=c.id
               GROUP BY l.id ORDER BY COALESCE(l.last_accessed_at,l.created_at) DESC"""
        ).fetchall()]
        users = [dict(row) for row in conn.execute(
            """SELECT iu.link_id,l.inn,l.company,iu.chel_id,iu.first_seen_at,
                      iu.last_seen_at,iu.visit_count,u.registered_at,
                      COALESCE(o.status,'not_started') AS onboarding_status,
                      COUNT(DISTINCT c.id) AS conversations,
                      COUNT(DISTINCT m.id) AS messages
               FROM ikp_access_users iu
               JOIN ikp_access_links l ON l.id=iu.link_id
               LEFT JOIN users u ON u.chel_id=iu.chel_id
               LEFT JOIN onboarding_state o ON o.chel_id=iu.chel_id
               LEFT JOIN conversations c ON c.chel_id=iu.chel_id
               LEFT JOIN messages m ON m.conversation_id=c.id
               GROUP BY iu.link_id,iu.chel_id ORDER BY iu.last_seen_at DESC LIMIT 500"""
        ).fetchall()]
    return {
        "summary": {
            "links": len(links),
            "visits": sum(int(item["visit_count"] or 0) for item in links),
            "users": len({item["chel_id"] for item in users}),
            "active_users": len({item["chel_id"] for item in users if item["conversations"] or item["messages"]}),
        },
        "links": links,
        "users": users,
        "generated_at": utc_now(),
    }


def _schedule_revenue_metrics(
    planned_people: int, arrived_users: set[str], applications: dict[str, int],
    payment_orders: list[dict], cash_probability: float = 0.99,
) -> dict:
    online_payers = {str(item["chel_id"]) for item in payment_orders}
    online_revenue = sum(int(item["amount_kopecks"] or 0) for item in payment_orders)
    application_value = sum(applications.values())
    estimated_cash = round(sum(
        amount for chel_id, amount in applications.items() if chel_id not in online_payers
    ) * cash_probability)
    estimated_revenue = online_revenue + estimated_cash
    arrived = len(arrived_users)
    application_count = len(applications)
    missing_people = max(0, int(planned_people) - arrived)
    application_conversion = application_count / arrived if arrived else 0
    expected_lost_applications = missing_people * application_conversion
    average_revenue_per_application = estimated_revenue / application_count if application_count else 0
    lost_revenue = round(expected_lost_applications * average_revenue_per_application)
    return {
        "planned_people": int(planned_people),
        "arrived_people": arrived,
        "missing_people": missing_people,
        "attendance_conversion": round(arrived * 100 / planned_people, 1) if planned_people else 0,
        "applications": application_count,
        "application_conversion": round(application_conversion * 100, 1),
        "application_value_kopecks": application_value,
        "average_application_value_kopecks": round(application_value / application_count) if application_count else 0,
        "online_payment_orders": len(payment_orders),
        "online_payers": len(online_payers),
        "online_payment_conversion": round(len(online_payers) * 100 / application_count, 1) if application_count else 0,
        "online_revenue_kopecks": online_revenue,
        "estimated_cash_revenue_kopecks": estimated_cash,
        "estimated_revenue_kopecks": estimated_revenue,
        "expected_lost_applications": round(expected_lost_applications, 1),
        "lost_revenue_kopecks": lost_revenue,
        "full_revenue_potential_kopecks": estimated_revenue + lost_revenue,
    }


def admin_schedule_revenue_report(schedule_rows: list[dict], month: str) -> dict:
    """Join an approved monthly schedule to Consilium users and revenue events."""
    if not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", str(month or "")):
        raise ValueError("Некорректный месяц листа")
    year, month_number = (int(part) for part in month.split("-"))
    next_year = year + (1 if month_number == 12 else 0)
    next_month = month_number % 12 + 1
    moscow = timezone(timedelta(hours=3))
    start = datetime(year, month_number, 1, tzinfo=moscow).astimezone(timezone.utc).isoformat()
    end = datetime(next_year, next_month, 1, tzinfo=moscow).astimezone(timezone.utc).isoformat()

    valid_rows = [
        row for row in schedule_rows
        if len(re.sub(r"\D", "", str(row.get("inn") or ""))) in {10, 12}
        and int(row.get("planned_people") or 0) >= 0
    ]
    approved_inns = {re.sub(r"\D", "", str(row["inn"])) for row in valid_rows}
    manager_by_inn: dict[str, set[str]] = {}
    for row in valid_rows:
        inn = re.sub(r"\D", "", str(row["inn"]))
        manager_by_inn.setdefault(inn, set()).add(str(row.get("manager") or "Не назначен").strip() or "Не назначен")

    with connection() as conn:
        excluded = _statistics_excluded_chel_ids(conn)
        user_rows = conn.execute(
            """SELECT u.chel_id,p.company_inn FROM users u
               JOIN user_profile p ON p.chel_id=u.chel_id
               WHERE u.created_at>=? AND u.created_at<?""",
            (start, end),
        ).fetchall()
        catalog = {
            str(row["id"]): int(row["price"] or 0) * 100
            for row in conn.execute("SELECT id,price FROM examination_catalog").fetchall()
        }
        application_rows = conn.execute(
            """SELECT o.chel_id,o.selected_tests FROM onboarding_state o
               WHERE o.status='complete' AND o.updated_at>=? AND o.updated_at<?
                 AND JSON_VALID(o.selected_tests) AND JSON_ARRAY_LENGTH(o.selected_tests)>0""",
            (start, end),
        ).fetchall()
        paid_rows = conn.execute(
            """SELECT chel_id,amount_kopecks,id FROM payment_orders
               WHERE paid=1 AND test=0 AND order_type='examinations'
                 AND paid_at>=? AND paid_at<?""",
            (start, end),
        ).fetchall()

    user_inn: dict[str, str] = {}
    for row in user_rows:
        chel_id = str(row["chel_id"])
        inn = re.sub(r"\D", "", str(row["company_inn"] or ""))
        if chel_id not in excluded and inn in approved_inns:
            user_inn[chel_id] = inn
    arrived_users = set(user_inn)

    applications: dict[str, int] = {}
    for row in application_rows:
        chel_id = str(row["chel_id"])
        if chel_id not in arrived_users:
            continue
        try:
            selected = json.loads(row["selected_tests"] or "[]")
        except json.JSONDecodeError:
            selected = []
        if isinstance(selected, list):
            selected_ids = {
                str(item) for item in selected if isinstance(item, (str, int))
            }
            applications[chel_id] = sum(catalog.get(item, 0) for item in selected_ids)
    payments = [dict(row) for row in paid_rows if str(row["chel_id"]) in arrived_users]

    def manager_for_inn(inn: str) -> str:
        managers = manager_by_inn.get(inn) or {"Не назначен"}
        return next(iter(managers)) if len(managers) == 1 else "Конфликт менеджеров"

    companies_by_inn: dict[str, dict] = {}
    for row in valid_rows:
        inn = re.sub(r"\D", "", str(row["inn"]))
        item = companies_by_inn.setdefault(inn, {
            "inn": inn, "organization_name": str(row.get("organization_name") or ""),
            "manager": manager_for_inn(inn), "planned_people": 0,
            "dates": set(), "brigades": set(),
        })
        item["planned_people"] += int(row.get("planned_people") or 0)
        if row.get("examination_date"):
            item["dates"].add(str(row["examination_date"]))
        if row.get("brigade"):
            item["brigades"].add(str(row["brigade"]))

    users_by_inn: dict[str, set[str]] = {inn: set() for inn in approved_inns}
    for chel_id, inn in user_inn.items():
        users_by_inn.setdefault(inn, set()).add(chel_id)
    app_by_inn = {
        inn: {chel_id: applications[chel_id] for chel_id in users if chel_id in applications}
        for inn, users in users_by_inn.items()
    }
    payments_by_inn = {
        inn: [item for item in payments if user_inn.get(str(item["chel_id"])) == inn]
        for inn in approved_inns
    }

    companies = []
    for inn, item in companies_by_inn.items():
        metrics = _schedule_revenue_metrics(
            item["planned_people"], users_by_inn.get(inn, set()),
            app_by_inn.get(inn, {}), payments_by_inn.get(inn, []),
        )
        companies.append({
            **{key: value for key, value in item.items() if key not in {"dates", "brigades"}},
            "dates": sorted(item["dates"]), "brigades": sorted(item["brigades"]), **metrics,
        })
    companies.sort(key=lambda item: (item["manager"].casefold(), item["organization_name"].casefold(), item["inn"]))

    manager_names = sorted({item["manager"] for item in companies}, key=str.casefold)
    managers = []
    for manager in manager_names:
        manager_companies = [item for item in companies if item["manager"] == manager]
        manager_inns = {item["inn"] for item in manager_companies}
        manager_users = {chel_id for chel_id, inn in user_inn.items() if inn in manager_inns}
        manager_apps = {chel_id: value for chel_id, value in applications.items() if chel_id in manager_users}
        manager_payments = [item for item in payments if str(item["chel_id"]) in manager_users]
        managers.append({
            "manager": manager, "approved_inns": len(manager_inns),
            **_schedule_revenue_metrics(
                sum(item["planned_people"] for item in manager_companies),
                manager_users, manager_apps, manager_payments,
            ),
        })
    managers.sort(key=lambda item: (-item["planned_people"], item["manager"].casefold()))

    summary = {
        "approved_inns": len(approved_inns), "managers": len(manager_names),
        "inactive_approved_inns": sum(
            1 for inn in approved_inns if not users_by_inn.get(inn)
        ),
        **_schedule_revenue_metrics(
            sum(item["planned_people"] for item in companies),
            arrived_users, applications, payments,
        ),
    }
    return {
        "month": month, "period_started_at": start, "period_ended_at": end,
        "cash_probability_percent": 99, "summary": summary,
        "managers": managers, "companies": companies, "generated_at": utc_now(),
    }


def admin_sales_dynamics(granularity: str = "day") -> dict:
    """Return checkup selections plus completed online and offline sale counts."""
    if granularity not in {"day", "week", "month"}:
        raise ValueError("Неизвестный срез динамики продаж")

    moscow = timezone(timedelta(hours=3))
    local_now = datetime.now(moscow)
    local_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

    if granularity == "day":
        starts = [local_today - timedelta(days=value) for value in range(29, -1, -1)]
    elif granularity == "week":
        current_week = local_today - timedelta(days=local_today.weekday())
        starts = [current_week - timedelta(weeks=value) for value in range(11, -1, -1)]
    else:
        current_month_index = local_today.year * 12 + local_today.month - 1
        starts = []
        for offset in range(11, -1, -1):
            month_index = current_month_index - offset
            starts.append(datetime(
                month_index // 12, month_index % 12 + 1, 1, tzinfo=moscow,
            ))

    def bucket_key(value: datetime) -> str:
        if granularity == "day":
            return value.strftime("%Y-%m-%d")
        if granularity == "week":
            return (value - timedelta(days=value.weekday())).strftime("%Y-%m-%d")
        return value.strftime("%Y-%m")

    buckets = {
        bucket_key(start): {
            "key": bucket_key(start),
            "started_at": start.astimezone(timezone.utc).isoformat(),
            "checkup_selections": 0,
            "online_sales": 0,
            "offline_sales": 0,
        }
        for start in starts
    }
    cutoff = starts[0].astimezone(timezone.utc).isoformat()

    with connection() as conn:
        application_rows = conn.execute(
            """SELECT chel_id,updated_at
               FROM onboarding_state
               WHERE status='complete' AND updated_at>=?
                 AND JSON_VALID(selected_tests)
                 AND JSON_ARRAY_LENGTH(selected_tests)>0
                 AND IS_STATS_USER(chel_id)=1""",
            (cutoff,),
        ).fetchall()
        offline_rows = conn.execute(
            """SELECT chel_id,tube_linked_at
               FROM user_profile
               WHERE TRIM(COALESCE(tube_number,''))<>''
                 AND tube_linked_at>=?
                 AND IS_STATS_USER(chel_id)=1""",
            (cutoff,),
        ).fetchall()
        online_rows = conn.execute(
            """SELECT id,chel_id,paid_at
               FROM payment_orders
               WHERE paid=1 AND test=0 AND order_type='examinations'
                 AND paid_at>=? AND IS_STATS_USER(chel_id)=1""",
            (cutoff,),
        ).fetchall()

    def local_datetime(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(moscow)

    for row in application_rows:
        occurred_at = local_datetime(row["updated_at"])
        item = buckets.get(bucket_key(occurred_at)) if occurred_at else None
        if not item:
            continue
        item["checkup_selections"] += 1

    for row in offline_rows:
        occurred_at = local_datetime(row["tube_linked_at"])
        item = buckets.get(bucket_key(occurred_at)) if occurred_at else None
        if item:
            item["offline_sales"] += 1

    for row in online_rows:
        occurred_at = local_datetime(row["paid_at"])
        item = buckets.get(bucket_key(occurred_at)) if occurred_at else None
        if item:
            item["online_sales"] += 1

    items = list(buckets.values())
    return {
        "granularity": granularity,
        "buckets": items,
        "summary": {
            "checkup_selections": sum(item["checkup_selections"] for item in items),
            "online_sales": sum(item["online_sales"] for item in items),
            "offline_sales": sum(item["offline_sales"] for item in items),
        },
        "generated_at": utc_now(),
    }


def reset_current_user(preserve_identity: bool = False) -> None:
    """Remove user content; optionally preserve a verified external identity."""
    chel_id = current_chel_id()
    with _write_lock, connection() as conn:
        attachment_items = _user_attachment_items(conn, chel_id)
        # Messages and handoffs are removed by the conversation foreign keys.
        conn.execute("DELETE FROM conversations WHERE chel_id = ?", (chel_id,))
        conn.execute("DELETE FROM memories WHERE chel_id = ?", (chel_id,))
        conn.execute("DELETE FROM body_symptoms WHERE chel_id = ?", (chel_id,))
        conn.execute("DELETE FROM lab_interpretations WHERE chel_id = ?", (chel_id,))
        conn.execute("DELETE FROM lab_result_subscriptions WHERE chel_id = ?", (chel_id,))
        conn.execute("DELETE FROM health_passports WHERE chel_id = ?", (chel_id,))
        conn.execute("DELETE FROM user_profile WHERE chel_id = ?", (chel_id,))
        conn.execute("DELETE FROM onboarding_state WHERE chel_id = ?", (chel_id,))
        conn.execute("DELETE FROM payment_orders WHERE chel_id = ?", (chel_id,))
        if not preserve_identity:
            conn.execute("DELETE FROM users WHERE chel_id = ?", (chel_id,))
        conn.commit()
    if attachment_items:
        from . import chat_attachments
        chat_attachments.delete_public_attachments(attachment_items, chel_id)


def _user_attachment_items(conn: sqlite3.Connection, chel_id: str) -> list[dict]:
    """Collect persisted attachment metadata before cascades remove messages."""
    rows = conn.execute(
        """SELECT m.metadata FROM messages m
        JOIN conversations c ON c.id=m.conversation_id
        WHERE c.chel_id=?""",
        (chel_id,),
    ).fetchall()
    items: list[dict] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        attachments = metadata.get("attachments", []) if isinstance(metadata, dict) else []
        if isinstance(attachments, list):
            items.extend(item for item in attachments if isinstance(item, dict))
    return items


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
        "conversations", "memories", "body_symptoms", "lab_interpretations", "lab_result_subscriptions",
        "user_result_notification_outbox", "health_passports",
        "user_profile", "onboarding_state", "payment_orders", "user_device_stats", "ai_usage",
        "login_tokens", "auth_intents", "user_sessions", "external_identities",
        "users",
    )
    deleted_by_table: dict[str, int] = {}
    with _write_lock, connection() as conn:
        attachment_items = _user_attachment_items(conn, chel_id)
        for table in owned_tables:
            cursor = conn.execute(f"DELETE FROM {table} WHERE chel_id = ?", (chel_id,))
            deleted_by_table[table] = max(0, int(cursor.rowcount or 0))
        conn.commit()
    if attachment_items:
        from . import chat_attachments
        chat_attachments.delete_public_attachments(attachment_items, chel_id)
    return {
        "chel_id": chel_id,
        "deleted": sum(deleted_by_table.values()),
        "deleted_by_table": deleted_by_table,
        "user_found": bool(deleted_by_table["users"]),
    }


def data_retention_preview(cutoff_at: str) -> dict:
    """Return inactive deletion candidates without changing user data."""
    cutoff_at = str(cutoff_at or "").strip()
    if not cutoff_at:
        raise ValueError("Не задана дата неактивности")
    with connection() as conn:
        rows = conn.execute(
            """WITH activity AS (
                 SELECT chel_id,last_seen_at AS seen_at FROM users
                 UNION ALL
                 SELECT chel_id,last_seen_at FROM user_sessions
                   WHERE revoked_at IS NULL
                 UNION ALL
                 SELECT chel_id,last_seen_at FROM user_device_stats
                 UNION ALL
                 SELECT chel_id,last_seen_at FROM ikp_access_users
                 UNION ALL
                 SELECT chel_id,last_seen_at FROM masterclass_access_users
                 UNION ALL
                 SELECT chel_id,last_login_at FROM external_identities
                   WHERE access_status='active'
               ), latest AS (
                 SELECT chel_id,MAX(seen_at) AS last_activity_at
                 FROM activity GROUP BY chel_id
               )
               SELECT u.chel_id,u.created_at,u.registered_at,
                      COALESCE(l.last_activity_at,u.last_seen_at) AS last_activity_at,
                      COALESCE(NULLIF(TRIM(p.preferred_name),''),'') AS preferred_name,
                      COALESCE((SELECT GROUP_CONCAT(DISTINCT e.provider)
                        FROM external_identities e
                        WHERE e.chel_id=u.chel_id AND e.access_status='active'
                          AND TRIM(e.chat_id)<>''),'') AS messenger_providers,
                      EXISTS(SELECT 1 FROM payment_orders po
                        WHERE po.chel_id=u.chel_id AND (po.paid=1 OR po.status='succeeded')) AS has_paid_orders,
                      EXISTS(SELECT 1 FROM payment_orders po
                        WHERE po.chel_id=u.chel_id AND po.status IN
                          ('creating','pending','waiting_for_capture')) AS has_pending_payment,
                      EXISTS(SELECT 1 FROM lab_result_subscriptions lr
                        WHERE lr.chel_id=u.chel_id AND lr.status='pending') AS has_pending_results,
                      EXISTS(SELECT 1 FROM psychology_sessions ps
                        WHERE ps.chel_id=u.chel_id AND ps.status='active') AS has_active_psychology_session,
                      EXISTS(SELECT 1 FROM conversations c
                        WHERE c.chel_id=u.chel_id AND c.human_status IN
                          ('pending','connected')) AS has_open_human_request
               FROM users u
               LEFT JOIN latest l ON l.chel_id=u.chel_id
               LEFT JOIN user_profile p ON p.chel_id=u.chel_id
               WHERE u.chel_id NOT IN ('chel_legacy','chel_test_default')
                 AND COALESCE(l.last_activity_at,u.last_seen_at) < ?
               ORDER BY COALESCE(l.last_activity_at,u.last_seen_at),u.chel_id""",
            (cutoff_at,),
        ).fetchall()
    candidates: list[dict] = []
    excluded: list[dict] = []
    for row in rows:
        item = dict(row)
        reasons: list[str] = []
        if item.pop("has_paid_orders"):
            reasons.append("есть оплаченные заказы: нужен отдельный финансовый архив")
        if item.pop("has_pending_payment"):
            reasons.append("есть незавершённая оплата")
        if item.pop("has_pending_results"):
            reasons.append("ожидаются результаты анализов")
        if item.pop("has_active_psychology_session"):
            reasons.append("есть активная сессия с психологом")
        if item.pop("has_open_human_request"):
            reasons.append("есть открытое обращение к специалисту")
        providers = [
            value for value in str(item.pop("messenger_providers") or "").split(",")
            if value
        ]
        item["messenger_providers"] = sorted(set(providers))
        item["messenger_linked"] = bool(providers)
        if reasons:
            item["exclusion_reasons"] = reasons
            excluded.append(item)
        else:
            item["planned_action"] = (
                "would_warn_then_delete" if providers else "would_delete_after_grace"
            )
            candidates.append(item)
    return {
        "cutoff_at": cutoff_at,
        "candidates": candidates,
        "excluded": excluded,
        "candidate_count": len(candidates),
        "messenger_count": sum(1 for item in candidates if item["messenger_linked"]),
        "no_messenger_count": sum(1 for item in candidates if not item["messenger_linked"]),
        "excluded_count": len(excluded),
    }


def last_completed_data_retention_audit() -> dict | None:
    with connection() as conn:
        row = conn.execute(
            """SELECT * FROM data_retention_audit_runs
               WHERE status='completed' ORDER BY completed_at DESC LIMIT 1"""
        ).fetchone()
    return dict(row) if row else None


def start_data_retention_audit(run_id: str, cutoff_at: str) -> None:
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO data_retention_audit_runs
               (id,mode,cutoff_at,started_at,status) VALUES (?,'dry-run',?,?,'running')""",
            (run_id, cutoff_at, now),
        )
        conn.commit()


def finish_data_retention_audit(run_id: str, preview: dict) -> None:
    summary = {
        key: int(preview.get(key, 0))
        for key in (
            "candidate_count", "messenger_count", "no_messenger_count", "excluded_count"
        )
    }
    summary["report_path"] = str(preview.get("report_path") or "")
    summary["report_notifications"] = int(preview.get("report_notifications", 0))
    with _write_lock, connection() as conn:
        conn.execute(
            """UPDATE data_retention_audit_runs SET status='completed',completed_at=?,
               candidate_count=?,messenger_count=?,no_messenger_count=?,excluded_count=?,
               report_summary=?,error='' WHERE id=?""",
            (
                utc_now(), summary["candidate_count"], summary["messenger_count"],
                summary["no_messenger_count"], summary["excluded_count"],
                json.dumps(summary, ensure_ascii=False), run_id,
            ),
        )
        conn.commit()


def fail_data_retention_audit(run_id: str, error: str) -> None:
    with _write_lock, connection() as conn:
        conn.execute(
            """UPDATE data_retention_audit_runs SET status='failed',completed_at=?,error=?
               WHERE id=?""",
            (utc_now(), str(error or "")[:1000], run_id),
        )
        conn.commit()


def queue_system_notification(
    provider: str, recipient_id: str, event_type: str, payload: dict, dedupe_key: str,
) -> bool:
    provider = str(provider or "").strip().lower()
    recipient_id = str(recipient_id or "").strip()
    if provider not in {"telegram", "max"} or not recipient_id:
        return False
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO system_notification_outbox
               (provider,recipient_id,event_type,dedupe_key,payload,status,created_at)
               VALUES (?,?,?,?,?,'pending',?)""",
            (
                provider, recipient_id, str(event_type or "system"),
                str(dedupe_key or "")[:160],
                json.dumps(payload or {}, ensure_ascii=False), utc_now(),
            ),
        )
        conn.commit()
    return bool(cursor.rowcount)


SYSTEM_NOTIFICATION_ID_OFFSET = 3_000_000_000


def claim_system_notifications(provider: str, limit: int = 20) -> list[dict]:
    provider = str(provider or "").strip().lower()
    if provider not in {"telegram", "max"}:
        raise ValueError("Неизвестный мессенджер")
    limit = max(1, min(50, int(limit)))
    now = datetime.now(timezone.utc)
    stale_before = (now - timedelta(minutes=2)).isoformat()
    result: list[dict] = []
    with _write_lock, connection() as conn:
        rows = conn.execute(
            """SELECT id,recipient_id,event_type,payload,attempts
               FROM system_notification_outbox
               WHERE provider=? AND attempts<100
                 AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                 AND (status='pending' OR (status='delivering' AND leased_at<?))
               ORDER BY id LIMIT ?""",
            (provider, now.isoformat(), stale_before, limit),
        ).fetchall()
        for row in rows:
            lease_token = secrets.token_urlsafe(18)
            conn.execute(
                """UPDATE system_notification_outbox SET status='delivering',lease_token=?,
                   leased_at=?,attempts=attempts+1 WHERE id=?""",
                (lease_token, now.isoformat(), row["id"]),
            )
            item = dict(row)
            item["id"] = -(SYSTEM_NOTIFICATION_ID_OFFSET + int(row["id"]))
            item["conversation_id"] = ""
            item["lease_token"] = lease_token
            item["payload"] = json.loads(item["payload"] or "{}")
            result.append(item)
        conn.commit()
    return result


def acknowledge_system_notification(
    notification_id: int, lease_token: str, success: bool, error: str = "",
) -> bool:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT attempts FROM system_notification_outbox
               WHERE id=? AND lease_token=? AND status='delivering'""",
            (int(notification_id), str(lease_token or "")),
        ).fetchone()
        if not row:
            return False
        permanent_failure = not success and _permanent_messenger_delivery_error(error)
        retry_delay = min(3600, 5 * (2 ** min(int(row["attempts"]), 10)))
        next_attempt_at = (
            (now_dt + timedelta(seconds=retry_delay)).isoformat()
            if not success and not permanent_failure else None
        )
        cursor = conn.execute(
            """UPDATE system_notification_outbox SET status=?,sent_at=?,last_error=?,
               next_attempt_at=?,lease_token=NULL,leased_at=NULL
               WHERE id=? AND lease_token=? AND status='delivering'""",
            (
                "sent" if success else ("failed" if permanent_failure else "pending"),
                now if success else None,
                str(error or "")[:500], next_attempt_at,
                int(notification_id), str(lease_token or ""),
            ),
        )
        conn.commit()
    return bool(cursor.rowcount)


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


def _external_identity_recipient(identity) -> str:
    """Return the actual private-chat destination for a linked messenger."""
    provider = str(identity["provider"] or "").strip().lower()
    if provider == "max":
        return str(identity["chat_id"] or "").strip()
    return str(identity["provider_user_id"] or "").strip()


def _service_auth_return_path(
    conn: sqlite3.Connection, chel_id: str, return_path: str,
) -> str:
    """Allow redirects only to a service link already opened by this user."""
    return_path = str(return_path or "").strip()
    if not return_path:
        return ""
    match = re.fullmatch(r"/(masterclass|ikp)/([A-Za-z0-9_-]{20,200})", return_path)
    if not match:
        raise ValueError("Некорректный адрес возврата после входа")
    flow, raw_token = match.groups()
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    links_table = "masterclass_access_links" if flow == "masterclass" else "ikp_access_links"
    users_table = "masterclass_access_users" if flow == "masterclass" else "ikp_access_users"
    allowed = conn.execute(
        f"""SELECT 1 FROM {links_table} AS links
           JOIN {users_table} AS users ON users.link_id=links.id
           WHERE links.token_hash=? AND users.chel_id=?""",
        (token_hash, chel_id),
    ).fetchone()
    if not allowed:
        raise ValueError("Сервисная ссылка не принадлежит текущему входу")
    return return_path


def _move_link_attribution(
    conn: sqlite3.Connection, source_chel_id: str, target_chel_id: str,
) -> None:
    """Move QR-link attribution from a temporary browser user to its account."""
    conn.execute(
        """INSERT INTO ikp_access_users
           (link_id,chel_id,first_seen_at,last_seen_at,visit_count)
           SELECT link_id,?,first_seen_at,last_seen_at,visit_count
           FROM ikp_access_users WHERE chel_id=?
           ON CONFLICT(link_id,chel_id) DO UPDATE SET
             first_seen_at=MIN(ikp_access_users.first_seen_at,excluded.first_seen_at),
             last_seen_at=MAX(ikp_access_users.last_seen_at,excluded.last_seen_at),
             visit_count=ikp_access_users.visit_count+excluded.visit_count""",
        (target_chel_id, source_chel_id),
    )
    conn.execute("DELETE FROM ikp_access_users WHERE chel_id=?", (source_chel_id,))
    conn.execute(
        """INSERT INTO masterclass_access_users
           (link_id,chel_id,first_seen_at,last_seen_at,continued_at,
            profile_completed_at,visit_count)
           SELECT link_id,?,first_seen_at,last_seen_at,continued_at,
                  profile_completed_at,visit_count
           FROM masterclass_access_users WHERE chel_id=?
           ON CONFLICT(link_id,chel_id) DO UPDATE SET
             first_seen_at=MIN(masterclass_access_users.first_seen_at,excluded.first_seen_at),
             last_seen_at=MAX(masterclass_access_users.last_seen_at,excluded.last_seen_at),
             continued_at=COALESCE(masterclass_access_users.continued_at,excluded.continued_at),
             profile_completed_at=COALESCE(masterclass_access_users.profile_completed_at,excluded.profile_completed_at),
             visit_count=masterclass_access_users.visit_count+excluded.visit_count""",
        (target_chel_id, source_chel_id),
    )
    conn.execute("DELETE FROM masterclass_access_users WHERE chel_id=?", (source_chel_id,))


def _move_masterclass_psychology_data(
    conn: sqlite3.Connection, source_chel_id: str, target_chel_id: str,
) -> None:
    """Preserve anonymous account data when signing into an existing account."""
    source_specialized = {
        str(row["dialog_type"])
        for row in conn.execute(
            """SELECT DISTINCT dialog_type FROM conversations
               WHERE chel_id=? AND dialog_type IN ('masterclass_psychology','weight_control')""",
            (source_chel_id,),
        ).fetchall()
    }
    target_specialized = {
        str(row["dialog_type"])
        for row in conn.execute(
            """SELECT DISTINCT dialog_type FROM conversations
               WHERE chel_id=? AND dialog_type IN ('masterclass_psychology','weight_control')""",
            (target_chel_id,),
        ).fetchall()
    }
    conflicting = source_specialized & target_specialized
    if conflicting:
        labels = {
            "masterclass_psychology": "психологический чат",
            "weight_control": "чат контроля питания",
        }
        conflict_label = ", ".join(labels[item] for item in sorted(conflicting))
        raise ValueError(
            f"В обоих аккаунтах уже есть {conflict_label}. "
            "Данные сохранены; обратитесь в поддержку для безопасного объединения"
        )
    source_conversations = [
        str(row["id"])
        for row in conn.execute(
            "SELECT id FROM conversations WHERE chel_id=?", (source_chel_id,),
        ).fetchall()
    ]
    if source_conversations:
        conn.execute(
            "UPDATE conversations SET chel_id=? WHERE chel_id=?",
            (target_chel_id, source_chel_id),
        )
    conn.execute(
        "UPDATE psychology_sessions SET chel_id=? WHERE chel_id=?",
        (target_chel_id, source_chel_id),
    )
    conn.execute(
        "UPDATE payment_orders SET chel_id=? WHERE chel_id=?",
        (target_chel_id, source_chel_id),
    )
    for table in (
        "weight_control_states", "weight_control_body_measurements",
        "weight_control_meals", "weight_control_reminders",
        "weight_control_reminder_outbox", "psychology_dialog_states",
        "memories", "body_symptoms",
    ):
        conn.execute(
            f"UPDATE {table} SET chel_id=? WHERE chel_id=?",
            (target_chel_id, source_chel_id),
        )
    source_profile = conn.execute(
        "SELECT 1 FROM user_profile WHERE chel_id=?", (source_chel_id,),
    ).fetchone()
    target_profile = conn.execute(
        "SELECT 1 FROM user_profile WHERE chel_id=?", (target_chel_id,),
    ).fetchone()
    if source_profile and not target_profile:
        conn.execute(
            "UPDATE user_profile SET chel_id=? WHERE chel_id=?",
            (target_chel_id, source_chel_id),
        )
    source_onboarding = conn.execute(
        "SELECT 1 FROM onboarding_state WHERE chel_id=?", (source_chel_id,),
    ).fetchone()
    target_onboarding = conn.execute(
        "SELECT 1 FROM onboarding_state WHERE chel_id=?", (target_chel_id,),
    ).fetchone()
    if source_onboarding and not target_onboarding:
        conn.execute(
            "UPDATE onboarding_state SET chel_id=? WHERE chel_id=?",
            (target_chel_id, source_chel_id),
        )
    conn.execute(
        """UPDATE users SET
             result_entry_at=COALESCE(result_entry_at,(
               SELECT result_entry_at FROM users WHERE chel_id=?
             )),
             last_seen_at=MAX(last_seen_at,(
               SELECT last_seen_at FROM users WHERE chel_id=?
             ))
           WHERE chel_id=?""",
        (source_chel_id, source_chel_id, target_chel_id),
    )


def create_auth_intent(provider: str, return_path: str = "") -> dict:
    """Create a short-lived token that lets a messenger bind the current browser user."""
    provider = _messenger_provider(provider)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=settings.auth_intent_ttl_seconds)
    raw_token = secrets.token_urlsafe(32)
    chel_id = current_chel_id()
    with _write_lock, connection() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE chel_id = ?", (chel_id,)).fetchone():
            raise ValueError("Пользователь не найден")
        return_path = _service_auth_return_path(conn, chel_id, return_path)
        conn.execute(
            """INSERT INTO auth_intents
            (token_hash, chel_id, provider, return_path, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (
                _token_hash(raw_token), chel_id, provider, return_path,
                expires_at.isoformat(), now.isoformat(),
            ),
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
        "return_path": return_path,
        "expires_at": expires_at.isoformat(),
    }


def create_messenger_login(
    provider: str,
    provider_user_id: str | int,
    chat_id: str | int = "",
    intent_token: str = "",
    legacy_chel_id: int | None = None,
    from_manager: str = "",
) -> dict:
    """Bind a verified messenger identity and issue a one-time Consilium login."""
    provider = _messenger_provider(provider)
    external_id = str(provider_user_id or "").strip()
    if not external_id or len(external_id) > 128:
        raise ValueError("Не указан идентификатор пользователя мессенджера")
    recipient_chat_id = str(chat_id or "").strip()
    if len(recipient_chat_id) > 128:
        raise ValueError("Некорректный идентификатор чата мессенджера")
    if recipient_chat_id and not re.fullmatch(r"-?\d+", recipient_chat_id):
        raise ValueError("Некорректный идентификатор чата мессенджера")
    if legacy_chel_id is not None and int(legacy_chel_id) <= 0:
        raise ValueError("chel_id должен быть положительным числом")

    now = datetime.now(timezone.utc)
    manager = normalize_from_manager(from_manager)
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
                """UPDATE external_identities
                SET last_login_at = ?, chat_id = CASE WHEN ? <> '' THEN ? ELSE chat_id END
                WHERE id = ?""",
                (now.isoformat(), recipient_chat_id, recipient_chat_id, identity["id"]),
            )
            if intent and intent["chel_id"] != chel_id:
                source_chel_id = str(intent["chel_id"])
                _move_masterclass_psychology_data(conn, source_chel_id, chel_id)
                _move_link_attribution(conn, source_chel_id, chel_id)
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
                """INSERT INTO users (chel_id, created_at, last_seen_at, from_manager)
                VALUES (?, ?, ?, ?) ON CONFLICT(chel_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    from_manager=CASE
                        WHEN users.registered_at IS NULL AND users.from_manager = ''
                        THEN excluded.from_manager
                        ELSE users.from_manager
                    END""",
                (chel_id, now.isoformat(), now.isoformat(), manager),
            )
            conn.execute(
                """INSERT INTO external_identities
                (provider, provider_user_id, chat_id, chel_id, legacy_chel_id,
                 access_status, created_at, last_login_at)
                VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
                (
                    provider, external_id, recipient_chat_id, chel_id, legacy_chel_id,
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
            """INSERT INTO login_tokens
               (token_hash, chel_id, return_path, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                _token_hash(raw_token), chel_id,
                str(intent["return_path"] or "") if intent else "",
                expires_at.isoformat(), now.isoformat(),
            ),
        )
        conn.execute(
            """DELETE FROM login_tokens
            WHERE expires_at < ? AND used_at IS NULL""",
            ((now - timedelta(days=1)).isoformat(),),
        )
        conn.commit()
    return {
        "token": raw_token, "chel_id": chel_id,
        "return_path": str(intent["return_path"] or "") if intent else "",
        "expires_at": expires_at.isoformat(),
    }


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
        "return_path": str(token["return_path"] or ""),
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


def get_consumed_login_return_path(raw_token: str) -> str:
    """Return the validated redirect stored with an already-consumed login."""
    if not raw_token:
        return ""
    with connection() as conn:
        token = conn.execute(
            """SELECT return_path FROM login_tokens
            WHERE token_hash = ? AND used_at IS NOT NULL""",
            (_token_hash(raw_token),),
        ).fetchone()
    return str(token["return_path"] or "") if token else ""


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


def revoke_user_session(session_value: str) -> bool:
    """Revoke only the browser session represented by the supplied cookie."""
    if not session_value:
        return False
    with _write_lock, connection() as conn:
        updated = conn.execute(
            """UPDATE user_sessions SET revoked_at = ?
            WHERE session_hash = ? AND revoked_at IS NULL""",
            (utc_now(), _token_hash(session_value)),
        )
        conn.commit()
    return updated.rowcount == 1


def create_linked_user_session(chel_id: str) -> dict:
    """Create a browser session for a user reached through an opaque service link."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=settings.session_ttl_days)
    session_value = secrets.token_urlsafe(32)
    with _write_lock, connection() as conn:
        linked = conn.execute(
            """SELECT 1 FROM external_identities
            WHERE chel_id=? AND access_status='active' LIMIT 1""",
            (str(chel_id),),
        ).fetchone()
        if not linked:
            raise ValueError("Для входа по ссылке сначала привяжите мессенджер")
        conn.execute(
            """INSERT INTO user_sessions
            (session_hash,chel_id,created_at,last_seen_at,expires_at)
            VALUES (?,?,?,?,?)""",
            (_token_hash(session_value), str(chel_id), now.isoformat(), now.isoformat(), expires.isoformat()),
        )
        conn.commit()
    return {"session": session_value, "chel_id": str(chel_id), "expires_at": expires.isoformat()}


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
    if is_test_user(str(item.get("chel_id") or current_chel_id())):
        return 0
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


def _backfill_known_ai_usage_pricing(conn: sqlite3.Connection) -> int:
    """Price earlier unpriced rows when a missing model tariff is added."""
    from .ai_costs import cost_fields_for_usage

    updated = 0
    rows = conn.execute(
        """SELECT id, model, long_context, input_tokens, cached_input_tokens,
                  output_tokens
           FROM ai_usage WHERE pricing_known = 0"""
    ).fetchall()
    for row in rows:
        costs = cost_fields_for_usage(
            row["model"], row["input_tokens"], row["cached_input_tokens"],
            row["output_tokens"], bool(row["long_context"]),
        )
        if not costs["pricing_known"]:
            continue
        conn.execute(
            """UPDATE ai_usage SET pricing_key=?, pricing_known=1,
                   input_rate=?, cached_input_rate=?, output_rate=?,
                   input_cost_usd=?, cached_input_cost_usd=?, output_cost_usd=?,
                   total_cost_usd=? WHERE id=?""",
            (
                costs["pricing_key"], costs["input_rate"],
                costs["cached_input_rate"], costs["output_rate"],
                costs["input_cost_usd"], costs["cached_input_cost_usd"],
                costs["output_cost_usd"], costs["total_cost_usd"], row["id"],
            ),
        )
        updated += 1
    return updated


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
    date_where = "created_at >= ?" if started_at else "1=1"
    where = f"IS_TEST_INN_USER(chel_id) = 0 AND {date_where}"
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
        all_time = normalized_summary(conn.execute(
            f"{summary_sql} WHERE IS_TEST_INN_USER(chel_id) = 0"
        ).fetchone())
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
            FROM ai_usage WHERE IS_TEST_INN_USER(chel_id) = 0 AND created_at >= ?
            GROUP BY day ORDER BY day""",
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
            "Расчёт по токенам из ответов API и сохранённым тарифам OpenAI и "
            "Yandex AI Studio. Окончательная сумма определяется биллингом провайдера."
        ),
    }


def admin_dashboard(days: int = 14, limit: int = 100) -> dict:
    """Return privacy-conscious aggregate analytics without message or profile text."""
    days = max(7, min(90, int(days)))
    limit = max(10, min(250, int(limit)))
    now = datetime.now(timezone.utc)
    first_day = (now - timedelta(days=days - 1)).date()
    real_users = "IS_STATS_USER(chel_id) = 1 AND registered_at IS NOT NULL"

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
                  AND IS_STATS_USER(e.chel_id) = 1"""
            ),
            "onboarding_complete": scalar(
                """SELECT COUNT(*) FROM onboarding_state
                WHERE status = 'complete'
                  AND IS_STATS_USER(chel_id) = 1"""
            ),
            "profiles_with_tube": scalar(
                """SELECT COUNT(*) FROM user_profile
                WHERE TRIM(tube_number) <> ''
                  AND IS_STATS_USER(chel_id) = 1"""
            ),
            "result_entry_users": scalar(
                """SELECT COUNT(*) FROM users
                WHERE result_entry_at IS NOT NULL
                  AND registered_at IS NOT NULL
                  AND IS_STATS_USER(chel_id) = 1"""
            ),
            "conversations_total": scalar(
                """SELECT COUNT(*) FROM conversations
                WHERE IS_STATS_USER(chel_id) = 1"""
            ),
            "messages_total": scalar(
                """SELECT COUNT(*) FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE IS_STATS_USER(c.chel_id) = 1"""
            ),
            "human_requests": scalar(
                """SELECT COUNT(*) FROM conversations
                WHERE human_ticket_id IS NOT NULL
                  AND IS_STATS_USER(chel_id) = 1"""
            ),
            "human_pending": scalar(
                """SELECT COUNT(*) FROM conversations
                WHERE human_status = 'pending'
                  AND IS_STATS_USER(chel_id) = 1"""
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
            WHERE IS_STATS_USER(c.chel_id) = 1
              AND m.created_at >= ? GROUP BY day""",
            (first_day.isoformat(),),
        ).fetchall()
        conversation_rows = conn.execute(
            """SELECT SUBSTR(created_at, 1, 10) AS day, COUNT(*) AS value
            FROM conversations
            WHERE IS_STATS_USER(chel_id) = 1
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
                  AND IS_STATS_USER(c.chel_id) = 1
                GROUP BY m.agent_id ORDER BY value DESC"""
            ).fetchall()
        ]
        human_channels = [
            {"channel": row["channel"], "requests": int(row["value"])}
            for row in conn.execute(
                """SELECT COALESCE(human_channel, 'not_selected') AS channel, COUNT(*) AS value
                FROM conversations WHERE human_ticket_id IS NOT NULL
                  AND IS_STATS_USER(chel_id) = 1
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
                WHERE IS_STATS_USER(d.chel_id) = 1
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
                WHERE IS_STATS_USER(d.chel_id) = 1
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
                WHERE IS_STATS_USER(d.chel_id) = 1
                  AND u.registered_at IS NOT NULL
                GROUP BY d.browser ORDER BY users DESC, visits DESC"""
            ).fetchall()
        ]
        summary["tracked_devices"] = scalar(
            """SELECT COUNT(DISTINCT d.chel_id) FROM user_device_stats d
            JOIN users u ON u.chel_id=d.chel_id
            WHERE IS_STATS_USER(d.chel_id) = 1
              AND u.registered_at IS NOT NULL"""
        )

        users = [
            dict(row) for row in conn.execute(
                f"""SELECT u.chel_id, u.registered_at AS created_at, u.last_seen_at,
                    CASE WHEN u.result_entry_at IS NOT NULL THEN 'result' ELSE 'standard' END AS entry_flow,
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
                WHERE IS_STATS_USER(u.chel_id) = 1 AND u.registered_at IS NOT NULL
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
                WHERE IS_STATS_USER(c.chel_id) = 1
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
              AND IS_STATS_USER(chel_id) = 1
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


def admin_manager_attribution(
    period: str = "30", date_from: str = "", date_to: str = "",
) -> dict:
    """Aggregate immutable manager attribution for registered users."""
    period = str(period or "30").strip().lower()
    now = datetime.now(timezone.utc)
    date_from = str(date_from or "").strip()
    date_to = str(date_to or "").strip()
    try:
        custom_from = date.fromisoformat(date_from) if date_from else None
        custom_to = date.fromisoformat(date_to) if date_to else None
    except ValueError as exc:
        raise ValueError("Дата должна быть указана в формате ГГГГ-ММ-ДД") from exc
    if custom_from and custom_to and custom_from > custom_to:
        raise ValueError("Дата начала периода не может быть позже даты окончания")
    if custom_from or custom_to:
        started_at = ""
    elif period == "all":
        started_at = ""
    elif period == "today":
        started_at = now.date().isoformat()
    else:
        try:
            days = max(1, min(3650, int(period)))
        except ValueError as exc:
            raise ValueError("Неизвестный период статистики") from exc
        started_at = (now - timedelta(days=days - 1)).date().isoformat()

    where = """IS_STATS_USER(u.chel_id) = 1
        AND u.registered_at IS NOT NULL"""
    params: list[str] = []
    report_timezone = timezone(timedelta(hours=3))
    if custom_from:
        from_boundary = datetime.combine(
            custom_from, datetime.min.time(), tzinfo=report_timezone,
        ).astimezone(timezone.utc).isoformat()
        where += " AND u.registered_at >= ?"
        params.append(from_boundary)
    elif started_at:
        where += " AND SUBSTR(u.registered_at, 1, 10) >= ?"
        params.append(started_at)
    if custom_to:
        to_boundary = datetime.combine(
            custom_to + timedelta(days=1), datetime.min.time(), tzinfo=report_timezone,
        ).astimezone(timezone.utc).isoformat()
        where += " AND u.registered_at < ?"
        params.append(to_boundary)

    with connection() as conn:
        total_users = int(conn.execute(
            f"SELECT COUNT(*) FROM users u WHERE {where}", tuple(params),
        ).fetchone()[0] or 0)
        attributed_users = int(conn.execute(
            f"SELECT COUNT(*) FROM users u WHERE {where} AND TRIM(u.from_manager) <> ''",
            tuple(params),
        ).fetchone()[0] or 0)
        rows = conn.execute(
            f"""SELECT u.from_manager, COUNT(*) AS users,
                SUM(CASE WHEN EXISTS (
                    SELECT 1 FROM onboarding_state o
                    WHERE o.chel_id = u.chel_id
                      AND JSON_VALID(o.selected_tests)
                      AND JSON_ARRAY_LENGTH(o.selected_tests) > 0
                ) THEN 1 ELSE 0 END) AS users_with_examinations
            FROM users u
            WHERE {where} AND TRIM(u.from_manager) <> ''
            GROUP BY u.from_manager
            ORDER BY users DESC, u.from_manager""",
            tuple(params),
        ).fetchall()

    managers = []
    for row in rows:
        users = int(row["users"] or 0)
        selected = int(row["users_with_examinations"] or 0)
        managers.append({
            "from_manager": row["from_manager"],
            "manager_name": from_manager_name(row["from_manager"]),
            "users": users,
            "percent_of_all": round(users * 100 / total_users, 1) if total_users else 0,
            "users_with_examinations": selected,
            "examination_conversion": round(selected * 100 / users, 1) if users else 0,
        })
    return {
        "period": period,
        "period_started_at": date_from or started_at or None,
        "period_ended_at": date_to or None,
        "total_users": total_users,
        "attributed_users": attributed_users,
        "attributed_percent": round(attributed_users * 100 / total_users, 1) if total_users else 0,
        "managers": managers,
    }


def admin_manager_first_arrivals(date_from: str = "", date_to: str = "") -> dict:
    """Aggregate new users who actually opened an application page.

    ``users`` rows can also be created by API and integration requests, so
    ``created_at`` is not an arrival signal. ``user_device_stats`` is written
    only when a non-bot browser receives an application page. Its first visit
    determines the arrival period, including people whose identity was
    provisioned earlier by a messenger. Registration is not required. The
    immutable ``from_manager`` value preserves the original attribution.
    Users whose initial route is a master class are excluded.
    """
    date_from = str(date_from or "").strip()
    date_to = str(date_to or "").strip()
    try:
        custom_from = date.fromisoformat(date_from) if date_from else None
        custom_to = date.fromisoformat(date_to) if date_to else None
    except ValueError as exc:
        raise ValueError("Дата должна быть указана в формате ГГГГ-ММ-ДД") from exc
    if custom_from and custom_to and custom_from > custom_to:
        raise ValueError("Дата начала периода не может быть позже даты окончания")

    where = "IS_STATS_USER(u.chel_id) = 1"
    params: list[str] = []
    first_open_sql = (
        "(SELECT MIN(d.first_seen_at) FROM user_device_stats d "
        "WHERE d.chel_id = u.chel_id)"
    )
    report_timezone = timezone(timedelta(hours=3))
    if custom_from:
        from_boundary = datetime.combine(
            custom_from, datetime.min.time(), tzinfo=report_timezone,
        ).astimezone(timezone.utc).isoformat()
        where += f" AND {first_open_sql} >= ?"
        params.append(from_boundary)
    if custom_to:
        to_boundary = datetime.combine(
            custom_to + timedelta(days=1), datetime.min.time(), tzinfo=report_timezone,
        ).astimezone(timezone.utc).isoformat()
        where += f" AND {first_open_sql} < ?"
        params.append(to_boundary)
    where += f" AND {first_open_sql} IS NOT NULL"
    # A direct master-class URL is recorded immediately before the page open;
    # choosing that route on the generic landing is recorded shortly after it.
    # Keep a bounded window so a long-standing regular user who visits a master
    # class much later does not disappear from historical newcomer statistics.
    where += f""" AND NOT EXISTS (
        SELECT 1 FROM masterclass_access_users m
        WHERE m.chel_id = u.chel_id
          AND julianday(m.first_seen_at) <= julianday({first_open_sql}) + 1
    )"""

    with connection() as conn:
        total_users = int(conn.execute(
            f"SELECT COUNT(*) FROM users u WHERE {where}", tuple(params),
        ).fetchone()[0] or 0)
        attributed_users = int(conn.execute(
            f"SELECT COUNT(*) FROM users u WHERE {where} AND TRIM(u.from_manager) <> ''",
            tuple(params),
        ).fetchone()[0] or 0)
        rows = conn.execute(
            f"""SELECT u.from_manager, COUNT(*) AS users
            FROM users u
            WHERE {where} AND TRIM(u.from_manager) <> ''
            GROUP BY u.from_manager
            ORDER BY users DESC, u.from_manager""",
            tuple(params),
        ).fetchall()

    return {
        "period_started_at": date_from or None,
        "period_ended_at": date_to or None,
        "total_users": total_users,
        "attributed_users": attributed_users,
        "managers": [{
            "from_manager": row["from_manager"],
            "manager_name": from_manager_name(row["from_manager"]),
            "users": int(row["users"] or 0),
        } for row in rows],
    }


def admin_first_arrival_count(
    period: str = "30", date_from: str = "", date_to: str = "",
) -> int:
    """Count verified first application opens in the selected Moscow period.

    The result deliberately ignores device, source and registration filters:
    this is the absolute number of entirely new people requested by the
    dashboard card. Explicit dates replace the ready-made period.
    """
    period = str(period or "30").strip().lower()
    date_from = str(date_from or "").strip()
    date_to = str(date_to or "").strip()
    if not date_from and not date_to:
        if period != "all":
            today = datetime.now(timezone(timedelta(hours=3))).date()
            if period == "today":
                days = 1
            else:
                try:
                    days = max(1, min(3650, int(period)))
                except ValueError as exc:
                    raise ValueError("Неизвестный период статистики") from exc
            date_from = (today - timedelta(days=days - 1)).isoformat()
            date_to = today.isoformat()
    return int(admin_manager_first_arrivals(date_from, date_to)["total_users"])


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
    sort: str = "",
    order: str = "desc",
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
    sort_columns = {
        "users": {
            "chel_id": "u.chel_id", "from_manager": "from_manager", "entry_flow": "entry_flow",
            "created_at": "u.registered_at", "last_seen_at": "u.last_seen_at",
            "onboarding_status": "onboarding_status", "messengers": "messengers",
            "conversations": "conversations", "messages": "messages",
        },
        "conversations": {
            "id": "c.id", "chel_id": "c.chel_id", "active_agent": "c.active_agent",
            "status": "c.status", "human_status": "c.human_status",
            "messages": "messages", "updated_at": "c.updated_at",
        },
        "human_requests": {
            "ticket_id": "c.human_ticket_id", "chel_id": "c.chel_id",
            "channel": "channel", "human_status": "c.human_status",
            "phone": "c.human_phone", "updated_at": "c.updated_at",
        },
        "devices": {
            "chel_id": "d.chel_id", "device_type": "d.device_type",
            "operating_system": "d.operating_system", "browser": "d.browser",
            "first_seen_at": "d.first_seen_at", "last_seen_at": "d.last_seen_at",
            "visit_count": "d.visit_count",
        },
    }
    default_sorts = {
        "users": "last_seen_at", "conversations": "updated_at",
        "human_requests": "updated_at", "devices": "last_seen_at",
    }
    sort = str(sort or "").strip()
    if sort not in sort_columns[name]:
        sort = default_sorts[name]
    order = str(order or "desc").strip().lower()
    if order not in {"asc", "desc"}:
        raise ValueError("Неизвестное направление сортировки")
    order_sql = f"{sort_columns[name][sort]} {order.upper()}"
    overall_total = None
    period_total = None

    with connection() as conn:
        if name == "users":
            where = """IS_STATS_USER(u.chel_id) = 1
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
                    OR COALESCE(u.from_manager, '') LIKE ? ESCAPE '\\'
                    OR CASE WHEN u.result_entry_at IS NOT NULL THEN 'result' ELSE 'standard' END LIKE ? ESCAPE '\\'
                )"""
                params.extend([pattern, pattern, pattern, pattern, pattern])
            overall_total = conn.execute(
                """SELECT COUNT(*) FROM users
                WHERE IS_STATS_USER(chel_id) = 1
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
                        CASE WHEN u.result_entry_at IS NOT NULL THEN 'result' ELSE 'standard' END AS entry_flow,
                        COALESCE(u.from_manager, '') AS from_manager,
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
                    GROUP BY u.chel_id ORDER BY {order_sql}, u.chel_id ASC LIMIT ? OFFSET ?""",
                    tuple(params + [limit, offset]),
                ).fetchall()
            ]
        elif name == "conversations":
            where = """IS_STATS_USER(c.chel_id) = 1"""
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
                    GROUP BY c.id ORDER BY {order_sql}, c.id ASC LIMIT ? OFFSET ?""",
                    tuple(params + [limit, offset]),
                ).fetchall()
            ]
        elif name == "human_requests":
            where = """c.human_ticket_id IS NOT NULL
                AND IS_STATS_USER(c.chel_id) = 1"""
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
                ORDER BY {order_sql}, c.human_ticket_id ASC LIMIT ? OFFSET ?""",
                tuple(params + [limit, offset]),
            ).fetchall():
                item = dict(row)
                phone = str(item.pop("human_phone") or "")
                item["phone"] = f"•••• {phone[-4:]}" if len(phone) >= 4 else ""
                rows.append(item)
        else:
            where = """IS_STATS_USER(d.chel_id) = 1
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
                    ORDER BY {order_sql}, d.chel_id ASC LIMIT ? OFFSET ?""",
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
        "sort": sort,
        "order": order,
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
            "driving_time": "unknown", "dark_in_eyes": "unknown", "joint_pain": "unknown", "fatigue": "unknown",
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


def _staff_role(value: str) -> str:
    role = str(value or "manager").strip().lower()
    if role not in {"manager", "doctor", "psychologist"}:
        raise ValueError("Выберите роль: менеджер, врач-специалист или психолог")
    return role


def admin_list_staff() -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT id, display_name, login, role, is_active, telegram_id, max_id,
            notify_new_requests, notify_new_messages, notify_successful_payments,
            created_at, updated_at,
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
            item["notify_successful_payments"] = bool(item["notify_successful_payments"])
            result.append(item)
    return result


def admin_create_staff(
    display_name: str, login: str, password: str, *, telegram_id="", max_id="",
    role: str = "manager",
    notify_new_requests: bool = True, notify_new_messages: bool = True,
    notify_successful_payments: bool = True,
) -> dict:
    display_name = " ".join(str(display_name or "").split())[:80]
    if len(display_name) < 2:
        raise ValueError("Укажите имя сотрудника")
    login = _staff_login(login)
    encoded = _password_hash(password)
    telegram_id = _staff_messenger_id(telegram_id, "Telegram ID")
    max_id = _staff_messenger_id(max_id, "MAX ID")
    role = _staff_role(role)
    now = utc_now()
    try:
        with _write_lock, connection() as conn:
            cursor = conn.execute(
                """INSERT INTO staff_users
                (display_name, login, password_hash, role, is_active, telegram_id,
                 telegram_chat_id, max_id, max_chat_id, notify_new_requests,
                 notify_new_messages, notify_successful_payments, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    display_name, login, encoded, role, telegram_id, telegram_id,
                    max_id, max_id, int(bool(notify_new_requests)),
                    int(bool(notify_new_messages)), int(bool(notify_successful_payments)),
                    now, now,
                ),
            )
            conn.commit()
            staff_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        details = str(exc).lower()
        if "telegram_id" in details:
            raise ValueError("Этот Telegram ID уже привязан к другому сотруднику") from exc
        if "max_id" in details:
            raise ValueError("Этот MAX ID уже привязан к другому сотруднику") from exc
        raise ValueError("Сотрудник с таким логином уже существует") from exc
    return next(item for item in admin_list_staff() if item["id"] == staff_id)


def admin_update_staff(
    staff_id: int, *, display_name: str | None = None,
    password: str | None = None, is_active: bool | None = None,
    role: str | None = None,
    telegram_id: str | None = None, max_id: str | None = None,
    notify_new_requests: bool | None = None,
    notify_new_messages: bool | None = None,
    notify_successful_payments: bool | None = None,
) -> dict | None:
    updates: list[str] = []
    params: list = []
    if display_name is not None:
        name = " ".join(str(display_name).split())[:80]
        if len(name) < 2:
            raise ValueError("Укажите имя сотрудника")
        updates.append("display_name = ?")
        params.append(name)
    if password is not None and str(password):
        updates.append("password_hash = ?")
        params.append(_password_hash(password))
    if is_active is not None:
        updates.append("is_active = ?")
        params.append(int(bool(is_active)))
    if role is not None:
        updates.append("role = ?")
        params.append(_staff_role(role))
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
    if notify_successful_payments is not None:
        updates.append("notify_successful_payments = ?")
        params.append(int(bool(notify_successful_payments)))
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
                raise ValueError(f"Этот {label} уже привязан к другому сотруднику")
        if telegram_id is not None:
            normalized = _staff_messenger_id(telegram_id, "Telegram ID")
            if normalized:
                conn.execute(
                    """UPDATE manager_notification_outbox
                    SET recipient_id=?,status='pending',attempts=0,next_attempt_at=NULL,
                        lease_token=NULL,leased_at=NULL,last_error=''
                    WHERE staff_user_id=? AND provider='telegram'
                      AND status IN ('pending','delivering','failed')""",
                    (normalized, int(staff_id)),
                )
            else:
                conn.execute(
                    """UPDATE manager_notification_outbox SET status='cancelled',
                    lease_token=NULL,leased_at=NULL
                    WHERE staff_user_id=? AND provider='telegram' AND status<>'sent'""",
                    (int(staff_id),),
                )
        if max_id is not None:
            normalized = _staff_messenger_id(max_id, "MAX ID")
            if normalized:
                conn.execute(
                    """UPDATE manager_notification_outbox
                    SET recipient_id=?,status='pending',attempts=0,next_attempt_at=NULL,
                        lease_token=NULL,leased_at=NULL,last_error=''
                    WHERE staff_user_id=? AND provider='max'
                      AND status IN ('pending','delivering','failed')""",
                    (normalized, int(staff_id)),
                )
            else:
                conn.execute(
                    """UPDATE manager_notification_outbox SET status='cancelled',
                    lease_token=NULL,leased_at=NULL
                    WHERE staff_user_id=? AND provider='max' AND status<>'sent'""",
                    (int(staff_id),),
                )
        if role is not None:
            conn.execute(
                "DELETE FROM manager_notification_outbox WHERE staff_user_id = ? AND status <> 'sent'",
                (int(staff_id),),
            )
        if notify_new_requests is False:
            conn.execute(
                """UPDATE manager_notification_outbox SET status = 'cancelled'
                WHERE staff_user_id = ?
                  AND event_type IN (
                    'new_request','request_cancelled','weight_program_completed',
                    'psychology_session_requested'
                  )
                  AND status <> 'sent'""",
                (int(staff_id),),
            )
        if notify_new_messages is False:
            conn.execute(
                """UPDATE manager_notification_outbox SET status = 'cancelled'
                WHERE staff_user_id = ? AND event_type = 'new_message' AND status <> 'sent'""",
                (int(staff_id),),
            )
        if notify_successful_payments is False:
            conn.execute(
                """UPDATE manager_notification_outbox SET status = 'cancelled'
                WHERE staff_user_id = ? AND event_type = 'psychology_payment_succeeded'
                  AND status <> 'sent'""",
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
            raise ValueError("Активный сотрудник не найден")
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
            raise ValueError("Учётная запись сотрудника отключена")
        id_column = "telegram_id" if provider == "telegram" else "max_id"
        chat_column = "telegram_chat_id" if provider == "telegram" else "max_chat_id"
        duplicate = conn.execute(
            f"SELECT id FROM staff_users WHERE {id_column} = ? AND id <> ?",
            (provider_user_id, row["staff_user_id"]),
        ).fetchone()
        if duplicate:
            raise ValueError("Этот аккаунт мессенджера уже привязан к другому сотруднику")
        conn.execute(
            f"UPDATE staff_users SET {id_column} = ?, {chat_column} = ?, updated_at = ? WHERE id = ?",
            (provider_user_id, chat_id, now.isoformat(), row["staff_user_id"]),
        )
        conn.execute(
            """UPDATE manager_notification_outbox
            SET recipient_id = ?, status = 'pending', attempts = 0,
                next_attempt_at = NULL, lease_token = NULL, leased_at = NULL,
                last_error = ''
            WHERE staff_user_id = ? AND provider = ?
              AND status IN ('pending', 'delivering', 'failed')""",
            (chat_id, row["staff_user_id"], provider),
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
    message_text: str = "", recipient_role: str | None = None,
    request_kind: str = "", request_number: str = "", amount_kopecks: int = 0,
) -> int:
    if event_type not in {
        "new_request", "new_message", "request_cancelled",
        "weight_program_completed", "psychology_session_requested",
        "psychology_payment_succeeded", "psychology_crisis",
    }:
        raise ValueError("Неизвестный тип уведомления")
    now = utc_now()
    with _write_lock, connection() as conn:
        conversation = conn.execute(
            """SELECT c.id, c.chel_id, c.human_ticket_id, c.human_channel,
                COALESCE(c.human_recipient_role, 'manager') AS human_recipient_role,
                COALESCE(p.preferred_name, '') AS preferred_name
            FROM conversations c LEFT JOIN user_profile p ON p.chel_id = c.chel_id
            WHERE c.id = ?""",
            (conversation_id,),
        ).fetchone()
        if not conversation:
            return 0
        recipient_role = _staff_role(recipient_role or conversation["human_recipient_role"])
        manager_url = f"{settings.public_base_url}/manager?conversation={conversation_id}"
        name = conversation["preferred_name"] or f"Пользователь {conversation['chel_id'][-6:]}"
        state_row = _psychology_state_row(conn, conversation_id)
        try:
            psychology_state = json.loads(state_row["state_json"]) if state_row else {}
        except (TypeError, json.JSONDecodeError):
            psychology_state = {}
        client_card = _psychology_client_card_text(
            psychology_state,
            conversation_id=conversation_id,
            request_number=str(request_number or conversation["human_ticket_id"] or ""),
        ) if recipient_role == "psychologist" else ""
        if event_type == "psychology_crisis":
            title = "Срочно: психологическая безопасность"
            body = (
                f"{name} отправил сообщение с признаками непосредственного риска "
                "причинения вреда себе или другим. ИИ остановлен. Срочно откройте "
                "защищённый чат и оцените ситуацию."
            )
            body += f"\n\n{client_card}"
            preference = None
        elif event_type == "psychology_payment_succeeded":
            amount = max(0, int(amount_kopecks or 0))
            amount_label = f"{amount // 100:,}".replace(",", " ") + " ₽"
            title = "Успешная оплата сессии"
            body = (
                f"{name} успешно оплатил сессию с психологом на сумму {amount_label}. "
                "Откройте чат пользователя, чтобы согласовать время и формат сессии."
            )
            preference = "notify_successful_payments"
        elif event_type == "psychology_session_requested":
            visible_request_number = str(
                request_number or conversation["human_ticket_id"] or "—"
            )
            title = f"Заявка № {visible_request_number} — новая сессия"
            body = (
                f"Номер заявки: {visible_request_number}. "
                f"{name} оставил заявку на сессию с психологом. "
                "Откройте чат, чтобы согласовать время и формат."
            )
            body += f"\n\n{client_card}"
            preference = "notify_new_requests"
        elif event_type == "weight_program_completed":
            title = "Завершён марафон контроля питания"
            body = (
                f"{name} завершил 14-дневный марафон. "
                "Откройте чат, чтобы увидеть дневник и дальнейшее общение."
            )
            preference = "notify_new_requests"
        elif event_type == "request_cancelled":
            title = "Заявка на чекап отменена"
            body = f"{name} отменил заявку на приобретение чекапа."
            preference = "notify_new_requests"
        elif event_type == "new_request":
            if recipient_role == "doctor":
                if conversation["human_channel"] == "paid_consultation":
                    title = "Оплачена консультация врача"
                    body = (
                        f"{name} оплатил консультацию. Откройте чат, чтобы "
                        "согласовать дату и время."
                    )
                else:
                    title = "Новая расшифровка чек-апа"
                    body = (
                        f"{name} просит врача расшифровать результаты. "
                        f"Обращение {conversation['human_ticket_id'] or conversation_id[:8]}."
                    )
            elif recipient_role == "psychologist":
                if request_kind == "psychology_session_messenger_help":
                    title = "Нужно согласовать сессию без мессенджера"
                    body = (
                        f"{name} хочет обсудить проведение платной сессии без "
                        "привязки Telegram или MAX. Откройте психологический чат "
                        "и помогите согласовать другой способ связи."
                    )
                else:
                    title = "Пользователь просит подключить психолога"
                    body = (
                        f"{name} просит подключить психолога к переписке. "
                        f"Обращение {conversation['human_ticket_id'] or conversation_id[:8]}."
                    )
            else:
                if request_kind == "checkup_purchase":
                    title = "Пользователь хочет купить чекап"
                    excerpt = " ".join(str(message_text or "").split())[:240]
                    body = f"{name} хочет приобрести чекап."
                    if excerpt:
                        body += f" Сообщение: «{excerpt}»."
                elif request_kind == "psychology_session_messenger_help":
                    title = "Нужно согласовать сессию без мессенджера"
                    body = (
                        f"{name} хочет обсудить проведение платной сессии без "
                        "привязки Telegram или MAX. Откройте диалог психолога и помогите "
                        "согласовать способ связи."
                    )
                else:
                    title = "Новое обращение в Консилиуме"
                    body = (
                        f"{name} просит подключить человека. "
                        f"Обращение {conversation['human_ticket_id'] or conversation_id[:8]}."
                    )
            preference = "notify_new_requests"
        else:
            title = "Новое сообщение пользователя"
            destination = (
                "врача" if recipient_role == "doctor"
                else "психолога" if recipient_role == "psychologist"
                else "менеджера"
            )
            body = f"{name} отправил новое сообщение. Откройте защищённую панель {destination}."
            preference = "notify_new_messages"
        if preference:
            recipients = conn.execute(
                f"""SELECT id, telegram_chat_id, max_chat_id FROM staff_users
                WHERE is_active = 1 AND role = ? AND {preference} = 1""",
                (recipient_role,),
            ).fetchall()
        else:
            recipients = conn.execute(
                """SELECT id, telegram_chat_id, max_chat_id FROM staff_users
                WHERE is_active = 1 AND role = ?""",
                (recipient_role,),
            ).fetchall()
        inserted = 0
        payload = json.dumps({
            "title": title, "body": body, "manager_url": manager_url,
            "action_url": manager_url,
            "action_label": (
                "Открыть чат"
                if event_type in {
                    "weight_program_completed", "psychology_session_requested",
                    "psychology_payment_succeeded",
                }
                else "Открыть чат пользователя" if recipient_role == "doctor"
                else "Открыть диалог"
            ),
            "conversation_id": conversation_id,
            "ticket_id": conversation["human_ticket_id"] or "",
            "request_number": str(request_number or conversation["human_ticket_id"] or ""),
            "recipient_role": recipient_role,
            "request_kind": str(request_kind or ""),
            "amount_kopecks": max(0, int(amount_kopecks or 0)),
            "client_card": client_card,
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
            FROM manager_notification_outbox AS outbox
            WHERE provider = ? AND attempts < 100
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
              AND (
                status = 'pending' OR (status = 'delivering' AND leased_at < ?)
              )
              AND EXISTS (
                SELECT 1 FROM staff_users AS staff
                JOIN conversations AS conversation ON conversation.id = outbox.conversation_id
                WHERE staff.id = outbox.staff_user_id AND staff.is_active = 1
                  AND staff.role = COALESCE(conversation.human_recipient_role, 'manager')
                  AND (
                    outbox.event_type IN ('request_cancelled','weight_program_completed','psychology_session_requested','psychology_payment_succeeded','psychology_crisis')
                    OR COALESCE(conversation.human_status, 'closed') <> 'closed'
                  )
                  AND (
                    (outbox.event_type = 'new_request' AND staff.notify_new_requests = 1)
                    OR (outbox.event_type = 'request_cancelled' AND staff.notify_new_requests = 1)
                    OR (outbox.event_type = 'weight_program_completed' AND staff.notify_new_requests = 1)
                    OR (outbox.event_type = 'psychology_session_requested' AND staff.notify_new_requests = 1)
                    OR (outbox.event_type = 'psychology_payment_succeeded' AND staff.notify_successful_payments = 1)
                    OR outbox.event_type = 'psychology_crisis'
                    OR (outbox.event_type = 'new_message' AND staff.notify_new_messages = 1)
                  )
              )
            ORDER BY id LIMIT ?""",
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
        permanent_failure = not success and _permanent_messenger_delivery_error(error)
        retry_delay = min(3600, 5 * (2 ** min(int(row["attempts"]), 10)))
        next_attempt_at = (
            (now_dt + timedelta(seconds=retry_delay)).isoformat()
            if not success and not permanent_failure else None
        )
        cursor = conn.execute(
            """UPDATE manager_notification_outbox SET status = ?, sent_at = ?,
                last_error = ?, next_attempt_at = ?, lease_token = NULL, leased_at = NULL
            WHERE id = ? AND lease_token = ? AND status = 'delivering'""",
            (
                "sent" if success else ("failed" if permanent_failure else "pending"),
                now if success else None,
                str(error or "")[:500], next_attempt_at,
                int(notification_id), str(lease_token or ""),
            ),
        )
        conn.commit()
    return bool(cursor.rowcount)


def list_examinations() -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT id, name, default_name, description, includes, price,
                competitor_price, price_without_discount,
                competitor_label, retail_price_label, discount_price_label,
                show_competitor_price, show_retail_price, show_discount_price,
                created_at, updated_at
            FROM examination_catalog ORDER BY created_at, name"""
        ).fetchall()
    return [dict(row) for row in rows]


def examination_default_names() -> dict[str, str]:
    """Return the authoritative Bitrix name for every catalog item ID."""
    with connection() as conn:
        rows = conn.execute(
            "SELECT id, default_name, name FROM examination_catalog"
        ).fetchall()
    return {
        str(row["id"]): str(row["default_name"] or row["name"] or "")[:140]
        for row in rows
    }


def get_lab_result_value_estimate(chel_id: str) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM lab_result_value_estimates WHERE chel_id = ?",
            (str(chel_id or "").strip(),),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    for key in ("matched_exam_ids", "recognized_analytes"):
        try:
            value = json.loads(result.get(key) or "[]")
        except json.JSONDecodeError:
            value = []
        result[key] = value if isinstance(value, list) else []
    return result


def list_lab_result_value_estimates(chel_ids: set[str]) -> dict[str, dict]:
    normalized = sorted({str(item or "").strip() for item in chel_ids if str(item or "").strip()})
    if not normalized:
        return {}
    rows: list = []
    with connection() as conn:
        for offset in range(0, len(normalized), 500):
            chunk = normalized[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(conn.execute(
                f"SELECT * FROM lab_result_value_estimates WHERE chel_id IN ({placeholders})",
                chunk,
            ).fetchall())
    result = {}
    for row in rows:
        item = dict(row)
        for key in ("matched_exam_ids", "recognized_analytes"):
            try:
                value = json.loads(item.get(key) or "[]")
            except json.JSONDecodeError:
                value = []
            item[key] = value if isinstance(value, list) else []
        result[str(item["chel_id"])] = item
    return result


def save_lab_result_value_estimate(chel_id: str, payload: dict) -> dict:
    normalized_chel_id = str(chel_id or "").strip()
    if not normalized_chel_id:
        raise ValueError("Не указан пользователь оценки результатов")
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO lab_result_value_estimates
            (chel_id,med_id,document_fingerprint,status,estimated_amount,confidence,
             document_count,analyzed_document_count,ocr_document_count,matched_exam_ids,
             recognized_analytes,error,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chel_id) DO UPDATE SET
                med_id=excluded.med_id,
                document_fingerprint=excluded.document_fingerprint,
                status=excluded.status,
                estimated_amount=excluded.estimated_amount,
                confidence=excluded.confidence,
                document_count=excluded.document_count,
                analyzed_document_count=excluded.analyzed_document_count,
                ocr_document_count=excluded.ocr_document_count,
                matched_exam_ids=excluded.matched_exam_ids,
                recognized_analytes=excluded.recognized_analytes,
                error=excluded.error,
                updated_at=excluded.updated_at""",
            (
                normalized_chel_id,
                str(payload.get("med_id") or "")[:80],
                str(payload.get("document_fingerprint") or "")[:128],
                str(payload.get("status") or "pending")[:32],
                max(0, int(payload.get("estimated_amount") or 0)),
                max(0.0, min(float(payload.get("confidence") or 0), 1.0)),
                max(0, int(payload.get("document_count") or 0)),
                max(0, int(payload.get("analyzed_document_count") or 0)),
                max(0, int(payload.get("ocr_document_count") or 0)),
                json.dumps(payload.get("matched_exam_ids") or [], ensure_ascii=False),
                json.dumps(payload.get("recognized_analytes") or [], ensure_ascii=False),
                str(payload.get("error") or "")[:500],
                utc_now(),
            ),
        )
        conn.commit()
    return get_lab_result_value_estimate(normalized_chel_id) or {}


def list_linked_user_tubes() -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT chel_id, tube_number FROM user_profile
            WHERE TRIM(COALESCE(tube_number,'')) <> ''
              AND chel_id NOT IN ('chel_legacy','chel_test_default')
            ORDER BY chel_id"""
        ).fetchall()
    return [
        {"chel_id": str(row["chel_id"]), "med_id": str(row["tube_number"])}
        for row in rows
    ]


def _payment_items(value: str) -> list[dict]:
    try:
        items = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return items if isinstance(items, list) else []


def _public_payment_order(row) -> dict | None:
    if not row:
        return None
    result = dict(row)
    result["items"] = _payment_items(result.get("items", "[]"))
    amount_kopecks = int(result.pop("amount_kopecks", 0))
    result["amount"] = f"{amount_kopecks // 100}.{amount_kopecks % 100:02d}"
    result["paid"] = bool(result.get("paid"))
    result["test"] = bool(result.get("test"))
    result["order_type"] = str(result.get("order_type") or "examinations")
    result["conversation_id"] = str(result.pop("target_conversation_id", "") or "")
    if result.get("status") != "pending" or not str(result.get("confirmation_url", "")).startswith("https://"):
        result["confirmation_url"] = ""
    result.pop("idempotence_key", None)
    result.pop("selection_fingerprint", None)
    result.pop("last_error", None)
    # The browser only needs the local order id. Internal identity and provider
    # identifiers stay server-side and are used for ownership/status checks.
    result.pop("chel_id", None)
    result.pop("provider_payment_id", None)
    result.pop("hidden_at", None)
    result.pop("fulfillment_at", None)
    return result


def create_payment_order() -> dict:
    """Freeze the selected catalog items and server-calculated amount before payment."""
    from .onboarding import normalize_examination_selection, online_examination_price

    onboarding = get_onboarding()
    selected_ids = normalize_examination_selection(onboarding.get("selected_tests") or [])
    catalog = {item["id"]: item for item in list_examinations()}
    if not selected_ids or any(item_id not in catalog for item_id in selected_ids):
        raise ValueError("Сначала выберите доступные дополнительные обследования")
    items = [
        {
            "id": item_id,
            "name": str(catalog[item_id]["name"])[:128],
            "default_name": str(
                catalog[item_id].get("default_name") or catalog[item_id]["name"]
            )[:128],
            "price": online_examination_price(catalog[item_id]),
        }
        for item_id in selected_ids
    ]
    if any(item["price"] <= 0 for item in items):
        raise ValueError("Для выбранного обследования не указана корректная цена")
    amount_kopecks = sum(item["price"] * 100 for item in items)
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    now = utc_now()
    chel_id = current_chel_id()
    with _write_lock, connection() as conn:
        existing = conn.execute(
            """SELECT * FROM payment_orders
            WHERE chel_id = ? AND selection_fingerprint = ?
              AND status IN ('creating', 'pending', 'abandoned')
            ORDER BY created_at DESC LIMIT 1""",
            (chel_id, fingerprint),
        ).fetchone()
        if existing:
            return _public_payment_order(existing)
        order_id = f"ord_{uuid.uuid4().hex}"
        conn.execute(
            """INSERT INTO payment_orders
            (id, chel_id, idempotence_key, selection_fingerprint, status,
             amount_kopecks, items, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'creating', ?, ?, ?, ?)""",
            (
                order_id, chel_id, str(uuid.uuid4()), fingerprint,
                amount_kopecks, canonical, now, now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM payment_orders WHERE id = ?", (order_id,)).fetchone()
    return _public_payment_order(row)


def create_consultation_payment_order(conversation_id: str) -> dict:
    """Create a fixed-price doctor consultation order for the current user."""
    conversation = get_conversation(conversation_id)
    if not conversation:
        raise ValueError("Диалог для консультации не найден")
    chel_id = current_chel_id()
    items = [{"id": "doctor_consultation", "name": "Консультация врача", "price": 1000}]
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(
        f"consultation:{conversation_id}:{canonical}".encode("utf-8")
    ).hexdigest()
    now = utc_now()
    with _write_lock, connection() as conn:
        existing = conn.execute(
            """SELECT * FROM payment_orders
            WHERE chel_id = ? AND order_type = 'consultation'
              AND selection_fingerprint = ?
              AND status IN ('creating', 'pending', 'abandoned')
            ORDER BY created_at DESC LIMIT 1""",
            (chel_id, fingerprint),
        ).fetchone()
        if existing:
            return _public_payment_order(existing)
        order_id = f"ord_{uuid.uuid4().hex}"
        conn.execute(
            """INSERT INTO payment_orders
            (id, chel_id, idempotence_key, selection_fingerprint, order_type,
             target_conversation_id, status, amount_kopecks, items, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'consultation', ?, 'creating', 100000, ?, ?, ?)""",
            (
                order_id, chel_id, str(uuid.uuid4()), fingerprint,
                conversation_id, canonical, now, now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM payment_orders WHERE id = ?", (order_id,)).fetchone()
    return _public_payment_order(row)


PSYCHOLOGY_SESSION_MIN_COUNT = 1
PSYCHOLOGY_SESSION_MAX_COUNT = 1
PSYCHOLOGY_SESSION_LEGACY_MAX_COUNT = 7
PSYCHOLOGY_SESSION_BASE_UNIT_PRICE_RUB = 3500
PSYCHOLOGY_SESSION_MIN_UNIT_PRICE_RUB = 3500


def psychology_session_package(session_count: int) -> dict:
    """Return the server-authoritative price for the single 60-minute session."""
    if isinstance(session_count, bool) or (
        isinstance(session_count, float) and not session_count.is_integer()
    ):
        raise ValueError("Можно приобрести одну сессию")
    try:
        count = int(session_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("Можно приобрести одну сессию") from exc
    if not PSYCHOLOGY_SESSION_MIN_COUNT <= count <= PSYCHOLOGY_SESSION_MAX_COUNT:
        raise ValueError("Можно приобрести одну сессию")
    unit_price = PSYCHOLOGY_SESSION_BASE_UNIT_PRICE_RUB
    total_price = unit_price
    return {
        "sessions": count,
        "unit_price": unit_price,
        "total_price": total_price,
        "saving": 0,
    }


def _psychology_order_session_count(items_value) -> int:
    items = items_value if isinstance(items_value, list) else _payment_items(items_value)
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("id") not in {"psychology_session", "psychology_session_package"}:
            continue
        try:
            count = int(item.get("sessions", 1))
        except (TypeError, ValueError):
            count = 1
        # Orders created before the single-session product may contain up to
        # seven already-paid credits. They must remain available to users.
        return max(1, min(PSYCHOLOGY_SESSION_LEGACY_MAX_COUNT, count))
    return 1


def _new_psychology_request_number(conn: sqlite3.Connection) -> str:
    """Generate a human-friendly unique five-digit psychology request number."""
    for _ in range(200):
        request_number = str(secrets.randbelow(90_000) + 10_000)
        if not conn.execute(
            "SELECT 1 FROM psychology_sessions WHERE request_number=?",
            (request_number,),
        ).fetchone():
            return request_number
    raise RuntimeError("Не удалось сформировать номер заявки. Попробуйте ещё раз")


def create_psychology_session_payment_order(
    conversation_id: str, session_count: int = 1,
) -> dict:
    """Create a server-priced 60-minute session for the psychology chat."""
    conversation = get_conversation(conversation_id)
    if not conversation or conversation.get("dialog_type") != "masterclass_psychology":
        raise ValueError("Чат психологической поддержки не найден")
    chel_id = current_chel_id()
    profile = get_profile()
    try:
        age = int(profile.get("age")) if profile.get("age") not in (None, "") else None
    except (TypeError, ValueError):
        age = None
    if age is not None and age < 18:
        raise ValueError(
            "Платные психологические сессии недоступны несовершеннолетним. "
            "Обратитесь к психологу через бесплатную поддержку"
        )
    package = psychology_session_package(session_count)
    count = package["sessions"]
    noun = "сессия" if count == 1 else "сессии" if count < 5 else "сессий"
    items = [{
        "id": "psychology_session",
        "name": f"{count} {noun} с психологом · 60 минут",
        "price": package["total_price"],
        "sessions": count,
        "unit_price": package["unit_price"],
    }]
    canonical = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(
        f"psychology-session:{conversation_id}:{canonical}".encode("utf-8")
    ).hexdigest()
    now = utc_now()
    with _write_lock, connection() as conn:
        existing = conn.execute(
            """SELECT * FROM payment_orders
            WHERE chel_id = ? AND order_type = 'psychology_session'
              AND selection_fingerprint = ?
              AND status IN ('creating', 'pending', 'abandoned')
            ORDER BY created_at DESC LIMIT 1""",
            (chel_id, fingerprint),
        ).fetchone()
        if existing:
            return _public_payment_order(existing)
        order_id = f"ord_{uuid.uuid4().hex}"
        conn.execute(
            """INSERT INTO payment_orders
            (id, chel_id, idempotence_key, selection_fingerprint, order_type,
             target_conversation_id, status, amount_kopecks, items, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'psychology_session', ?, 'creating', ?, ?, ?, ?)""",
            (
                order_id, chel_id, str(uuid.uuid4()), fingerprint,
                conversation_id, package["total_price"] * 100, canonical, now, now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM payment_orders WHERE id = ?", (order_id,)).fetchone()
    return _public_payment_order(row)


def complete_test_psychology_session_payment(order_id: str) -> dict:
    """Complete a payment locally only for the dedicated test master-class link."""
    chel_id = current_chel_id()
    now = utc_now()
    with _write_lock, connection() as conn:
        access = conn.execute(
            """SELECT l.is_test FROM masterclass_access_users AS u
               JOIN masterclass_access_links AS l ON l.id=u.link_id
               WHERE u.chel_id=? ORDER BY u.last_seen_at DESC LIMIT 1""",
            (chel_id,),
        ).fetchone()
        if not access or not bool(access["is_test"]):
            raise ValueError("Тестовая оплата доступна только по тестовой ссылке")
        order = conn.execute(
            """SELECT * FROM payment_orders
               WHERE id=? AND chel_id=? AND order_type='psychology_session'""",
            (str(order_id or ""), chel_id),
        ).fetchone()
        if not order:
            raise ValueError("Заказ тестовой сессии не найден")
        if order["status"] == "succeeded" and bool(order["paid"]):
            return _public_payment_order(order)
        if order["status"] not in {"creating", "pending", "abandoned"}:
            raise ValueError("Тестовый заказ уже завершён")
        conn.execute(
            """UPDATE payment_orders SET provider_payment_id=?,status='succeeded',
               paid=1,test=1,confirmation_url='',paid_at=COALESCE(paid_at,?),
               hidden_at=NULL,updated_at=? WHERE id=?""",
            (f"test_{uuid.uuid4().hex}", now, now, order["id"]),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM payment_orders WHERE id=?", (order["id"],),
        ).fetchone()
    return _public_payment_order(updated)


def psychology_session_summary(conversation_id: str) -> dict:
    """Return call-session history and the free/paid balance for one psychology chat."""
    conversation = get_conversation(conversation_id)
    if not conversation or conversation.get("dialog_type") != "masterclass_psychology":
        raise ValueError("Чат психологической поддержки не найден")
    chel_id = current_chel_id()
    with connection() as conn:
        sessions = conn.execute(
            """SELECT id,session_number,request_number,access_type,status,started_at,completed_at,
                      payment_order_id
               FROM psychology_sessions
               WHERE conversation_id=? AND chel_id=?
               ORDER BY session_number DESC""",
            (conversation_id, chel_id),
        ).fetchall()
        purchased = conn.execute(
            """SELECT id,items,paid_at,created_at FROM payment_orders
               WHERE chel_id=? AND order_type='psychology_session'
                 AND target_conversation_id=? AND status='succeeded' AND paid=1
               ORDER BY COALESCE(paid_at,created_at),created_at""",
            (chel_id, conversation_id),
        ).fetchall()
    session_items = [dict(row) for row in sessions]
    paid_used = sum(
        1 for item in session_items
        if item["access_type"] == "paid" and item["status"] == "completed"
    )
    free_used = any(item["access_type"] == "free" for item in session_items)
    completed = sum(1 for item in session_items if item["status"] == "completed")
    requested = next((item for item in session_items if item["status"] == "requested"), None)
    purchased_count = sum(_psychology_order_session_count(row["items"]) for row in purchased)
    remaining_paid = max(0, purchased_count - paid_used)
    return {
        "conversation_id": conversation_id,
        "sessions": session_items,
        "total_started": len(session_items),
        "completed_count": completed,
        "pending_session": requested,
        "free_available": not free_used,
        "purchased_count": purchased_count,
        "remaining_paid": remaining_paid,
        "available_total": remaining_paid + int(not free_used),
        "packages": [psychology_session_package(1)],
    }


def _psychology_state_row(conn: sqlite3.Connection, conversation_id: str):
    return conn.execute(
        "SELECT state_json,message_count FROM psychology_dialog_states WHERE conversation_id=?",
        (str(conversation_id or ""),),
    ).fetchone()


def get_psychology_dialog_state(conversation_id: str) -> dict:
    """Load Vita's structured state and reconcile server-owned session fields."""
    chel_id = current_chel_id()
    with connection() as conn:
        conversation = conn.execute(
            """SELECT id FROM conversations
               WHERE id=? AND chel_id=? AND dialog_type='masterclass_psychology'""",
            (str(conversation_id or ""), chel_id),
        ).fetchone()
        if not conversation:
            raise ValueError("Чат психологической поддержки не найден")
        row = _psychology_state_row(conn, conversation_id)
    try:
        raw = json.loads(row["state_json"]) if row else {}
    except (TypeError, json.JSONDecodeError):
        raw = {}
    state = normalize_psychology_dialog_state(raw)
    summary = psychology_session_summary(conversation_id)
    pending = summary.get("pending_session") or None
    state.update({
        "free_session_available": bool(summary.get("free_available")),
        "remaining_paid": int(summary.get("remaining_paid") or 0),
        "pending_request": ({
            "request_number": str(pending.get("request_number") or ""),
            "status": str(pending.get("status") or ""),
        } if pending else None),
        "message_index": int(row["message_count"] or 0) if row else 0,
    })
    return state


def save_psychology_dialog_state(
    conversation_id: str, value: dict | None, *, increment_message: bool = True,
) -> dict:
    """Persist normalized Vita state while keeping session balance authoritative."""
    chel_id = current_chel_id()
    normalized = normalize_psychology_dialog_state(value)
    summary = psychology_session_summary(conversation_id)
    pending = summary.get("pending_session") or None
    normalized.update({
        "free_session_available": bool(summary.get("free_available")),
        "remaining_paid": int(summary.get("remaining_paid") or 0),
        "pending_request": ({
            "request_number": str(pending.get("request_number") or ""),
            "status": str(pending.get("status") or ""),
        } if pending else None),
    })
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (chel_id,created_at,last_seen_at) VALUES (?,?,?)",
            (chel_id, now, now),
        )
        conversation = conn.execute(
            """SELECT id FROM conversations
               WHERE id=? AND chel_id=? AND dialog_type='masterclass_psychology'""",
            (str(conversation_id or ""), chel_id),
        ).fetchone()
        if not conversation:
            raise ValueError("Чат психологической поддержки не найден")
        current = _psychology_state_row(conn, conversation_id)
        message_count = int(current["message_count"] or 0) if current else 0
        if increment_message:
            message_count += 1
        conn.execute(
            """INSERT INTO psychology_dialog_states
               (conversation_id,chel_id,state_json,message_count,created_at,updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(conversation_id) DO UPDATE SET
                 chel_id=excluded.chel_id,state_json=excluded.state_json,
                 message_count=excluded.message_count,updated_at=excluded.updated_at""",
            (
                conversation_id, chel_id,
                json.dumps(normalized, ensure_ascii=False), message_count, now, now,
            ),
        )
        conn.commit()
    return {**normalized, "message_index": message_count}


def _psychology_client_card_text(
    state: dict | None, *, conversation_id: str, request_number: str = "",
) -> str:
    """Build the fixed-order private psychologist card required by Vita 2.0."""
    item = normalize_psychology_dialog_state(state)
    unknown = "не обсуждали"
    consent = item.get("consent_summary") is True
    name = item.get("name") or unknown
    age = str(item.get("age")) if item.get("age") is not None else unknown
    timezone_name = item.get("timezone") or unknown
    preferred = item.get("preferred_time") or unknown
    if not consent:
        lines = [
            f"Имя: {name}",
            f"Предпочтительное время: {preferred}",
            "Согласие: согласия на выжимку нет",
        ]
    else:
        manifestations = item.get("manifestations") or {}
        techniques = item.get("techniques_used") or []
        technique_label = "; ".join(
            f"{entry.get('name') or unknown}: {entry.get('before')}→{entry.get('after')} ({entry.get('feedback') or unknown})"
            for entry in techniques[-2:]
        ) or unknown
        risks = "; ".join(
            f"{entry.get('type')}: {entry.get('evidence') or unknown}; ответ: {entry.get('client_answer') or unknown}"
            for entry in (item.get("risk_flags") or [])[-3:]
        ) or "не выявлены"
        quote = " ".join(str(item.get("client_quote") or unknown).split())[:180]
        lines = [
            f"Приоритет: {item.get('priority') or 'routine'}",
            f"Имя / возраст / часовой пояс / время: {name} / {age} / {timezone_name} / {preferred}",
            f"Фраза клиента: «{quote}»",
            f"Категория: {item.get('category_primary') or unknown}; вторичные: {', '.join(item.get('categories_secondary') or []) or unknown}",
            f"Длительность / влияние: {item.get('duration') or unknown} / {item.get('impact_score') if item.get('impact_score') is not None else unknown}",
            "Проявления: мысли — {}; тело — {}; поведение — {}".format(
                manifestations.get("thoughts") or unknown,
                manifestations.get("body") or unknown,
                manifestations.get("behavior") or unknown,
            ),
            f"Контекст: {item.get('context') or unknown}",
            f"Что пробовали: {item.get('tried_before') or unknown}",
            f"Техника и результат: {technique_label}",
            f"Ресурсы: {item.get('resources') or unknown}",
            f"Ожидание: {item.get('expectation') or unknown}",
            f"Риски: {risks}",
            f"Стиль общения: {item.get('communication_style') or unknown}",
            f"Гипотеза-вопрос: {item.get('hypothesis') or unknown}",
            "Согласие: получено",
        ]
    if request_number:
        lines.insert(0, f"Заявка №{request_number}")
    if consent:
        lines.append(f"Полный чат: {settings.public_base_url}/manager?conversation={conversation_id}")
    return "\n".join(lines)[:1300]


def psychology_client_card(conversation_id: str, request_number: str = "") -> str:
    with connection() as conn:
        row = _psychology_state_row(conn, conversation_id)
    try:
        state = json.loads(row["state_json"]) if row else {}
    except (TypeError, json.JSONDecodeError):
        state = {}
    return _psychology_client_card_text(
        state, conversation_id=conversation_id, request_number=request_number,
    )


def request_psychology_session(conversation_id: str) -> dict:
    """Create a request for a human consultation, consuming free/paid access once."""
    chel_id = current_chel_id()
    now = utc_now()
    with _write_lock, connection() as conn:
        conversation = conn.execute(
            """SELECT id FROM conversations
               WHERE id=? AND chel_id=? AND dialog_type='masterclass_psychology'""",
            (str(conversation_id or ""), chel_id),
        ).fetchone()
        if not conversation:
            raise ValueError("Чат психологической поддержки не найден")
        pending = conn.execute(
            """SELECT * FROM psychology_sessions
               WHERE conversation_id=? AND chel_id=? AND status='requested'
               ORDER BY session_number DESC LIMIT 1""",
            (conversation_id, chel_id),
        ).fetchone()
        if pending:
            return {**dict(pending), "created": False}
        free_used = conn.execute(
            """SELECT 1 FROM psychology_sessions
               WHERE conversation_id=? AND chel_id=? AND access_type='free' LIMIT 1""",
            (conversation_id, chel_id),
        ).fetchone()
        access_type = "free"
        payment_order_id = None
        payment_credit_number = None
        paid_orders = []
        if free_used:
            paid_orders = conn.execute(
                """SELECT po.id,po.items FROM payment_orders po
                   WHERE po.chel_id=? AND po.order_type='psychology_session'
                     AND po.target_conversation_id=? AND po.status='succeeded'
                     AND po.paid=1
                   ORDER BY COALESCE(po.paid_at,po.created_at),po.created_at""",
                (chel_id, conversation_id),
            ).fetchall()
            for paid_order in paid_orders:
                credit_count = _psychology_order_session_count(paid_order["items"])
                legacy_uses = int(conn.execute(
                    "SELECT COUNT(*) FROM psychology_sessions WHERE payment_order_id=?",
                    (paid_order["id"],),
                ).fetchone()[0])
                package_uses = int(conn.execute(
                    "SELECT COUNT(*) FROM psychology_session_credit_uses WHERE payment_order_id=?",
                    (paid_order["id"],),
                ).fetchone()[0])
                used_count = legacy_uses + package_uses
                if used_count < credit_count:
                    payment_order_id = paid_order["id"]
                    payment_credit_number = used_count + 1
                    break
            if not payment_order_id:
                raise ValueError("Бесплатная консультация уже использована. Купите сессию, чтобы оставить заявку")
            access_type = "paid"
        number = int(conn.execute(
            """SELECT COALESCE(MAX(session_number),0)+1 FROM psychology_sessions
               WHERE conversation_id=? AND chel_id=?""",
            (conversation_id, chel_id),
        ).fetchone()[0])
        session_id = f"psy_{uuid.uuid4().hex}"
        request_number = _new_psychology_request_number(conn)
        conn.execute(
            """INSERT INTO psychology_sessions
               (id,conversation_id,chel_id,session_number,request_number,
                access_type,payment_order_id,status,started_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'requested',?,?,?)""",
            (
                session_id, conversation_id, chel_id, number, request_number, access_type,
                None if payment_credit_number else payment_order_id, now, now, now,
            ),
        )
        if payment_order_id and payment_credit_number:
            conn.execute(
                """INSERT INTO psychology_session_credit_uses
                   (id,payment_order_id,session_id,credit_number,created_at)
                   VALUES (?,?,?,?,?)""",
                (
                    f"psycredit_{uuid.uuid4().hex}", payment_order_id, session_id,
                    payment_credit_number, now,
                ),
            )
        conn.execute(
            """UPDATE conversations SET human_status='pending',human_ticket_id=?,
               human_channel='call',human_recipient_role='psychologist',updated_at=?
               WHERE id=? AND chel_id=?""",
            (request_number, now, conversation_id, chel_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM psychology_sessions WHERE id=?", (session_id,)).fetchone()
    return {**dict(row), "created": True}


def ensure_psychology_session_access(conversation_id: str) -> dict:
    """Backward-compatible alias for explicitly requesting a human session."""
    return request_psychology_session(conversation_id)


def complete_psychology_session(conversation_id: str) -> dict:
    """Finish the current session in the user context (legacy internal helper)."""
    chel_id = current_chel_id()
    now = utc_now()
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT ps.id FROM psychology_sessions ps
               JOIN conversations c ON c.id=ps.conversation_id
               WHERE ps.conversation_id=? AND ps.chel_id=? AND c.chel_id=?
                 AND c.dialog_type='masterclass_psychology' AND ps.status='requested'
               ORDER BY ps.session_number DESC LIMIT 1""",
            (str(conversation_id or ""), chel_id, chel_id),
        ).fetchone()
        if not row:
            raise ValueError("Заявка на сессию не найдена")
        conn.execute(
            """UPDATE psychology_sessions SET status='completed',completed_at=?,updated_at=?
               WHERE id=?""",
            (now, now, row["id"]),
        )
        conn.commit()
    return psychology_session_summary(conversation_id)


def manager_complete_psychology_session(
    conversation_id: str, manager_name: str, staff_role: str = "psychologist",
) -> dict | None:
    """Mark the latest requested human psychology call as conducted."""
    manager_name = " ".join(str(manager_name or "").split())[:80] or "Психолог"
    staff_role = _staff_role(staff_role)
    now = utc_now()
    with _write_lock, connection() as conn:
        conversation = conn.execute(
            """SELECT id FROM conversations WHERE id=?
               AND dialog_type='masterclass_psychology'
               AND COALESCE(human_recipient_role, 'manager')=?""",
            (str(conversation_id or ""), staff_role),
        ).fetchone()
        if not conversation:
            return None
        session = conn.execute(
            """SELECT * FROM psychology_sessions
               WHERE conversation_id=? AND status='requested'
               ORDER BY session_number DESC LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        if not session:
            raise ValueError("Активная заявка на сессию не найдена")
        conn.execute(
            """UPDATE psychology_sessions SET status='completed',completed_at=?,updated_at=?
               WHERE id=?""",
            (now, now, session["id"]),
        )
        conn.execute(
            """UPDATE conversations SET ai_enabled=1,human_status='closed',
               status='active',updated_at=? WHERE id=?""",
            (now, conversation_id),
        )
        completion_text = (
            "Сессия с психологом проведена. "
            f"Заявка №{session['request_number']} завершена, "
            "а сессия добавлена в вашу историю."
        )
        completion_metadata = {
            "action": "psychology_session_completed",
            "session_id": session["id"],
            "session_number": session["session_number"],
            "request_number": session["request_number"],
            "sender_type": "human_manager",
            "sender_name": manager_name,
            "staff_role": "psychologist",
        }
        conn.execute(
            """INSERT INTO messages
               (conversation_id,role,agent_id,content,metadata,created_at)
               VALUES (?, 'assistant', 'psychologist', ?, ?, ?)""",
            (
                conversation_id, completion_text,
                json.dumps(completion_metadata, ensure_ascii=False), now,
            ),
        )
        credit_use = conn.execute(
            """SELECT payment_order_id FROM psychology_session_credit_uses
               WHERE session_id=?""",
            (session["id"],),
        ).fetchone()
        payment_order_id = (
            credit_use["payment_order_id"] if credit_use
            else session["payment_order_id"]
        )
        if payment_order_id:
            order = conn.execute(
                "SELECT items FROM payment_orders WHERE id=?",
                (payment_order_id,),
            ).fetchone()
            completed_uses = int(conn.execute(
                """SELECT COUNT(*) FROM psychology_session_credit_uses uses
                   JOIN psychology_sessions sessions ON sessions.id=uses.session_id
                   WHERE uses.payment_order_id=? AND sessions.status='completed'""",
                (payment_order_id,),
            ).fetchone()[0])
            legacy_completed = int(conn.execute(
                """SELECT COUNT(*) FROM psychology_sessions
                   WHERE payment_order_id=? AND status='completed'""",
                (payment_order_id,),
            ).fetchone()[0])
            if order and completed_uses + legacy_completed >= _psychology_order_session_count(order["items"]):
                conn.execute(
                    """UPDATE payment_orders SET fulfillment_at=COALESCE(fulfillment_at,?),updated_at=?
                       WHERE id=?""",
                    (now, now, payment_order_id),
                )
        conn.execute(
            """INSERT INTO manager_actions
               (conversation_id, manager_name, action, details, created_at)
               VALUES (?, ?, 'complete_psychology_session', ?, ?)""",
            (
                conversation_id,
                manager_name,
                json.dumps({
                    "session_id": session["id"],
                    "session_number": session["session_number"],
                    "request_number": session["request_number"],
                    "access_type": session["access_type"],
                }, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        completed = conn.execute(
            "SELECT * FROM psychology_sessions WHERE id=?", (session["id"],),
        ).fetchone()
    return dict(completed)


def list_paid_consultations(limit: int = 100) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT * FROM payment_orders
            WHERE chel_id = ? AND order_type = 'consultation'
              AND status = 'succeeded' AND paid = 1
            ORDER BY paid_at DESC, created_at DESC LIMIT ?""",
            (current_chel_id(), max(1, min(200, int(limit)))),
        ).fetchall()
    return [_public_payment_order(row) for row in rows]


def payment_order_private(order_id: str, *, require_owner: bool = True) -> dict | None:
    order_id = str(order_id or "").strip()
    with connection() as conn:
        if require_owner:
            row = conn.execute(
                "SELECT * FROM payment_orders WHERE id = ? AND chel_id = ?",
                (order_id, current_chel_id()),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM payment_orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["items"] = _payment_items(result.get("items", "[]"))
    return result


def payment_order_by_provider_id(provider_payment_id: str) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM payment_orders WHERE provider_payment_id = ?",
            (str(provider_payment_id or "").strip(),),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["items"] = _payment_items(result.get("items", "[]"))
    return result


def payment_customer_profile(chel_id: str) -> dict:
    """Return the bounded profile fields used in an internal payment notification."""
    with connection() as conn:
        row = conn.execute(
            "SELECT preferred_name, company_inn FROM user_profile WHERE chel_id = ?",
            (str(chel_id or ""),),
        ).fetchone()
    if not row:
        return {"preferred_name": "", "company_inn": ""}
    return {
        "preferred_name": str(row["preferred_name"] or "")[:100],
        "company_inn": str(row["company_inn"] or "")[:12],
    }


def mark_payment_creation_failed(order_id: str, error: str) -> None:
    with _write_lock, connection() as conn:
        conn.execute(
            """UPDATE payment_orders SET status = 'failed', last_error = ?, updated_at = ?
            WHERE id = ? AND status = 'creating'""",
            (str(error or "")[:500], utc_now(), str(order_id or "")),
        )
        conn.commit()


def mark_payment_abandoned(order_id: str) -> dict:
    """Close an unfinished local attempt after the user returns without paying.

    YooKassa does not allow a regular one-stage redirect payment to be canceled
    while it is still ``pending``. The provider webhook may therefore replace
    this local status later if the bank actually completes the payment.
    """
    with _write_lock, connection() as conn:
        row = conn.execute(
            "SELECT * FROM payment_orders WHERE id = ? AND chel_id = ?",
            (str(order_id or ""), current_chel_id()),
        ).fetchone()
        if not row:
            raise ValueError("Заказ не найден")
        if row["status"] == "pending" and not bool(row["paid"]):
            conn.execute(
                "UPDATE payment_orders SET status = 'abandoned', updated_at = ? WHERE id = ?",
                (utc_now(), row["id"]),
            )
            conn.commit()
        updated = conn.execute(
            "SELECT * FROM payment_orders WHERE id = ?", (row["id"],),
        ).fetchone()
    return _public_payment_order(updated)


def list_payment_orders(limit: int = 100) -> list[dict]:
    """Return the current user's payment attempts without provider secrets."""
    limit = max(1, min(int(limit or 100), 200))
    with connection() as conn:
        rows = conn.execute(
            """SELECT * FROM payment_orders WHERE chel_id = ? AND hidden_at IS NULL
            ORDER BY created_at DESC LIMIT ?""",
            (current_chel_id(), limit),
        ).fetchall()
    return [_public_payment_order(row) for row in rows]


def hide_payment_order(order_id: str) -> dict:
    """Hide a final unsuccessful attempt while retaining the audit record."""
    with _write_lock, connection() as conn:
        row = conn.execute(
            "SELECT * FROM payment_orders WHERE id = ? AND chel_id = ?",
            (str(order_id or ""), current_chel_id()),
        ).fetchone()
        if not row:
            raise ValueError("Заказ не найден")
        if row["status"] not in {"canceled", "abandoned", "failed"} or bool(row["paid"]):
            raise ValueError("Можно удалить только неуспешную или незавершённую попытку")
        conn.execute(
            "UPDATE payment_orders SET hidden_at = ?, updated_at = ? WHERE id = ?",
            (utc_now(), utc_now(), row["id"]),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM payment_orders WHERE id = ?", (row["id"],),
        ).fetchone()
    return _public_payment_order(updated)


def attach_yookassa_payment(order_id: str, payment: dict) -> dict:
    provider_id = str(payment.get("id", "")).strip()
    status = str(payment.get("status", "pending")).strip()
    confirmation = payment.get("confirmation") if isinstance(payment.get("confirmation"), dict) else {}
    confirmation_url = str(confirmation.get("confirmation_url", "")).strip()
    if not provider_id or status not in {"pending", "waiting_for_capture", "succeeded", "canceled"}:
        raise ValueError("ЮKassa вернула некорректный платёж")
    if status == "pending" and not confirmation_url.startswith("https://"):
        raise ValueError("ЮKassa не вернула безопасную ссылку для оплаты")
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            """UPDATE payment_orders SET provider_payment_id = ?, status = ?,
                confirmation_url = ?, test = ?, hidden_at = NULL, updated_at = ?
            WHERE id = ? AND status IN ('creating', 'abandoned')""",
            (
                provider_id, status, confirmation_url, int(bool(payment.get("test"))),
                utc_now(), str(order_id or ""),
            ),
        )
        if not cursor.rowcount:
            row = conn.execute("SELECT * FROM payment_orders WHERE id = ?", (order_id,)).fetchone()
            if not row or row["provider_payment_id"] != provider_id:
                raise ValueError("Заказ уже связан с другим платежом")
        conn.commit()
        row = conn.execute("SELECT * FROM payment_orders WHERE id = ?", (order_id,)).fetchone()
    return _public_payment_order(row)


def apply_yookassa_status(order_id: str, payment: dict) -> dict:
    """Apply a provider-verified status and complete onboarding exactly once."""
    provider_id = str(payment.get("id", "")).strip()
    status = str(payment.get("status", "")).strip()
    if status not in {"pending", "waiting_for_capture", "succeeded", "canceled"}:
        raise ValueError("Неизвестный статус платежа ЮKassa")
    now = utc_now()
    with _write_lock, connection() as conn:
        row = conn.execute("SELECT * FROM payment_orders WHERE id = ?", (order_id,)).fetchone()
        if not row or not provider_id or row["provider_payment_id"] != provider_id:
            raise ValueError("Платёж не относится к этому заказу")
        amount = payment.get("amount") if isinstance(payment.get("amount"), dict) else {}
        try:
            provider_amount = Decimal(str(amount.get("value", "0")))
            if provider_amount.as_tuple().exponent < -2:
                raise InvalidOperation
            provider_kopecks = int(provider_amount * 100)
        except (InvalidOperation, TypeError, ValueError):
            provider_kopecks = -1
        metadata = payment.get("metadata") if isinstance(payment.get("metadata"), dict) else {}
        if (
            provider_kopecks != int(row["amount_kopecks"])
            or str(amount.get("currency", "")) != row["currency"]
            or str(metadata.get("order_id", "")) != row["id"]
        ):
            raise ValueError("Сумма или реквизиты платежа не совпадают с заказом")
        current_status = str(row["status"] or "")
        terminal_statuses = {"succeeded", "canceled"}
        status_rank = {
            "creating": -2, "abandoned": -1, "pending": 0,
            "waiting_for_capture": 1, "succeeded": 2, "canceled": 2,
        }
        if (
            current_status in terminal_statuses
            or status_rank.get(status, -1) < status_rank.get(current_status, -1)
        ):
            # Provider notifications and status checks may arrive out of order.
            # A delayed pending/canceled response must never undo a confirmed
            # payment, and a final provider state must remain final locally.
            return _public_payment_order(row)
        paid = status == "succeeded" and bool(payment.get("paid"))
        if status == "succeeded" and not paid:
            raise ValueError("ЮKassa вернула неподтверждённый успешный платёж")
        conn.execute(
            """UPDATE payment_orders SET status = ?, paid = ?, test = ?, updated_at = ?,
                last_error = CASE WHEN ? = 'canceled' THEN ? ELSE last_error END,
                paid_at = CASE WHEN ? THEN COALESCE(paid_at, ?) ELSE paid_at END,
                canceled_at = CASE WHEN ? = 'canceled' THEN COALESCE(canceled_at, ?) ELSE canceled_at END,
                hidden_at = CASE WHEN ? THEN NULL ELSE hidden_at END
            WHERE id = ?""",
            (
                status, int(paid), int(bool(payment.get("test"))), now,
                status,
                str((payment.get("cancellation_details") or {}).get("reason", ""))[:500]
                if isinstance(payment.get("cancellation_details"), dict) else "",
                int(paid), now, status, now, int(paid), row["id"],
            ),
        )
        if paid and str(row["order_type"] or "examinations") == "examinations":
            item_ids = [str(item.get("id", "")) for item in _payment_items(row["items"])]
            onboarding = conn.execute(
                "SELECT intro_seen, font_size FROM onboarding_state WHERE chel_id = ?",
                (row["chel_id"],),
            ).fetchone()
            conn.execute(
                """INSERT INTO onboarding_state
                (chel_id, status, selected_tests, payment_status, intro_seen, font_size, updated_at)
                VALUES (?, 'complete', ?, 'paid_online', ?, ?, ?)
                ON CONFLICT(chel_id) DO UPDATE SET status='complete',
                  selected_tests=excluded.selected_tests, payment_status='paid_online',
                  updated_at=excluded.updated_at""",
                (
                    row["chel_id"], json.dumps(item_ids, ensure_ascii=False),
                    int(onboarding["intro_seen"]) if onboarding else 0,
                    onboarding["font_size"] if onboarding else "extra", now,
                ),
            )
        conn.commit()
        updated = conn.execute("SELECT * FROM payment_orders WHERE id = ?", (row["id"],)).fetchone()
    return _public_payment_order(updated)


def fulfill_paid_consultation(order_id: str) -> dict | None:
    """Open the paid consultation chat exactly once after provider confirmation."""
    now = utc_now()
    with _write_lock, connection() as conn:
        order = conn.execute(
            """SELECT * FROM payment_orders WHERE id = ?
            AND order_type = 'consultation' AND status = 'succeeded' AND paid = 1""",
            (str(order_id or ""),),
        ).fetchone()
        if not order:
            return None
        conversation_id = str(order["target_conversation_id"] or "")
        conversation = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND chel_id = ?",
            (conversation_id, order["chel_id"]),
        ).fetchone()
        if not conversation:
            conversation_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO conversations
                (id, chel_id, title, created_at, updated_at)
                VALUES (?, ?, 'Консультация врача', ?, ?)""",
                (conversation_id, order["chel_id"], now, now),
            )
            conn.execute(
                """INSERT INTO conversation_reads
                (conversation_id, last_read_message_id, read_at) VALUES (?, 0, ?)""",
                (conversation_id, now),
            )
            conn.execute(
                "UPDATE payment_orders SET target_conversation_id = ? WHERE id = ?",
                (conversation_id, order["id"]),
            )
        if order["fulfillment_at"]:
            return {
                "created": False, "conversation_id": conversation_id,
                "message_id": 0,
            }
        ticket_id = f"C-{secrets.token_hex(3).upper()}"
        conn.execute(
            """UPDATE conversations SET active_agent = 'manager', status = 'waiting_human',
            human_status = 'pending', human_ticket_id = ?, human_channel = 'paid_consultation',
            human_phone = NULL, human_recipient_role = 'doctor', ai_enabled = 0,
            updated_at = ? WHERE id = ? AND chel_id = ?""",
            (ticket_id, now, conversation_id, order["chel_id"]),
        )
        user_cursor = conn.execute(
            """INSERT INTO messages
            (conversation_id, role, agent_id, content, metadata, created_at)
            VALUES (?, 'user', NULL, ?, ?, ?)""",
            (
                conversation_id,
                f"Оплачена консультация врача — 1 000 ₽. Заказ № {str(order['id'])[-8:].upper()}.",
                json.dumps({
                    "action": "paid_consultation_purchased",
                    "payment_order_id": order["id"],
                    "amount_kopecks": int(order["amount_kopecks"]),
                }, ensure_ascii=False),
                now,
            ),
        )
        message_cursor = conn.execute(
            """INSERT INTO messages
            (conversation_id, role, agent_id, content, metadata, created_at)
            VALUES (?, 'assistant', 'manager', ?, ?, ?)""",
            (
                conversation_id,
                "Оплата консультации подтверждена. Скоро врач напишет вам в этот чат, "
                "и вы вместе согласуете удобные дату и время. До завершения консультации "
                "ИИ в этом диалоге выключен, поэтому все новые сообщения увидит врач.",
                json.dumps({
                    "action": "paid_consultation_confirmed",
                    "payment_order_id": order["id"],
                    "human_ticket_id": ticket_id,
                    "recipient_role": "doctor",
                }, ensure_ascii=False),
                now,
            ),
        )
        conn.execute(
            "UPDATE payment_orders SET fulfillment_at = ?, updated_at = ? WHERE id = ?",
            (now, now, order["id"]),
        )
        conn.commit()
    return {
        "created": True, "conversation_id": conversation_id,
        "ticket_id": ticket_id, "user_message_id": user_cursor.lastrowid,
        "message_id": message_cursor.lastrowid,
    }


def public_payment_order(order_id: str) -> dict | None:
    private = payment_order_private(order_id)
    if not private:
        return None
    private["items"] = json.dumps(private["items"], ensure_ascii=False)
    return _public_payment_order(private)


def _validate_examination(
    name: str, description: str, includes: str, price,
    competitor_price=0, price_without_discount=0,
    competitor_label="У конкурентов", retail_price_label="Розничная цена",
    discount_price_label="С учётом вашей скидки",
    show_competitor_price=True, show_retail_price=True, show_discount_price=True,
    default_name="",
) -> tuple:
    name = " ".join(str(name or "").split())[:140]
    default_name = " ".join(str(default_name or name).split())[:140]
    description = " ".join(str(description or "").split())[:1200]
    includes = " ".join(str(includes or "").split())[:1200]
    if len(name) < 2:
        raise ValueError("Название должно содержать минимум 2 символа")
    if len(default_name) < 2:
        raise ValueError("Название для Bitrix должно содержать минимум 2 символа")
    if len(description) < 5:
        raise ValueError("Добавьте понятное описание обследования")
    def normalize_price(value, label: str, *, optional: bool = False) -> int:
        if optional and value in (None, ""):
            return 0
        try:
            result = int(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{label} должна быть целым числом") from exc
        if result < 0 or result > 10_000_000:
            raise ValueError(f"{label} должна быть от 0 до 10 000 000 рублей")
        return result

    normalized_price = normalize_price(price, "Цена с учётом скидки")
    normalized_competitor_price = normalize_price(
        competitor_price, "Цена конкурентов", optional=True,
    )
    normalized_price_without_discount = normalize_price(
        price_without_discount, "Цена без скидки", optional=True,
    )
    if normalized_price_without_discount and normalized_price_without_discount <= normalized_price:
        raise ValueError("Цена без скидки должна быть больше цены с учётом скидки")
    def normalize_label(value, default: str) -> str:
        return " ".join(str(value or default).split())[:80] or default

    def normalize_visibility(value) -> int:
        if value is None:
            return 1
        if isinstance(value, str):
            return int(value.strip().lower() not in {"", "0", "false", "off", "no"})
        return int(bool(value))

    return (
        name, description, includes, normalized_price,
        normalized_competitor_price, normalized_price_without_discount,
        normalize_label(competitor_label, "У конкурентов"),
        normalize_label(retail_price_label, "Розничная цена"),
        normalize_label(discount_price_label, "С учётом вашей скидки"),
        normalize_visibility(show_competitor_price),
        normalize_visibility(show_retail_price),
        normalize_visibility(show_discount_price),
        default_name,
    )


def admin_create_examination(
    name: str, description: str, includes: str, price,
    competitor_price=0, price_without_discount=0,
    competitor_label="У конкурентов", retail_price_label="Розничная цена",
    discount_price_label="С учётом вашей скидки",
    show_competitor_price=True, show_retail_price=True, show_discount_price=True,
    default_name="",
) -> dict:
    validated = _validate_examination(
        name, description, includes, price, competitor_price, price_without_discount,
        competitor_label, retail_price_label, discount_price_label,
        show_competitor_price, show_retail_price, show_discount_price, default_name,
    )
    (
        name, description, includes, price, competitor_price, price_without_discount,
        competitor_label, retail_price_label, discount_price_label,
        show_competitor_price, show_retail_price, show_discount_price, default_name,
    ) = validated
    examination_id = f"exam_{secrets.token_hex(8)}"
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO examination_catalog
            (id, name, default_name, description, includes, price, competitor_price, price_without_discount,
             competitor_label, retail_price_label, discount_price_label,
             show_competitor_price, show_retail_price, show_discount_price,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                examination_id, name, default_name, description, includes, price,
                competitor_price, price_without_discount,
                competitor_label, retail_price_label, discount_price_label,
                show_competitor_price, show_retail_price, show_discount_price,
                now, now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM examination_catalog WHERE id = ?", (examination_id,),
        ).fetchone()
    return dict(row)


def admin_update_examination(
    examination_id: str, name: str, description: str, includes: str, price,
    competitor_price=0, price_without_discount=0,
    competitor_label="У конкурентов", retail_price_label="Розничная цена",
    discount_price_label="С учётом вашей скидки",
    show_competitor_price=True, show_retail_price=True, show_discount_price=True,
    default_name="",
) -> dict | None:
    examination_id = str(examination_id or "").strip()
    if not str(default_name or "").strip():
        # Preserve the existing Bitrix name for older API clients and internal
        # callers that predate this field.
        with connection() as conn:
            existing = conn.execute(
                "SELECT default_name, name FROM examination_catalog WHERE id = ?",
                (examination_id,),
            ).fetchone()
        if existing:
            default_name = existing["default_name"] or existing["name"]
    validated = _validate_examination(
        name, description, includes, price, competitor_price, price_without_discount,
        competitor_label, retail_price_label, discount_price_label,
        show_competitor_price, show_retail_price, show_discount_price, default_name,
    )
    (
        name, description, includes, price, competitor_price, price_without_discount,
        competitor_label, retail_price_label, discount_price_label,
        show_competitor_price, show_retail_price, show_discount_price, default_name,
    ) = validated
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            """UPDATE examination_catalog
            SET name = ?, default_name = ?, description = ?, includes = ?, price = ?,
                competitor_price = ?, price_without_discount = ?,
                competitor_label = ?, retail_price_label = ?, discount_price_label = ?,
                show_competitor_price = ?, show_retail_price = ?, show_discount_price = ?,
                updated_at = ?
            WHERE id = ?""",
            (
                name, default_name, description, includes, price, competitor_price,
                price_without_discount,
                competitor_label, retail_price_label, discount_price_label,
                show_competitor_price, show_retail_price, show_discount_price,
                utc_now(), examination_id,
            ),
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
        "user": {
            "id": row["id"], "display_name": row["display_name"],
            "login": row["login"], "role": row["role"],
        },
    }


def get_staff_session(token: str) -> dict | None:
    if not token:
        return None
    now = datetime.now(timezone.utc)
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT s.token_hash, s.expires_at, s.revoked_at, u.id, u.display_name,
            u.login, u.role, u.is_active FROM staff_sessions s
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
    return {
        "id": row["id"], "display_name": row["display_name"],
        "login": row["login"], "role": row["role"],
    }


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
    include_related: bool = False, staff_role: str = "manager",
    staff_id: int | None = None,
) -> list[dict]:
    """Return the human-support queue. This deliberately contains sensitive data."""
    query = " ".join(str(query or "").split())[:120]
    queue = str(queue or "open")
    if queue not in {"open", "all", "ai_off"}:
        raise ValueError("Неизвестный фильтр очереди")
    limit = max(10, min(250, int(limit)))
    search_value = query.casefold()
    conversation_alias = "c0" if include_related else "c"
    profile_alias = "p0" if include_related else "p"
    staff_role = _staff_role(staff_role)
    where = [f"{conversation_alias}.chel_id NOT IN ('chel_legacy', 'chel_test_default')"]
    # A short-lived draft created a second psychology_support conversation.
    # It is obsolete now that the psychologist joins the main psychology chat.
    where.append(f"{conversation_alias}.dialog_type <> 'psychology_support'")
    where.append(f"COALESCE({conversation_alias}.human_recipient_role, 'manager') = ?")
    params: list = [staff_role]
    if queue == "open":
        where.append(
            f"({conversation_alias}.human_ticket_id IS NOT NULL "
            f"OR {conversation_alias}.ai_enabled = 0)"
        )
        where.append(f"{conversation_alias}.human_status IN ('pending', 'connected')")
    elif queue == "ai_off":
        where.append(f"{conversation_alias}.ai_enabled = 0")
    if query:
        where.append(f"""(
            INSTR(CASEFOLD({conversation_alias}.id), ?) > 0
            OR INSTR(CASEFOLD({conversation_alias}.chel_id), ?) > 0
            OR INSTR(CASEFOLD({conversation_alias}.human_ticket_id), ?) > 0
            OR INSTR(CASEFOLD({profile_alias}.preferred_name), ?) > 0
            OR INSTR(CASEFOLD({profile_alias}.company_inn), ?) > 0
            OR INSTR(CASEFOLD({conversation_alias}.human_phone), ?) > 0
        )""")
        params.extend([search_value] * 6)
    where_sql = " AND ".join(where)
    with connection() as conn:
        if include_related:
            rows = conn.execute(
                f"""WITH matched_users AS (
                    SELECT c0.chel_id, MAX(c0.updated_at) AS matched_updated
                    FROM conversations c0
                    LEFT JOIN user_profile p0 ON p0.chel_id = c0.chel_id
                    WHERE {where_sql}
                    GROUP BY c0.chel_id
                    ORDER BY matched_updated DESC
                    LIMIT ?
                )
                SELECT c.id, c.chel_id, c.title, c.active_agent, c.dialog_type, c.status,
                c.ai_enabled, c.human_status, c.human_ticket_id,
                COALESCE(c.human_recipient_role, 'manager') AS human_recipient_role,
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
                JOIN matched_users matched ON matched.chel_id = c.chel_id
                LEFT JOIN user_profile p ON p.chel_id = c.chel_id
                WHERE COALESCE(c.human_recipient_role, 'manager') = ?
                  AND c.dialog_type <> 'psychology_support'
                ORDER BY matched.matched_updated DESC,
                         CASE WHEN c.ai_enabled = 0 THEN 0 ELSE 1 END,
                         c.updated_at DESC""",
                tuple(params + [limit, staff_role]),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT c.id, c.chel_id, c.title, c.active_agent, c.dialog_type, c.status,
                    c.ai_enabled, c.human_status, c.human_ticket_id,
                    COALESCE(c.human_recipient_role, 'manager') AS human_recipient_role,
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
        unread_by_conversation: dict[str, int] = {}
        if staff_id is not None and rows:
            conversation_ids = [str(row["id"]) for row in rows]
            # Keep the query below SQLite's bound-variable limit when a search
            # returns many dialogs belonging to the same person.
            for offset in range(0, len(conversation_ids), 500):
                chunk = conversation_ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                unread_rows = conn.execute(
                    f"""SELECT m.conversation_id, COUNT(*) AS unread_count
                    FROM messages m
                    LEFT JOIN staff_conversation_reads r
                      ON r.conversation_id = m.conversation_id
                     AND r.staff_user_id = ?
                    WHERE m.role = 'user'
                      AND m.id > COALESCE(r.last_read_message_id, 0)
                      AND m.conversation_id IN ({placeholders})
                    GROUP BY m.conversation_id""",
                    (int(staff_id), *chunk),
                ).fetchall()
                unread_by_conversation.update({
                    str(unread["conversation_id"]): int(unread["unread_count"] or 0)
                    for unread in unread_rows
                })
    result = []
    for row in rows:
        item = dict(row)
        item["ai_enabled"] = bool(item["ai_enabled"])
        item["last_message"] = str(item.get("last_message") or "")[:180]
        item["unread_user_messages"] = unread_by_conversation.get(item["id"], 0)
        result.append(item)
    return result


def manager_mark_conversation_read(
    conversation_id: str, staff_id: int, staff_role: str,
    last_message_id: int | None = None,
) -> dict | None:
    """Persist how far one staff member has actually viewed a conversation."""
    conversation_id = str(conversation_id or "").strip()
    staff_id = int(staff_id)
    staff_role = _staff_role(staff_role)
    requested_id = None if last_message_id is None else max(0, int(last_message_id))
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT c.id, COALESCE(MAX(m.id), 0) AS last_message_id
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            WHERE c.id = ?
              AND COALESCE(c.human_recipient_role, 'manager') = ?
            GROUP BY c.id""",
            (conversation_id, staff_role),
        ).fetchone()
        if not row:
            return None
        visible_message_id = int(row["last_message_id"] or 0)
        if requested_id is not None:
            visible_message_id = min(visible_message_id, requested_id)
        now = utc_now()
        conn.execute(
            """INSERT INTO staff_conversation_reads
            (staff_user_id, conversation_id, last_read_message_id, read_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(staff_user_id, conversation_id) DO UPDATE SET
                last_read_message_id = MAX(
                    staff_conversation_reads.last_read_message_id,
                    excluded.last_read_message_id
                ),
                read_at = excluded.read_at""",
            (staff_id, conversation_id, visible_message_id, now),
        )
        conn.commit()
    return {
        "conversation_id": conversation_id,
        "last_read_message_id": visible_message_id,
        "read_at": now,
    }


def manager_conversation_detail(
    conversation_id: str, staff_role: str | None = None,
) -> dict | None:
    """Return a complete manager view for one conversation and its owner."""
    with connection() as conn:
        conversation_row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,),
        ).fetchone()
        if not conversation_row:
            return None
        conversation = dict(conversation_row)
        if staff_role is not None and conversation.get("human_recipient_role", "manager") != _staff_role(staff_role):
            return None
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
        examination_names = {
            str(row["id"]): str(row["name"])
            for row in conn.execute(
                "SELECT id, name FROM examination_catalog"
            ).fetchall()
        }
        onboarding["selected_test_names"] = [
            examination_names.get(str(examination_id), str(examination_id))
            for examination_id in onboarding["selected_tests"]
        ]
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
        psychology_session_row = None
        psychology_client_card_value = ""
        if conversation.get("dialog_type") == "masterclass_psychology":
            psychology_session_row = conn.execute(
                """SELECT id,session_number,request_number,access_type,status,started_at,completed_at
                   FROM psychology_sessions
                   WHERE conversation_id=? AND status='requested'
                   ORDER BY session_number DESC LIMIT 1""",
                (conversation_id,),
            ).fetchone()
            state_row = _psychology_state_row(conn, conversation_id)
            try:
                psychology_state = json.loads(state_row["state_json"]) if state_row else {}
            except (TypeError, json.JSONDecodeError):
                psychology_state = {}
            psychology_client_card_value = _psychology_client_card_text(
                psychology_state,
                conversation_id=conversation_id,
                request_number=str(
                    psychology_session_row["request_number"]
                    if psychology_session_row else conversation.get("human_ticket_id") or ""
                ),
            )

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
        "psychology_session": (
            dict(psychology_session_row) if psychology_session_row else None
        ),
        "psychology_client_card": psychology_client_card_value,
    }


def _manager_profile_from_value(chel_id: str) -> dict:
    with connection() as conn:
        return _manager_profile(conn, chel_id)


def manager_add_reply(
    conversation_id: str, content: str, manager_name: str,
    staff_role: str = "manager", message_action: str = "manager_reply",
    extra_metadata: dict | None = None,
) -> dict:
    content = str(content or "").strip()
    manager_name = " ".join(str(manager_name or "").split())[:80] or "Менеджер"
    if not content or len(content) > 12_000:
        raise ValueError("Ответ должен содержать от 1 до 12000 символов")
    now = utc_now()
    staff_role = _staff_role(staff_role)
    if message_action not in {
        "manager_reply", "consultation_payment_instruction",
        "psychology_session_payment_instruction",
        "psychology_session_invitation",
    }:
        raise ValueError("Неизвестное действие сообщения")
    metadata = {
        "sender_type": "human_manager",
        "manager_name": manager_name,
        "staff_role": staff_role,
        "action": message_action,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    with _write_lock, connection() as conn:
        conversation = conn.execute(
            """SELECT id FROM conversations WHERE id = ?
            AND COALESCE(human_recipient_role, 'manager') = ?""",
            (conversation_id, staff_role),
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
            VALUES (?, ?, ?, ?, ?)""",
            (
                conversation_id, manager_name,
                message_action if message_action != "manager_reply" else "reply",
                json.dumps({"message_id": cursor.lastrowid}), now,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,),
        ).fetchone()
    return _manager_message(row)


def manager_delete_message(
    conversation_id: str, message_id: int, manager_name: str,
    staff_role: str = "manager",
) -> dict | None:
    """Delete a user or staff message and retain a sync tombstone for open clients."""
    manager_name = " ".join(str(manager_name or "").split())[:80] or "Менеджер"
    staff_role = _staff_role(staff_role)
    try:
        message_id = int(message_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректный идентификатор сообщения") from exc
    if message_id <= 0:
        raise ValueError("Некорректный идентификатор сообщения")
    now = utc_now()
    with _write_lock, connection() as conn:
        conversation = conn.execute(
            """SELECT id,chel_id FROM conversations WHERE id = ?
            AND COALESCE(human_recipient_role, 'manager') = ?""",
            (conversation_id, staff_role),
        ).fetchone()
        if not conversation:
            return None
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ? AND conversation_id = ?",
            (message_id, conversation_id),
        ).fetchone()
        if not row:
            return None
        message = _manager_message(row)
        metadata = message.get("metadata") or {}
        is_user = message.get("role") == "user"
        is_staff = metadata.get("sender_type") == "human_manager"
        if not (is_user or is_staff):
            raise ValueError("Можно удалить только сообщение пользователя или сотрудника")
        conn.execute(
            """INSERT OR REPLACE INTO message_deletions
            (message_id, conversation_id, deleted_at) VALUES (?, ?, ?)""",
            (message_id, conversation_id, now),
        )
        conn.execute(
            """DELETE FROM manager_notification_outbox
            WHERE conversation_id = ? AND source_message_id = ? AND status <> 'sent'""",
            (conversation_id, message_id),
        )
        conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        conn.execute(
            """INSERT INTO manager_actions
            (conversation_id, manager_name, action, details, created_at)
            VALUES (?, ?, 'delete_message', ?, ?)""",
            (
                conversation_id, manager_name,
                json.dumps({
                    "message_id": message_id,
                    "message_role": message.get("role", ""),
                    "sender_type": metadata.get(
                        "sender_type", "user" if is_user else "",
                    ),
                }, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
    attachments = metadata.get("attachments", []) if isinstance(metadata, dict) else []
    if isinstance(attachments, list) and attachments:
        from . import chat_attachments
        chat_attachments.delete_public_attachments(
            attachments, str(conversation["chel_id"] or ""),
        )
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "deleted_at": now,
    }


def manager_send_consultation_payment_instruction(
    conversation_id: str, manager_name: str, staff_role: str = "manager",
) -> dict:
    """Send the canonical consultation purchase instructions from a doctor chat."""
    if _staff_role(staff_role) != "doctor":
        raise ValueError("Инструкцию по оплате консультации может отправить только врач")
    content = (
        "Как оплатить платную консультацию врача за 1 000 ₽:\n\n"
        "1. Откройте меню ☰ в правом верхнем углу.\n"
        "2. Выберите раздел «Консультации».\n"
        "3. Нажмите «Оплатить консультацию · 1 000 ₽».\n"
        "4. При необходимости укажите электронную почту для чека и завершите оплату через ЮKassa.\n\n"
        "После подтверждения оплаты врач автоматически получит уведомление и напишет вам "
        "в отдельном чате, чтобы согласовать удобные дату и время. Информация об оплате "
        "сохранится в разделе «Мои покупки»."
    )
    return manager_add_reply(
        conversation_id, content, manager_name, "doctor",
        message_action="consultation_payment_instruction",
    )


def manager_send_psychology_session_payment_instruction(
    conversation_id: str, manager_name: str, staff_role: str = "psychologist",
) -> dict:
    """Send session purchase instructions with an interactive sessions-menu action."""
    if _staff_role(staff_role) != "psychologist":
        raise ValueError("Инструкцию по оплате сессий может отправить только психолог")
    with connection() as conn:
        conversation = conn.execute(
            """SELECT id FROM conversations
               WHERE id=? AND dialog_type='masterclass_psychology'
               AND COALESCE(human_recipient_role, 'manager')='psychologist'""",
            (conversation_id,),
        ).fetchone()
    if not conversation:
        raise ValueError("Психологический диалог не найден")
    content = (
        "Как приобрести дополнительные сессии с психологом:\n\n"
        "1. Нажмите кнопку «Открыть меню сессий» под этим сообщением.\n"
        "2. В меню нажмите «Купить».\n"
        "3. Выберите одну 60-минутную сессию за 3 500 ₽.\n"
        "4. Укажите электронную почту для чека и завершите оплату через ЮKassa.\n\n"
        "После подтверждения оплаты сессии сразу появятся в предоплаченном балансе. "
        "Затем откройте меню ещё раз и оставьте заявку на удобное время созвона."
    )
    return manager_add_reply(
        conversation_id, content, manager_name, "psychologist",
        message_action="psychology_session_payment_instruction",
    )


def manager_send_psychology_session_invitation(
    conversation_id: str, manager_name: str, staff_role: str,
    meeting_url: str, meeting_date: str, meeting_time: str,
) -> dict:
    """Send a validated video-meeting invitation to a psychology chat."""
    if _staff_role(staff_role) != "psychologist":
        raise ValueError("Приглашение на сессию может отправить только психолог")
    meeting_url = str(meeting_url or "").strip()
    parsed_url = urlparse(meeting_url)
    if parsed_url.scheme != "https" or not parsed_url.hostname or len(meeting_url) > 2_000:
        raise ValueError("Укажите корректную ссылку на видеовстречу, начинающуюся с https://")
    try:
        scheduled = datetime.fromisoformat(
            f"{str(meeting_date or '').strip()}T{str(meeting_time or '').strip()}"
        ).replace(tzinfo=MOSCOW_TZ)
    except ValueError as exc:
        raise ValueError("Укажите дату и время онлайн-встречи") from exc
    now = datetime.now(MOSCOW_TZ)
    if scheduled < now - timedelta(minutes=5):
        raise ValueError("Дата и время встречи уже прошли")
    if scheduled > now + timedelta(days=366):
        raise ValueError("Встречу можно назначить не более чем на год вперёд")
    with connection() as conn:
        conversation = conn.execute(
            """SELECT id FROM conversations
               WHERE id=? AND dialog_type='masterclass_psychology'
               AND COALESCE(human_recipient_role, 'manager')='psychologist'""",
            (conversation_id,),
        ).fetchone()
    if not conversation:
        raise ValueError("Психологический диалог не найден")
    months = (
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    )
    date_label = f"{scheduled.day} {months[scheduled.month - 1]} {scheduled.year} года"
    time_label = scheduled.strftime("%H:%M")
    content = (
        "Ваша онлайн-встреча с психологом назначена.\n\n"
        f"Дата: {date_label}\n"
        f"Время: {time_label} (мск)\n\n"
        "Нажмите кнопку ниже, чтобы подключиться к видеовстрече. "
        "Пожалуйста, подключитесь за 5 минут до начала. Если планы изменятся, "
        "напишите об этом в этом чате."
    )
    return manager_add_reply(
        conversation_id, content, manager_name, "psychologist",
        message_action="psychology_session_invitation",
        extra_metadata={
            "meeting_url": meeting_url,
            "scheduled_at": scheduled.isoformat(),
            "meeting_date": date_label,
            "meeting_time": time_label,
            "action_label": "Подключиться к видеовстрече",
        },
    )


def manager_set_ai_enabled(
    conversation_id: str, enabled: bool, manager_name: str,
    staff_role: str = "manager",
) -> dict | None:
    manager_name = " ".join(str(manager_name or "").split())[:80] or "Менеджер"
    staff_role = _staff_role(staff_role)
    now = utc_now()
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            """UPDATE conversations SET ai_enabled = ?, human_status = CASE
                WHEN ? = 0 THEN 'connected'
                WHEN human_ticket_id IS NOT NULL THEN 'pending'
                ELSE human_status END,
                status = CASE WHEN ? = 0 THEN 'waiting_human' ELSE 'active' END,
                updated_at = ? WHERE id = ?
                AND COALESCE(human_recipient_role, 'manager') = ?""",
            (int(bool(enabled)), int(bool(enabled)), int(bool(enabled)), now, conversation_id, staff_role),
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
    conversation_id: str, manager_name: str, staff_role: str = "manager",
) -> dict | None:
    manager_name = " ".join(str(manager_name or "").split())[:80] or "Менеджер"
    staff_role = _staff_role(staff_role)
    now = utc_now()
    with _write_lock, connection() as conn:
        previous = conn.execute(
            """SELECT *
            FROM conversations WHERE id = ?
            AND COALESCE(human_recipient_role, 'manager') = ?""",
            (conversation_id, staff_role),
        ).fetchone()
        if not previous:
            return None
        conn.execute(
            """UPDATE conversations SET ai_enabled = ?, human_status = 'closed',
            status = ?, updated_at = ? WHERE id = ?""",
            (
                1,
                "active",
                now, conversation_id,
            ),
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
        conn.execute(
            """UPDATE manager_notification_outbox SET status = 'cancelled',
            lease_token = NULL, leased_at = NULL, next_attempt_at = NULL
            WHERE conversation_id = ? AND status <> 'sent'
              AND staff_user_id IN (SELECT id FROM staff_users WHERE role = ?)""",
            (conversation_id, staff_role),
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
    from .chat_attachments import public_metadata as attachment_public_metadata
    attachment_meta = [
        attachment_public_metadata(item) for item in attachments or []
    ]
    message = add_message(
        conversation_id, "user", content,
        metadata={"attachments": attachment_meta, "awaiting_manager": True},
    )
    if conversation.get("human_status") == "closed":
        with _write_lock, connection() as conn:
            conn.execute(
                """UPDATE conversations SET human_status = 'pending',
                status = 'waiting_human', updated_at = ?
                WHERE id = ? AND chel_id = ? AND ai_enabled = 0""",
                (utc_now(), conversation_id, current_chel_id()),
            )
            conn.commit()
    updated = get_conversation(conversation_id)
    return message, updated


@contextmanager
def connection():
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.database_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.create_function("CASEFOLD", 1, _sqlite_casefold, deterministic=True)
    test_company_inn_users = _test_company_inn_chel_ids(conn)
    excluded_from_statistics = _statistics_excluded_chel_ids(conn)
    conn.create_function(
        "IS_STATS_USER", 1,
        lambda value: 0 if str(value or "") in excluded_from_statistics else 1,
        deterministic=True,
    )
    conn.create_function(
        "IS_TEST_INN_USER", 1,
        lambda value: 1 if str(value or "") in test_company_inn_users else 0,
        deterministic=True,
    )
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
                registration_method TEXT,
                from_manager TEXT NOT NULL DEFAULT '',
                result_entry_at TEXT
            );

            CREATE TABLE IF NOT EXISTS ikp_access_links (
                id TEXT PRIMARY KEY,
                source_key TEXT NOT NULL UNIQUE,
                inn TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                access_token TEXT NOT NULL UNIQUE,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_accessed_at TEXT,
                visit_count INTEGER NOT NULL DEFAULT 0,
                is_test INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS ikp_access_users (
                link_id TEXT NOT NULL,
                chel_id TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                visit_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(link_id,chel_id),
                FOREIGN KEY(link_id) REFERENCES ikp_access_links(id) ON DELETE CASCADE,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS masterclass_access_links (
                id TEXT PRIMARY KEY,
                source_key TEXT NOT NULL UNIQUE,
                inn TEXT NOT NULL,
                company TEXT NOT NULL DEFAULT '',
                masterclass_code TEXT NOT NULL,
                masterclass_name TEXT NOT NULL,
                access_token TEXT NOT NULL UNIQUE,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_accessed_at TEXT,
                visit_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS masterclass_access_users (
                link_id TEXT NOT NULL,
                chel_id TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                continued_at TEXT,
                profile_completed_at TEXT,
                visit_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(link_id,chel_id),
                FOREIGN KEY(link_id) REFERENCES masterclass_access_links(id) ON DELETE CASCADE,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
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
                dialog_type TEXT NOT NULL DEFAULT 'general',
                pinned INTEGER NOT NULL DEFAULT 0,
                active_agent TEXT NOT NULL DEFAULT 'manager',
                context_summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                ai_enabled INTEGER NOT NULL DEFAULT 1,
                human_status TEXT NOT NULL DEFAULT 'none',
                human_ticket_id TEXT,
                human_channel TEXT,
                human_phone TEXT,
                human_recipient_role TEXT NOT NULL DEFAULT 'manager',
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

            CREATE TABLE IF NOT EXISTS message_deletions (
                message_id INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                deleted_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_message_deletions_conversation
            ON message_deletions(conversation_id, message_id);

            CREATE TABLE IF NOT EXISTS conversation_context_compactions (
                conversation_id TEXT PRIMARY KEY,
                through_message_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                FOREIGN KEY(through_message_id) REFERENCES messages(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS conversation_reads (
                conversation_id TEXT PRIMARY KEY,
                last_read_message_id INTEGER NOT NULL DEFAULT 0,
                read_at TEXT NOT NULL,
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
                role TEXT NOT NULL DEFAULT 'manager',
                is_active INTEGER NOT NULL DEFAULT 1,
                telegram_id TEXT NOT NULL DEFAULT '',
                telegram_chat_id TEXT NOT NULL DEFAULT '',
                max_id TEXT NOT NULL DEFAULT '',
                max_chat_id TEXT NOT NULL DEFAULT '',
                notify_new_requests INTEGER NOT NULL DEFAULT 1,
                notify_new_messages INTEGER NOT NULL DEFAULT 1,
                notify_successful_payments INTEGER NOT NULL DEFAULT 1,
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

            CREATE TABLE IF NOT EXISTS staff_conversation_reads (
                staff_user_id INTEGER NOT NULL,
                conversation_id TEXT NOT NULL,
                last_read_message_id INTEGER NOT NULL DEFAULT 0,
                read_at TEXT NOT NULL,
                PRIMARY KEY(staff_user_id, conversation_id),
                FOREIGN KEY(staff_user_id) REFERENCES staff_users(id) ON DELETE CASCADE,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
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

            CREATE TABLE IF NOT EXISTS system_notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                lease_token TEXT,
                leased_at TEXT,
                sent_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(provider, recipient_id, dedupe_key)
            );

            CREATE TABLE IF NOT EXISTS data_retention_audit_runs (
                id TEXT PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'dry-run',
                cutoff_at TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                candidate_count INTEGER NOT NULL DEFAULT 0,
                messenger_count INTEGER NOT NULL DEFAULT 0,
                no_messenger_count INTEGER NOT NULL DEFAULT 0,
                excluded_count INTEGER NOT NULL DEFAULT 0,
                report_summary TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT ''
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
                driving_time TEXT NOT NULL DEFAULT 'unknown',
                dark_in_eyes TEXT NOT NULL DEFAULT 'unknown',
                joint_pain TEXT NOT NULL DEFAULT 'unknown',
                fatigue TEXT NOT NULL DEFAULT 'unknown',
                tube_number TEXT NOT NULL DEFAULT '',
                tube_linked_at TEXT,
                notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS onboarding_state (
                chel_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'questionnaire',
                entry_route TEXT NOT NULL DEFAULT '',
                selected_tests TEXT NOT NULL DEFAULT '[]',
                payment_status TEXT NOT NULL DEFAULT 'none',
                questionnaire_skipped INTEGER NOT NULL DEFAULT 0,
                marketer_result_seen INTEGER NOT NULL DEFAULT 0,
                intro_seen INTEGER NOT NULL DEFAULT 0,
                font_size TEXT NOT NULL DEFAULT 'extra',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS examination_catalog (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                default_name TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL,
                includes TEXT NOT NULL DEFAULT '',
                price INTEGER NOT NULL,
                competitor_price INTEGER NOT NULL DEFAULT 0,
                price_without_discount INTEGER NOT NULL DEFAULT 0,
                competitor_label TEXT NOT NULL DEFAULT 'У конкурентов',
                retail_price_label TEXT NOT NULL DEFAULT 'Розничная цена',
                discount_price_label TEXT NOT NULL DEFAULT 'С учётом вашей скидки',
                show_competitor_price INTEGER NOT NULL DEFAULT 1,
                show_retail_price INTEGER NOT NULL DEFAULT 1,
                show_discount_price INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_migrations (
                migration_key TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS funnel_monitor_settings (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                dialog_id TEXT NOT NULL DEFAULT '',
                send_time TEXT NOT NULL DEFAULT '09:00',
                timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
                period_days INTEGER NOT NULL DEFAULT 1,
                include_standard INTEGER NOT NULL DEFAULT 1,
                include_result INTEGER NOT NULL DEFAULT 1,
                include_payments INTEGER NOT NULL DEFAULT 1,
                include_errors INTEGER NOT NULL DEFAULT 1,
                minimum_users INTEGER NOT NULL DEFAULT 20,
                alert_threshold_pp REAL NOT NULL DEFAULT 10,
                last_sent_date TEXT NOT NULL DEFAULT '',
                last_sent_at TEXT NOT NULL DEFAULT '',
                last_attempt_at TEXT NOT NULL DEFAULT '',
                last_status TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            INSERT OR IGNORE INTO funnel_monitor_settings (id,updated_at)
            VALUES (1,CURRENT_TIMESTAMP);

            CREATE TABLE IF NOT EXISTS experiment_settings (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                enabled INTEGER NOT NULL DEFAULT 0,
                experiment_key TEXT NOT NULL DEFAULT 'marketer_funnel_2026',
                name TEXT NOT NULL DEFAULT 'Воронка маркетолога',
                marketer_percent INTEGER NOT NULL DEFAULT 25,
                control_version TEXT NOT NULL DEFAULT 'main_v1',
                marketer_version TEXT NOT NULL DEFAULT 'marketer_v1',
                yandex_enabled INTEGER NOT NULL DEFAULT 1,
                yandex_goal_prefix TEXT NOT NULL DEFAULT 'consilium_marketer',
                assignment_counter INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            INSERT OR IGNORE INTO experiment_settings (id,updated_at)
            VALUES (1,CURRENT_TIMESTAMP);

            CREATE TABLE IF NOT EXISTS experiment_assignments (
                experiment_key TEXT NOT NULL,
                chel_id TEXT NOT NULL,
                variant TEXT NOT NULL CHECK(variant IN ('control','marketer')),
                version TEXT NOT NULL,
                assigned_at TEXT NOT NULL,
                PRIMARY KEY(experiment_key,chel_id),
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ai_branch (
                chel_id TEXT PRIMARY KEY,
                branch TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS assistant_personality_settings (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                preset TEXT NOT NULL DEFAULT 'balanced',
                address_mode TEXT NOT NULL DEFAULT 'formal',
                formality INTEGER NOT NULL DEFAULT 35 CHECK(formality BETWEEN 0 AND 100),
                warmth INTEGER NOT NULL DEFAULT 80 CHECK(warmth BETWEEN 0 AND 100),
                sociability INTEGER NOT NULL DEFAULT 70 CHECK(sociability BETWEEN 0 AND 100),
                supportiveness INTEGER NOT NULL DEFAULT 85 CHECK(supportiveness BETWEEN 0 AND 100),
                humor INTEGER NOT NULL DEFAULT 15 CHECK(humor BETWEEN 0 AND 100),
                emoji INTEGER NOT NULL DEFAULT 5 CHECK(emoji BETWEEN 0 AND 100),
                initiative INTEGER NOT NULL DEFAULT 60 CHECK(initiative BETWEEN 0 AND 100),
                provider TEXT NOT NULL DEFAULT 'openai',
                openai_model TEXT NOT NULL DEFAULT 'gpt-5.6-sol',
                apply_to_all INTEGER NOT NULL DEFAULT 0 CHECK(apply_to_all IN (0,1)),
                test_branch_model TEXT NOT NULL DEFAULT 'deepseek-v4.1-flash',
                test_branch_fallback_model TEXT NOT NULL DEFAULT 'qwen3.6-35b-a3b',
                updated_at TEXT NOT NULL
            );

            INSERT OR IGNORE INTO assistant_personality_settings
            (id,preset,formality,warmth,sociability,supportiveness,updated_at)
            VALUES (1,'balanced',35,80,70,85,CURRENT_TIMESTAMP);

            CREATE TABLE IF NOT EXISTS assistant_personality_profiles (
                profile_key TEXT PRIMARY KEY CHECK(
                    profile_key IN ('weight_control','masterclass_psychology')
                ),
                preset TEXT NOT NULL DEFAULT 'balanced',
                address_mode TEXT NOT NULL DEFAULT 'formal',
                formality INTEGER NOT NULL DEFAULT 35 CHECK(formality BETWEEN 0 AND 100),
                warmth INTEGER NOT NULL DEFAULT 80 CHECK(warmth BETWEEN 0 AND 100),
                sociability INTEGER NOT NULL DEFAULT 70 CHECK(sociability BETWEEN 0 AND 100),
                supportiveness INTEGER NOT NULL DEFAULT 85 CHECK(supportiveness BETWEEN 0 AND 100),
                humor INTEGER NOT NULL DEFAULT 15 CHECK(humor BETWEEN 0 AND 100),
                emoji INTEGER NOT NULL DEFAULT 5 CHECK(emoji BETWEEN 0 AND 100),
                initiative INTEGER NOT NULL DEFAULT 60 CHECK(initiative BETWEEN 0 AND 100),
                provider TEXT NOT NULL DEFAULT 'openai',
                openai_model TEXT NOT NULL DEFAULT 'gpt-5.6-sol',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS psychology_dialog_settings (
                id INTEGER PRIMARY KEY CHECK(id=1),
                duration_trigger_days INTEGER NOT NULL DEFAULT 14,
                impact_trigger_score INTEGER NOT NULL DEFAULT 6,
                technique_shift_trigger INTEGER NOT NULL DEFAULT 2,
                max_messages_before_offer INTEGER NOT NULL DEFAULT 10,
                offer_cooldown_messages INTEGER NOT NULL DEFAULT 3,
                updated_at TEXT NOT NULL
            );

            INSERT OR IGNORE INTO psychology_dialog_settings
            (id,duration_trigger_days,impact_trigger_score,technique_shift_trigger,
             max_messages_before_offer,offer_cooldown_messages,updated_at)
            VALUES (1,14,6,2,10,3,CURRENT_TIMESTAMP);

            CREATE INDEX IF NOT EXISTS idx_experiment_assignments_variant_time
            ON experiment_assignments(experiment_key,variant,assigned_at);

            CREATE TABLE IF NOT EXISTS marketer_variant_assignments (
                experiment_key TEXT NOT NULL,
                chel_id TEXT NOT NULL,
                variant TEXT NOT NULL CHECK(variant IN ('b','c')),
                assigned_at TEXT NOT NULL,
                PRIMARY KEY(experiment_key,chel_id),
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_marketer_variant_assignments_variant_time
            ON marketer_variant_assignments(experiment_key,variant,assigned_at);

            CREATE TABLE IF NOT EXISTS content_text_settings (
                section_id TEXT PRIMARY KEY,
                value_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS health_passports (
                chel_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'offered'
                    CHECK(status IN ('offered','declined','generating','ready','failed')),
                content_json TEXT NOT NULL DEFAULT '{}',
                last_error TEXT NOT NULL DEFAULT '',
                generated_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS weight_control_states (
                conversation_id TEXT PRIMARY KEY,
                chel_id TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT 'intake',
                assessment TEXT NOT NULL DEFAULT '{}',
                analysis TEXT NOT NULL DEFAULT '{}',
                body_measurements TEXT NOT NULL DEFAULT '{}',
                risk_level TEXT NOT NULL DEFAULT 'routine',
                program_started_at TEXT,
                program_ends_at TEXT,
                onboarding_shown_at TEXT,
                messenger_prompt_last_shown_on TEXT,
                messenger_prompt_dismissed_at TEXT,
                completion_notified_at TEXT,
                conclusion_status TEXT NOT NULL DEFAULT 'idle',
                conclusion_error TEXT NOT NULL DEFAULT '',
                conclusion_message_id INTEGER,
                personality_mode TEXT NOT NULL DEFAULT 'balanced',
                address_mode TEXT NOT NULL DEFAULT 'formal',
                use_emoji INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS weight_control_body_measurements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                chel_id TEXT NOT NULL,
                measured_on TEXT NOT NULL,
                weight_kg REAL,
                waist_cm REAL,
                hips_cm REAL,
                chest_cm REAL,
                thigh_cm REAL,
                arm_cm REAL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(conversation_id, measured_on),
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_weight_body_measurements_history
            ON weight_control_body_measurements(conversation_id,measured_on DESC);

            CREATE TABLE IF NOT EXISTS weight_control_meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                chel_id TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'text',
                status TEXT NOT NULL DEFAULT 'confirmed',
                description TEXT NOT NULL DEFAULT '',
                analysis TEXT NOT NULL DEFAULT '{}',
                user_message_id INTEGER,
                assistant_message_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE,
                FOREIGN KEY(user_message_id) REFERENCES messages(id) ON DELETE SET NULL,
                FOREIGN KEY(assistant_message_id) REFERENCES messages(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS idx_weight_control_meals_day
            ON weight_control_meals(conversation_id,entry_date,created_at);

            CREATE TABLE IF NOT EXISTS weight_control_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                chel_id TEXT NOT NULL,
                reminder_type TEXT NOT NULL DEFAULT 'custom',
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                time_local TEXT NOT NULL,
                weekdays TEXT NOT NULL DEFAULT '[]',
                timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
                interval_days INTEGER NOT NULL DEFAULT 0,
                one_off INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                next_run_at TEXT NOT NULL,
                last_sent_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_weight_reminders_due
            ON weight_control_reminders(enabled,next_run_at);

            CREATE TABLE IF NOT EXISTS weight_control_reminder_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reminder_id INTEGER NOT NULL,
                scheduled_for TEXT NOT NULL,
                message_id INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(reminder_id,scheduled_for),
                FOREIGN KEY(reminder_id) REFERENCES weight_control_reminders(id) ON DELETE CASCADE,
                FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS weight_control_reminder_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                chel_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT,
                leased_at TEXT,
                next_attempt_at TEXT,
                sent_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id,provider),
                FOREIGN KEY(run_id) REFERENCES weight_control_reminder_runs(id) ON DELETE CASCADE,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS payment_orders (
                id TEXT PRIMARY KEY,
                chel_id TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'yookassa',
                provider_payment_id TEXT UNIQUE,
                idempotence_key TEXT NOT NULL UNIQUE,
                selection_fingerprint TEXT NOT NULL,
                order_type TEXT NOT NULL DEFAULT 'examinations',
                target_conversation_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'creating',
                amount_kopecks INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',
                items TEXT NOT NULL,
                confirmation_url TEXT NOT NULL DEFAULT '',
                paid INTEGER NOT NULL DEFAULT 0,
                test INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                paid_at TEXT,
                canceled_at TEXT,
                hidden_at TEXT,
                fulfillment_at TEXT,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS psychology_sessions (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                chel_id TEXT NOT NULL,
                session_number INTEGER NOT NULL,
                request_number TEXT,
                access_type TEXT NOT NULL,
                payment_order_id TEXT UNIQUE,
                status TEXT NOT NULL DEFAULT 'active',
                started_at TEXT NOT NULL,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(conversation_id,session_number),
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE,
                FOREIGN KEY(payment_order_id) REFERENCES payment_orders(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS psychology_session_credit_uses (
                id TEXT PRIMARY KEY,
                payment_order_id TEXT NOT NULL,
                session_id TEXT NOT NULL UNIQUE,
                credit_number INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(payment_order_id,credit_number),
                FOREIGN KEY(payment_order_id) REFERENCES payment_orders(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES psychology_sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS psychology_dialog_states (
                conversation_id TEXT PRIMARY KEY,
                chel_id TEXT NOT NULL,
                state_json TEXT NOT NULL DEFAULT '{}',
                message_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS enterprise_examination_schedule (
                source_sheet_id TEXT NOT NULL,
                source_row_id TEXT NOT NULL,
                inn TEXT NOT NULL,
                organization_name TEXT NOT NULL,
                examination_date TEXT NOT NULL,
                brigade TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                source_updated_at TEXT NOT NULL DEFAULT '',
                synced_at TEXT NOT NULL,
                PRIMARY KEY(source_sheet_id, source_row_id)
            );

            CREATE TABLE IF NOT EXISTS lab_result_value_estimates (
                chel_id TEXT PRIMARY KEY,
                med_id TEXT NOT NULL DEFAULT '',
                document_fingerprint TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                estimated_amount INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0,
                document_count INTEGER NOT NULL DEFAULT 0,
                analyzed_document_count INTEGER NOT NULL DEFAULT 0,
                ocr_document_count INTEGER NOT NULL DEFAULT 0,
                matched_exam_ids TEXT NOT NULL DEFAULT '[]',
                recognized_analytes TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS external_identities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                provider_user_id TEXT NOT NULL,
                chat_id TEXT NOT NULL DEFAULT '',
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
                return_path TEXT NOT NULL DEFAULT '',
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS auth_intents (
                token_hash TEXT PRIMARY KEY,
                chel_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                return_path TEXT NOT NULL DEFAULT '',
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

            CREATE TABLE IF NOT EXISTS lab_result_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chel_id TEXT NOT NULL,
                med_id TEXT NOT NULL,
                providers TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                notified_at TEXT,
                next_check_at TEXT,
                last_checked_at TEXT,
                UNIQUE(chel_id, med_id),
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_result_notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                chel_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT,
                leased_at TEXT,
                next_attempt_at TEXT,
                sent_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(subscription_id, provider),
                FOREIGN KEY(subscription_id) REFERENCES lab_result_subscriptions(id) ON DELETE CASCADE,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS checkup_reoffer_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chel_id TEXT NOT NULL,
                journey_id TEXT NOT NULL,
                company_inn TEXT NOT NULL DEFAULT '',
                active_seconds INTEGER NOT NULL DEFAULT 0,
                duration_bucket TEXT NOT NULL DEFAULT 'under_30',
                experiment_variant TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE(chel_id, journey_id),
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS checkup_reoffers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chel_id TEXT NOT NULL,
                candidate_id INTEGER NOT NULL,
                company_inn TEXT NOT NULL,
                examination_date TEXT NOT NULL,
                active_seconds INTEGER NOT NULL,
                duration_bucket TEXT NOT NULL,
                access_token TEXT NOT NULL UNIQUE,
                conversation_id TEXT NOT NULL DEFAULT '',
                message_id INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                exclusion_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                sent_at TEXT,
                opened_at TEXT,
                clicked_at TEXT,
                is_test INTEGER NOT NULL DEFAULT 0,
                UNIQUE(chel_id, examination_date),
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE,
                FOREIGN KEY(candidate_id) REFERENCES checkup_reoffer_candidates(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reoffer_id INTEGER NOT NULL,
                chel_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                recipient_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT,
                leased_at TEXT,
                next_attempt_at TEXT,
                sent_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(reoffer_id, provider),
                FOREIGN KEY(reoffer_id) REFERENCES checkup_reoffers(id) ON DELETE CASCADE,
                FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS checkup_reoffer_settings (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
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
        personality_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(assistant_personality_settings)"
            ).fetchall()
        }
        personality_migrations = {
            "address_mode": "TEXT NOT NULL DEFAULT 'formal'",
            "humor": "INTEGER NOT NULL DEFAULT 15",
            "emoji": "INTEGER NOT NULL DEFAULT 5",
            "initiative": "INTEGER NOT NULL DEFAULT 60",
            "provider": "TEXT NOT NULL DEFAULT 'openai'",
            "openai_model": "TEXT NOT NULL DEFAULT 'gpt-5.6-sol'",
            "apply_to_all": "INTEGER NOT NULL DEFAULT 0",
            "test_branch_model": "TEXT NOT NULL DEFAULT 'deepseek-v4.1-flash'",
            "test_branch_fallback_model": "TEXT NOT NULL DEFAULT 'qwen3.6-35b-a3b'",
        }
        for name, declaration in personality_migrations.items():
            if name not in personality_columns:
                conn.execute(
                    f"ALTER TABLE assistant_personality_settings ADD COLUMN {name} {declaration}"
                )
        profile_personality_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(assistant_personality_profiles)"
            ).fetchall()
        }
        for name, declaration in {
            "provider": "TEXT NOT NULL DEFAULT 'openai'",
            "openai_model": "TEXT NOT NULL DEFAULT 'gpt-5.6-sol'",
        }.items():
            if name not in profile_personality_columns:
                conn.execute(
                    f"ALTER TABLE assistant_personality_profiles ADD COLUMN {name} {declaration}"
                )
        psychology_defaults = {
            **ASSISTANT_PERSONALITY_DEFAULTS,
            **ASSISTANT_PERSONALITY_PROFILE_DEFAULTS["masterclass_psychology"],
        }
        for profile_key, defaults in (
            ("weight_control", ASSISTANT_PERSONALITY_DEFAULTS),
            ("masterclass_psychology", psychology_defaults),
        ):
            conn.execute(
                """INSERT OR IGNORE INTO assistant_personality_profiles
                   (profile_key,preset,address_mode,formality,warmth,sociability,
                    supportiveness,humor,emoji,initiative,provider,openai_model,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    profile_key, defaults["preset"], defaults["address_mode"],
                    defaults["formality"], defaults["warmth"],
                    defaults["sociability"], defaults["supportiveness"],
                    defaults["humor"], defaults["emoji"],
                    defaults["initiative"], defaults["provider"],
                    defaults["openai_model"], now,
                ),
            )
        conn.execute(
            "INSERT OR IGNORE INTO checkup_reoffer_settings (id,enabled,updated_at) VALUES (1,1,?)",
            (now,),
        )
        reoffer_columns = {
            item[1] for item in conn.execute("PRAGMA table_info(checkup_reoffers)").fetchall()
        }
        if "is_test" not in reoffer_columns:
            conn.execute(
                "ALTER TABLE checkup_reoffers ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0"
            )
        experiment_settings_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(experiment_settings)").fetchall()
        }
        if "assignment_counter" not in experiment_settings_columns:
            conn.execute(
                "ALTER TABLE experiment_settings ADD COLUMN assignment_counter INTEGER NOT NULL DEFAULT 0"
            )
        users_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        registration_columns_added = False
        if "registered_at" not in users_columns:
            conn.execute("ALTER TABLE users ADD COLUMN registered_at TEXT")
            registration_columns_added = True
        if "registration_method" not in users_columns:
            conn.execute("ALTER TABLE users ADD COLUMN registration_method TEXT")
            registration_columns_added = True
        if "from_manager" not in users_columns:
            conn.execute("ALTER TABLE users ADD COLUMN from_manager TEXT NOT NULL DEFAULT ''")
        if "result_entry_at" not in users_columns:
            conn.execute("ALTER TABLE users ADD COLUMN result_entry_at TEXT")
        examination_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(examination_catalog)").fetchall()
        }
        if "competitor_price" not in examination_columns:
            conn.execute(
                "ALTER TABLE examination_catalog ADD COLUMN competitor_price INTEGER NOT NULL DEFAULT 0"
            )
        if "price_without_discount" not in examination_columns:
            conn.execute(
                "ALTER TABLE examination_catalog ADD COLUMN price_without_discount INTEGER NOT NULL DEFAULT 0"
            )
        examination_defaults = {
            "default_name": "TEXT NOT NULL DEFAULT ''",
            "competitor_label": "TEXT NOT NULL DEFAULT 'У конкурентов'",
            "retail_price_label": "TEXT NOT NULL DEFAULT 'Розничная цена'",
            "discount_price_label": "TEXT NOT NULL DEFAULT 'С учётом вашей скидки'",
            "show_competitor_price": "INTEGER NOT NULL DEFAULT 1",
            "show_retail_price": "INTEGER NOT NULL DEFAULT 1",
            "show_discount_price": "INTEGER NOT NULL DEFAULT 1",
        }
        for column, declaration in examination_defaults.items():
            if column not in examination_columns:
                conn.execute(
                    f"ALTER TABLE examination_catalog ADD COLUMN {column} {declaration}"
                )
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
                (id, name, default_name, description, includes, price, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    examination["id"], examination["name"], examination["default_name"],
                    examination["description"],
                    examination.get("includes", ""), int(examination["price"]), now, now,
                ),
            )
        catalog_migration_key = "examination_catalog_2026_08_31"
        catalog_migrated = conn.execute(
            "SELECT 1 FROM app_migrations WHERE migration_key = ?",
            (catalog_migration_key,),
        ).fetchone()
        if not catalog_migrated:
            # The spreadsheet dated 2026-08-31 is authoritative for the core
            # name, benefit, composition and actual sale price. Presentation-only
            # comparison prices and custom labels remain under administrator control.
            for examination in TEST_CATALOG:
                conn.execute(
                    """UPDATE examination_catalog
                    SET name = ?, description = ?, includes = ?, price = ?, updated_at = ?
                    WHERE id = ?""",
                    (
                        examination["name"], examination["description"],
                        examination.get("includes", ""), int(examination["price"]),
                        now, examination["id"],
                    ),
                )
            # This old standalone item was merged into «Профилактика анемии».
            conn.execute("DELETE FROM examination_catalog WHERE id = 'ferritin'")
            for state_row in conn.execute(
                "SELECT chel_id, selected_tests FROM onboarding_state"
            ).fetchall():
                try:
                    selected_tests = json.loads(state_row["selected_tests"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    selected_tests = []
                if not isinstance(selected_tests, list) or "ferritin" not in selected_tests:
                    continue
                selected_tests = [item for item in selected_tests if item != "ferritin"]
                conn.execute(
                    "UPDATE onboarding_state SET selected_tests = ?, updated_at = ? WHERE chel_id = ?",
                    (json.dumps(selected_tests, ensure_ascii=False), now, state_row["chel_id"]),
                )
            conn.execute(
                "INSERT INTO app_migrations (migration_key, applied_at) VALUES (?, ?)",
                (catalog_migration_key, now),
            )
        contract_catalog_migration_key = "examination_catalog_2026_09_17"
        contract_catalog_migrated = conn.execute(
            "SELECT 1 FROM app_migrations WHERE migration_key = ?",
            (contract_catalog_migration_key,),
        ).fetchone()
        if not contract_catalog_migrated:
            # "Приложение к договору от 17.09.2026" is now authoritative: the
            # extended fatigue/weight complexes drop cortisol and cost less,
            # and cortisol is no longer sold as a standalone item.
            for examination in TEST_CATALOG:
                conn.execute(
                    """UPDATE examination_catalog
                    SET name = ?, description = ?, includes = ?, price = ?, updated_at = ?
                    WHERE id = ?""",
                    (
                        examination["name"], examination["description"],
                        examination.get("includes", ""), int(examination["price"]),
                        now, examination["id"],
                    ),
                )
            conn.execute("DELETE FROM examination_catalog WHERE id = 'cortisol'")
            for state_row in conn.execute(
                "SELECT chel_id, selected_tests FROM onboarding_state"
            ).fetchall():
                try:
                    selected_tests = json.loads(state_row["selected_tests"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    selected_tests = []
                if not isinstance(selected_tests, list) or "cortisol" not in selected_tests:
                    continue
                selected_tests = [item for item in selected_tests if item != "cortisol"]
                conn.execute(
                    "UPDATE onboarding_state SET selected_tests = ?, updated_at = ? WHERE chel_id = ?",
                    (json.dumps(selected_tests, ensure_ascii=False), now, state_row["chel_id"]),
                )
            conn.execute(
                "INSERT INTO app_migrations (migration_key, applied_at) VALUES (?, ?)",
                (contract_catalog_migration_key, now),
            )
        default_names_migration_key = "examination_default_names_2026_09_09"
        default_names_migrated = conn.execute(
            "SELECT 1 FROM app_migrations WHERE migration_key = ?",
            (default_names_migration_key,),
        ).fetchone()
        if not default_names_migrated:
            # Custom catalog entries keep their current visible name until an
            # administrator explicitly sets a separate Bitrix/default name.
            conn.execute(
                """UPDATE examination_catalog SET default_name = name
                WHERE TRIM(COALESCE(default_name, '')) = ''"""
            )
            # Core names come from the authoritative old/new-name spreadsheet.
            for examination in TEST_CATALOG:
                conn.execute(
                    "UPDATE examination_catalog SET default_name = ? WHERE id = ?",
                    (examination["default_name"], examination["id"]),
                )
            conn.execute(
                "INSERT INTO app_migrations (migration_key, applied_at) VALUES (?, ?)",
                (default_names_migration_key, now),
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
        if "human_recipient_role" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN human_recipient_role TEXT NOT NULL DEFAULT 'manager'")
        if "dialog_type" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN dialog_type TEXT NOT NULL DEFAULT 'general'")
        if "pinned" not in columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_weight_control_one_per_user
            ON conversations(chel_id, dialog_type)
            WHERE dialog_type = 'weight_control'"""
        )
        weight_state_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(weight_control_states)"
            ).fetchall()
        }
        for name, declaration in (
            ("body_measurements", "TEXT NOT NULL DEFAULT '{}'"),
            ("program_started_at", "TEXT"),
            ("program_ends_at", "TEXT"),
            ("onboarding_shown_at", "TEXT"),
            ("messenger_prompt_last_shown_on", "TEXT"),
            ("messenger_prompt_dismissed_at", "TEXT"),
            ("completion_notified_at", "TEXT"),
            ("conclusion_status", "TEXT NOT NULL DEFAULT 'idle'"),
            ("conclusion_error", "TEXT NOT NULL DEFAULT ''"),
            ("conclusion_message_id", "INTEGER"),
            ("personality_mode", "TEXT NOT NULL DEFAULT 'balanced'"),
            ("address_mode", "TEXT NOT NULL DEFAULT 'formal'"),
            ("use_emoji", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in weight_state_columns:
                conn.execute(
                    f"ALTER TABLE weight_control_states ADD COLUMN {name} {declaration}"
                )
        weight_reminder_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(weight_control_reminders)"
            ).fetchall()
        }
        for name, declaration in (
            ("interval_days", "INTEGER NOT NULL DEFAULT 0"),
            ("one_off", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in weight_reminder_columns:
                conn.execute(
                    f"ALTER TABLE weight_control_reminders ADD COLUMN {name} {declaration}"
                )

        staff_columns = {row[1] for row in conn.execute("PRAGMA table_info(staff_users)").fetchall()}
        for name in ("telegram_id", "telegram_chat_id", "max_id", "max_chat_id"):
            if name not in staff_columns:
                conn.execute(f"ALTER TABLE staff_users ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        for name in (
            "notify_new_requests", "notify_new_messages",
            "notify_successful_payments",
        ):
            if name not in staff_columns:
                conn.execute(f"ALTER TABLE staff_users ADD COLUMN {name} INTEGER NOT NULL DEFAULT 1")
        if "role" not in staff_columns:
            conn.execute("ALTER TABLE staff_users ADD COLUMN role TEXT NOT NULL DEFAULT 'manager'")
        identity_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(external_identities)").fetchall()
        }
        if "chat_id" not in identity_columns:
            conn.execute(
                "ALTER TABLE external_identities ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''"
            )
        outbox_columns = {row[1] for row in conn.execute("PRAGMA table_info(manager_notification_outbox)").fetchall()}
        if "next_attempt_at" not in outbox_columns:
            conn.execute("ALTER TABLE manager_notification_outbox ADD COLUMN next_attempt_at TEXT")

        result_subscription_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(lab_result_subscriptions)").fetchall()
        }
        if "next_check_at" not in result_subscription_columns:
            conn.execute("ALTER TABLE lab_result_subscriptions ADD COLUMN next_check_at TEXT")
        if "last_checked_at" not in result_subscription_columns:
            conn.execute("ALTER TABLE lab_result_subscriptions ADD COLUMN last_checked_at TEXT")

        valuation_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(lab_result_value_estimates)"
            ).fetchall()
        }
        if "ocr_document_count" not in valuation_columns:
            conn.execute(
                "ALTER TABLE lab_result_value_estimates "
                "ADD COLUMN ocr_document_count INTEGER NOT NULL DEFAULT 0"
            )

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
                    driving_time TEXT NOT NULL DEFAULT 'unknown',
                    dark_in_eyes TEXT NOT NULL DEFAULT 'unknown', joint_pain TEXT NOT NULL DEFAULT 'unknown',
                    fatigue TEXT NOT NULL DEFAULT 'unknown', tube_number TEXT NOT NULL DEFAULT '',
                    tube_linked_at TEXT,
                    notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
                )"""
            )
            conn.execute(
                """INSERT INTO user_profile
                (chel_id, preferred_name, company_inn, age, sex, height_cm, weight_kg, pregnancy, conditions,
                 medications, allergies, smoking, alcohol, activity, blood_pressure, blood_sugar,
                 driving_time, dark_in_eyes, joint_pain, fatigue, notes, updated_at)
                SELECT 'chel_legacy', preferred_name, '', age, sex, height_cm, weight_kg, pregnancy,
                 conditions, medications, allergies, smoking, alcohol, activity, blood_pressure,
                 blood_sugar, 'unknown', dark_in_eyes, joint_pain, fatigue, notes, updated_at
                FROM user_profile_legacy WHERE id = 1"""
            )
            conn.execute("DROP TABLE user_profile_legacy")
            profile_columns = {row[1] for row in conn.execute("PRAGMA table_info(user_profile)").fetchall()}
        for name in ("alcohol", "activity", "blood_pressure", "blood_sugar", "driving_time", "dark_in_eyes", "joint_pain", "fatigue"):
            if name not in profile_columns:
                conn.execute(f"ALTER TABLE user_profile ADD COLUMN {name} TEXT NOT NULL DEFAULT 'unknown'")
        if "tube_number" not in profile_columns:
            conn.execute("ALTER TABLE user_profile ADD COLUMN tube_number TEXT NOT NULL DEFAULT ''")
        if "tube_linked_at" not in profile_columns:
            conn.execute("ALTER TABLE user_profile ADD COLUMN tube_linked_at TEXT")
            conn.execute(
                """UPDATE user_profile SET tube_linked_at=updated_at
                WHERE TRIM(COALESCE(tube_number,'')) <> ''
                  AND tube_linked_at IS NULL"""
            )
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
                    questionnaire_skipped INTEGER NOT NULL DEFAULT 0,
                    marketer_result_seen INTEGER NOT NULL DEFAULT 0,
                    intro_seen INTEGER NOT NULL DEFAULT 0,
                    font_size TEXT NOT NULL DEFAULT 'standard',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(chel_id) REFERENCES users(chel_id) ON DELETE CASCADE
                )"""
            )
            conn.execute(
                """INSERT INTO onboarding_state
                (chel_id, status, selected_tests, payment_status, questionnaire_skipped, intro_seen, font_size, updated_at)
                SELECT 'chel_legacy', status, selected_tests, payment_status, 0, intro_seen, font_size, updated_at
                FROM onboarding_state_legacy WHERE id = 1"""
            )
            conn.execute("DROP TABLE onboarding_state_legacy")
            onboarding_columns = {row[1] for row in conn.execute("PRAGMA table_info(onboarding_state)").fetchall()}
        if "intro_seen" not in onboarding_columns:
            conn.execute("ALTER TABLE onboarding_state ADD COLUMN intro_seen INTEGER NOT NULL DEFAULT 0")
        if "font_size" not in onboarding_columns:
            conn.execute("ALTER TABLE onboarding_state ADD COLUMN font_size TEXT NOT NULL DEFAULT 'standard'")
        if "questionnaire_skipped" not in onboarding_columns:
            conn.execute("ALTER TABLE onboarding_state ADD COLUMN questionnaire_skipped INTEGER NOT NULL DEFAULT 0")
        if "marketer_result_seen" not in onboarding_columns:
            conn.execute("ALTER TABLE onboarding_state ADD COLUMN marketer_result_seen INTEGER NOT NULL DEFAULT 0")
        if "entry_route" not in onboarding_columns:
            conn.execute("ALTER TABLE onboarding_state ADD COLUMN entry_route TEXT NOT NULL DEFAULT ''")

        masterclass_link_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(masterclass_access_links)"
            ).fetchall()
        }
        if "is_test" not in masterclass_link_columns:
            conn.execute(
                "ALTER TABLE masterclass_access_links ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            """UPDATE masterclass_access_links SET is_test=1
               WHERE company IN (?, ?)""",
            ("Тестовая ветка Метрики 2.0", "Локальный тест мастер-класса"),
        )

        masterclass_user_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(masterclass_access_users)"
            ).fetchall()
        }
        if "profile_completed_at" not in masterclass_user_columns:
            conn.execute(
                "ALTER TABLE masterclass_access_users ADD COLUMN profile_completed_at TEXT"
            )

        login_token_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(login_tokens)").fetchall()
        }
        if "return_path" not in login_token_columns:
            conn.execute(
                "ALTER TABLE login_tokens ADD COLUMN return_path TEXT NOT NULL DEFAULT ''"
            )
        auth_intent_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(auth_intents)").fetchall()
        }
        if "return_path" not in auth_intent_columns:
            conn.execute(
                "ALTER TABLE auth_intents ADD COLUMN return_path TEXT NOT NULL DEFAULT ''"
            )

        payment_columns = {row[1] for row in conn.execute("PRAGMA table_info(payment_orders)").fetchall()}
        if "hidden_at" not in payment_columns:
            conn.execute("ALTER TABLE payment_orders ADD COLUMN hidden_at TEXT")
        if "order_type" not in payment_columns:
            conn.execute("ALTER TABLE payment_orders ADD COLUMN order_type TEXT NOT NULL DEFAULT 'examinations'")
        if "target_conversation_id" not in payment_columns:
            conn.execute("ALTER TABLE payment_orders ADD COLUMN target_conversation_id TEXT NOT NULL DEFAULT ''")
        if "fulfillment_at" not in payment_columns:
            conn.execute("ALTER TABLE payment_orders ADD COLUMN fulfillment_at TEXT")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_chel_id ON conversations(chel_id, updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_registered_at ON users(registered_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ikp_access_users_chel_id ON ikp_access_users(chel_id,last_seen_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_masterclass_access_users_chel_id ON masterclass_access_users(chel_id,last_seen_at)")
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_masterclass_psychology_one_per_user
            ON conversations(chel_id, dialog_type)
            WHERE dialog_type = 'masterclass_psychology'"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_last_seen_at ON users(last_seen_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_updated_at ON conversations(updated_at)")
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_conversations_staff_queue
            ON conversations(human_recipient_role, human_status, ai_enabled, updated_at)"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_staff_role_active ON staff_users(role, is_active)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_device_stats_last_seen_at ON user_device_stats(last_seen_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_onboarding_status ON onboarding_state(status)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_from_manager "
            "ON users(from_manager, registered_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_device_stats_audience "
            "ON user_device_stats(device_type, operating_system, browser, last_seen_at)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_human_queue ON conversations(human_status, updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id, id)")
        conn.execute(
            """INSERT OR IGNORE INTO conversation_reads
            (conversation_id, last_read_message_id, read_at)
            SELECT c.id, COALESCE(MAX(m.id), 0), ?
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id AND m.role = 'assistant'
            GROUP BY c.id""",
            (now,),
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_manager_actions_conversation_id ON manager_actions(conversation_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_sessions_user ON staff_sessions(staff_user_id, expires_at)")
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_staff_conversation_reads_conversation
            ON staff_conversation_reads(conversation_id, staff_user_id)"""
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_telegram_id ON staff_users(telegram_id) WHERE telegram_id <> ''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_max_id ON staff_users(max_id) WHERE max_id <> ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_messenger_tokens ON staff_messenger_tokens(staff_user_id, provider, expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_manager_notification_delivery ON manager_notification_outbox(provider, status, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_system_notification_delivery ON system_notification_outbox(provider, status, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_retention_runs_completed ON data_retention_audit_runs(status, completed_at)")
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lab_result_subscriptions_status "
            "ON lab_result_subscriptions(status, next_check_at, updated_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_result_notification_delivery "
            "ON user_result_notification_outbox(provider, status, next_attempt_at, id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_created_at ON ai_usage(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_usage_model ON ai_usage(model, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_health_passports_status ON health_passports(status, updated_at)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payment_orders_user "
            "ON payment_orders(chel_id, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payment_orders_visible "
            "ON payment_orders(chel_id, hidden_at, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payment_orders_provider "
            "ON payment_orders(provider_payment_id, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payment_orders_type_user "
            "ON payment_orders(order_type, chel_id, paid_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_psychology_sessions_conversation "
            "ON psychology_sessions(conversation_id, session_number DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_psychology_sessions_balance "
            "ON psychology_sessions(chel_id, access_type, status)"
        )
        psychology_session_columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(psychology_sessions)"
            ).fetchall()
        }
        if "request_number" not in psychology_session_columns:
            conn.execute("ALTER TABLE psychology_sessions ADD COLUMN request_number TEXT")
        used_request_numbers = {
            str(row[0]) for row in conn.execute(
                """SELECT request_number FROM psychology_sessions
                   WHERE TRIM(COALESCE(request_number,'')) <> ''"""
            ).fetchall()
        }
        sessions_without_request_number = conn.execute(
            """SELECT id FROM psychology_sessions
               WHERE TRIM(COALESCE(request_number,'')) = '' ORDER BY created_at,id"""
        ).fetchall()
        for session_row in sessions_without_request_number:
            for _ in range(200):
                request_number = str(secrets.randbelow(90_000) + 10_000)
                if request_number not in used_request_numbers:
                    break
            else:
                raise RuntimeError("Не удалось сформировать номера существующих заявок")
            used_request_numbers.add(request_number)
            conn.execute(
                "UPDATE psychology_sessions SET request_number=? WHERE id=?",
                (request_number, session_row["id"]),
            )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_psychology_sessions_request_number
               ON psychology_sessions(request_number)
               WHERE request_number IS NOT NULL"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_psychology_session_credit_uses_order "
            "ON psychology_session_credit_uses(payment_order_id, credit_number)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_psychology_dialog_states_user "
            "ON psychology_dialog_states(chel_id, updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_enterprise_schedule_inn_date "
            "ON enterprise_examination_schedule(inn, status, examination_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_checkup_reoffer_candidates_due "
            "ON checkup_reoffer_candidates(duration_bucket, last_seen_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_checkup_reoffers_status_date "
            "ON checkup_reoffers(status, examination_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_notification_outbox_delivery "
            "ON user_notification_outbox(provider, status, next_attempt_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lab_result_value_estimates_status "
            "ON lab_result_value_estimates(status, updated_at)"
        )
        _backfill_known_ai_usage_pricing(conn)

        # Keep the canonical welcome text in chats created by earlier versions.
        # Only the first automatic psychologist message is touched; the user's
        # conversation and all later assistant replies remain unchanged.
        conn.execute(
            """UPDATE messages SET content=?
               WHERE id IN (
                   SELECT MIN(m.id)
                   FROM messages m
                   JOIN conversations c ON c.id=m.conversation_id
                   WHERE c.dialog_type='masterclass_psychology'
                     AND m.role='assistant' AND m.agent_id='psychologist'
                   GROUP BY m.conversation_id
               )""",
            (MASTERCLASS_PSYCHOLOGY_WELCOME,),
        )

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


def replace_enterprise_examination_schedule(
    rows: list[dict], fetched_sheet_ids: list[str],
) -> None:
    """Upsert one complete API snapshot and deactivate rows no longer returned."""
    now = utc_now()
    with _write_lock, connection() as conn:
        for item in rows:
            conn.execute(
                """INSERT INTO enterprise_examination_schedule
                (source_sheet_id, source_row_id, inn, organization_name,
                 examination_date, brigade, status, source_updated_at, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_sheet_id, source_row_id) DO UPDATE SET
                    inn=excluded.inn,
                    organization_name=excluded.organization_name,
                    examination_date=excluded.examination_date,
                    brigade=excluded.brigade,
                    status=excluded.status,
                    source_updated_at=excluded.source_updated_at,
                    synced_at=excluded.synced_at""",
                (
                    item["source_sheet_id"], item["source_row_id"], item["inn"],
                    item["organization_name"], item["examination_date"], item["brigade"],
                    item.get("status", "active"), item.get("source_updated_at", ""), now,
                ),
            )
        for sheet_id in {str(value) for value in fetched_sheet_ids if str(value)}:
            active_ids = {
                str(item["source_row_id"]) for item in rows
                if str(item["source_sheet_id"]) == sheet_id
            }
            if active_ids:
                placeholders = ",".join("?" for _ in active_ids)
                conn.execute(
                    f"""UPDATE enterprise_examination_schedule
                    SET status='inactive', synced_at=?
                    WHERE source_sheet_id=? AND source_row_id NOT IN ({placeholders})""",
                    (now, sheet_id, *sorted(active_ids)),
                )
            else:
                conn.execute(
                    """UPDATE enterprise_examination_schedule
                    SET status='inactive', synced_at=? WHERE source_sheet_id=?""",
                    (now, sheet_id),
                )
        conn.commit()


def find_upcoming_enterprise_examination(
    company_inn: str, reference_date: date | None = None,
) -> dict | None:
    """Return the nearest active examination, combining same-day brigades."""
    inn = re.sub(r"\D", "", str(company_inn or ""))
    if len(inn) not in {10, 12}:
        return None
    start = (reference_date or date.today()).isoformat()
    with connection() as conn:
        nearest = conn.execute(
            """SELECT examination_date FROM enterprise_examination_schedule
            WHERE inn=? AND status='active' AND examination_date>=?
            ORDER BY examination_date, source_sheet_id, source_row_id LIMIT 1""",
            (inn, start),
        ).fetchone()
        if not nearest:
            return None
        records = conn.execute(
            """SELECT organization_name, examination_date, brigade
            FROM enterprise_examination_schedule
            WHERE inn=? AND status='active' AND examination_date=?
            ORDER BY brigade, source_sheet_id, source_row_id""",
            (inn, nearest["examination_date"]),
        ).fetchall()
    brigades = list(dict.fromkeys(str(row["brigade"]) for row in records if row["brigade"]))
    organizations = list(dict.fromkeys(
        str(row["organization_name"]) for row in records if row["organization_name"]
    ))
    return {
        "inn": inn,
        "organization_name": organizations[0] if organizations else "",
        "examination_date": str(nearest["examination_date"]),
        "brigade": ", ".join(brigades),
    }


def admin_checkup_reoffer_settings() -> dict:
    with connection() as conn:
        row = conn.execute(
            "SELECT enabled,updated_at FROM checkup_reoffer_settings WHERE id=1"
        ).fetchone()
    return {
        "enabled": bool(row["enabled"]) if row else True,
        "updated_at": str(row["updated_at"] or "") if row else "",
    }


def admin_update_checkup_reoffer_settings(enabled: bool) -> dict:
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO checkup_reoffer_settings (id,enabled,updated_at)
            VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET
            enabled=excluded.enabled,updated_at=excluded.updated_at""",
            (1 if enabled else 0, now),
        )
        conn.commit()
    return {"enabled": bool(enabled), "updated_at": now}


def admin_checkup_reoffer_diagnostics(reference_date: date | None = None) -> dict:
    """Return operational counters without exposing user or medical data."""
    today = reference_date or date.today()
    tomorrow = (today + timedelta(days=1)).isoformat()
    day_after_tomorrow = (today + timedelta(days=2)).isoformat()
    with connection() as conn:
        schedule = conn.execute(
            """SELECT COUNT(*) rows_count,COUNT(DISTINCT inn) inns_count,
            MIN(examination_date) first_date,MAX(examination_date) last_date,
            MAX(synced_at) last_synced_at FROM enterprise_examination_schedule
            WHERE status='active'"""
        ).fetchone()
        tomorrow_schedule = conn.execute(
            """SELECT COUNT(*) rows_count,COUNT(DISTINCT inn) inns_count
            FROM enterprise_examination_schedule
            WHERE status='active' AND examination_date=?""",
            (tomorrow,),
        ).fetchone()
        candidates = conn.execute(
            """SELECT
              COUNT(DISTINCT CASE WHEN active_seconds<30 THEN chel_id END) under_30,
              COUNT(DISTINCT CASE WHEN active_seconds>=30 AND active_seconds<=120 THEN chel_id END) between_30_120,
              COUNT(DISTINCT CASE WHEN active_seconds>120 THEN chel_id END) over_120,
              COUNT(DISTINCT CASE WHEN active_seconds>=30 THEN chel_id END) eligible
            FROM checkup_reoffer_candidates
            WHERE experiment_variant NOT IN ('marketer','diagnostic')"""
        ).fetchone()
        due = conn.execute(
            """SELECT COUNT(DISTINCT c.chel_id) count
            FROM checkup_reoffer_candidates c
            WHERE c.active_seconds>=30
              AND c.experiment_variant NOT IN ('marketer','diagnostic')
              AND EXISTS (SELECT 1 FROM enterprise_examination_schedule s
                WHERE s.inn=c.company_inn AND s.status='active' AND s.examination_date=?)""",
            (tomorrow,),
        ).fetchone()

        def planned_count(examination_date: str) -> int:
            rows = conn.execute(
                """SELECT c.chel_id FROM checkup_reoffer_candidates c
                WHERE c.active_seconds>=30
                  AND c.experiment_variant NOT IN ('marketer','diagnostic')
                  AND EXISTS (SELECT 1 FROM enterprise_examination_schedule s
                    WHERE s.inn=c.company_inn AND s.status='active'
                      AND s.examination_date=?)
                GROUP BY c.chel_id""",
                (examination_date,),
            ).fetchall()
            planned = 0
            for candidate in rows:
                chel_id = str(candidate["chel_id"])
                if conn.execute(
                    "SELECT 1 FROM checkup_reoffers WHERE chel_id=? AND examination_date=?",
                    (chel_id, examination_date),
                ).fetchone():
                    continue
                onboarding = conn.execute(
                    "SELECT status,selected_tests,payment_status FROM onboarding_state WHERE chel_id=?",
                    (chel_id,),
                ).fetchone()
                try:
                    selected = json.loads(onboarding["selected_tests"] or "[]") if onboarding else []
                except json.JSONDecodeError:
                    selected = []
                if (
                    onboarding and onboarding["status"] == "complete" and selected
                    and onboarding["payment_status"] != "skipped"
                ):
                    continue
                if conn.execute(
                    """SELECT 1 FROM payment_orders WHERE chel_id=? AND paid=1
                    AND order_type='examinations' LIMIT 1""",
                    (chel_id,),
                ).fetchone():
                    continue
                planned += 1
            return planned

        pending_today = planned_count(tomorrow)
        scheduled_tomorrow = planned_count(day_after_tomorrow)
        sent = conn.execute(
            """SELECT COUNT(*) count,MAX(sent_at) last_sent_at FROM checkup_reoffers
            WHERE is_test=0"""
        ).fetchone()
        outbox_rows = conn.execute(
            """SELECT status,COUNT(*) count FROM user_notification_outbox
            GROUP BY status"""
        ).fetchall()
        outbox_errors = conn.execute(
            """SELECT COUNT(*) errors,
            SUM(CASE WHEN attempts>=100 AND status<>'sent' THEN 1 ELSE 0 END) exhausted
            FROM user_notification_outbox WHERE last_error<>''"""
        ).fetchone()
        linked = conn.execute(
            """SELECT provider,COUNT(DISTINCT chel_id) count FROM external_identities
            WHERE access_status='active' AND provider IN ('telegram','max') GROUP BY provider"""
        ).fetchall()
    last_synced_at = schedule["last_synced_at"]
    schedule_fresh = False
    if last_synced_at:
        try:
            synced_at = datetime.fromisoformat(str(last_synced_at))
            if synced_at.tzinfo is None:
                synced_at = synced_at.replace(tzinfo=timezone.utc)
            schedule_fresh = (
                datetime.now(timezone.utc) - synced_at.astimezone(timezone.utc)
            ) <= timedelta(hours=36)
        except ValueError:
            schedule_fresh = False
    return {
        "date": today.isoformat(),
        "tomorrow": tomorrow,
        "day_after_tomorrow": day_after_tomorrow,
        "schedule": {
            "rows": int(schedule["rows_count"] or 0),
            "inns": int(schedule["inns_count"] or 0),
            "first_date": schedule["first_date"],
            "last_date": schedule["last_date"],
            "last_synced_at": schedule["last_synced_at"],
            "fresh": schedule_fresh,
            "tomorrow_rows": int(tomorrow_schedule["rows_count"] or 0),
            "tomorrow_inns": int(tomorrow_schedule["inns_count"] or 0),
        },
        "candidates": {key: int(candidates[key] or 0) for key in (
            "under_30", "between_30_120", "over_120", "eligible"
        )},
        "due_tomorrow": int(due["count"] or 0),
        "pending_today": pending_today,
        "scheduled_tomorrow": scheduled_tomorrow,
        "sent_total": int(sent["count"] or 0),
        "last_sent_at": sent["last_sent_at"],
        "outbox": {
            **{str(row["status"]): int(row["count"]) for row in outbox_rows},
            "errors": int(outbox_errors["errors"] or 0),
            "exhausted": int(outbox_errors["exhausted"] or 0),
        },
        "linked_messengers": {
            str(row["provider"]): int(row["count"]) for row in linked
        },
    }


CHECKUP_REOFFER_MESSAGE = (
    "Завтра у вас медосмотр. Во время осмотра можно сдать дополнительные "
    "чек-апы из той же пробы крови — без нового укола и отдельного визита.\n\n"
    "После готовности результаты появятся в Консилиуме: вы получите понятную "
    "ИИ-расшифровку и сможете бесплатно передать результаты медицинскому специалисту.\n\n"
    "Возможно, вы захотите добавить чек-апы к завтрашнему осмотру."
)

CHECKUP_REOFFER_TEST_MESSAGE = (
    "Тестовое уведомление системы повторного предложения.\n\n"
    + CHECKUP_REOFFER_MESSAGE
)


def queue_test_checkup_reoffer(identifier: str, public_url: str) -> dict:
    """Send the real chat/messenger payload to one explicitly selected user."""
    target = str(identifier or "").strip()
    if not target:
        raise ValueError("Укажите chel_id или ИНН пользователя")
    normalized_inn = re.sub(r"\D", "", target)
    with _write_lock, connection() as conn:
        user = conn.execute("SELECT chel_id FROM users WHERE chel_id=?", (target,)).fetchone()
        if not user and len(normalized_inn) in {10, 12}:
            matches = conn.execute(
                """SELECT p.chel_id FROM user_profile p JOIN users u ON u.chel_id=p.chel_id
                WHERE p.company_inn=? ORDER BY u.last_seen_at DESC""",
                (normalized_inn,),
            ).fetchall()
            if len(matches) > 1:
                raise ValueError(
                    f"По ИНН найдено пользователей: {len(matches)}. Укажите точный chel_id"
                )
            user = matches[0] if matches else None
        if not user:
            raise ValueError("Пользователь не найден")
        chel_id = str(user["chel_id"])
        profile = conn.execute(
            "SELECT company_inn FROM user_profile WHERE chel_id=?", (chel_id,),
        ).fetchone()
        company_inn = re.sub(r"\D", "", str(profile["company_inn"] or "")) if profile else ""
        now = utc_now()
        diagnostic_key = f"diagnostic-{uuid.uuid4().hex}"
        candidate_cursor = conn.execute(
            """INSERT INTO checkup_reoffer_candidates
            (chel_id,journey_id,company_inn,active_seconds,duration_bucket,
             experiment_variant,first_seen_at,last_seen_at)
            VALUES (?,?,?,30,'30_to_120','diagnostic',?,?)""",
            (chel_id, diagnostic_key, company_inn, now, now),
        )
        token = secrets.token_urlsafe(32)
        reoffer_cursor = conn.execute(
            """INSERT INTO checkup_reoffers
            (chel_id,candidate_id,company_inn,examination_date,active_seconds,
             duration_bucket,access_token,status,created_at,is_test)
            VALUES (?,?,?, ?,30,'30_to_120',?,'queued',?,1)""",
            (chel_id, int(candidate_cursor.lastrowid), company_inn, diagnostic_key, token, now),
        )
        reoffer_id = int(reoffer_cursor.lastrowid)
        action_url = f"{str(public_url or '').rstrip('/')}/?checkup_reoffer={token}"
        conversation = conn.execute(
            "SELECT id FROM conversations WHERE chel_id=? AND status='active' ORDER BY updated_at DESC LIMIT 1",
            (chel_id,),
        ).fetchone()
        conversation_id = str(conversation["id"]) if conversation else str(uuid.uuid4())
        if not conversation:
            conn.execute(
                """INSERT INTO conversations (id,chel_id,title,created_at,updated_at)
                VALUES (?,?,?,?,?)""",
                (conversation_id, chel_id, "Дополнительные обследования", now, now),
            )
            conn.execute(
                "INSERT INTO conversation_reads (conversation_id,last_read_message_id,read_at) VALUES (?,0,?)",
                (conversation_id, now),
            )
        metadata = {
            "action": "checkup_reoffer", "reoffer_id": reoffer_id,
            "access_token": token, "action_label": "Проверить кнопку →",
            "action_url": action_url, "diagnostic": True,
        }
        message_cursor = conn.execute(
            """INSERT INTO messages
            (conversation_id,role,agent_id,content,metadata,created_at)
            VALUES (?,'assistant','manager',?,?,?)""",
            (
                conversation_id, CHECKUP_REOFFER_TEST_MESSAGE,
                json.dumps(metadata, ensure_ascii=False), now,
            ),
        )
        message_id = int(message_cursor.lastrowid)
        conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
        conn.execute(
            """UPDATE checkup_reoffers SET conversation_id=?,message_id=?,status='sent',sent_at=?
            WHERE id=?""",
            (conversation_id, message_id, now, reoffer_id),
        )
        identities = conn.execute(
            """SELECT provider,provider_user_id,chat_id FROM external_identities
            WHERE chel_id=? AND access_status='active' AND provider IN ('telegram','max')""",
            (chel_id,),
        ).fetchall()
        payload = json.dumps({
            "title": "Тест повторного предложения",
            "body": CHECKUP_REOFFER_TEST_MESSAGE,
            "kind": "checkup_reoffer", "diagnostic": True,
            "action_url": action_url, "action_label": "Проверить кнопку",
            "reoffer_id": reoffer_id,
        }, ensure_ascii=False)
        providers: list[str] = []
        for identity in identities:
            recipient = _external_identity_recipient(identity)
            if not recipient:
                continue
            provider = str(identity["provider"])
            conn.execute(
                """INSERT INTO user_notification_outbox
                (reoffer_id,chel_id,provider,recipient_id,event_type,payload,status,
                 attempts,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'pending',0,?,?)""",
                (
                    reoffer_id, chel_id, provider, recipient,
                    "checkup_reoffer_test", payload, now, now,
                ),
            )
            providers.append(provider)
        conn.commit()
    return {
        "status": "queued", "reoffer_id": reoffer_id, "chel_id": chel_id,
        "conversation_id": conversation_id, "chat_message_id": message_id,
        "messenger_providers": providers, "action_url": action_url,
    }


def save_checkup_reoffer_dwell(journey_id: str, active_seconds: int) -> dict:
    """Persist active, visible time on the ordinary check-up selection screen."""
    journey_id = str(journey_id or "").strip()[:100]
    if not journey_id:
        raise ValueError("Не указан идентификатор посещения")
    seconds = max(0, min(24 * 60 * 60, int(active_seconds or 0)))
    assignment = current_experiment_assignment()
    variant = str(assignment.get("variant") or "off")
    if variant == "marketer":
        return {"status": "excluded", "reason": "marketer"}
    profile = get_profile()
    company_inn = re.sub(r"\D", "", str(profile.get("company_inn") or ""))
    bucket = "over_120" if seconds > 120 else "30_to_120" if seconds >= 30 else "under_30"
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO checkup_reoffer_candidates
            (chel_id,journey_id,company_inn,active_seconds,duration_bucket,
             experiment_variant,first_seen_at,last_seen_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(chel_id,journey_id) DO UPDATE SET
              company_inn=excluded.company_inn,
              active_seconds=MAX(checkup_reoffer_candidates.active_seconds,excluded.active_seconds),
              duration_bucket=CASE
                WHEN MAX(checkup_reoffer_candidates.active_seconds,excluded.active_seconds)>120 THEN 'over_120'
                WHEN MAX(checkup_reoffer_candidates.active_seconds,excluded.active_seconds)>=30 THEN '30_to_120'
                ELSE 'under_30' END,
              experiment_variant=excluded.experiment_variant,
              last_seen_at=excluded.last_seen_at""",
            (
                current_chel_id(), journey_id, company_inn, seconds, bucket,
                variant, now, now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM checkup_reoffer_candidates WHERE chel_id=? AND journey_id=?",
            (current_chel_id(), journey_id),
        ).fetchone()
        conn.commit()
    return {"status": "ok", **dict(row)}


def queue_due_checkup_reoffers(examination_date: date, public_url: str) -> list[dict]:
    """Create chat messages and messenger jobs for eligible users exactly once."""
    exam_day = examination_date.isoformat()
    now = utc_now()
    base_url = str(public_url or "").rstrip("/")
    queued: list[dict] = []
    with _write_lock, connection() as conn:
        candidates = conn.execute(
            """SELECT c.* FROM checkup_reoffer_candidates c
            JOIN (
              SELECT chel_id,MAX(active_seconds) max_seconds,MAX(last_seen_at) last_seen
              FROM checkup_reoffer_candidates
              WHERE active_seconds>=30
                AND experiment_variant NOT IN ('marketer','diagnostic')
              GROUP BY chel_id
            ) best ON best.chel_id=c.chel_id AND best.max_seconds=c.active_seconds
            WHERE c.active_seconds>=30
              AND c.experiment_variant NOT IN ('marketer','diagnostic')
              AND EXISTS (
                SELECT 1 FROM enterprise_examination_schedule s
                WHERE s.inn=c.company_inn AND s.status='active' AND s.examination_date=?
              )
            GROUP BY c.chel_id ORDER BY c.last_seen_at DESC""",
            (exam_day,),
        ).fetchall()
        for candidate in candidates:
            chel_id = str(candidate["chel_id"])
            existing = conn.execute(
                "SELECT id FROM checkup_reoffers WHERE chel_id=? AND examination_date=?",
                (chel_id, exam_day),
            ).fetchone()
            if existing:
                continue
            onboarding = conn.execute(
                "SELECT status,selected_tests,payment_status FROM onboarding_state WHERE chel_id=?",
                (chel_id,),
            ).fetchone()
            try:
                selected = json.loads(onboarding["selected_tests"] or "[]") if onboarding else []
            except json.JSONDecodeError:
                selected = []
            reached_completion = bool(
                onboarding and onboarding["status"] == "complete" and selected
                and onboarding["payment_status"] != "skipped"
            )
            paid = bool(conn.execute(
                """SELECT 1 FROM payment_orders WHERE chel_id=? AND paid=1
                AND order_type='examinations' LIMIT 1""",
                (chel_id,),
            ).fetchone())
            if reached_completion or paid:
                continue
            token = secrets.token_urlsafe(32)
            cursor = conn.execute(
                """INSERT INTO checkup_reoffers
                (chel_id,candidate_id,company_inn,examination_date,active_seconds,
                 duration_bucket,access_token,status,created_at)
                VALUES (?,?,?,?,?,?,?,'queued',?)""",
                (
                    chel_id, candidate["id"], candidate["company_inn"], exam_day,
                    candidate["active_seconds"], candidate["duration_bucket"], token, now,
                ),
            )
            reoffer_id = int(cursor.lastrowid)
            action_url = f"{base_url}/?checkup_reoffer={token}"
            conversation = conn.execute(
                "SELECT id FROM conversations WHERE chel_id=? AND status='active' ORDER BY updated_at DESC LIMIT 1",
                (chel_id,),
            ).fetchone()
            conversation_id = str(conversation["id"]) if conversation else str(uuid.uuid4())
            if not conversation:
                conn.execute(
                    """INSERT INTO conversations
                    (id,chel_id,title,created_at,updated_at) VALUES (?,?,?,?,?)""",
                    (conversation_id, chel_id, "Дополнительные обследования", now, now),
                )
                conn.execute(
                    "INSERT INTO conversation_reads (conversation_id,last_read_message_id,read_at) VALUES (?,0,?)",
                    (conversation_id, now),
                )
            metadata = {
                "action": "checkup_reoffer", "reoffer_id": reoffer_id,
                "access_token": token, "action_label": "Выбрать чек-апы →",
                "action_url": action_url, "examination_date": exam_day,
            }
            message_cursor = conn.execute(
                """INSERT INTO messages
                (conversation_id,role,agent_id,content,metadata,created_at)
                VALUES (?,'assistant','manager',?,?,?)""",
                (conversation_id, CHECKUP_REOFFER_MESSAGE, json.dumps(metadata, ensure_ascii=False), now),
            )
            message_id = int(message_cursor.lastrowid)
            conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
            conn.execute(
                """UPDATE checkup_reoffers SET conversation_id=?,message_id=?,status='sent',
                sent_at=? WHERE id=?""",
                (conversation_id, message_id, now, reoffer_id),
            )
            identities = conn.execute(
                """SELECT provider,provider_user_id,chat_id FROM external_identities
                WHERE chel_id=? AND access_status='active' AND provider IN ('telegram','max')""",
                (chel_id,),
            ).fetchall()
            payload = json.dumps({
                "title": "Завтра медосмотр",
                "body": CHECKUP_REOFFER_MESSAGE,
                "kind": "checkup_reoffer",
                "action_url": action_url,
                "action_label": "Выбрать чек-апы",
                "reoffer_id": reoffer_id,
            }, ensure_ascii=False)
            messenger_count = 0
            for identity in identities:
                recipient = _external_identity_recipient(identity)
                if not recipient:
                    continue
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO user_notification_outbox
                    (reoffer_id,chel_id,provider,recipient_id,event_type,payload,status,
                     attempts,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,'pending',0,?,?)""",
                    (
                        reoffer_id, chel_id, identity["provider"], recipient,
                        "checkup_reoffer", payload, now, now,
                    ),
                )
                messenger_count += int(bool(cursor.rowcount))
            queued.append({
                "id": reoffer_id, "chel_id": chel_id,
                "active_seconds": int(candidate["active_seconds"]),
                "duration_bucket": str(candidate["duration_bucket"]),
                "examination_date": exam_day,
                "messenger_count": messenger_count,
            })
        conn.commit()
    return queued


def open_checkup_reoffer(access_token: str) -> dict:
    token = str(access_token or "").strip()
    with _write_lock, connection() as conn:
        row = conn.execute(
            "SELECT * FROM checkup_reoffers WHERE access_token=?",
            (token,),
        ).fetchone()
        if not row:
            raise ValueError("Предложение не найдено")
        now = utc_now()
        first_click = not bool(row["clicked_at"])
        conn.execute(
            """UPDATE checkup_reoffers SET opened_at=COALESCE(opened_at,?),
            clicked_at=COALESCE(clicked_at,?) WHERE id=?""",
            (now, now, row["id"]),
        )
        conn.commit()
    return {**dict(row), "first_click": first_click}


def create_conversation(
    title: str = "Новый диалог", *, dialog_type: str = "general", pinned: bool = False,
) -> dict:
    dialog_type = str(dialog_type or "general").strip().lower()
    if dialog_type not in {
        "general", "weight_control", "masterclass_psychology",
    }:
        raise ValueError("Неизвестный тип диалога")
    conversation_id = str(uuid.uuid4())
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO conversations
            (id, chel_id, title, dialog_type, pinned, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                conversation_id, current_chel_id(), title[:80], dialog_type,
                int(bool(pinned)), now, now,
            ),
        )
        conn.execute(
            "INSERT INTO conversation_reads (conversation_id, last_read_message_id, read_at) VALUES (?, 0, ?)",
            (conversation_id, now),
        )
        conn.commit()
    return get_conversation(conversation_id)


def _weight_assessment_from_profile(profile: dict) -> dict:
    """Prefill stable questionnaire facts without pretending they are current."""
    assessment = {
        "age": profile.get("age"),
        "sex": profile.get("sex") or None,
        "height_cm": profile.get("height_cm"),
        "current_weight_kg": profile.get("weight_kg"),
        "waist_cm": None,
        "weight_change": None,
        "weight_gain_started": None,
        "goal": None,
        "target_weight_kg": None,
        "eating_pattern": None,
        "hunger_pattern": None,
        "sleep": None,
        "stress": None,
        "activity": None if profile.get("activity") in {None, "", "unknown"} else profile.get("activity"),
        "conditions": list(profile.get("conditions") or []),
        "medications": list(profile.get("medications") or []),
        "previous_attempts": None,
        "red_flags": [],
        "readiness_score": None,
        "willingness": None,
        "diet_change_readiness": None,
        "barriers": None,
    }
    return assessment


def create_or_get_weight_control_conversation() -> dict:
    """Return the user's single pinned weight-control dialogue."""
    with connection() as conn:
        existing = conn.execute(
            """SELECT * FROM conversations
            WHERE chel_id=? AND dialog_type='weight_control' LIMIT 1""",
            (current_chel_id(),),
        ).fetchone()
    if existing:
        result = dict(existing)
        if result.get("title") == "Контроль веса":
            with _write_lock, connection() as conn:
                conn.execute(
                    "UPDATE conversations SET title=? WHERE id=?",
                    ("Контроль питания", result["id"]),
                )
                conn.commit()
            result["title"] = "Контроль питания"
        return result

    conversation = create_conversation(
        "Контроль питания", dialog_type="weight_control", pinned=True,
    )
    now = utc_now()
    assessment = _weight_assessment_from_profile(get_profile())
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO weight_control_states
            (conversation_id,chel_id,stage,assessment,analysis,risk_level,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                conversation["id"], current_chel_id(), "intake",
                json.dumps(assessment, ensure_ascii=False), "{}", "routine", now, now,
            ),
        )
        conn.commit()
    add_message(
        conversation["id"], "assistant",
        (
            "Здравствуйте! Я Ольга, ваша помощница. Это диалог "
            "**«Контроль питания»**. Здесь мы будем вести простой дневник еды: "
            "вы присылаете текстом или фотографией то, что съели, а я разбираю "
            "состав приёма пищи и предлагаю небольшие улучшения без жёстких диет.\n\n"
            "Сначала я задам несколько коротких вопросов о вашей цели, режиме питания "
            "и важных особенностях здоровья. Обычно это занимает несколько минут.\n\n"
            "Часть данных из вашей основной анкеты уже учтена — изменившиеся сведения "
            "я попрошу подтвердить. Этот диалог не заменяет очную диагностику и "
            "экстренную медицинскую помощь. **Готовы начать?**"
        ),
        "manager",
        {"weight_control_stage": "intro", "specialized_dialog": True},
    )
    return get_conversation(conversation["id"])


def create_or_get_masterclass_psychology_conversation() -> dict:
    """Return one pinned psychology chat for the user's master-class journey."""
    entry = current_masterclass_access()
    if not entry or not entry.get("profile_completed_at"):
        raise ValueError("Сначала завершите мини-анкету мастер-класса")
    with connection() as conn:
        existing = conn.execute(
            """SELECT * FROM conversations
            WHERE chel_id=? AND dialog_type='masterclass_psychology' LIMIT 1""",
            (current_chel_id(),),
        ).fetchone()
    if existing:
        return dict(existing)

    conversation = create_conversation(
        "Психологическая поддержка",
        dialog_type="masterclass_psychology", pinned=True,
    )
    context = {
        "current_topic": "стресс, усталость и эмоциональное состояние",
        "topic_relation": "new",
        "user_goal": "получить психологическую поддержку после мастер-класса",
        "known_facts": [
            f"Пользователь пришёл с мастер-класса «{entry['masterclass_name']}»",
            f"Предприятие: {entry['company']}",
        ],
        "open_questions": [],
    }
    with _write_lock, connection() as conn:
        conn.execute(
            """UPDATE conversations
            SET active_agent='psychologist',context_summary=?,updated_at=?
            WHERE id=? AND chel_id=?""",
            (
                json.dumps(context, ensure_ascii=False), utc_now(),
                conversation["id"], current_chel_id(),
            ),
        )
        conn.commit()
    add_message(
        conversation["id"], "assistant",
        MASTERCLASS_PSYCHOLOGY_WELCOME,
        "psychologist",
        {
            "specialized_dialog": "masterclass_psychology",
            "masterclass_code": entry["masterclass_code"],
            "company_inn": entry["inn"],
        },
    )
    return get_conversation(conversation["id"])


def get_weight_control_state(conversation_id: str) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            """SELECT s.* FROM weight_control_states s
            JOIN conversations c ON c.id=s.conversation_id
            WHERE s.conversation_id=? AND s.chel_id=? AND c.chel_id=?""",
            (conversation_id, current_chel_id(), current_chel_id()),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    for key in ("assessment", "analysis", "body_measurements"):
        try:
            result[key] = json.loads(result[key] or "{}")
        except json.JSONDecodeError:
            result[key] = {}
    if result.get("personality_mode") not in WEIGHT_CONTROL_PERSONALITY_MODES:
        result["personality_mode"] = "balanced"
    if result.get("address_mode") not in WEIGHT_CONTROL_ADDRESS_MODES:
        result["address_mode"] = "formal"
    result["use_emoji"] = bool(result.get("use_emoji"))
    return result


def weight_control_personality(conversation_id: str) -> dict:
    """Return the conversation-specific communication style for nutrition chat."""
    state = get_weight_control_state(conversation_id)
    if not state:
        raise ValueError("Диалог «Контроль питания» не найден")
    return {
        "conversation_id": conversation_id,
        "personality_mode": state["personality_mode"],
        "address_mode": state["address_mode"],
        "use_emoji": bool(state["use_emoji"]),
    }


def update_weight_control_personality(
    conversation_id: str, *, personality_mode: str,
    address_mode: str, use_emoji: bool,
) -> dict:
    """Validate and persist style controls scoped to one weight-control chat."""
    personality_mode = str(personality_mode or "").strip().lower()
    address_mode = str(address_mode or "").strip().lower()
    if personality_mode not in WEIGHT_CONTROL_PERSONALITY_MODES:
        raise ValueError("Выберите характер: дружелюбный, баланс или строгий")
    if address_mode not in WEIGHT_CONTROL_ADDRESS_MODES:
        raise ValueError("Выберите обращение на «ты» или на «вы»")
    if not isinstance(use_emoji, bool):
        raise ValueError("Некорректная настройка эмодзи")
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            """UPDATE weight_control_states
               SET personality_mode=?,address_mode=?,use_emoji=?,updated_at=?
               WHERE conversation_id=? AND chel_id=?""",
            (
                personality_mode, address_mode, int(use_emoji), utc_now(),
                conversation_id, current_chel_id(),
            ),
        )
        if not cursor.rowcount:
            raise ValueError("Диалог «Контроль питания» не найден")
        conn.commit()
    return weight_control_personality(conversation_id)


WEIGHT_BODY_MEASUREMENT_LIMITS = {
    "weight_kg": (25.0, 400.0, "Вес"),
    "waist_cm": (30.0, 300.0, "Обхват талии"),
    "hips_cm": (30.0, 300.0, "Обхват бёдер"),
    "chest_cm": (30.0, 300.0, "Обхват груди"),
    "thigh_cm": (15.0, 180.0, "Обхват бедра"),
    "arm_cm": (10.0, 100.0, "Обхват плеча"),
}


def weight_control_body_measurements(conversation_id: str) -> dict:
    """Return dated body measurements and changes for the nutrition dialogue."""
    state = get_weight_control_state(conversation_id)
    if not state:
        raise ValueError("Диалог «Контроль питания» не найден")
    profile = get_profile()
    with connection() as conn:
        rows = conn.execute(
            """SELECT * FROM weight_control_body_measurements
            WHERE conversation_id=? AND chel_id=?
            ORDER BY measured_on DESC,id DESC LIMIT 365""",
            (conversation_id, current_chel_id()),
        ).fetchall()
    history = []
    previous = None
    for row in reversed(rows):
        item = dict(row)
        measurement = {
            key: item.get(key) for key in WEIGHT_BODY_MEASUREMENT_LIMITS
        }
        changes = {}
        if previous:
            for key in WEIGHT_BODY_MEASUREMENT_LIMITS:
                current_value = measurement.get(key)
                previous_value = previous.get(key)
                changes[key] = (
                    round(float(current_value) - float(previous_value), 1)
                    if current_value is not None and previous_value is not None else None
                )
        history.append({
            "id": int(item["id"]),
            "measured_on": str(item["measured_on"]),
            **measurement,
            "notes": str(item.get("notes") or ""),
            "changes_from_previous": changes,
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        })
        previous = measurement
    history.reverse()
    latest = dict(history[0]) if history else {}
    if not latest:
        latest = {
            key: None for key in WEIGHT_BODY_MEASUREMENT_LIMITS
        }
        latest["weight_kg"] = (
            (state.get("assessment") or {}).get("current_weight_kg")
            or profile.get("weight_kg")
        )
        latest.update({
            "measured_on": None, "notes": "", "updated_at": None,
            "changes_from_previous": {},
        })
    assessment = dict(state.get("assessment") or {})
    starting_weight = assessment.get("current_weight_kg") or profile.get("weight_kg")
    target_weight = assessment.get("target_weight_kg")
    current_weight = next(
        (item.get("weight_kg") for item in history if item.get("weight_kg") is not None),
        latest.get("weight_kg"),
    )
    goal = None
    try:
        starting_number = float(starting_weight)
        target_number = float(target_weight)
        current_number = float(current_weight if current_weight is not None else starting_number)
        if 25 <= target_number < starting_number <= 400:
            distance = starting_number - target_number
            progress = max(0.0, min(100.0, (starting_number - current_number) / distance * 100))
            goal = {
                "starting_weight_kg": round(starting_number, 1),
                "target_weight_kg": round(target_number, 1),
                "current_weight_kg": round(current_number, 1),
                "remaining_kg": round(max(0.0, current_number - target_number), 1),
                "progress_percent": round(progress, 1),
                "achieved": current_number <= target_number,
            }
    except (TypeError, ValueError):
        goal = None
    return {
        "conversation_id": conversation_id,
        "height_cm": profile.get("height_cm"),
        "today": datetime.now(timezone.utc).astimezone(MOSCOW_TZ).date().isoformat(),
        "measurements": latest,
        "history": history,
        "goal": goal,
    }


def update_weight_control_body_measurements(
    conversation_id: str, payload: dict,
) -> dict:
    """Validate and upsert one dated measurement, then refresh the model context."""
    state = get_weight_control_state(conversation_id)
    if not state:
        raise ValueError("Диалог «Контроль питания» не найден")
    if not isinstance(payload, dict):
        raise ValueError("Параметры тела должны быть объектом")
    measured_on_raw = str(payload.get("measured_on") or "").strip()
    try:
        measured_on = datetime.strptime(measured_on_raw, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Укажите корректную дату замера") from None
    today = datetime.now(timezone.utc).astimezone(MOSCOW_TZ).date()
    if measured_on > today:
        raise ValueError("Нельзя сохранить замер на будущую дату")
    with connection() as conn:
        existing = conn.execute(
            """SELECT * FROM weight_control_body_measurements
            WHERE conversation_id=? AND chel_id=? AND measured_on=?""",
            (conversation_id, current_chel_id(), measured_on.isoformat()),
        ).fetchone()
    current = {
        key: existing[key] if existing else None
        for key in WEIGHT_BODY_MEASUREMENT_LIMITS
    }
    for key, (minimum, maximum, label) in WEIGHT_BODY_MEASUREMENT_LIMITS.items():
        if key not in payload:
            continue
        raw = payload.get(key)
        if raw is None or raw == "":
            current[key] = None
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{label}: укажите число") from None
        if not minimum <= value <= maximum:
            raise ValueError(
                f"{label}: допустимое значение от {minimum:g} до {maximum:g}"
            )
        current[key] = round(value, 1)
    current["notes"] = (
        " ".join(str(payload.get("notes") or "").split())[:500]
        if "notes" in payload else str(existing["notes"] or "") if existing else ""
    )
    if not any(current.get(key) is not None for key in WEIGHT_BODY_MEASUREMENT_LIMITS):
        raise ValueError("Укажите хотя бы один параметр замера")
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO weight_control_body_measurements
            (conversation_id,chel_id,measured_on,weight_kg,waist_cm,hips_cm,
             chest_cm,thigh_cm,arm_cm,notes,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(conversation_id,measured_on) DO UPDATE SET
              weight_kg=excluded.weight_kg,waist_cm=excluded.waist_cm,
              hips_cm=excluded.hips_cm,chest_cm=excluded.chest_cm,
              thigh_cm=excluded.thigh_cm,arm_cm=excluded.arm_cm,
              notes=excluded.notes,updated_at=excluded.updated_at""",
            (
                conversation_id, current_chel_id(), measured_on.isoformat(),
                current["weight_kg"], current["waist_cm"], current["hips_cm"],
                current["chest_cm"], current["thigh_cm"], current["arm_cm"],
                current["notes"], now, now,
            ),
        )
        latest = conn.execute(
            """SELECT * FROM weight_control_body_measurements
            WHERE conversation_id=? AND chel_id=?
            ORDER BY measured_on DESC,id DESC LIMIT 1""",
            (conversation_id, current_chel_id()),
        ).fetchone()
        latest_context = {
            key: latest[key] for key in WEIGHT_BODY_MEASUREMENT_LIMITS
        }
        latest_context.update({
            "measured_on": latest["measured_on"],
            "notes": latest["notes"],
            "updated_at": latest["updated_at"],
        })
        conn.execute(
            """UPDATE weight_control_states SET body_measurements=?,updated_at=?
            WHERE conversation_id=? AND chel_id=?""",
            (
                json.dumps(latest_context, ensure_ascii=False), now,
                conversation_id, current_chel_id(),
            ),
        )
        conn.execute(
            """UPDATE weight_control_reminders SET enabled=0,updated_at=?
            WHERE conversation_id=? AND chel_id=? AND reminder_type='body_prompt_once'""",
            (now, conversation_id, current_chel_id()),
        )
        conn.commit()
    result = weight_control_body_measurements(conversation_id)
    latest_for_model = dict(result["measurements"])
    latest_for_model["history"] = [
        {
            key: item.get(key)
            for key in (
                "measured_on", *WEIGHT_BODY_MEASUREMENT_LIMITS,
                "changes_from_previous", "notes",
            )
        }
        for item in result["history"][:12]
    ]
    with _write_lock, connection() as conn:
        conn.execute(
            """UPDATE weight_control_states SET body_measurements=?,updated_at=?
            WHERE conversation_id=? AND chel_id=?""",
            (
                json.dumps(latest_for_model, ensure_ascii=False), utc_now(),
                conversation_id, current_chel_id(),
            ),
        )
        conn.commit()
    result["saved_measurement"] = next(
        item for item in result["history"] if item["measured_on"] == measured_on.isoformat()
    )
    return result


def save_weight_control_state(
    conversation_id: str, *, stage: str, assessment: dict, analysis: dict,
    risk_level: str,
) -> dict:
    if stage not in {"intake", "analysis", "readiness"}:
        raise ValueError("Некорректный этап контроля веса")
    if risk_level not in {"routine", "soon", "urgent", "emergency"}:
        raise ValueError("Некорректный уровень внимания")
    now = utc_now()
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            """UPDATE weight_control_states SET stage=?,assessment=?,analysis=?,
            risk_level=?,updated_at=? WHERE conversation_id=? AND chel_id=?""",
            (
                stage, json.dumps(assessment, ensure_ascii=False),
                json.dumps(analysis, ensure_ascii=False), risk_level, now,
                conversation_id, current_chel_id(),
            ),
        )
        if not cursor.rowcount:
            raise ValueError("Анкета контроля веса не найдена")
        conn.commit()
    return get_weight_control_state(conversation_id)


def ensure_weight_control_program(conversation_id: str) -> dict:
    """Start the fourteen-day nutrition diary once and return its persisted dates."""
    state = get_weight_control_state(conversation_id)
    if not state:
        raise ValueError("Дневник питания не найден")
    if not state.get("program_started_at") or not state.get("program_ends_at"):
        started = datetime.now(timezone.utc)
        ends = started + timedelta(days=14)
        with _write_lock, connection() as conn:
            conn.execute(
                """UPDATE weight_control_states
                SET program_started_at=COALESCE(program_started_at,?),
                    program_ends_at=COALESCE(program_ends_at,?),updated_at=?
                WHERE conversation_id=? AND chel_id=?""",
                (
                    started.isoformat(), ends.isoformat(), utc_now(),
                    conversation_id, current_chel_id(),
                ),
            )
            conn.commit()
        state = get_weight_control_state(conversation_id) or state
    return {
        "started_at": state.get("program_started_at"),
        "ends_at": state.get("program_ends_at"),
        "onboarding_shown": bool(state.get("onboarding_shown_at")),
        "conclusion_ready": bool(state.get("conclusion_message_id")),
    }


def mark_weight_control_onboarding_shown(conversation_id: str) -> None:
    with _write_lock, connection() as conn:
        conn.execute(
            """UPDATE weight_control_states
            SET onboarding_shown_at=COALESCE(onboarding_shown_at,?),updated_at=?
            WHERE conversation_id=? AND chel_id=?""",
            (utc_now(), utc_now(), conversation_id, current_chel_id()),
        )
        conn.commit()


def weight_messenger_prompt_offer_due(conversation_id: str) -> bool:
    """Return whether the daily messenger-link offer should be shown now."""
    conversation = get_conversation(conversation_id)
    if not conversation or conversation.get("dialog_type") != "weight_control":
        raise ValueError("Диалог «Контроль питания» не найден")
    today = datetime.now(timezone.utc).astimezone(MOSCOW_TZ).date().isoformat()
    with connection() as conn:
        state = conn.execute(
            """SELECT stage,program_started_at,messenger_prompt_last_shown_on,
            messenger_prompt_dismissed_at FROM weight_control_states
            WHERE conversation_id=? AND chel_id=?""",
            (conversation_id, current_chel_id()),
        ).fetchone()
        linked = conn.execute(
            """SELECT 1 FROM external_identities
            WHERE chel_id=? AND access_status='active'
              AND provider IN ('telegram','max') LIMIT 1""",
            (current_chel_id(),),
        ).fetchone()
    return bool(
        state
        and state["stage"] in {"analysis", "readiness"}
        and state["program_started_at"]
        and not state["messenger_prompt_dismissed_at"]
        and str(state["messenger_prompt_last_shown_on"] or "") != today
        and not linked
    )


def mark_weight_messenger_prompt_shown(conversation_id: str) -> None:
    today = datetime.now(timezone.utc).astimezone(MOSCOW_TZ).date().isoformat()
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            """UPDATE weight_control_states
            SET messenger_prompt_last_shown_on=?,updated_at=?
            WHERE conversation_id=? AND chel_id=?
              AND messenger_prompt_dismissed_at IS NULL""",
            (today, utc_now(), conversation_id, current_chel_id()),
        )
        if not cursor.rowcount:
            raise ValueError("Диалог «Контроль питания» не найден")
        conn.commit()


def dismiss_weight_messenger_prompt(conversation_id: str) -> dict:
    conversation = get_conversation(conversation_id)
    if not conversation or conversation.get("dialog_type") != "weight_control":
        raise ValueError("Диалог «Контроль питания» не найден")
    now = utc_now()
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            """UPDATE weight_control_states
            SET messenger_prompt_dismissed_at=COALESCE(messenger_prompt_dismissed_at,?),
                updated_at=? WHERE conversation_id=? AND chel_id=?""",
            (now, now, conversation_id, current_chel_id()),
        )
        if not cursor.rowcount:
            raise ValueError("Диалог «Контроль питания» не найден")
        conn.commit()
    return {"status": "dismissed", "conversation_id": conversation_id}


def record_weight_control_meal(
    conversation_id: str, *, user_message_id: int, assistant_message_id: int,
    source_type: str, meal_event: str, description: str, analysis: dict,
) -> dict | None:
    """Persist a recognized or confirmed diary entry from one AI turn."""
    if meal_event not in {"draft", "confirmed"}:
        return None
    description = str(description or "").strip()[:4_000]
    if not description:
        return None
    now_dt = datetime.now(timezone.utc)
    entry_date = now_dt.astimezone(MOSCOW_TZ).date().isoformat()
    now = now_dt.isoformat()
    safe_analysis = {
        key: analysis.get(key) for key in (
            "summary", "criticality", "factors", "connections", "unknowns"
        )
    }
    with _write_lock, connection() as conn:
        pending = None
        if meal_event == "confirmed":
            pending = conn.execute(
                """SELECT id FROM weight_control_meals
                WHERE conversation_id=? AND chel_id=? AND status='pending'
                ORDER BY id DESC LIMIT 1""",
                (conversation_id, current_chel_id()),
            ).fetchone()
        if pending:
            meal_id = int(pending["id"])
            conn.execute(
                """UPDATE weight_control_meals
                SET status='confirmed',description=?,analysis=?,assistant_message_id=?,updated_at=?
                WHERE id=?""",
                (
                    description, json.dumps(safe_analysis, ensure_ascii=False),
                    int(assistant_message_id), now, meal_id,
                ),
            )
        else:
            cursor = conn.execute(
                """INSERT INTO weight_control_meals
                (conversation_id,chel_id,entry_date,source_type,status,description,
                 analysis,user_message_id,assistant_message_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    conversation_id, current_chel_id(), entry_date,
                    "photo" if source_type == "photo" else "text",
                    "pending" if meal_event == "draft" else "confirmed",
                    description, json.dumps(safe_analysis, ensure_ascii=False),
                    int(user_message_id), int(assistant_message_id), now, now,
                ),
            )
            meal_id = int(cursor.lastrowid)
        conn.commit()
    return {"id": meal_id, "status": "pending" if meal_event == "draft" else "confirmed"}


def _weight_diary_editable_date(conversation_id: str, entry_date: str) -> tuple[dict, str]:
    """Validate that a diary date belongs to the program and is not in the future."""
    conversation = get_conversation(conversation_id)
    if not conversation or conversation.get("dialog_type") != "weight_control":
        raise ValueError("Дневник питания не найден")
    state = get_weight_control_state(conversation_id)
    if not state or not state.get("program_started_at"):
        raise ValueError("Программа контроля питания ещё не началась")
    try:
        requested = datetime.strptime(str(entry_date or ""), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError("Укажите корректную дату") from exc
    started = datetime.fromisoformat(str(state["program_started_at"])).astimezone(MOSCOW_TZ).date()
    last_day = started + timedelta(days=13)
    today = datetime.now(timezone.utc).astimezone(MOSCOW_TZ).date()
    if requested > today:
        raise ValueError("Будущие дни нельзя редактировать")
    if requested < started or requested > last_day:
        raise ValueError("Дата находится вне 14-дневной программы")
    return state, requested.isoformat()


def create_weight_control_diary_meal(
    conversation_id: str, *, entry_date: str, description: str,
) -> dict:
    """Add a manual food entry to today or an earlier program day."""
    _, normalized_date = _weight_diary_editable_date(conversation_id, entry_date)
    description = str(description or "").strip()[:4_000]
    if not description:
        raise ValueError("Опишите, что вы ели")
    now = utc_now()
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            """INSERT INTO weight_control_meals
            (conversation_id,chel_id,entry_date,source_type,status,description,
             analysis,user_message_id,assistant_message_id,created_at,updated_at)
            VALUES (?, ?, ?, 'manual', 'confirmed', ?, '{}', NULL, NULL, ?, ?)""",
            (
                conversation_id, current_chel_id(), normalized_date,
                description, now, now,
            ),
        )
        meal_id = int(cursor.lastrowid)
        conn.commit()
    return {"id": meal_id, "entry_date": normalized_date, "description": description}


def update_weight_control_diary_meal(meal_id: int, *, description: str) -> dict:
    """Edit an owned food entry if its program day is not in the future."""
    description = str(description or "").strip()[:4_000]
    if not description:
        raise ValueError("Опишите, что вы ели")
    with connection() as conn:
        row = conn.execute(
            """SELECT * FROM weight_control_meals
            WHERE id=? AND chel_id=?""",
            (int(meal_id), current_chel_id()),
        ).fetchone()
    if not row:
        raise ValueError("Запись дневника не найдена")
    _weight_diary_editable_date(str(row["conversation_id"]), str(row["entry_date"]))
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """UPDATE weight_control_meals
            SET description=?,status='confirmed',updated_at=?
            WHERE id=? AND chel_id=?""",
            (description, now, int(meal_id), current_chel_id()),
        )
        conn.commit()
    return {
        "id": int(meal_id), "entry_date": str(row["entry_date"]),
        "description": description,
    }


def delete_weight_control_diary_meal(meal_id: int) -> bool:
    """Delete an owned food entry if its program day is not in the future."""
    with connection() as conn:
        row = conn.execute(
            """SELECT conversation_id,entry_date FROM weight_control_meals
            WHERE id=? AND chel_id=?""",
            (int(meal_id), current_chel_id()),
        ).fetchone()
    if not row:
        raise ValueError("Запись дневника не найдена")
    _weight_diary_editable_date(str(row["conversation_id"]), str(row["entry_date"]))
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            "DELETE FROM weight_control_meals WHERE id=? AND chel_id=?",
            (int(meal_id), current_chel_id()),
        )
        conn.commit()
    return bool(cursor.rowcount)


def weight_control_diary(conversation_id: str, *, require_owner: bool = True) -> dict:
    if require_owner:
        conversation = get_conversation(conversation_id)
        if not conversation or conversation.get("dialog_type") != "weight_control":
            raise ValueError("Дневник питания не найден")
    with connection() as conn:
        state = conn.execute(
            "SELECT * FROM weight_control_states WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        meals = conn.execute(
            """SELECT * FROM weight_control_meals WHERE conversation_id=?
            ORDER BY entry_date,created_at,id""",
            (conversation_id,),
        ).fetchall()
    if not state:
        raise ValueError("Дневник питания не найден")
    state = dict(state)
    started_raw = state.get("program_started_at")
    if not started_raw:
        return {
            "conversation_id": conversation_id,
            "program_started": False,
            "started_at": None,
            "ends_at": None,
            "elapsed_days": 0,
            "remaining_days": 14,
            "completed": False,
            "conclusion_ready": False,
            "analysis_available": False,
            "analysis_minimum_meals": WEIGHT_ANALYSIS_MIN_CONFIRMED_MEALS,
            "analysis_missing_meals": WEIGHT_ANALYSIS_MIN_CONFIRMED_MEALS,
            "conclusion_status": "idle",
            "conclusion_error": "",
            "days": [],
            "meal_count": 0,
        }
    ends_raw = state.get("program_ends_at") or (
        datetime.fromisoformat(started_raw) + timedelta(days=14)
    ).isoformat()
    started = datetime.fromisoformat(started_raw).astimezone(MOSCOW_TZ)
    ends = datetime.fromisoformat(ends_raw).astimezone(MOSCOW_TZ)
    today = datetime.now(timezone.utc).astimezone(MOSCOW_TZ).date()
    by_date: dict[str, list[dict]] = {}
    for row in meals:
        item = dict(row)
        try:
            item["analysis"] = json.loads(item.get("analysis") or "{}")
        except json.JSONDecodeError:
            item["analysis"] = {}
        by_date.setdefault(str(item["entry_date"]), []).append(item)
    days = []
    for offset in range(14):
        day_date = started.date() + timedelta(days=offset)
        days.append({
            "day": offset + 1,
            "date": day_date.isoformat(),
            "status": "today" if day_date == today else ("past" if day_date < today else "future"),
            "meals": by_date.get(day_date.isoformat(), []),
        })
    elapsed_days = max(1, min(14, (today - started.date()).days + 1))
    confirmed_count = len([row for row in meals if row["status"] == "confirmed"])
    completed = datetime.now(timezone.utc) >= datetime.fromisoformat(ends_raw)
    conclusion_ready = bool(state.get("conclusion_message_id"))
    conclusion_status = "ready" if conclusion_ready else str(
        state.get("conclusion_status") or "idle"
    )
    return {
        "conversation_id": conversation_id,
        "program_started": True,
        "started_at": started_raw,
        "ends_at": ends_raw,
        "elapsed_days": elapsed_days,
        "remaining_days": max(0, 14 - elapsed_days),
        "completed": completed,
        "conclusion_ready": conclusion_ready,
        "analysis_available": bool(
            completed and confirmed_count >= WEIGHT_ANALYSIS_MIN_CONFIRMED_MEALS
        ),
        "analysis_minimum_meals": WEIGHT_ANALYSIS_MIN_CONFIRMED_MEALS,
        "analysis_missing_meals": max(
            0, WEIGHT_ANALYSIS_MIN_CONFIRMED_MEALS - confirmed_count,
        ),
        "conclusion_status": conclusion_status,
        "conclusion_error": str(state.get("conclusion_error") or ""),
        "days": days,
        "meal_count": confirmed_count,
    }


def queue_due_weight_control_completions(public_url: str, limit: int = 50) -> int:
    """Queue one completion notice when a used fourteen-day diary ends."""
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    local_time = now_dt.astimezone(MOSCOW_TZ).strftime("%H:%M")
    queued: list[str] = []
    with _write_lock, connection() as conn:
        rows = conn.execute(
            """SELECT s.conversation_id,s.chel_id FROM weight_control_states s
            WHERE s.program_ends_at IS NOT NULL AND s.program_ends_at<=?
              AND s.completion_notified_at IS NULL
              AND (
                SELECT COUNT(*) FROM weight_control_meals m
                WHERE m.conversation_id=s.conversation_id AND m.status='confirmed'
              ) >= ?
            ORDER BY s.program_ends_at LIMIT ?""",
            (
                now, WEIGHT_ANALYSIS_MIN_CONFIRMED_MEALS,
                max(1, min(100, int(limit))),
            ),
        ).fetchall()
        for row in rows:
            cursor = conn.execute(
                """UPDATE weight_control_states SET completion_notified_at=?,updated_at=?
                WHERE conversation_id=? AND completion_notified_at IS NULL""",
                (now, now, row["conversation_id"]),
            )
            if not cursor.rowcount:
                continue
            conn.execute(
                """INSERT INTO weight_control_reminders
                (conversation_id,chel_id,reminder_type,title,message,time_local,
                 weekdays,timezone,interval_days,one_off,enabled,next_run_at,
                 created_at,updated_at)
                VALUES (?,?,'program_complete',?,?,?,?,?,0,1,1,?,?,?)""",
                (
                    row["conversation_id"], row["chel_id"],
                    "14-дневный марафон завершён",
                    "Поздравляем! Получите персональный анализ питания и рекомендации по вашему дневнику.",
                    local_time, "[]", "Europe/Moscow", now, now, now,
                ),
            )
            queued.append(str(row["conversation_id"]))
        conn.commit()
    for conversation_id in queued:
        enqueue_manager_notifications(
            "weight_program_completed", conversation_id, recipient_role="manager",
        )
    return len(queued)


def begin_weight_control_conclusion(conversation_id: str) -> dict:
    """Reserve user-triggered conclusion generation and prevent duplicate requests."""
    diary = weight_control_diary(conversation_id)
    if not diary.get("completed"):
        raise ValueError("Анализ питания будет доступен после завершения 14 дней")
    if int(diary.get("meal_count") or 0) < WEIGHT_ANALYSIS_MIN_CONFIRMED_MEALS:
        raise ValueError(
            "Для анализа нужно не менее "
            f"{WEIGHT_ANALYSIS_MIN_CONFIRMED_MEALS} подтверждённых записей о питании"
        )
    now = utc_now()
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT conclusion_status,conclusion_message_id,updated_at
            FROM weight_control_states WHERE conversation_id=? AND chel_id=?""",
            (conversation_id, current_chel_id()),
        ).fetchone()
        if not row:
            raise ValueError("Дневник питания не найден")
        if row["conclusion_message_id"]:
            return {"status": "ready", "message_id": int(row["conclusion_message_id"])}
        if row["conclusion_status"] == "generating":
            try:
                still_running = datetime.fromisoformat(str(row["updated_at"])) > (
                    datetime.now(timezone.utc) - timedelta(minutes=15)
                )
            except (TypeError, ValueError):
                still_running = False
            if still_running:
                return {"status": "generating", "message_id": None}
        conn.execute(
            """UPDATE weight_control_states
            SET conclusion_status='generating',conclusion_error='',updated_at=?
            WHERE conversation_id=? AND chel_id=?""",
            (now, conversation_id, current_chel_id()),
        )
        conn.commit()
    return {"status": "started", "message_id": None}


def fail_weight_control_conclusion(conversation_id: str, error: str) -> None:
    with _write_lock, connection() as conn:
        conn.execute(
            """UPDATE weight_control_states
            SET conclusion_status='failed',conclusion_error=?,updated_at=?
            WHERE conversation_id=? AND chel_id=? AND conclusion_message_id IS NULL""",
            (str(error or "")[:500], utc_now(), conversation_id, current_chel_id()),
        )
        conn.commit()


def complete_weight_control_conclusion(conversation_id: str, content: str) -> dict | None:
    content = str(content or "").strip()
    if not content:
        return None
    now = utc_now()
    with _write_lock, connection() as conn:
        state = conn.execute(
            """SELECT conclusion_message_id FROM weight_control_states
            WHERE conversation_id=? AND chel_id=?""",
            (conversation_id, current_chel_id()),
        ).fetchone()
        if not state or state["conclusion_message_id"]:
            return None
        cursor = conn.execute(
            """INSERT INTO messages
            (conversation_id,role,agent_id,content,metadata,created_at)
            VALUES (?,'assistant','manager',?,?,?)""",
            (
                conversation_id, content,
                json.dumps({"action": "weight_program_conclusion"}, ensure_ascii=False),
                now,
            ),
        )
        message_id = int(cursor.lastrowid)
        conn.execute(
            """UPDATE weight_control_states SET conclusion_message_id=?,
            conclusion_status='ready',conclusion_error='',updated_at=?
            WHERE conversation_id=?""",
            (message_id, now, conversation_id),
        )
        conn.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?",
            (now, conversation_id),
        )
        conn.commit()
    return {"message_id": message_id, "conversation_id": conversation_id}


WEIGHT_REMINDER_NOTIFICATION_ID_OFFSET = 2_000_000_000
WEIGHT_REMINDER_TYPES = {
    "medication", "sport", "warmup", "meal", "custom",
    "body_measurement", "body_prompt_once",
}


def _weight_reminder_next_run(
    time_local: str, weekdays: list[int], *, after: datetime | None = None,
    interval_days: int = 0,
) -> str:
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(time_local or ""))
    if not match:
        raise ValueError("Укажите время в формате ЧЧ:ММ")
    interval_days = max(0, min(365, int(interval_days or 0)))
    tz = MOSCOW_TZ
    cursor = (after or datetime.now(timezone.utc)).astimezone(tz)
    hour, minute = int(match.group(1)), int(match.group(2))
    if interval_days:
        candidate_day = cursor.date() + timedelta(days=interval_days)
        candidate = datetime.combine(candidate_day, datetime.min.time(), tzinfo=tz).replace(
            hour=hour, minute=minute,
        )
        return candidate.astimezone(timezone.utc).isoformat()
    normalized_days = sorted({int(day) for day in weekdays if 1 <= int(day) <= 7})
    if not normalized_days:
        raise ValueError("Выберите хотя бы один день недели")
    for offset in range(8):
        candidate_day = cursor.date() + timedelta(days=offset)
        if candidate_day.isoweekday() not in normalized_days:
            continue
        candidate = datetime.combine(candidate_day, datetime.min.time(), tzinfo=tz).replace(
            hour=hour, minute=minute,
        )
        if candidate > cursor:
            return candidate.astimezone(timezone.utc).isoformat()
    raise ValueError("Не удалось рассчитать следующее напоминание")


def list_weight_control_reminders(conversation_id: str) -> dict:
    conversation = get_conversation(conversation_id)
    if not conversation or conversation.get("dialog_type") != "weight_control":
        raise ValueError("Диалог «Контроль питания» не найден")
    with connection() as conn:
        rows = conn.execute(
            """SELECT * FROM weight_control_reminders
            WHERE conversation_id=? AND chel_id=?
              AND reminder_type<>'program_complete'
              AND (one_off=0 OR enabled=1)
            ORDER BY enabled DESC,time_local,id""",
            (conversation_id, current_chel_id()),
        ).fetchall()
        linked = conn.execute(
            """SELECT DISTINCT provider FROM external_identities
            WHERE chel_id=? AND access_status='active' AND provider IN ('telegram','max')""",
            (current_chel_id(),),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        try:
            item["weekdays"] = json.loads(item["weekdays"] or "[]")
        except json.JSONDecodeError:
            item["weekdays"] = []
        items.append(item)
    providers = [str(row["provider"]) for row in linked]
    return {"reminders": items, "messenger_linked": bool(providers), "providers": providers}


def create_weight_control_reminder(
    conversation_id: str, *, reminder_type: str, title: str, message: str,
    time_local: str, weekdays: list[int], interval_days: int = 0,
) -> dict:
    conversation = get_conversation(conversation_id)
    if not conversation or conversation.get("dialog_type") != "weight_control":
        raise ValueError("Напоминания доступны в диалоге «Контроль питания»")
    reminder_type = str(reminder_type or "custom").strip().lower()
    if reminder_type not in WEIGHT_REMINDER_TYPES:
        reminder_type = "custom"
    title = str(title or "").strip()[:80]
    message = str(message or "").strip()[:500]
    if not title or not message:
        raise ValueError("Заполните название и текст напоминания")
    days = sorted({int(day) for day in (weekdays or []) if 1 <= int(day) <= 7})
    interval_days = max(0, min(365, int(interval_days or 0)))
    next_run_at = _weight_reminder_next_run(
        time_local, days, interval_days=interval_days,
    )
    now = utc_now()
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            """INSERT INTO weight_control_reminders
            (conversation_id,chel_id,reminder_type,title,message,time_local,weekdays,
             timezone,interval_days,one_off,enabled,next_run_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,'Europe/Moscow',?,0,1,?,?,?)""",
            (
                conversation_id, current_chel_id(), reminder_type, title, message,
                time_local, json.dumps(days), interval_days, next_run_at, now, now,
            ),
        )
        reminder_id = int(cursor.lastrowid)
        conn.commit()
    return next(
        item for item in list_weight_control_reminders(conversation_id)["reminders"]
        if int(item["id"]) == reminder_id
    )


def ensure_default_weight_meal_reminders(conversation_id: str) -> list[dict]:
    """Create the standard food and body-measurement schedule after intake."""
    conversation = get_conversation(conversation_id)
    if not conversation or conversation.get("dialog_type") != "weight_control":
        raise ValueError("Диалог «Контроль питания» не найден")
    defaults = (
        (
            "Завтрак", "09:00",
            "Время отметить завтрак. Пришлите в чат описание или фото — разберём состав и баланс.",
        ),
        (
            "Обед", "13:00",
            "Время отметить обед. Пришлите в чат описание или фото того, что съели.",
        ),
        (
            "Ужин", "19:00",
            "Время отметить ужин. Пришлите описание или фото — подскажу, что можно улучшить.",
        ),
    )
    now = utc_now()
    days = [1, 2, 3, 4, 5, 6, 7]
    with _write_lock, connection() as conn:
        for title, time_local, message in defaults:
            exists = conn.execute(
                """SELECT 1 FROM weight_control_reminders
                WHERE conversation_id=? AND chel_id=? AND reminder_type='meal' AND title=?""",
                (conversation_id, current_chel_id(), title),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """INSERT INTO weight_control_reminders
                (conversation_id,chel_id,reminder_type,title,message,time_local,weekdays,
                 timezone,interval_days,one_off,enabled,next_run_at,created_at,updated_at)
                VALUES (?,?,'meal',?,?,?,?, 'Europe/Moscow',0,0,1,?,?,?)""",
                (
                    conversation_id, current_chel_id(), title, message, time_local,
                    json.dumps(days), _weight_reminder_next_run(time_local, days), now, now,
                ),
            )
        body_exists = conn.execute(
            """SELECT 1 FROM weight_control_reminders
            WHERE conversation_id=? AND chel_id=? AND reminder_type='body_measurement'
            LIMIT 1""",
            (conversation_id, current_chel_id()),
        ).fetchone()
        if not body_exists:
            conn.execute(
                """INSERT INTO weight_control_reminders
                (conversation_id,chel_id,reminder_type,title,message,time_local,weekdays,
                 timezone,interval_days,one_off,enabled,next_run_at,created_at,updated_at)
                VALUES (?,?,'body_measurement','Обновить параметры тела',
                'Пора обновить вес и объёмы, чтобы видеть динамику и учитывать её в рекомендациях.',
                '10:00','[]','Europe/Moscow',5,0,1,?,?,?)""",
                (
                    conversation_id, current_chel_id(),
                    _weight_reminder_next_run("10:00", [], interval_days=5), now, now,
                ),
            )
        conn.commit()
    return list_weight_control_reminders(conversation_id)["reminders"]


def snooze_weight_body_prompt(conversation_id: str) -> dict:
    """Create or move the one-time body-measurement prompt to the next day."""
    conversation = get_conversation(conversation_id)
    if not conversation or conversation.get("dialog_type") != "weight_control":
        raise ValueError("Диалог «Контроль питания» не найден")
    with connection() as conn:
        completed = conn.execute(
            """SELECT 1 FROM weight_control_body_measurements
            WHERE conversation_id=? AND chel_id=? LIMIT 1""",
            (conversation_id, current_chel_id()),
        ).fetchone()
    if completed:
        return {"status": "already_completed", "conversation_id": conversation_id}
    now_dt = datetime.now(timezone.utc)
    local_now = now_dt.astimezone(MOSCOW_TZ)
    due_at = now_dt + timedelta(days=1)
    time_local = due_at.astimezone(MOSCOW_TZ).strftime("%H:%M")
    weekdays = [due_at.astimezone(MOSCOW_TZ).isoweekday()]
    now = now_dt.isoformat()
    with _write_lock, connection() as conn:
        existing = conn.execute(
            """SELECT id FROM weight_control_reminders
            WHERE conversation_id=? AND chel_id=? AND reminder_type='body_prompt_once'
            ORDER BY id DESC LIMIT 1""",
            (conversation_id, current_chel_id()),
        ).fetchone()
        if existing:
            reminder_id = int(existing["id"])
            conn.execute(
                """UPDATE weight_control_reminders
                SET title='Указать параметры тела',
                    message='Добавьте текущий вес и объёмы — так будет видна динамика изменений.',
                    time_local=?,weekdays=?,interval_days=0,one_off=1,enabled=1,
                    next_run_at=?,last_sent_at=NULL,updated_at=? WHERE id=?""",
                (
                    time_local, json.dumps(weekdays), due_at.isoformat(), now,
                    reminder_id,
                ),
            )
        else:
            cursor = conn.execute(
                """INSERT INTO weight_control_reminders
                (conversation_id,chel_id,reminder_type,title,message,time_local,weekdays,
                 timezone,interval_days,one_off,enabled,next_run_at,created_at,updated_at)
                VALUES (?,?,'body_prompt_once','Указать параметры тела',
                'Добавьте текущий вес и объёмы — так будет видна динамика изменений.',
                ?,?,'Europe/Moscow',0,1,1,?,?,?)""",
                (
                    conversation_id, current_chel_id(), time_local,
                    json.dumps(weekdays), due_at.isoformat(), now, now,
                ),
            )
            reminder_id = int(cursor.lastrowid)
        conn.commit()
    return {
        "status": "scheduled", "reminder_id": reminder_id,
        "scheduled_for": due_at.isoformat(),
        "local_date": (local_now.date() + timedelta(days=1)).isoformat(),
    }


def update_weight_control_reminder_status(reminder_id: int, enabled: bool) -> dict:
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT r.* FROM weight_control_reminders r
            JOIN conversations c ON c.id=r.conversation_id
            WHERE r.id=? AND r.chel_id=? AND c.chel_id=?""",
            (int(reminder_id), current_chel_id(), current_chel_id()),
        ).fetchone()
        if not row:
            raise ValueError("Напоминание не найдено")
        next_run = _weight_reminder_next_run(
            str(row["time_local"]), json.loads(row["weekdays"] or "[]"),
            interval_days=int(row["interval_days"] or 0),
        ) if enabled else str(row["next_run_at"])
        conn.execute(
            "UPDATE weight_control_reminders SET enabled=?,next_run_at=?,updated_at=? WHERE id=?",
            (int(enabled), next_run, utc_now(), int(reminder_id)),
        )
        conn.commit()
    return {"id": int(reminder_id), "enabled": bool(enabled)}


def update_weight_control_reminder(
    reminder_id: int, *, title: str, message: str, time_local: str,
    weekdays: list[int], reminder_type: str | None = None,
    interval_days: int | None = None,
) -> dict:
    """Update a reminder owned by the current user and recalculate its next run."""
    title = str(title or "").strip()[:80]
    message = str(message or "").strip()[:500]
    if not title or not message:
        raise ValueError("Заполните название и текст напоминания")
    days = sorted({int(day) for day in (weekdays or []) if 1 <= int(day) <= 7})
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT r.* FROM weight_control_reminders r
            JOIN conversations c ON c.id=r.conversation_id
            WHERE r.id=? AND r.chel_id=? AND c.chel_id=?""",
            (int(reminder_id), current_chel_id(), current_chel_id()),
        ).fetchone()
        if not row:
            raise ValueError("Напоминание не найдено")
        normalized_interval = (
            int(row["interval_days"] or 0) if interval_days is None
            else max(0, min(365, int(interval_days or 0)))
        )
        next_run_at = _weight_reminder_next_run(
            time_local, days, interval_days=normalized_interval,
        )
        normalized_type = str(reminder_type or row["reminder_type"] or "custom").strip().lower()
        if normalized_type not in WEIGHT_REMINDER_TYPES:
            normalized_type = "custom"
        conn.execute(
            """UPDATE weight_control_reminders
            SET reminder_type=?,title=?,message=?,time_local=?,weekdays=?,interval_days=?,next_run_at=?,updated_at=?
            WHERE id=?""",
            (
                normalized_type, title, message, time_local, json.dumps(days),
                normalized_interval, next_run_at, utc_now(), int(reminder_id),
            ),
        )
        conversation_id = str(row["conversation_id"])
        conn.commit()
    return next(
        item for item in list_weight_control_reminders(conversation_id)["reminders"]
        if int(item["id"]) == int(reminder_id)
    )


def delete_weight_control_reminder(reminder_id: int) -> bool:
    with _write_lock, connection() as conn:
        cursor = conn.execute(
            "DELETE FROM weight_control_reminders WHERE id=? AND chel_id=?",
            (int(reminder_id), current_chel_id()),
        )
        conn.commit()
    return bool(cursor.rowcount)


def dispatch_due_weight_control_reminders(public_url: str) -> int:
    """Write due reminders to chat and enqueue each linked messenger exactly once."""
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    today_moscow = now_dt.astimezone(MOSCOW_TZ).date().isoformat()
    delivered = 0
    with _write_lock, connection() as conn:
        rows = conn.execute(
            """SELECT * FROM weight_control_reminders
            WHERE enabled=1 AND next_run_at<=? ORDER BY next_run_at LIMIT 100""",
            (now,),
        ).fetchall()
        for row in rows:
            scheduled_for = str(row["next_run_at"])
            cursor = conn.execute(
                """INSERT OR IGNORE INTO weight_control_reminder_runs
                (reminder_id,scheduled_for,created_at) VALUES (?,?,?)""",
                (row["id"], scheduled_for, now),
            )
            if cursor.rowcount:
                run_id = int(cursor.lastrowid)
                program_complete = row["reminder_type"] == "program_complete"
                content = (
                    f"🎉 **{row['title']}**\n\n{row['message']}"
                    if program_complete else f"⏰ **{row['title']}**\n\n{row['message']}"
                )
                identities = conn.execute(
                    """SELECT provider,provider_user_id,chat_id FROM external_identities
                    WHERE chel_id=? AND access_status='active' AND provider IN ('telegram','max')""",
                    (row["chel_id"],),
                ).fetchall()
                prompt_state = conn.execute(
                    """SELECT stage,program_started_at,messenger_prompt_last_shown_on,
                    messenger_prompt_dismissed_at FROM weight_control_states
                    WHERE conversation_id=? AND chel_id=?""",
                    (row["conversation_id"], row["chel_id"]),
                ).fetchone()
                daily_offer = bool(
                    not identities
                    and prompt_state
                    and prompt_state["stage"] in {"analysis", "readiness"}
                    and prompt_state["program_started_at"]
                    and not prompt_state["messenger_prompt_dismissed_at"]
                    and str(prompt_state["messenger_prompt_last_shown_on"] or "") != today_moscow
                )
                message_metadata = {
                    "action": (
                        "weight_program_completed" if program_complete else "weight_reminder"
                    ),
                    "reminder_id": int(row["id"]),
                    "reminder_type": row["reminder_type"],
                }
                if program_complete:
                    message_metadata.update({
                        "weight_diary_open": True,
                        "action_label": "Открыть дневник и получить анализ",
                    })
                if row["reminder_type"] in {"body_measurement", "body_prompt_once"}:
                    message_metadata.update({
                        "weight_body_prompt": True,
                        "weight_body_remind_later": row["reminder_type"] == "body_prompt_once",
                    })
                if daily_offer:
                    content += (
                        "\n\nПривяжите Telegram или MAX, чтобы такие напоминания "
                        "приходили даже когда Консилиум закрыт."
                    )
                    message_metadata.update({
                        "weight_messenger_link": True,
                        "weight_messenger_dismiss": True,
                        "weight_messenger_label": "Привязать Telegram или MAX",
                    })
                    conn.execute(
                        """UPDATE weight_control_states
                        SET messenger_prompt_last_shown_on=?,updated_at=?
                        WHERE conversation_id=? AND chel_id=?""",
                        (today_moscow, now, row["conversation_id"], row["chel_id"]),
                    )
                metadata = json.dumps(message_metadata, ensure_ascii=False)
                message_cursor = conn.execute(
                    """INSERT INTO messages
                    (conversation_id,role,agent_id,content,metadata,created_at)
                    VALUES (?,'assistant','manager',?,?,?)""",
                    (row["conversation_id"], content, metadata, now),
                )
                conn.execute(
                    "UPDATE weight_control_reminder_runs SET message_id=? WHERE id=?",
                    (int(message_cursor.lastrowid), run_id),
                )
                conn.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?",
                    (now, row["conversation_id"]),
                )
                conversation_url = (
                    f"{str(public_url or '').rstrip('/')}/?conversation={row['conversation_id']}"
                    + ("&weight_diary=1" if program_complete else "")
                )
                payload = {
                    "title": row["title"] if program_complete else f"Напоминание: {row['title']}",
                    "body": row["message"],
                    "kind": (
                        "weight_program_completed" if program_complete
                        else "weight_control_reminder"
                    ),
                    "conversation_id": row["conversation_id"],
                    "action_url": conversation_url,
                    "action_label": (
                        "Получить анализ питания" if program_complete else "Открыть чат"
                    ),
                }
                for identity in identities:
                    recipient = _external_identity_recipient(identity)
                    recipient_kind = "chat"
                    if identity["provider"] == "max" and not recipient:
                        recipient = str(identity["provider_user_id"] or "").strip()
                        recipient_kind = "user"
                    if recipient:
                        identity_payload = dict(payload)
                        if identity["provider"] == "max":
                            identity_payload["recipient_kind"] = recipient_kind
                        conn.execute(
                            """INSERT OR IGNORE INTO weight_control_reminder_outbox
                            (run_id,chel_id,provider,recipient_id,payload,status,attempts,created_at,updated_at)
                            VALUES (?,?,?,?,?,'pending',0,?,?)""",
                            (
                                run_id, row["chel_id"], identity["provider"], recipient,
                                json.dumps(identity_payload, ensure_ascii=False), now, now,
                            ),
                        )
                delivered += 1
            if int(row["one_off"] or 0):
                conn.execute(
                    """UPDATE weight_control_reminders
                    SET enabled=0,last_sent_at=?,updated_at=? WHERE id=?""",
                    (now, now, row["id"]),
                )
            else:
                days = json.loads(row["weekdays"] or "[]")
                next_run = _weight_reminder_next_run(
                    str(row["time_local"]), days,
                    after=datetime.fromisoformat(scheduled_for) + timedelta(seconds=1),
                    interval_days=int(row["interval_days"] or 0),
                )
                conn.execute(
                    """UPDATE weight_control_reminders
                    SET next_run_at=?,last_sent_at=?,updated_at=? WHERE id=?""",
                    (next_run, now, now, row["id"]),
                )
        conn.commit()
    return delivered


def claim_weight_reminder_notifications(provider: str, limit: int = 20) -> list[dict]:
    provider = _messenger_provider(provider)
    now = datetime.now(timezone.utc)
    stale_before = (now - timedelta(minutes=2)).isoformat()
    result = []
    with _write_lock, connection() as conn:
        rows = conn.execute(
            """SELECT id,recipient_id,payload,attempts FROM weight_control_reminder_outbox
            WHERE provider=? AND attempts<100
              AND (next_attempt_at IS NULL OR next_attempt_at<=?)
              AND (status='pending' OR (status='delivering' AND leased_at<?))
            ORDER BY id LIMIT ?""",
            (provider, now.isoformat(), stale_before, max(1, min(50, int(limit)))),
        ).fetchall()
        for row in rows:
            token = secrets.token_urlsafe(18)
            conn.execute(
                """UPDATE weight_control_reminder_outbox SET status='delivering',lease_token=?,
                leased_at=?,attempts=attempts+1,updated_at=? WHERE id=?""",
                (token, now.isoformat(), now.isoformat(), row["id"]),
            )
            item = dict(row)
            item.update({
                "id": -(WEIGHT_REMINDER_NOTIFICATION_ID_OFFSET + int(row["id"])),
                "event_type": "weight_control_reminder", "conversation_id": "",
                "lease_token": token, "payload": json.loads(row["payload"] or "{}"),
            })
            result.append(item)
        conn.commit()
    return result


def acknowledge_weight_reminder_notification(
    notification_id: int, lease_token: str, success: bool, error: str = "",
) -> bool:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT attempts FROM weight_control_reminder_outbox
            WHERE id=? AND lease_token=? AND status='delivering'""",
            (int(notification_id), str(lease_token or "")),
        ).fetchone()
        if not row:
            return False
        permanent = not success and _permanent_messenger_delivery_error(error)
        delay = min(3600, 5 * (2 ** min(int(row["attempts"]), 10)))
        next_attempt = (now_dt + timedelta(seconds=delay)).isoformat() if not success and not permanent else None
        cursor = conn.execute(
            """UPDATE weight_control_reminder_outbox SET status=?,sent_at=?,last_error=?,
            next_attempt_at=?,lease_token=NULL,leased_at=NULL,updated_at=?
            WHERE id=? AND lease_token=? AND status='delivering'""",
            (
                "sent" if success else ("failed" if permanent else "pending"),
                now if success else None, str(error or "")[:500], next_attempt, now,
                int(notification_id), str(lease_token or ""),
            ),
        )
        conn.commit()
    return bool(cursor.rowcount)


def get_conversation(conversation_id: str) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND chel_id = ?",
            (conversation_id, current_chel_id()),
        ).fetchone()
    return dict(row) if row else None


def reset_masterclass_psychology_conversation(conversation_id: str) -> dict | None:
    """Irreversibly clear Vita's transcript and AI state, preserving sessions/payments."""
    owner = current_chel_id()
    now = utc_now()
    attachment_items: list[dict] = []
    with _write_lock, connection() as conn:
        conversation = conn.execute(
            """SELECT * FROM conversations
               WHERE id=? AND chel_id=? AND dialog_type='masterclass_psychology'""",
            (str(conversation_id or ""), owner),
        ).fetchone()
        if not conversation:
            return None
        crisis_locked = bool(
            not conversation["ai_enabled"]
            and str(conversation["human_ticket_id"] or "").startswith("URG-")
        )
        rows = conn.execute(
            "SELECT id,metadata FROM messages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchall()
        for row in rows:
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            attachment_items.extend(metadata.get("attachments") or [])

        # Session and payment tables deliberately remain intact: they are
        # account records rather than conversational context.
        conn.execute(
            """DELETE FROM manager_notification_outbox
               WHERE conversation_id=?
                 AND event_type IN ('new_message','new_request')""",
            (conversation_id,),
        )
        for table in (
            "conversation_context_compactions", "message_deletions",
            "conversation_reads", "staff_conversation_reads", "handoffs",
            "manager_actions", "psychology_dialog_states",
        ):
            conn.execute(f"DELETE FROM {table} WHERE conversation_id=?", (conversation_id,))
        conn.executemany(
            """INSERT INTO message_deletions
               (message_id,conversation_id,deleted_at) VALUES (?,?,?)""",
            [(int(row["id"]), conversation_id, now) for row in rows],
        )
        conn.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
        cursor = conn.execute(
            """INSERT INTO messages
               (conversation_id,role,agent_id,content,metadata,created_at)
               VALUES (?,'assistant','psychologist',?,?,?)""",
            (
                conversation_id, MASTERCLASS_PSYCHOLOGY_WELCOME,
                json.dumps({
                    "specialized_dialog": "masterclass_psychology",
                    "conversation_reset": True,
                }, ensure_ascii=False),
                now,
            ),
        )
        welcome_message_id = int(cursor.lastrowid)
        conn.execute(
            """INSERT INTO conversation_reads
               (conversation_id,last_read_message_id,read_at) VALUES (?,?,?)""",
            (conversation_id, welcome_message_id, now),
        )
        if crisis_locked:
            conn.execute(
                """UPDATE conversations SET active_agent='psychologist',
                   context_summary='{}',status='waiting_human',ai_enabled=0,
                   human_status='pending',human_channel='chat',human_phone=NULL,
                   human_recipient_role='psychologist',updated_at=?
                   WHERE id=? AND chel_id=?""",
                (now, conversation_id, owner),
            )
        else:
            conn.execute(
                """UPDATE conversations SET active_agent='psychologist',
                   context_summary='{}',status='active',ai_enabled=1,
                   human_status='none',human_ticket_id=NULL,human_channel=NULL,
                   human_phone=NULL,human_recipient_role='psychologist',updated_at=?
                   WHERE id=? AND chel_id=?""",
                (now, conversation_id, owner),
            )
        conn.commit()

    # Files live outside SQLite and are removed only after the DB reset commits.
    if attachment_items:
        from . import chat_attachments
        chat_attachments.delete_public_attachments(attachment_items, owner)
    result = get_conversation(conversation_id)
    if result:
        result["messages"] = list_messages(conversation_id)
    return result


def list_conversations(limit: int = 50) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT c.*,
                COALESCE((
                    SELECT COUNT(*) FROM messages incoming
                    WHERE incoming.conversation_id = c.id
                      AND incoming.role = 'assistant'
                      AND incoming.id > COALESCE(r.last_read_message_id, 0)
                ), 0) AS unread_count
            FROM conversations c
            LEFT JOIN conversation_reads r ON r.conversation_id = c.id
            WHERE c.chel_id = ? AND c.dialog_type <> 'psychology_support'
            ORDER BY c.pinned DESC, c.updated_at DESC LIMIT ?""",
            (current_chel_id(), limit),
        ).fetchall()
    return [dict(row) for row in rows]


def conversation_unread_counts() -> dict[str, int]:
    """Return unread incoming-message counts for all current user's dialogs."""
    return {
        item["id"]: int(item.get("unread_count") or 0)
        for item in list_conversations()
        if int(item.get("unread_count") or 0) > 0
    }


def mark_conversation_read(conversation_id: str) -> dict | None:
    """Mark every currently persisted incoming message in one owned dialog as read."""
    conversation = get_conversation(conversation_id)
    if not conversation:
        return None
    now = utc_now()
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT COALESCE(MAX(m.id), 0) AS last_incoming_id
            FROM messages m JOIN conversations c ON c.id = m.conversation_id
            WHERE m.conversation_id = ? AND c.chel_id = ? AND m.role = 'assistant'""",
            (conversation_id, current_chel_id()),
        ).fetchone()
        last_incoming_id = int(row["last_incoming_id"] or 0)
        conn.execute(
            """INSERT INTO conversation_reads (conversation_id, last_read_message_id, read_at)
            VALUES (?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                last_read_message_id = MAX(last_read_message_id, excluded.last_read_message_id),
                read_at = excluded.read_at""",
            (conversation_id, last_incoming_id, now),
        )
        conn.commit()
    return {"conversation_id": conversation_id, "last_read_message_id": last_incoming_id}


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


def conversation_context_usage(conversation_id: str) -> dict:
    """Return lightweight transcript usage data for the context-limit notice."""
    with connection() as conn:
        checkpoint = conn.execute(
            """SELECT through_message_id FROM conversation_context_compactions cc
               JOIN conversations c ON c.id=cc.conversation_id
               WHERE cc.conversation_id=? AND c.chel_id=?""",
            (conversation_id, current_chel_id()),
        ).fetchone()
        after_id = int(checkpoint["through_message_id"] or 0) if checkpoint else 0
        row = conn.execute(
            """SELECT COUNT(m.id) AS message_count,
                COALESCE(SUM(LENGTH(m.content)), 0) AS content_chars,
                COALESCE(MAX(CASE
                    WHEN m.metadata LIKE '%\"context_limit_warning\": true%' THEN 1
                    ELSE 0 END), 0) AS warning_shown
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.conversation_id = ? AND c.chel_id = ? AND m.id > ?""",
            (conversation_id, current_chel_id(), after_id),
        ).fetchone()
    return {
        "message_count": int(row["message_count"] or 0),
        "content_chars": int(row["content_chars"] or 0),
        "warning_shown": bool(row["warning_shown"]),
        "compressed_through_message_id": after_id,
    }


def conversation_context_compaction(conversation_id: str) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            """SELECT cc.* FROM conversation_context_compactions cc
               JOIN conversations c ON c.id=cc.conversation_id
               WHERE cc.conversation_id=? AND c.chel_id=?""",
            (conversation_id, current_chel_id()),
        ).fetchone()
    return dict(row) if row else None


def list_messages_for_context_compaction(
    conversation_id: str, *, after_id: int = 0, through_id: int | None = None,
) -> list[dict]:
    upper = int(through_id or 0)
    with connection() as conn:
        rows = conn.execute(
            """SELECT m.* FROM messages m
               JOIN conversations c ON c.id=m.conversation_id
               WHERE m.conversation_id=? AND c.chel_id=? AND m.id>?
                 AND (?=0 OR m.id<=?)
               ORDER BY m.id""",
            (
                conversation_id, current_chel_id(), max(0, int(after_id)),
                upper, upper,
            ),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.get("metadata") or "{}")
        except json.JSONDecodeError:
            item["metadata"] = {}
        result.append(item)
    return result


def list_messages_since_context_compaction(
    conversation_id: str, after_id: int, limit: int,
) -> list[dict]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT * FROM (
                 SELECT m.* FROM messages m
                 JOIN conversations c ON c.id=m.conversation_id
                 WHERE m.conversation_id=? AND c.chel_id=? AND m.id>?
                 ORDER BY m.id DESC LIMIT ?
               ) ORDER BY id""",
            (
                conversation_id, current_chel_id(), max(0, int(after_id)),
                max(1, int(limit)),
            ),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.get("metadata") or "{}")
        except json.JSONDecodeError:
            item["metadata"] = {}
        result.append(item)
    return result


def validate_context_compaction_warning(
    conversation_id: str, through_message_id: int,
) -> tuple[dict, dict | None]:
    with connection() as conn:
        row = conn.execute(
            """SELECT m.* FROM messages m
               JOIN conversations c ON c.id=m.conversation_id
               WHERE m.id=? AND m.conversation_id=? AND c.chel_id=?""",
            (int(through_message_id), conversation_id, current_chel_id()),
        ).fetchone()
    if not row:
        raise ValueError("Сообщение для сжатия контекста не найдено")
    message = dict(row)
    try:
        message["metadata"] = json.loads(message.get("metadata") or "{}")
    except json.JSONDecodeError:
        message["metadata"] = {}
    if not message["metadata"].get("context_limit_warning"):
        raise ValueError("Сжатие доступно только по кнопке под предупреждением")
    current = conversation_context_compaction(conversation_id)
    if current and int(current["through_message_id"]) >= int(through_message_id):
        raise ValueError("Эта часть диалога уже сжата")
    return message, current


def save_context_compaction(
    conversation_id: str, through_message_id: int, summary: str,
) -> dict:
    summary = str(summary or "").strip()
    if not summary:
        raise ValueError("Не удалось сформировать сжатый контекст")
    now = utc_now()
    with _write_lock, connection() as conn:
        owner = conn.execute(
            "SELECT 1 FROM conversations WHERE id=? AND chel_id=?",
            (conversation_id, current_chel_id()),
        ).fetchone()
        if not owner:
            raise ValueError("Диалог не найден")
        message = conn.execute(
            "SELECT metadata FROM messages WHERE id=? AND conversation_id=?",
            (int(through_message_id), conversation_id),
        ).fetchone()
        if not message:
            raise ValueError("Сообщение для сжатия контекста не найдено")
        try:
            metadata = json.loads(message["metadata"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        if not metadata.get("context_limit_warning"):
            raise ValueError("Сжатие доступно только по кнопке под предупреждением")
        metadata["context_compacted"] = True
        conn.execute(
            """INSERT INTO conversation_context_compactions
               (conversation_id,through_message_id,summary,created_at,updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(conversation_id) DO UPDATE SET
                 through_message_id=excluded.through_message_id,
                 summary=excluded.summary,updated_at=excluded.updated_at""",
            (
                conversation_id, int(through_message_id), summary, now, now,
            ),
        )
        conn.execute(
            "UPDATE messages SET metadata=? WHERE id=? AND conversation_id=?",
            (
                json.dumps(metadata, ensure_ascii=False), int(through_message_id),
                conversation_id,
            ),
        )
        conn.commit()
    return conversation_context_compaction(conversation_id) or {}


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


def list_message_deletions(conversation_id: str) -> list[int]:
    """Return deleted message IDs so an already open client can remove them."""
    with connection() as conn:
        rows = conn.execute(
            """SELECT d.message_id FROM message_deletions d
            JOIN conversations c ON c.id = d.conversation_id
            WHERE d.conversation_id = ? AND c.chel_id = ?
            ORDER BY d.message_id""",
            (conversation_id, current_chel_id()),
        ).fetchall()
    return [int(row["message_id"]) for row in rows]


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
    human_channel: str | None = None, ai_enabled: bool | None = None,
) -> None:
    with _write_lock, connection() as conn:
        if ai_enabled is None:
            conn.execute(
                "UPDATE conversations SET active_agent = ?, context_summary = ?, status = ?, human_status = ?, human_ticket_id = ?, human_channel = ?, updated_at = ? WHERE id = ? AND chel_id = ?",
                (active_agent, context_summary, status, human_status, human_ticket_id, human_channel, utc_now(), conversation_id, current_chel_id()),
            )
        else:
            conn.execute(
                "UPDATE conversations SET active_agent = ?, context_summary = ?, status = ?, human_status = ?, human_ticket_id = ?, human_channel = ?, ai_enabled = ?, updated_at = ? WHERE id = ? AND chel_id = ?",
                (active_agent, context_summary, status, human_status, human_ticket_id, human_channel, int(ai_enabled), utc_now(), conversation_id, current_chel_id()),
            )
        conn.commit()


def confirm_human_chat(
    conversation_id: str, proposed_ticket_id: str, *, recipient_role: str = "manager",
) -> tuple[dict | None, bool]:
    """Atomically creates or reuses a confirmed specialist chat request."""
    owner = current_chel_id()
    recipient_role = _staff_role(recipient_role)
    now = utc_now()
    with _write_lock, connection() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND chel_id = ?",
            (conversation_id, owner),
        ).fetchone()
        if not row:
            return None, False
        current = dict(row)
        same_role_request = bool(
            current.get("human_ticket_id")
            and current.get("human_status") in {"pending", "connected"}
            and current.get("human_recipient_role", "manager") == recipient_role
        )
        # A call/session request and a request to join the text chat are two
        # different user actions. Reuse the visible ticket when switching the
        # same specialist from a call to chat, but report the chat handoff as
        # newly created so its messenger notification is queued.
        already_requested = bool(
            same_role_request and current.get("human_channel") == "chat"
        )
        ticket_id = (
            current["human_ticket_id"] if same_role_request else proposed_ticket_id
        )
        active_agent = "psychologist" if recipient_role == "psychologist" else "manager"
        conn.execute(
            """UPDATE conversations
            SET active_agent = ?, status = 'waiting_human',
                human_status = 'pending', human_ticket_id = ?, human_channel = 'chat',
                human_phone = NULL, human_recipient_role = ?, ai_enabled = 0, updated_at = ?
            WHERE id = ? AND chel_id = ?""",
            (active_agent, ticket_id, recipient_role, now, conversation_id, owner),
        )
        conn.commit()
        saved = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND chel_id = ?",
            (conversation_id, owner),
        ).fetchone()
    return (dict(saved) if saved else None), not already_requested


def cancel_checkup_purchase_request(conversation_id: str) -> tuple[dict | None, bool]:
    """Cancel only a user-owned checkup purchase handoff and resume its AI chat."""
    owner = current_chel_id()
    now = utc_now()
    with _write_lock, connection() as conn:
        conversation = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND chel_id = ?",
            (conversation_id, owner),
        ).fetchone()
        if not conversation:
            return None, False
        message_rows = conn.execute(
            """SELECT id, metadata FROM messages
            WHERE conversation_id = ? AND role = 'assistant' ORDER BY id DESC""",
            (conversation_id,),
        ).fetchall()
        purchase_message = None
        purchase_metadata = None
        for row in message_rows:
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            if metadata.get("action") == "checkup_purchase_handoff":
                purchase_message = row
                purchase_metadata = metadata
                break
        if purchase_message is None:
            raise ValueError("Заявка на приобретение чекапа не найдена")
        if purchase_metadata.get("checkup_purchase_cancelled"):
            result = dict(conversation)
            result["ai_enabled"] = bool(result["ai_enabled"])
            return result, False
        if conversation["human_status"] not in {"pending", "connected"}:
            raise ValueError("Заявка на приобретение чекапа уже закрыта")

        purchase_metadata["checkup_purchase_cancel"] = False
        purchase_metadata["checkup_purchase_cancelled"] = True
        conn.execute(
            "UPDATE messages SET metadata = ? WHERE id = ?",
            (json.dumps(purchase_metadata, ensure_ascii=False), purchase_message["id"]),
        )
        conn.execute(
            """UPDATE conversations SET ai_enabled = 1, human_status = 'closed',
            status = 'active', updated_at = ? WHERE id = ? AND chel_id = ?""",
            (now, conversation_id, owner),
        )
        conn.execute(
            """UPDATE manager_notification_outbox SET status = 'cancelled',
            lease_token = NULL, leased_at = NULL, next_attempt_at = NULL
            WHERE conversation_id = ? AND status <> 'sent'""",
            (conversation_id,),
        )
        conn.commit()
        saved = conn.execute(
            "SELECT * FROM conversations WHERE id = ? AND chel_id = ?",
            (conversation_id, owner),
        ).fetchone()
    result = dict(saved) if saved else None
    if result:
        result["ai_enabled"] = bool(result["ai_enabled"])
    return result, True

def set_human_channel(conversation_id: str, channel: str, phone: str | None = None) -> dict | None:
    if channel not in {"chat", "call"}:
        raise ValueError("Неизвестный способ связи")
    with _write_lock, connection() as conn:
        conn.execute(
            "UPDATE conversations SET human_channel = ?, human_phone = ?, ai_enabled = 0, status = 'waiting_human', updated_at = ? WHERE id = ? AND chel_id = ?",
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
            "driving_time": "unknown", "fatigue": "unknown", "tube_number": "",
            "tube_linked_at": None,
            "updated_at": None,
        }
    result = dict(row)
    for key in ("conditions", "medications", "allergies"):
        try:
            result[key] = json.loads(result[key] or "[]")
        except json.JSONDecodeError:
            result[key] = []
    return result


def health_passport_eligible() -> bool:
    """A passport is offered only after the full standard questionnaire flow."""
    onboarding = get_onboarding()
    profile = get_profile()
    return bool(
        onboarding.get("status") == "complete"
        and onboarding.get("intro_seen")
        and not onboarding.get("questionnaire_skipped")
        and profile.get("updated_at")
    )


def get_health_passport() -> dict:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM health_passports WHERE chel_id=?",
            (current_chel_id(),),
        ).fetchone()
    eligible = health_passport_eligible()
    if not row:
        return {
            "status": "not_created", "content": {}, "generated_at": None,
            "updated_at": None, "eligible": eligible, "should_offer": eligible,
        }
    result = dict(row)
    result.pop("chel_id", None)
    try:
        content = json.loads(result.pop("content_json") or "{}")
    except json.JSONDecodeError:
        content = {}
    result["content"] = content if isinstance(content, dict) else {}
    result["eligible"] = eligible
    result["should_offer"] = bool(
        eligible and result["status"] in {"offered", "failed"}
    )
    return result


def decline_health_passport() -> dict:
    if not health_passport_eligible():
        raise ValueError("Сначала завершите анкету")
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO health_passports
            (chel_id,status,content_json,last_error,created_at,updated_at)
            VALUES (?,'declined','{}','',?,?)
            ON CONFLICT(chel_id) DO UPDATE SET
              status=CASE WHEN health_passports.status='ready' THEN 'ready' ELSE 'declined' END,
              last_error='',updated_at=excluded.updated_at""",
            (current_chel_id(), now, now),
        )
        conn.commit()
    return get_health_passport()


def begin_health_passport_generation() -> dict:
    if not health_passport_eligible():
        raise ValueError("Сначала завершите анкету")
    existing = get_health_passport()
    if existing.get("status") == "ready":
        return existing
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO health_passports
            (chel_id,status,content_json,last_error,created_at,updated_at)
            VALUES (?,'generating','{}','',?,?)
            ON CONFLICT(chel_id) DO UPDATE SET
              status='generating',last_error='',updated_at=excluded.updated_at""",
            (current_chel_id(), now, now),
        )
        conn.commit()
    return get_health_passport()


def save_health_passport(content: dict) -> dict:
    if not isinstance(content, dict):
        raise ValueError("Некорректный формат паспорта")
    now = utc_now()
    serialized = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO health_passports
            (chel_id,status,content_json,last_error,generated_at,created_at,updated_at)
            VALUES (?,'ready',?,'',?,?,?)
            ON CONFLICT(chel_id) DO UPDATE SET status='ready',content_json=excluded.content_json,
              last_error='',generated_at=excluded.generated_at,updated_at=excluded.updated_at""",
            (current_chel_id(), serialized, now, now, now),
        )
        conn.commit()
    return get_health_passport()


def fail_health_passport_generation(error: str) -> dict:
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """UPDATE health_passports SET status='failed',last_error=?,updated_at=?
            WHERE chel_id=? AND status='generating'""",
            (str(error or "")[:500], now, current_chel_id()),
        )
        conn.commit()
    return get_health_passport()


def health_passport_ai_context() -> dict:
    passport = get_health_passport()
    content = passport.get("content") or {}
    return {
        "status": passport.get("status"),
        "available_in": "Мои данные → Паспорт здоровья",
        "overview": str(content.get("overview") or "")[:1_000],
        "questions": list(content.get("questions") or [])[:5],
    }


def save_profile(profile: dict) -> dict:
    now = utc_now()
    tube_number = str(profile.get("tube_number", ""))[:80]
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
        str(profile.get("driving_time", "unknown"))[:30],
        str(profile.get("dark_in_eyes", "unknown"))[:30], str(profile.get("joint_pain", "unknown"))[:30],
        str(profile.get("fatigue", "unknown"))[:30],
        tube_number, now if tube_number else None,
        str(profile.get("notes", ""))[:1000], now,
    )
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO user_profile
            (chel_id, preferred_name, company_inn, age, sex, height_cm, weight_kg, pregnancy, conditions, medications, allergies, smoking, alcohol, activity, blood_pressure, blood_sugar, driving_time, dark_in_eyes, joint_pain, fatigue, tube_number, tube_linked_at, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chel_id) DO UPDATE SET preferred_name=excluded.preferred_name, company_inn=excluded.company_inn, age=excluded.age,
            sex=excluded.sex, height_cm=excluded.height_cm, weight_kg=excluded.weight_kg,
            pregnancy=excluded.pregnancy, conditions=excluded.conditions, medications=excluded.medications,
            allergies=excluded.allergies, smoking=excluded.smoking, alcohol=excluded.alcohol,
            activity=excluded.activity, blood_pressure=excluded.blood_pressure, blood_sugar=excluded.blood_sugar,
            driving_time=excluded.driving_time,
            dark_in_eyes=excluded.dark_in_eyes, joint_pain=excluded.joint_pain, fatigue=excluded.fatigue,
            tube_number=excluded.tube_number,
            tube_linked_at=CASE
              WHEN excluded.tube_number='' THEN NULL
              WHEN user_profile.tube_number=excluded.tube_number
                THEN COALESCE(user_profile.tube_linked_at, excluded.tube_linked_at)
              ELSE excluded.tube_linked_at
            END,
            notes=excluded.notes, updated_at=excluded.updated_at""",
            (current_chel_id(), *values),
        )
        conn.commit()
    saved_profile = get_profile()
    if saved_profile.get("company_inn") == TEST_COMPANY_INN:
        try:
            from . import analytics
            analytics.delete_user_data(current_chel_id())
        except Exception:
            # Analytics cleanup must never block questionnaire completion.
            pass
    return saved_profile


def update_preferred_name(preferred_name: str) -> dict:
    """Replace only the user's name without resetting questionnaire answers."""
    normalized = " ".join(str(preferred_name or "").split())[:100]
    if not normalized:
        raise ValueError("Укажите ФИО")
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO user_profile (chel_id,preferred_name,updated_at)
            VALUES (?,?,?)
            ON CONFLICT(chel_id) DO UPDATE SET
              preferred_name=excluded.preferred_name,
              updated_at=excluded.updated_at""",
            (current_chel_id(), normalized, now),
        )
        conn.commit()
    return get_profile()


def profile_fingerprint(profile: dict | None = None) -> str:
    """Stable cache key for profile fields that can change lab interpretation."""
    source = dict(profile or get_profile())
    for key in ("chel_id", "company_inn", "tube_number", "tube_linked_at", "updated_at"):
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


def create_lab_result_subscription(med_id: str) -> dict:
    """Remember that the current user wants a messenger notification when results appear."""
    med_id = str(med_id or "").strip()[:80]
    if not med_id:
        raise ValueError("Сначала введите номер пробирки")
    identities = current_external_identities()
    providers = sorted({
        str(item.get("provider") or "") for item in identities
        if str(item.get("provider") or "") in {"telegram", "max"}
    })
    if not providers:
        raise ValueError("Для уведомления привяжите Telegram или MAX")
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO lab_result_subscriptions
            (chel_id, med_id, providers, status, created_at, updated_at, next_check_at)
            VALUES (?, ?, ?, 'pending', ?, ?, ?)
            ON CONFLICT(chel_id, med_id) DO UPDATE SET
              providers=excluded.providers, status='pending', updated_at=excluded.updated_at,
              next_check_at=excluded.next_check_at""",
            (current_chel_id(), med_id, json.dumps(providers), now, now, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM lab_result_subscriptions WHERE chel_id=? AND med_id=?",
            (current_chel_id(), med_id),
        ).fetchone()
    result = dict(row)
    result["providers"] = providers
    return result


def get_lab_result_subscription(med_id: str = "") -> dict | None:
    med_id = str(med_id or "").strip()[:80]
    with connection() as conn:
        if med_id:
            row = conn.execute(
                """SELECT * FROM lab_result_subscriptions
                WHERE chel_id=? AND med_id=?""",
                (current_chel_id(), med_id),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT * FROM lab_result_subscriptions
                WHERE chel_id=? ORDER BY updated_at DESC LIMIT 1""",
                (current_chel_id(),),
            ).fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        result["providers"] = json.loads(result.get("providers") or "[]")
    except json.JSONDecodeError:
        result["providers"] = []
    return result


def claim_due_lab_result_subscriptions(limit: int = 3) -> list[dict]:
    """Reserve a small batch for checking without blocking later bot polls."""
    limit = max(1, min(10, int(limit)))
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    next_check = (now_dt + timedelta(minutes=15)).isoformat()
    with _write_lock, connection() as conn:
        rows = conn.execute(
            """SELECT id, chel_id, med_id, providers
            FROM lab_result_subscriptions
            WHERE status = 'pending'
              AND (next_check_at IS NULL OR next_check_at <= ?)
            ORDER BY COALESCE(next_check_at, created_at), id LIMIT ?""",
            (now, limit),
        ).fetchall()
        if rows:
            conn.executemany(
                """UPDATE lab_result_subscriptions
                SET next_check_at = ?, last_checked_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'""",
                [(next_check, now, now, row["id"]) for row in rows],
            )
            conn.commit()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["providers"] = json.loads(item.get("providers") or "[]")
        except json.JSONDecodeError:
            item["providers"] = []
        result.append(item)
    return result


def complete_lab_result_subscription_check(
    subscription_id: int, documents: list[dict], public_url: str,
) -> int:
    """Queue one result-ready notification per linked messenger."""
    if not documents:
        return 0
    now = utc_now()
    with _write_lock, connection() as conn:
        subscription = conn.execute(
            """SELECT id, chel_id, med_id, providers FROM lab_result_subscriptions
            WHERE id = ? AND status = 'pending'""",
            (int(subscription_id),),
        ).fetchone()
        if not subscription:
            return 0
        try:
            providers = {
                str(value) for value in json.loads(subscription["providers"] or "[]")
                if str(value) in {"telegram", "max"}
            }
        except json.JSONDecodeError:
            providers = set()
        identities = conn.execute(
            """SELECT provider, provider_user_id, chat_id FROM external_identities
            WHERE chel_id = ? AND access_status = 'active'""",
            (subscription["chel_id"],),
        ).fetchall()
        service_url = str(public_url or "").rstrip("/") + "/result"
        payload = json.dumps({
            "title": "Результаты анализов готовы",
            "body": (
                f"Для пробирки {subscription['med_id']} появились результаты. "
                f"Откройте Консилиум: {service_url}"
            ),
            "kind": "lab_results_ready",
            "med_id": subscription["med_id"],
            "action_url": service_url,
            "action_label": "Открыть результаты",
        }, ensure_ascii=False)
        inserted = 0
        for identity in identities:
            provider = str(identity["provider"] or "")
            recipient = _external_identity_recipient(identity)
            if provider not in providers or not recipient:
                continue
            cursor = conn.execute(
                """INSERT OR IGNORE INTO user_result_notification_outbox
                (subscription_id, chel_id, provider, recipient_id, payload, status,
                 attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
                (
                    subscription["id"], subscription["chel_id"], provider,
                    recipient, payload, now, now,
                ),
            )
            inserted += int(bool(cursor.rowcount))
        conn.execute(
            """UPDATE lab_result_subscriptions SET status = ?, updated_at = ?
            WHERE id = ?""",
            ("queued" if inserted else "pending", now, subscription["id"]),
        )
        conn.commit()
    return inserted


def claim_user_result_notifications(provider: str, limit: int = 20) -> list[dict]:
    provider = str(provider or "").strip().lower()
    if provider not in {"telegram", "max"}:
        raise ValueError("Неизвестный мессенджер")
    limit = max(1, min(50, int(limit)))
    now = datetime.now(timezone.utc)
    stale_before = (now - timedelta(minutes=2)).isoformat()
    result = []
    with _write_lock, connection() as conn:
        rows = conn.execute(
            """SELECT id, recipient_id, payload, attempts
            FROM user_result_notification_outbox
            WHERE provider = ? AND attempts < 100
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
              AND (status = 'pending' OR (status = 'delivering' AND leased_at < ?))
            ORDER BY id LIMIT ?""",
            (provider, now.isoformat(), stale_before, limit),
        ).fetchall()
        for row in rows:
            lease_token = secrets.token_urlsafe(18)
            conn.execute(
                """UPDATE user_result_notification_outbox
                SET status = 'delivering', lease_token = ?, leased_at = ?,
                    attempts = attempts + 1, updated_at = ? WHERE id = ?""",
                (lease_token, now.isoformat(), now.isoformat(), row["id"]),
            )
            item = dict(row)
            # Negative IDs let unchanged bot clients acknowledge the user outbox
            # through the existing notification endpoint without ID collisions.
            item["id"] = -int(item["id"])
            item["event_type"] = "lab_results_ready"
            item["conversation_id"] = ""
            item["lease_token"] = lease_token
            item["payload"] = json.loads(item["payload"] or "{}")
            result.append(item)
        conn.commit()
    return result


def _permanent_messenger_delivery_error(error: str) -> bool:
    normalized = str(error or "").lower()
    return any(code in normalized for code in (
        "chat.not.found", "dialog.not.found", "chat not found", "dialog not found",
        "bot was blocked", "user is deactivated",
    ))


def acknowledge_user_result_notification(
    notification_id: int, lease_token: str, success: bool, error: str = "",
) -> bool:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT attempts, subscription_id FROM user_result_notification_outbox
            WHERE id = ? AND lease_token = ? AND status = 'delivering'""",
            (int(notification_id), str(lease_token or "")),
        ).fetchone()
        if not row:
            return False
        permanent_failure = not success and _permanent_messenger_delivery_error(error)
        retry_delay = min(3600, 5 * (2 ** min(int(row["attempts"]), 10)))
        next_attempt_at = (
            (now_dt + timedelta(seconds=retry_delay)).isoformat()
            if not success and not permanent_failure else None
        )
        cursor = conn.execute(
            """UPDATE user_result_notification_outbox SET status = ?, sent_at = ?,
                last_error = ?, next_attempt_at = ?, lease_token = NULL,
                leased_at = NULL, updated_at = ?
            WHERE id = ? AND lease_token = ? AND status = 'delivering'""",
            (
                "sent" if success else ("failed" if permanent_failure else "pending"),
                now if success else None,
                str(error or "")[:500], next_attempt_at, now,
                int(notification_id), str(lease_token or ""),
            ),
        )
        if success:
            remaining = conn.execute(
                """SELECT COUNT(*) FROM user_result_notification_outbox
                WHERE subscription_id = ? AND status <> 'sent'""",
                (row["subscription_id"],),
            ).fetchone()[0]
            if not remaining:
                conn.execute(
                    """UPDATE lab_result_subscriptions
                    SET status = 'notified', notified_at = ?, updated_at = ? WHERE id = ?""",
                    (now, now, row["subscription_id"]),
                )
        conn.commit()
    return bool(cursor.rowcount)


USER_NOTIFICATION_ID_OFFSET = 1_000_000_000


def user_notification_details(notification_id: int) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            """SELECT o.chel_id,o.provider,o.event_type,o.reoffer_id,r.duration_bucket,r.is_test
            FROM user_notification_outbox o
            LEFT JOIN checkup_reoffers r ON r.id=o.reoffer_id WHERE o.id=?""",
            (int(notification_id),),
        ).fetchone()
    return dict(row) if row else None


def claim_user_notifications(provider: str, limit: int = 20) -> list[dict]:
    """Claim generic user-facing messenger notifications for an existing bot."""
    provider = str(provider or "").strip().lower()
    if provider not in {"telegram", "max"}:
        raise ValueError("Неизвестный мессенджер")
    limit = max(1, min(50, int(limit)))
    now = datetime.now(timezone.utc)
    stale_before = (now - timedelta(minutes=2)).isoformat()
    result: list[dict] = []
    with _write_lock, connection() as conn:
        rows = conn.execute(
            """SELECT id,recipient_id,event_type,payload,attempts FROM user_notification_outbox
            WHERE provider=? AND attempts<100
              AND (next_attempt_at IS NULL OR next_attempt_at<=?)
              AND (status='pending' OR (status='delivering' AND leased_at<?))
            ORDER BY id LIMIT ?""",
            (provider, now.isoformat(), stale_before, limit),
        ).fetchall()
        for row in rows:
            lease_token = secrets.token_urlsafe(18)
            conn.execute(
                """UPDATE user_notification_outbox SET status='delivering',lease_token=?,
                leased_at=?,attempts=attempts+1,updated_at=? WHERE id=?""",
                (lease_token, now.isoformat(), now.isoformat(), row["id"]),
            )
            item = dict(row)
            item["id"] = -(USER_NOTIFICATION_ID_OFFSET + int(row["id"]))
            item["conversation_id"] = ""
            item["lease_token"] = lease_token
            item["payload"] = json.loads(item["payload"] or "{}")
            result.append(item)
        conn.commit()
    return result


def acknowledge_user_notification(
    notification_id: int, lease_token: str, success: bool, error: str = "",
) -> bool:
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with _write_lock, connection() as conn:
        row = conn.execute(
            """SELECT attempts FROM user_notification_outbox
            WHERE id=? AND lease_token=? AND status='delivering'""",
            (int(notification_id), str(lease_token or "")),
        ).fetchone()
        if not row:
            return False
        permanent_failure = not success and _permanent_messenger_delivery_error(error)
        retry_delay = min(3600, 5 * (2 ** min(int(row["attempts"]), 10)))
        next_attempt_at = (
            (now_dt + timedelta(seconds=retry_delay)).isoformat()
            if not success and not permanent_failure else None
        )
        cursor = conn.execute(
            """UPDATE user_notification_outbox SET status=?,sent_at=?,last_error=?,
            next_attempt_at=?,lease_token=NULL,leased_at=NULL,updated_at=?
            WHERE id=? AND lease_token=? AND status='delivering'""",
            (
                "sent" if success else ("failed" if permanent_failure else "pending"),
                now if success else None,
                str(error or "")[:500], next_attempt_at, now,
                int(notification_id), str(lease_token or ""),
            ),
        )
        conn.commit()
    return bool(cursor.rowcount)


def get_onboarding() -> dict:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM onboarding_state WHERE chel_id = ?",
            (current_chel_id(),),
        ).fetchone()
    if not row:
        return {"status": "appearance", "entry_route": "", "selected_tests": [], "payment_status": "none", "questionnaire_skipped": False, "marketer_result_seen": False, "intro_seen": False, "font_size": "extra", "updated_at": None}
    result = dict(row)
    try:
        result["selected_tests"] = json.loads(result["selected_tests"] or "[]")
    except json.JSONDecodeError:
        result["selected_tests"] = []
    result["intro_seen"] = bool(result.get("intro_seen"))
    result["questionnaire_skipped"] = bool(result.get("questionnaire_skipped"))
    result["marketer_result_seen"] = bool(result.get("marketer_result_seen"))
    if result.get("font_size") not in {"standard", "large", "extra"}:
        result["font_size"] = "standard"
    return result


def entry_route_choice_required() -> bool:
    """Show the direction chooser only to a genuinely unattributed new user."""
    chel_id = current_chel_id()
    with connection() as conn:
        onboarding = conn.execute(
            "SELECT entry_route,updated_at FROM onboarding_state WHERE chel_id=?",
            (chel_id,),
        ).fetchone()
        if onboarding and (
            str(onboarding["entry_route"] or "").strip()
            or str(onboarding["updated_at"] or "").strip()
        ):
            return False
        established = conn.execute(
            """SELECT
                 EXISTS(SELECT 1 FROM masterclass_access_users WHERE chel_id=?)
                 OR EXISTS(SELECT 1 FROM ikp_access_users WHERE chel_id=?)
                 OR EXISTS(SELECT 1 FROM conversations WHERE chel_id=?)
                 OR EXISTS(SELECT 1 FROM user_profile WHERE chel_id=? AND updated_at IS NOT NULL)
                 OR EXISTS(SELECT 1 FROM users WHERE chel_id=? AND result_entry_at IS NOT NULL)
               AS established""",
            (chel_id, chel_id, chel_id, chel_id, chel_id),
        ).fetchone()
    return not bool(established and established["established"])


def set_entry_route(route: str) -> dict:
    """Persist the user's self-selected entry direction across devices."""
    route = str(route or "").strip().lower()
    if route not in {"masterclass", "medical_exam", "results", "other"}:
        raise ValueError("Выберите направление входа")
    now = utc_now()
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO onboarding_state
               (chel_id,status,entry_route,selected_tests,payment_status,
                questionnaire_skipped,marketer_result_seen,intro_seen,font_size,updated_at)
               VALUES (?,'appearance',?,'[]','none',0,0,0,'extra',?)
               ON CONFLICT(chel_id) DO UPDATE SET
                 entry_route=excluded.entry_route,
                 updated_at=excluded.updated_at""",
            (current_chel_id(), route, now),
        )
        conn.commit()
    return get_onboarding()


def save_onboarding(
    *, status: str, selected_tests: list[str] | None = None,
    payment_status: str | None = None, intro_seen: bool | None = None,
    font_size: str | None = None, questionnaire_skipped: bool | None = None,
    marketer_result_seen: bool | None = None,
) -> dict:
    current = get_onboarding()
    selected = current["selected_tests"] if selected_tests is None else selected_tests
    payment = current["payment_status"] if payment_status is None else payment_status
    seen = current.get("intro_seen", False) if intro_seen is None else intro_seen
    size = current.get("font_size", "extra") if font_size is None else font_size
    skipped = current.get("questionnaire_skipped", False) if questionnaire_skipped is None else questionnaire_skipped
    result_seen = current.get("marketer_result_seen", False) if marketer_result_seen is None else marketer_result_seen
    if size not in {"standard", "large", "extra"}:
        raise ValueError("Некорректный размер текста")
    with _write_lock, connection() as conn:
        conn.execute(
            """INSERT INTO onboarding_state (chel_id, status, selected_tests, payment_status, questionnaire_skipped, marketer_result_seen, intro_seen, font_size, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(chel_id) DO UPDATE SET status=excluded.status,
            selected_tests=excluded.selected_tests, payment_status=excluded.payment_status,
            questionnaire_skipped=excluded.questionnaire_skipped, marketer_result_seen=excluded.marketer_result_seen,
            intro_seen=excluded.intro_seen,
            font_size=excluded.font_size, updated_at=excluded.updated_at""",
            (current_chel_id(), status, json.dumps(selected, ensure_ascii=False), payment,
             int(bool(skipped)), int(bool(result_seen)), int(bool(seen)), size, utc_now()),
        )
        conn.commit()
    return get_onboarding()


def mark_marketer_result_seen() -> dict:
    """Persist completion of the one-time variant-C questionnaire summary."""
    with _write_lock, connection() as conn:
        conn.execute(
            "UPDATE onboarding_state SET marketer_result_seen=1,updated_at=? WHERE chel_id=?",
            (utc_now(), current_chel_id()),
        )
        conn.commit()
    return get_onboarding()
