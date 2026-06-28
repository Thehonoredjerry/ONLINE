import os

from dotenv import load_dotenv


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Bot owner (only this user can run /allow-bot)
OWNER_ID = int(os.getenv("OWNER_ID", "1150660855025909800").strip() or "1150660855025909800")

DEV_GUILD_ID = os.getenv("DEV_GUILD_ID", "").strip() or None
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "").strip() or None

DELETE_CLOSED_TICKETS = _get_bool("DELETE_CLOSED_TICKETS", False)


def validate_config() -> None:
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not DATABASE_URL:
        missing.append("DATABASE_URL")
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
