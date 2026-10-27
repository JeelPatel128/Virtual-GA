"""Purpose-scoped signed links for ticket-related pages (status, an approved download,
a staff approve/decline link). Replaces the single shared ticket_token() for these three
surfaces - that one token worked interchangeably for all of them (and for GA-console
actions), so a requester's own status link could, for example, also approve or decline
their own ticket. Each token here is an HMAC over (purpose, ticket_id, expires_at, nonce),
so a token minted for one purpose or one ticket is cryptographically rejected by any
other. "staff_approve" is additionally single-use, tracked in StaffApprovalLink since a
signature alone can't record "already spent" - see consume_staff_link."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select

from .config import settings
from .db import StaffApprovalLink, utcnow

PURPOSES = ("status", "download", "staff_approve")

DOWNLOAD_WINDOW_HOURS = 24
STAFF_APPROVE_WINDOW_HOURS = 72


def _sign(payload: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(purpose: str, ticket_id: int, expires_at: datetime | None = None, nonce: str = "") -> str:
    if purpose not in PURPOSES:
        raise ValueError(f"Unknown token purpose {purpose!r}")
    exp = str(int(expires_at.timestamp())) if expires_at else "0"
    base = f"{purpose}:{ticket_id}:{exp}:{nonce}"
    return f"{base}:{_sign(base)}"


def verify_token(token: str | None, purpose: str, ticket_id: int) -> bool:
    """Checks the signature, purpose, ticket_id and (if the token carries one) its
    embedded expiry. Does NOT check staff_approve single-use - see consume_staff_link,
    which needs a database session this function deliberately doesn't take."""
    if not token:
        return False
    parts = token.split(":")
    if len(parts) != 5:
        return False
    p, tid, exp, nonce, sig = parts
    base = f"{p}:{tid}:{exp}:{nonce}"
    if not hmac.compare_digest(_sign(base), sig):
        return False
    if p != purpose or tid != str(ticket_id):
        return False
    if exp != "0" and int(exp) < int(utcnow().timestamp()):
        return False
    return True


def token_nonce(token: str) -> str | None:
    parts = token.split(":")
    return parts[3] if len(parts) == 5 else None


def status_token(ticket_id: int) -> str:
    """Long-lived on purpose - the requester may come back to this link days later."""
    return make_token("status", ticket_id)


def download_token(ticket_id: int, approved_at: datetime) -> str:
    return make_token("download", ticket_id, approved_at + timedelta(hours=DOWNLOAD_WINDOW_HOURS))


def issue_staff_approval_link(db, ticket_id: int, staff_name: str) -> str:
    """Mints a new single-use, 72-hour token and records it so it can be checked (and
    later marked spent) independent of the token's own embedded expiry."""
    nonce = secrets.token_urlsafe(16)
    expires_at = utcnow() + timedelta(hours=STAFF_APPROVE_WINDOW_HOURS)
    db.add(StaffApprovalLink(ticket_id=ticket_id, nonce=nonce, staff_name=staff_name, expires_at=expires_at))
    db.commit()
    return make_token("staff_approve", ticket_id, expires_at, nonce)


def check_staff_link(db, token: str, ticket_id: int) -> StaffApprovalLink | None:
    """None if the token's signature/purpose/ticket/expiry don't check out, or if its
    specific link has already been used or expired server-side. Never marks it used -
    that only happens once a decision is actually submitted, so loading the page to look
    doesn't burn the one-time use."""
    if not verify_token(token, "staff_approve", ticket_id):
        return None
    nonce = token_nonce(token)
    link = db.scalar(select(StaffApprovalLink).where(StaffApprovalLink.nonce == nonce,
                                                      StaffApprovalLink.ticket_id == ticket_id))
    if not link or link.used or link.expires_at < utcnow():
        return None
    return link


def consume_staff_link(db, link: StaffApprovalLink) -> None:
    link.used = True
    db.commit()
