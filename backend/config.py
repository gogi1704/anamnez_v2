import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Minimal .env loader so the project has no third-party dependencies."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development").strip().lower()
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    orchestrator_model: str = os.getenv("ORCHESTRATOR_MODEL", "gpt-5.6-luna")
    specialist_model: str = os.getenv("SPECIALIST_MODEL", "gpt-5.6-sol")
    database_path: Path = Path(os.getenv("DATABASE_PATH", BASE_DIR / "data" / "consilium.db"))
    analytics_database_path: Path = Path(os.getenv(
        "ANALYTICS_DATABASE_PATH", BASE_DIR / "data" / "analytics.db",
    ))
    analytics_enabled: bool = env_bool("ANALYTICS_ENABLED", True)
    analytics_retention_days: int = int(os.getenv("ANALYTICS_RETENTION_DAYS", "90"))
    yandex_metrika_counter_id: str = os.getenv("YANDEX_METRIKA_COUNTER_ID", "").strip()
    dadata_api_key: str = os.getenv("DADATA_API_KEY", "").strip()
    dadata_suggestions_url: str = os.getenv(
        "DADATA_SUGGESTIONS_URL",
        "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party",
    ).strip()
    dadata_timeout_seconds: int = int(os.getenv("DADATA_TIMEOUT_SECONDS", "5"))
    dadata_suggestions_cache_seconds: int = int(
        os.getenv("DADATA_SUGGESTIONS_CACHE_SECONDS", "600")
    )
    online_payments_enabled: bool = env_bool("ONLINE_PAYMENTS_ENABLED", False)
    yookassa_shop_id: str = os.getenv("YOOKASSA_SHOP_ID", "").strip()
    yookassa_secret_key: str = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
    yookassa_api_url: str = os.getenv(
        "YOOKASSA_API_URL", "https://api.yookassa.ru/v3",
    ).strip().rstrip("/")
    yookassa_timeout_seconds: int = int(os.getenv("YOOKASSA_TIMEOUT_SECONDS", "20"))
    yookassa_receipts_enabled: bool = env_bool("YOOKASSA_RECEIPTS_ENABLED", False)
    yookassa_vat_code: int = int(os.getenv("YOOKASSA_VAT_CODE", "1"))
    yookassa_payment_mode: str = os.getenv(
        "YOOKASSA_PAYMENT_MODE", "full_prepayment",
    ).strip()
    log_path: Path = Path(os.getenv("LOG_PATH", BASE_DIR / "server-error.log"))
    # The structured conversation summary and questionnaire are passed separately,
    # so the model only needs a bounded recent transcript.  Limiting both message
    # count and text size prevents a long-running chat from growing in cost forever.
    max_history_messages: int = int(os.getenv("MAX_HISTORY_MESSAGES", "30"))
    max_history_chars: int = int(os.getenv("MAX_HISTORY_CHARS", "16000"))
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))
    auto_open_browser: bool = env_bool("AUTO_OPEN_BROWSER", True)
    cookie_secure: bool = env_bool("COOKIE_SECURE", False)
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    bot_integration_secret: str = os.getenv("BOT_INTEGRATION_SECRET", "")
    auth_link_ttl_seconds: int = int(os.getenv("AUTH_LINK_TTL_SECONDS", "604800"))
    auth_intent_ttl_seconds: int = int(os.getenv("AUTH_INTENT_TTL_SECONDS", "604800"))
    telegram_bot_auth_url: str = os.getenv("TELEGRAM_BOT_AUTH_URL", "").strip()
    max_bot_auth_url: str = os.getenv("MAX_BOT_AUTH_URL", "").strip()
    session_ttl_days: int = int(os.getenv("SESSION_TTL_DAYS", "90"))
    session_cookie_name: str = os.getenv("SESSION_COOKIE_NAME", "consilium_session")
    admin_dashboard_token: str = os.getenv("ADMIN_DASHBOARD_TOKEN", "")
    lab_results_enabled: bool = env_bool("LAB_RESULTS_ENABLED", False)
    after_tests_google_credentials: str = os.getenv(
        "AFTER_TESTS_GOOGLE_CREDENTIALS",
        "/run/secrets/after-tests-google.json",
    )
    after_tests_spreadsheet: str = os.getenv("AFTER_TESTS_SPREADSHEET", "after_tests_db")
    after_tests_worksheet: str = os.getenv("AFTER_TESTS_WORKSHEET", "tetst_and_results")
    google_sheets_timeout_seconds: int = int(os.getenv("GOOGLE_SHEETS_TIMEOUT_SECONDS", "15"))
    lab_results_cache_seconds: int = int(os.getenv("LAB_RESULTS_CACHE_SECONDS", "60"))


settings = Settings()
