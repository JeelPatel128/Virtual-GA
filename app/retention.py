"""Data retention: anonymizing visitor info and rolling up question logs once they age
out. Preview mode by default - every run computes what it WOULD change and stores that
report, but only actually writes anything if the admin "Enable retention" setting
(retention_enabled) is on. Called once a day from app/tickets.py::tick().

Phone purge (app/tickets.py::purge_old_phone_numbers) and the task-file sweep
(purge_old_task_files) already existed and already always ran for real - this module
only adds their day-counts to the same admin-settable periods, it doesn't change their
behavior or put them behind the preview toggle.
"""
import json
from datetime import timedelta

from sqlalchemy import select

from .config import settings
from .db import ChatSession, QueryLog, Ticket, TopicCount, kv_get, kv_set, utcnow

DEFAULT_VISITOR_DAYS = 180
DEFAULT_QUERY_LOG_DAYS = 180
DEFAULT_APPROVAL_LOG_DAYS = 365
DEFAULT_PHONE_DAYS = settings.PHONE_RETENTION_DAYS
DEFAULT_FILE_DAYS = 14

_PERIOD_DEFAULTS = {
    "visitor_days": DEFAULT_VISITOR_DAYS,
    "query_log_days": DEFAULT_QUERY_LOG_DAYS,
    "approval_log_days": DEFAULT_APPROVAL_LOG_DAYS,
    "phone_days": DEFAULT_PHONE_DAYS,
    "file_days": DEFAULT_FILE_DAYS,
}


def enabled(db) -> bool:
    return (kv_get(db, "retention_enabled") or "").strip().lower() in ("1", "true", "yes", "on")


def set_enabled(db, value: bool) -> None:
    kv_set(db, "retention_enabled", "true" if value else "false")
    db.commit()


def get_periods(db) -> dict:
    periods = {}
    for key, default in _PERIOD_DEFAULTS.items():
        raw = kv_get(db, f"retention_{key}")
        try:
            periods[key] = int(raw) if raw is not None else default
        except ValueError:
            periods[key] = default
    return periods


def set_periods(db, **periods) -> dict:
    """Keyword args are any of _PERIOD_DEFAULTS's keys; unknown keys are ignored."""
    for key, value in periods.items():
        if key in _PERIOD_DEFAULTS and value is not None:
            kv_set(db, f"retention_{key}", str(int(value)))
    db.commit()
    return get_periods(db)


def _mask_name(name: str | None) -> str:
    """"Alex Student" -> "A*** S*******" - enough to spot-check in the preview without
    actually showing a real name."""
    if not name:
        return ""
    return " ".join(w[0] + "*" * (len(w) - 1) if w else w for w in name.split())


def _identifying_tickets(db, cutoff, approval: bool):
    """Tickets older than cutoff whose session still has a visitor_name or whose own
    requester_email is still set, excluding ones already anonymized by a previous run
    (the replacement name, "Visitor #<id>", is itself non-null, so without this check
    an anonymized ticket would match the same filter forever and get "re-counted" every
    run). approval=True/False selects kind == "approval" or kind != "approval" - an
    approval request's requester identity is kept for the longer approval_log_days
    window (see get_periods), everything else for the shorter visitor_days window, so
    the two sweeps never target the same ticket."""
    kind_filter = Ticket.kind == "approval" if approval else Ticket.kind != "approval"
    rows = db.scalars(
        select(Ticket).join(ChatSession, Ticket.session_id == ChatSession.id)
        .where(Ticket.created_at < cutoff, kind_filter,
               (ChatSession.visitor_name.is_not(None)) | (Ticket.requester_email.is_not(None)))
    ).all()
    result = []
    for t in rows:
        sess = db.get(ChatSession, t.session_id)
        already_anon = t.requester_email is None and (sess is None or sess.visitor_name == f"Visitor #{t.id}")
        if not already_anon:
            result.append(t)
    return result


def _anonymize_ticket(db, t: Ticket, apply: bool) -> dict:
    sess = db.get(ChatSession, t.session_id)
    example = {"ticket_id": t.id, "kind": t.kind,
               "name": _mask_name(sess.visitor_name) if sess else "",
               "email": "set" if t.requester_email else ""}
    if apply:
        if sess is not None:
            sess.visitor_name = f"Visitor #{t.id}"
        t.requester_email = None
    return example


def preview_or_apply(db, apply: bool) -> dict:
    periods = get_periods(db)
    now = utcnow()
    report = {"applied": apply, "at": now.isoformat(), "periods": periods, "tables": {}}

    visitor_cutoff = now - timedelta(days=periods["visitor_days"])
    approval_cutoff = now - timedelta(days=periods["approval_log_days"])
    candidates = (_identifying_tickets(db, visitor_cutoff, approval=False) +
                  _identifying_tickets(db, approval_cutoff, approval=True))
    examples = [_anonymize_ticket(db, t, apply) for t in candidates]
    report["tables"]["sessions_tickets"] = {"count": len(candidates), "examples": examples[:5]}

    query_log_cutoff = now - timedelta(days=periods["query_log_days"])
    query_rows = db.scalars(select(QueryLog).where(QueryLog.created_at < query_log_cutoff)).all()
    q_examples = [{"question": (r.question or "")[:40] + ("..." if len(r.question or "") > 40 else ""),
                   "category": r.category or "Unanswered"} for r in query_rows[:5]]
    if apply and query_rows:
        counts: dict[tuple[str, str], int] = {}
        for r in query_rows:
            key = (r.category or "Unanswered", r.created_at.strftime("%Y-%m"))
            counts[key] = counts.get(key, 0) + 1
        for (category, month), n in counts.items():
            row = db.scalar(select(TopicCount).where(TopicCount.category == category, TopicCount.month == month))
            if row:
                row.count += n
            else:
                db.add(TopicCount(category=category, month=month, count=n))
        for r in query_rows:
            db.delete(r)
    report["tables"]["query_log"] = {"count": len(query_rows), "examples": q_examples}

    if apply:
        db.commit()
    kv_set(db, "retention_last_report", json.dumps(report))
    db.commit()
    return report


def run(db) -> dict:
    """Called once a day from tickets.tick(). Always computes a preview - the admin
    panel reflects what WOULD happen even while retention is off - and only actually
    mutates anything if the admin has turned retention_enabled on."""
    return preview_or_apply(db, apply=enabled(db))


def last_report(db) -> dict | None:
    raw = kv_get(db, "retention_last_report")
    return json.loads(raw) if raw else None
