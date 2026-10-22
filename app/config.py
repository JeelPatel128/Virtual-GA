"""All settings come from environment variables (see .env.example)."""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class GA:
    name: str
    teams_email: str
    aliases: list = field(default_factory=list)
    lead: bool = False

    def matches(self, text: str) -> bool:
        t = text.lower()
        return any(n.lower() in t for n in [self.name, *self.aliases] if n)


class Settings:
    # Core
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
    GA_KEY = os.getenv("GA_KEY", "ga")
    CRON_SECRET = os.getenv("CRON_SECRET", "cron")
    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'virtual_ga.db'}")
    TIMEZONE = os.getenv("TIMEZONE", "America/New_York")

    # AI (Gemini). Without a key the bot falls back to keyword search.
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_CHAT_MODEL = os.getenv("GEMINI_CHAT_MODEL", "gemini-2.5-flash")
    GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")

    # Teams (Workflows "post to a channel when a webhook request is received")
    TEAMS_WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "")
    LEAD_ALERT_ON_EVERY_HANDOFF = _bool("LEAD_ALERT_ON_EVERY_HANDOFF", True)

    # Google Calendar: the calendar's "Secret address in iCal format"
    GCAL_ICS_URL = os.getenv("GCAL_ICS_URL", "")
    SCHEDULE_CACHE_MINUTES = int(os.getenv("SCHEDULE_CACHE_MINUTES", "5"))

    # Behaviour
    ESCALATE_AFTER_MINUTES = int(os.getenv("ESCALATE_AFTER_MINUTES", "5"))
    REMINDER_STEP_MINUTES = int(os.getenv("REMINDER_STEP_MINUTES", "15"))
    DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "17"))  # local time, 24h
    PHONE_RETENTION_DAYS = int(os.getenv("PHONE_RETENTION_DAYS", "30"))
    MAX_BOT_QUESTIONS_PER_HOUR = int(os.getenv("MAX_BOT_QUESTIONS_PER_HOUR", "30"))
    MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "200"))
    FRONT_DESK_LOCATION = os.getenv("FRONT_DESK_LOCATION", "the School of Business front desk")

    TEAM_FILE = Path(os.getenv("TEAM_FILE", str(BASE_DIR / "team.json")))


settings = Settings()


def load_team() -> list[GA]:
    """team.json lists every GA: name, Teams email, name aliases used in the calendar, and who is lead.
    TEAM_JSON env var (the same JSON as a string) wins, so hosts without a disk can still configure it."""
    raw = os.getenv("TEAM_JSON")
    if not raw:
        if not settings.TEAM_FILE.exists():
            return []
        raw = settings.TEAM_FILE.read_text()
    return [GA(**g) for g in json.loads(raw)]


def lead_ga() -> GA | None:
    return next((g for g in load_team() if g.lead), None)
