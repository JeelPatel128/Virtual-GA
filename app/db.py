"""Database models. SQLite locally, Supabase Postgres in production (same code)."""
import json
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text,
                        create_engine, JSON)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import check_database_safety, settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


check_database_safety()

_url = settings.EFFECTIVE_DATABASE_URL
if _url.startswith("postgres://"):
    _url = _url.replace("postgres://", "postgresql+psycopg://", 1)
elif _url.startswith("postgresql://"):
    _url = _url.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(
    _url,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if _url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Doc(Base):
    """One piece of knowledge a GA uploaded: a Q&A pair, a document, an image or a video."""
    __tablename__ = "docs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(20))  # qa | text | pdf | docx | image | video
    extracted_text: Mapped[str] = mapped_column(Text)
    image_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    mime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    added_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # FAQ bulk import (migrated onto the existing table, see _ensure_columns). faq_type
    # is the CSV's own "type" column (Answer/Directory/Task/Live/Handoff) - named to
    # avoid clashing with kind above. external_id is the CSV's short code (e.g. P1, R3),
    # used to upsert by id on re-import rather than duplicating rows.
    external_id: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    faq_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    variants: Mapped[list | None] = mapped_column(JSON, nullable=True)
    asked_by: Mapped[str | None] = mapped_column(String(300), nullable=True)
    times_mentioned: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str | None] = mapped_column(String(300), nullable=True)
    handled_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    changes_each_semester: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # GA resources library (migrated onto the existing table). knowledge.search's
    # visitor flag only ever includes audience="public" docs - "ga_only" and
    # "approval_required" (and anything else added later) are excluded by default
    # (an allowlist, not a denylist), searchable only from the GA console/admin.
    # Default "public"/False so every pre-existing Doc (manual Q&As, uploads) is
    # unaffected. linked_faq_ids is reference metadata (which FAQ rows a resource
    # supports), not used to gate anything. source_file is the path under
    # data/resources/ (committed to the repo, unlike the gitignored CSVs) - kept so an
    # approved download can re-locate the file without depending on the CSV still
    # being present.
    audience: Mapped[str] = mapped_column(String(20), default="public")
    seasonal: Mapped[bool] = mapped_column(Boolean, default=False)
    linked_faq_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    source_file: Mapped[str | None] = mapped_column(String(500), nullable=True)


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("docs.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)


class ChatSession(Base):
    """One visitor's conversation, created when they open the QR page."""
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    visitor_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    visitor_phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    phone_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Directory resolver memory (migrated onto the existing table, see _ensure_columns).
    last_person_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_choices: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Pickups (migrated onto the existing table). Set while the bot is mid "what's your
    # pickup code" exchange with this visitor - the next message is read as a code
    # attempt rather than a normal question. See app/pickups.py.
    pending_pickup_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    sender: Mapped[str] = mapped_column(String(10))  # visitor | bot | ga | system
    ga_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Ticket(Base):
    """One owned, deadlined, GA-team-only item: a visitor handoff, a message for a
    specific person, or a task request. status: new -> claimed -> in_progress ->
    done | cancelled, unified across all three kinds (see app/tickets.py)."""
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    on_shift: Mapped[list] = mapped_column(JSON, default=list)  # GA names on shift when created
    responded_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Migrated onto the existing table, see _ensure_columns. kind: visitor | message | task.
    kind: Mapped[str] = mapped_column(String(20), default="visitor")
    recipient_person_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # legacy, superseded by next_deadline
    # Reliability pack: ownership, deadline, and reminder-ladder state (migrated in).
    owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    owner_history: Mapped[list] = mapped_column(JSON, default=list)
    activity: Mapped[str | None] = mapped_column(String(20), nullable=True)  # on_the_way | busy | replied
    next_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_reminder_step: Mapped[int] = mapped_column(Integer, default=0)
    snooze_count: Mapped[int] = mapped_column(Integer, default=0)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # task kind only
    cancelled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    task: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {type, details, copies, pickup_location}
    file_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    file_mime: Mapped[str | None] = mapped_column(String(100), nullable=True)
    file_bytes: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)  # unused, see file_backend/file_ref
    requester_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Task file attachment: "supabase" (file_ref = storage object path) or "local"
    # (file_ref = str(TaskFile.id)). Never file_bytes above - see app/storage.py.
    file_backend: Mapped[str | None] = mapped_column(String(20), nullable=True)
    file_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)


class TaskFile(Base):
    """Fallback file storage when Supabase Storage isn't configured. Bytes are only
    loaded when a GA explicitly opens the file (app/main.py's dedicated endpoint) -
    never pulled into a ticket-list or ticket-detail query."""
    __tablename__ = "task_files"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    file_bytes: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReminderLog(Base):
    """One row per reminder card fired by the ladder (app/tickets.py::check_reminders),
    naming the GA it was actually sent to. Lets the weekly lead report count missed
    deadlines per GA without needing a point-in-time snapshot of the ladder state."""
    __tablename__ = "reminder_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    ga_name: Mapped[str] = mapped_column(String(120), index=True)
    step: Mapped[int] = mapped_column(Integer)
    fired_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApprovalLog(Base):
    """Audit trail for an approval_required resource request (app/approvals.py): who
    approved/declined/handed it over or was asked to, when, which ticket and resource."""
    __tablename__ = "approval_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    resource_id: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(String(200))  # a GA's name, or a staff approver's name
    action: Mapped[str] = mapped_column(String(30))  # approved | declined | handed_in_person | asked_staff
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AdminActionLog(Base):
    """Every admin-authenticated write (POST/PUT/DELETE under /api/admin/), logged by
    app/main.py's admin_audit_log middleware - who (best-effort, from X-Admin-Name; the
    admin password is one shared credential, not a per-user login, so this is a hint the
    admin typed, not a verified identity), what (method + path), when."""
    __tablename__ = "admin_action_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_name: Mapped[str] = mapped_column(String(120), default="Admin")
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(300))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class StaffApprovalLink(Base):
    """One issued staff approve/decline link (app/tokens.py). The token embeds and signs
    its own expiry, but single-use has to live here - a signature alone can't be marked
    "already spent." Looked up by nonce (the random component inside the token), never by
    ticket_id alone, so a new ask for the same ticket doesn't invalidate an older link
    that's still mid-use."""
    __tablename__ = "staff_approval_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    nonce: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    staff_name: Mapped[str] = mapped_column(String(200))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PickupLog(Base):
    """Handover record for a pickup (app/pickups.py, app/tickets.py): who it was for,
    when, and how it was verified - visible in admin."""
    __tablename__ = "pickup_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    recipient_name: Mapped[str] = mapped_column(String(200))
    verified_by: Mapped[str] = mapped_column(String(20))  # code | ga_in_person | not_sensitive
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MonthlyExport(Base):
    """One CSV snapshot per calendar month (app/tickets.py::maybe_run_monthly_export,
    run on the 1st). Aggregate counts only - never a name, email, or phone number - so
    it's safe to download straight from the admin panel."""
    __tablename__ = "monthly_exports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    month: Mapped[str] = mapped_column(String(7), unique=True, index=True)  # "YYYY-MM"
    csv_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QueryLog(Base):
    """Every question asked to the bot, for the daily digest and the 'questions to add' list.
    Deleted by the retention job once past its window (see app/retention.py) - category is
    kept, folded into TopicCount, first."""
    __tablename__ = "query_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    question: Mapped[str] = mapped_column(Text)
    confident: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # Migrated onto the existing table, see _ensure_columns. Whatever answered the question:
    # an FAQ Doc's own category, "People & offices"/"Places" (directory), "Supplies"
    # (inventory), or "Unanswered" if nothing matched or the matching Doc has none set.
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)


class TopicCount(Base):
    """Retention-job output (app/retention.py): once a QueryLog row's text passes its
    retention window, it's deleted and folded in here instead - a category/month count,
    never the question text or anything visitor-identifying."""
    __tablename__ = "topic_counts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(100), index=True)
    month: Mapped[str] = mapped_column(String(7), index=True)  # "YYYY-MM"
    count: Mapped[int] = mapped_column(Integer, default=0)


class KV(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Person(Base):
    """One SSB directory entry, imported (upserted) from an admin-uploaded CSV."""
    __tablename__ = "people"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    first_name: Mapped[str] = mapped_column(String(100), default="")
    last_name: Mapped[str] = mapped_column(String(100), default="")
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    title: Mapped[str] = mapped_column(String(200), default="")
    type: Mapped[str] = mapped_column(String(30), default="")
    building: Mapped[str] = mapped_column(String(50), default="")
    room: Mapped[str] = mapped_column(String(30), default="")
    floor: Mapped[str] = mapped_column(String(10), default="")
    directions: Mapped[str] = mapped_column(Text, default="")
    office_hours: Mapped[str] = mapped_column(String(200), default="")
    extension: Mapped[str] = mapped_column(String(20), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    roles: Mapped[str] = mapped_column(Text, default="")
    route_topics_here: Mapped[bool] = mapped_column(Boolean, default=True)
    show_publicly: Mapped[bool] = mapped_column(Boolean, default=True)
    last_verified: Mapped[str] = mapped_column(String(20), default="")
    review_notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Room(Base):
    """One place on the babbio4_rooms.csv map (lounge, washroom, a specific room, ...)."""
    __tablename__ = "rooms"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    place: Mapped[str] = mapped_column(String(100), index=True)
    directions_from_front_desk: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Item(Base):
    """One row from the SSB inventory CSV, imported (upserted by item_id) from an
    admin-uploaded CSV, same replace-by-key rules as Person/Room. A visitor is only ever
    told about audience="visitors" items, and never anything whose area starts with
    "Josa" (her personal stock) regardless of audience - see app/inventory.py."""
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[str] = mapped_column(String(20), index=True, unique=True)
    item: Mapped[str] = mapped_column(String(300), default="")
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    area: Mapped[str] = mapped_column(String(200), default="")
    spot: Mapped[str] = mapped_column(String(200), default="")
    quantity: Mapped[str] = mapped_column(String(50), default="")
    lend_or_give: Mapped[str] = mapped_column(String(20), default="")  # lend | give | internal
    audience: Mapped[str] = mapped_column(String(20), default="")  # visitors | ga_only
    status: Mapped[str] = mapped_column(String(20), default="")  # in stock | low | out | unknown
    on_order: Mapped[bool] = mapped_column(Boolean, default=False)
    return_within: Mapped[str] = mapped_column(String(50), default="")
    last_counted: Mapped[str] = mapped_column(String(20), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class EmailCandidate(Base):
    """A proposed email from tools/find_emails.py, awaiting admin approval."""
    __tablename__ = "email_candidates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    email: Mapped[str] = mapped_column(String(200))
    confidence: Mapped[str] = mapped_column(String(10))  # high | medium | low
    source_url: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending | approved | rejected
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# Columns added to tables that already exist in production after this feature shipped.
# Base.metadata.create_all() never alters an existing table, so each entry here is checked
# and added individually on every startup (cheap, idempotent). sql_type is either one
# portable name (works unchanged on SQLite and Postgres) or a {dialect: name} dict for the
# rare type that isn't spelled the same on both (e.g. binary data: BLOB vs BYTEA).
_MIGRATIONS = [
    ("sessions", "last_person_id", "INTEGER"),
    ("sessions", "pending_query", "TEXT"),
    ("sessions", "pending_choices", "JSON"),
    ("tickets", "kind", "TEXT"),
    ("tickets", "recipient_person_id", "INTEGER"),
    ("tickets", "deadline_at", "TIMESTAMP"),
    # Reliability pack
    ("tickets", "owner", "TEXT"),
    ("tickets", "owner_history", "JSON"),
    ("tickets", "activity", "TEXT"),
    ("tickets", "next_deadline", "TIMESTAMP"),
    ("tickets", "last_reminder_step", "INTEGER"),
    ("tickets", "snooze_count", "INTEGER"),
    ("tickets", "due_at", "TIMESTAMP"),
    ("tickets", "cancelled_reason", "TEXT"),
    ("tickets", "task", "JSON"),
    ("tickets", "file_name", "TEXT"),
    ("tickets", "file_mime", "TEXT"),
    ("tickets", "file_bytes", {"sqlite": "BLOB", "postgresql": "BYTEA"}),
    ("tickets", "requester_email", "TEXT"),
    ("tickets", "confirmed", "BOOLEAN"),
    ("tickets", "confirmed_at", "TIMESTAMP"),
    ("tickets", "file_backend", "TEXT"),
    ("tickets", "file_ref", "TEXT"),
    # FAQ bulk import
    ("docs", "external_id", "TEXT"),
    ("docs", "category", "TEXT"),
    ("docs", "faq_type", "TEXT"),
    ("docs", "variants", "JSON"),
    ("docs", "asked_by", "TEXT"),
    ("docs", "times_mentioned", "INTEGER"),
    ("docs", "source", "TEXT"),
    ("docs", "handled_by", "TEXT"),
    ("docs", "changes_each_semester", "BOOLEAN"),
    ("docs", "status", "TEXT"),
    # GA resources library
    ("docs", "audience", "TEXT"),
    ("docs", "seasonal", "BOOLEAN"),
    ("docs", "linked_faq_ids", "JSON"),
    ("docs", "source_file", "TEXT"),
    # Security and data retention
    ("query_log", "category", "TEXT"),
    # Pickups
    ("sessions", "pending_pickup_id", "INTEGER"),
]


def _existing_columns(conn, table: str) -> set[str]:
    if conn.engine.dialect.name == "sqlite":
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()}
    rows = conn.exec_driver_sql(
        f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}'").fetchall()
    return {row[0] for row in rows}


def _ensure_columns(eng=None) -> None:
    """Base.metadata.create_all() never alters a table that already exists (true for
    'sessions'/'tickets' in production), so each migration entry is added by hand, once,
    checked idempotently every startup. `eng` is overridable so this can be tested against
    a throwaway engine without touching the app's real one."""
    eng = eng or engine
    with eng.begin() as conn:
        dialect = conn.engine.dialect.name
        for table, column, sql_type in _MIGRATIONS:
            if isinstance(sql_type, dict):
                sql_type = sql_type.get(dialect, next(iter(sql_type.values())))
            if column not in _existing_columns(conn, table):
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def _ensure_rls(eng=None) -> None:
    """Enables row-level security on every table, with no policies defined. The app
    always connects to Postgres as the table owner (DATABASE_URL/DATABASE_URL_DEV), which
    bypasses RLS regardless of policies - this is a backstop against the Supabase anon or
    authenticated roles (which the app never uses or exposes; it only ever holds
    SUPABASE_SERVICE_KEY, for the Storage API) ever being able to read these tables
    directly through some other route, like PostgREST. A no-op on SQLite, which has no
    such concept."""
    eng = eng or engine
    if eng.dialect.name != "postgresql":
        return
    with eng.begin() as conn:
        for table in Base.metadata.tables:
            conn.exec_driver_sql(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def rls_status(eng=None) -> list[str]:
    """Tables (if any) where row-level security is off, checked live rather than cached
    from startup, for the admin status panel. Always empty on SQLite."""
    eng = eng or engine
    if eng.dialect.name != "postgresql":
        return []
    names = ", ".join(f"'{t}'" for t in Base.metadata.tables)
    with eng.connect() as conn:
        rows = conn.exec_driver_sql(
            f"SELECT relname FROM pg_class WHERE relkind = 'r' AND relrowsecurity = false "
            f"AND relname IN ({names})").fetchall()
    return sorted(row[0] for row in rows)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _ensure_columns()
    _ensure_rls()


def kv_get(db, key: str) -> str | None:
    row = db.get(KV, key)
    return row.value if row else None


def kv_set(db, key: str, value: str) -> None:
    row = db.get(KV, key)
    if row:
        row.value = value
    else:
        db.add(KV(key=key, value=value))


def get_today_note(db) -> str | None:
    """The GA-maintained "what's happening at SSB today" note. Expires at midnight
    America/New_York (not server-local time) - a stale note from a previous day is
    never shown."""
    raw = kv_get(db, "today_note")
    if not raw:
        return None
    data = json.loads(raw)
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    return data["text"] if data.get("date") == today else None


def set_today_note(db, text: str) -> None:
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    kv_set(db, "today_note", json.dumps({"text": text.strip()[:500], "date": today}))
