"""All settings come from environment variables (see .env.example)."""
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _detect_git_branch() -> str:
    """RENDER_GIT_BRANCH (Render sets this for every deploy) or GIT_BRANCH (a plain
    override, e.g. for tests) wins; a local `git` checkout is the fallback for running
    directly on a dev machine. "unknown" (never "main") if none of that resolves, so an
    environment we can't identify is treated as non-production rather than silently
    assumed safe."""
    env_branch = os.getenv("RENDER_GIT_BRANCH") or os.getenv("GIT_BRANCH")
    if env_branch:
        return env_branch
    try:
        result = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(BASE_DIR),
                                capture_output=True, text=True, timeout=5)
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


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
    TIMEZONE = os.getenv("TIMEZONE", "America/New_York")

    # Database: only the main branch (deployed on Render) ever uses DATABASE_URL, the
    # production Supabase connection. Every other branch - and tests - uses
    # DATABASE_URL_DEV, falling back to a local SQLite file if that's unset, never to
    # DATABASE_URL itself. See EFFECTIVE_DATABASE_URL and app/db.py's startup check.
    GIT_BRANCH = _detect_git_branch()
    DATABASE_URL = os.getenv("DATABASE_URL", "")
    DATABASE_URL_DEV = os.getenv("DATABASE_URL_DEV", "")
    if GIT_BRANCH == "main":
        EFFECTIVE_DATABASE_URL = DATABASE_URL or f"sqlite:///{BASE_DIR / 'virtual_ga.db'}"
    else:
        EFFECTIVE_DATABASE_URL = DATABASE_URL_DEV or f"sqlite:///{BASE_DIR / 'virtual_ga_dev.db'}"

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

    # Task file attachments: Supabase Storage (private bucket, signed URLs, GA console
    # only). If unset, falls back to a local DB table instead - see app/storage.py.
    SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
    SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
    TASK_FILE_MAX_MB = int(os.getenv("TASK_FILE_MAX_MB", "10"))

    # Optional: emails the requester a link to confirm a task was taken care of.
    # Skipped quietly (see app/tickets.py) if SMTP_HOST is unset.
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM = os.getenv("SMTP_FROM", "")

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


def check_database_safety() -> None:
    """Refuses to start if a non-main branch somehow ends up pointed at the production
    DATABASE_URL - the one case the branch-aware EFFECTIVE_DATABASE_URL selection above
    can't itself prevent (e.g. DATABASE_URL_DEV mistakenly set to the same value).
    Called once at startup, before anything touches the database - see app/db.py."""
    if (settings.GIT_BRANCH != "main" and settings.DATABASE_URL
            and settings.EFFECTIVE_DATABASE_URL == settings.DATABASE_URL):
        raise RuntimeError(
            f"Refusing to start: branch '{settings.GIT_BRANCH}' is not main, but the "
            "database in use is the production DATABASE_URL. Set DATABASE_URL_DEV to a "
            "non-production database for this branch instead."
        )


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
