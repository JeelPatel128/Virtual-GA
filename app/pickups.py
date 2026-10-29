"""Pickups: a GA prepares something for a named recipient (e.g. printed exam copies for a
professor), who collects it later, possibly with no GA at the desk. Ticket creation, the
"mark ready" GA action, and digest itemization live in app/tickets.py - same split as
app/approvals.py/tickets.py for approval-gated resources. This module owns only the
pickup-specific domain logic: sensitivity, the pickup code, and matching a visitor's
claimed name against an open pickup's recipient.
"""
import hashlib
import logging
import re
import secrets
import smtplib
from datetime import datetime
from difflib import SequenceMatcher
from email.mime.text import MIMEText

from sqlalchemy import select

from . import directory
from .config import settings
from .db import Ticket, utcnow

log = logging.getLogger("virtual_ga.pickups")

CODE_EXPIRE_DAYS = 7
MAX_CODE_ATTEMPTS = 3
UNCOLLECTED_AFTER_DAYS = 2
NAME_MATCH_RATIO = 0.80

SENSITIVE_RE = re.compile(r"\b(exam|quiz|test|midterm|final|answer key)s?\b", re.I)
# "pick ... up" tolerates a few words in between ("pick my copies up", "pick something
# up"), not just the adjacent "pick up"/"pickup" form - natural phrasing doesn't always
# keep them together.
PICKUP_INTENT_RE = re.compile(
    r"\bpick(?:ing)?\b(?:\s+\S+){0,4}?\s*-?\s*up\b|\bcollect(?:ing)?\b|"
    r"\bhere (?:to get|for)\b|\bcame (?:to get|for)\b", re.I)
CODE_RE = re.compile(r"\b(\d{4})\b")


def is_sensitive_default(description: str) -> bool:
    return bool(SENSITIVE_RE.search(description))


def generate_code() -> str:
    return f"{secrets.randbelow(10000):04d}"


def hash_code(ticket_id: int, code: str) -> str:
    return hashlib.sha256(f"pickup:{ticket_id}:{code}:{settings.SECRET_KEY}".encode()).hexdigest()


def code_matches(ticket_id: int, task: dict, code: str) -> bool:
    return bool(task.get("code_hash")) and hash_code(ticket_id, code) == task["code_hash"]


def send_pickup_code_email(task: dict, code: str) -> bool:
    """True if actually emailed. False (never raises) if SMTP isn't configured or
    there's no email on file - the caller falls back to showing the code to the GA
    instead, same degrade-gracefully pattern as app/tickets.py's requester-confirmation
    email."""
    email = task.get("recipient_email")
    if not (settings.SMTP_HOST and email):
        return False
    msg = MIMEText(
        "Hi,\n\nYour pickup at the Stevens School of Business front desk is ready: "
        f"{task.get('description', 'your item')}.\n\n"
        f"Your pickup code is {code}. Show or tell this code to the GA when you collect it.\n\n"
        "Thanks,\nStevens SSB Front Desk")
    msg["Subject"] = "Your SSB front desk pickup is ready"
    msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
    msg["To"] = email
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as s:
            s.starttls()
            if settings.SMTP_USER:
                s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception as e:
        log.warning("Pickup code email failed: %s", e)
        return False


def _normalize(name: str) -> str:
    return directory.TITLE_WORD_RE.sub("", name or "").strip().lower()


def _name_matches(db, claimed_name: str, task: dict) -> bool:
    recipient_name = task.get("recipient_name", "")
    if SequenceMatcher(None, _normalize(claimed_name), _normalize(recipient_name)).ratio() >= NAME_MATCH_RATIO:
        return True
    recipient_person_id = task.get("recipient_person_id")
    if recipient_person_id:
        matched = directory.match_person(db, claimed_name)
        if matched and matched.id == recipient_person_id:
            return True
    return False


def find_open_pickups_for_name(db, claimed_name: str) -> list["Ticket"]:
    """Open pickups (not yet picked up or cancelled) whose recipient matches the
    visitor's claimed name - fuzzy on the name text itself (handles a typed name like
    "Prof. Mario"), and directory-aware: a claimed name that resolves to the SAME Person
    a pickup's recipient was set from also counts, even phrased differently (e.g. "Chris
    Alwell" for a pickup set for "Christina Alwell"). Oldest first, so if more than one
    happens to match, the one that's been waiting longest is handled first."""
    from .tickets import OPEN_STATUSES
    if not claimed_name:
        return []
    candidates = db.scalars(select(Ticket).where(Ticket.kind == "pickup", Ticket.status.in_(OPEN_STATUSES))
                            .order_by(Ticket.id)).all()
    return [t for t in candidates if _name_matches(db, claimed_name, t.task or {})]


def record_code_attempt(db, t: Ticket, code: str) -> str:
    """Checks `code` against t's stored hash, updating attempt/lock state as needed.
    Returns "ok", "wrong", "locked" (already locked, or just reached MAX_CODE_ATTEMPTS
    on this attempt), or "expired". Never reveals the location itself - that's the
    caller's job, only once this returns "ok"."""
    task = dict(t.task or {})
    if task.get("locked"):
        return "locked"
    expires_at = task.get("code_expires_at")
    if expires_at and datetime.fromisoformat(expires_at) < utcnow():
        return "expired"
    if code_matches(t.id, task, code):
        return "ok"
    task["code_attempts"] = task.get("code_attempts", 0) + 1
    locked_now = task["code_attempts"] >= MAX_CODE_ATTEMPTS
    if locked_now:
        task["locked"] = True
    t.task = task
    db.commit()
    return "locked" if locked_now else "wrong"
