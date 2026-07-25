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
    log_path: Path = Path(os.getenv("LOG_PATH", BASE_DIR / "server-error.log"))
    max_history_messages: int = int(os.getenv("MAX_HISTORY_MESSAGES", "30"))
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))
    auto_open_browser: bool = env_bool("AUTO_OPEN_BROWSER", True)
    cookie_secure: bool = env_bool("COOKIE_SECURE", False)
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    bot_integration_secret: str = os.getenv("BOT_INTEGRATION_SECRET", "")
    auth_link_ttl_seconds: int = int(os.getenv("AUTH_LINK_TTL_SECONDS", "604800"))
    session_ttl_days: int = int(os.getenv("SESSION_TTL_DAYS", "90"))
    session_cookie_name: str = os.getenv("SESSION_COOKIE_NAME", "consilium_session")


settings = Settings()
