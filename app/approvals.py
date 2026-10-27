"""Approval-gated resources: a Doc marked audience="approval_required" is never shown,
attached, linked or quoted to a visitor in any form (knowledge.search's allowlist already
guarantees this - see app/knowledge.py). A visitor can only request a copy, which a GA (or
a staff approver asked to by a GA) approves or declines through the normal ticket/reminder
pipeline (app/tickets.py)."""
import json
import logging
import re
import smtplib
from email.mime.text import MIMEText

from sqlalchemy import select

from . import tickets
from .config import settings
from .db import ApprovalLog, Doc, Person, Ticket, kv_get, kv_set

log = logging.getLogger("virtual_ga.approvals")

DEFAULT_APPROVER_NAMES = ["Josa Balisi", "Yadilsa Gomez", "Megan Maher"]

# Loose on purpose: a false positive here just offers "Request a copy" for the wrong
# thing (low-stakes, recoverable in conversation) rather than leaking anything - the
# actual safety property (never show the file) is enforced by knowledge.search's
# audience allowlist regardless of whether this regex matches well or not.
APPROVAL_INTENT_RE = re.compile(
    r"employment verification|on.?campus (job|employment) form|\bssn\b.*\bform\b|"
    r"\bform\b.*\bssn\b|verification form|isss form|\bthe form\b", re.I)

COMPLETED_FORM_RE = re.compile(
    r"\b(filled( it)?( out)?|completed|already (filled|have it)|signed|attach(ed|ing)?|upload(ed|ing)?)\b", re.I)

REFUSE_COMPLETED_FORM_TEXT = (
    "I can't take a completed copy of that form - it has your SEVIS ID and date of birth on "
    "it, so a filled-in version has to go to International Student and Scholar Services (ISSS) "
    "directly, or be brought in person. I can still request a blank copy for you if you need one.")


def find_requested_resource(db, text: str) -> Doc | None:
    """None if nothing approval-gated was asked about - a visitor's actual question
    never surfaces this Doc's content either way, since knowledge.search already
    excludes audience="approval_required" from every visitor-facing search."""
    if not APPROVAL_INTENT_RE.search(text):
        return None
    return db.scalars(select(Doc).where(Doc.audience == "approval_required")).first()


def get_approvers(db) -> list[Person]:
    raw = kv_get(db, "approvers")
    names = json.loads(raw) if raw else DEFAULT_APPROVER_NAMES
    people = []
    for name in names:
        p = db.scalar(select(Person).where(Person.name.ilike(name)))
        if p:
            people.append(p)
    return people


def set_approvers(db, names: list[str]) -> None:
    kv_set(db, "approvers", json.dumps(names))
    db.commit()


def approval_url(ticket_id: int) -> str:
    return f"{settings.PUBLIC_BASE_URL}/approve/{ticket_id}?t={tickets.ticket_token(ticket_id)}"


def send_staff_approval_email(staff: Person, t: Ticket) -> bool:
    """True if actually emailed. False (and the caller shows the link instead for the GA
    to forward manually) if SMTP isn't configured or sending fails - same
    degrade-gracefully pattern as the requester confirmation email."""
    if not (settings.SMTP_HOST and staff.email):
        return False
    resource_title = (t.task or {}).get("resource_title", "a resource")
    msg = MIMEText(
        f"Hi {staff.name.split()[0]},\n\n"
        f"A visitor at the SSB front desk has requested {resource_title}. "
        f"A GA is asking you to review this request.\n\n"
        f"Approve or decline here: {approval_url(t.id)}\n\n"
        "Thanks,\nStevens SSB Front Desk")
    msg["Subject"] = f"Approval needed: {resource_title}"
    msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
    msg["To"] = staff.email
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as s:
            s.starttls()
            if settings.SMTP_USER:
                s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception as e:
        log.warning("Staff approval email failed: %s", e)
        return False


def get_asked_staff_name(db, t: Ticket) -> str | None:
    """Who a GA most recently asked to approve this ticket, if anyone - used to
    attribute a decision made through the staff's signed link to the right person."""
    row = db.scalars(select(ApprovalLog).where(ApprovalLog.ticket_id == t.id, ApprovalLog.action == "asked_staff")
                     .order_by(ApprovalLog.id.desc())).first()
    return row.reason if row else None


def ask_staff_to_approve(db, t: Ticket, staff_name: str, ga_name: str) -> dict:
    """A manual, one-off GA action - never automatic, and the staff member asked never
    enters the reminder ladder (that only ever mentions team.json names, untouched by
    this function) or gets added anywhere persistent. Just this one link, once."""
    if t.kind != "approval":
        raise ValueError("Only an approval request can be sent to a staff approver.")
    approvers = {p.name: p for p in get_approvers(db)}
    staff = approvers.get(staff_name)
    if not staff:
        raise ValueError(f"{staff_name} isn't on the approvers list.")
    emailed = send_staff_approval_email(staff, t)
    db.add(ApprovalLog(ticket_id=t.id, resource_id=(t.task or {}).get("resource_id", ""),
                       actor=ga_name, action="asked_staff", reason=staff.name))
    db.commit()
    return {"link": approval_url(t.id), "emailed": emailed, "staff_email": staff.email}
