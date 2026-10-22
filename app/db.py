"""Database models. SQLite locally, Supabase Postgres in production (same code)."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text,
                        create_engine, JSON)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


_url = settings.DATABASE_URL
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
    """A handoff to a human GA."""
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    # waiting -> on_the_way | follow_up | replied -> resolved
    status: Mapped[str] = mapped_column(String(20), default="waiting", index=True)
    on_shift: Mapped[list] = mapped_column(JSON, default=list)  # GA names on shift when created
    responded_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Migrated onto the existing table, see _ensure_columns. kind: visitor | message.
    kind: Mapped[str] = mapped_column(String(20), default="visitor")
    recipient_person_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class QueryLog(Base):
    """Every question asked to the bot, for the daily digest and the 'questions to add' list."""
    __tablename__ = "query_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    question: Mapped[str] = mapped_column(Text)
    confident: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
# and added individually on every startup (cheap, idempotent).
_MIGRATIONS = [
    ("sessions", "last_person_id", "INTEGER"),
    ("sessions", "pending_query", "TEXT"),
    ("sessions", "pending_choices", "JSON"),
    ("tickets", "kind", "TEXT"),
    ("tickets", "recipient_person_id", "INTEGER"),
    ("tickets", "deadline_at", "TIMESTAMP"),
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
        for table, column, sql_type in _MIGRATIONS:
            if column not in _existing_columns(conn, table):
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")


def init_db() -> None:
    Base.metadata.create_all(engine)
    _ensure_columns()


def kv_get(db, key: str) -> str | None:
    row = db.get(KV, key)
    return row.value if row else None


def kv_set(db, key: str, value: str) -> None:
    row = db.get(KV, key)
    if row:
        row.value = value
    else:
        db.add(KV(key=key, value=value))
