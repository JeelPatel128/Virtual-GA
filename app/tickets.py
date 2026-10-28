"""Handoffs, GA responses, 5-minute escalation to the lead, the daily digest and data cleanup."""
import csv
import io
import json
import logging
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from . import retention, schedule, storage, teams, tokens
from .config import lead_ga, load_team, settings
from .db import (ApprovalLog, ChatSession, Item, Message, MonthlyExport, QueryLog, ReminderLog, TaskFile, Ticket,
                 TopicCount, get_today_note, kv_get, kv_set, utcnow)

log = logging.getLogger("virtual_ga.tickets")
TZ = ZoneInfo(settings.TIMEZONE)
# Unified across all three kinds (visitor/message/task). `activity` carries the old
# visitor-facing nuance (on_the_way/busy/replied) that used to live in `status` itself.
OPEN_STATUSES = ("new", "claimed", "in_progress")


def ticket_url(ticket_id: int) -> str:
    return f"{settings.PUBLIC_BASE_URL}/ga/ticket/{ticket_id}?t={tokens.ga_action_token(ticket_id)}"


def local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)


def _to_naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def say(db, session_id: str, sender: str, text: str, ga_name: str | None = None, meta: dict | None = None) -> Message:
    m = Message(session_id=session_id, sender=sender, text=text, ga_name=ga_name, meta=meta)
    db.add(m)
    return m


def open_ticket_for(db, session_id: str) -> Ticket | None:
    return db.scalars(select(Ticket).where(Ticket.session_id == session_id, Ticket.status.in_(OPEN_STATUSES))
                      .order_by(Ticket.id.desc())).first()


# ---------- creating a handoff ----------

def create_ticket(db, sess: ChatSession, question: str) -> Ticket:
    on_shift = schedule.on_shift_now()
    lead = lead_ga()
    t = Ticket(session_id=sess.id, question=question.strip()[:2000], on_shift=[g.name for g in on_shift],
              next_deadline=utcnow() + timedelta(minutes=settings.ESCALATE_AFTER_MINUTES))
    db.add(t)
    db.flush()

    first = sess.visitor_name.split()[0] if sess.visitor_name else "there"
    if on_shift:
        names = " or ".join(g.name for g in on_shift)
        say(db, sess.id, "system",
            f"Thanks {first}. I've let {names} know you're waiting. You'll see their reply right here, "
            "so keep this page open.")
    else:
        nxt = schedule.next_shift()
        when = f" The next GA is in {nxt['start'].strftime('%A at %I:%M %p').replace(' 0', ' ')}." if nxt else ""
        follow = "We'll call you back at the number you left." if sess.visitor_phone else \
            "If you leave a phone number, the team can call you back."
        say(db, sess.id, "system",
            f"Thanks {first}. No GA is on the desk right now, but I've sent your question to the team lead. "
            f"{follow}{when}")
        t.escalated_at = utcnow()  # lead is alerted right away, no need to escalate again

    mention = list(on_shift)
    if lead and lead not in mention and (settings.LEAD_ALERT_ON_EVERY_HANDOFF or not on_shift):
        mention.append(lead)
    title = "Visitor waiting at the front desk" if on_shift else "Visitor question, no GA on shift"
    teams.post(teams.build_card(
        title=title,
        mention=mention,
        body_text=f"“{t.question}”",
        lines=[
            ("Visitor", sess.visitor_name or "Not given"),
            ("Callback number", "Left a number (see console)" if sess.visitor_phone else "None"),
            ("On shift", ", ".join(t.on_shift) or "Nobody"),
            ("Ticket", f"#{t.id} at {local(t.created_at).strftime('%I:%M %p')}"),
        ],
        buttons=[("Open and respond", ticket_url(t.id))],
        style="default" if on_shift else "urgent",
    ))
    db.commit()
    return t


def create_message_ticket(db, sess: ChatSession, question: str, person) -> Ticket:
    """"Leave a message" for a specific person, found through the directory resolver.
    Unlike a normal handoff, the deadline is the end of the current shift (not 5 minutes)
    since nobody needs to drop what they're doing - but it still goes through the exact
    same reminder ladder (check_reminders) so it can't be forgotten."""
    on_shift = schedule.on_shift_now()
    lead = lead_ga()
    shift_end = schedule.current_shift_end()
    deadline = _to_naive_utc(shift_end) if shift_end else utcnow() + timedelta(minutes=settings.ESCALATE_AFTER_MINUTES)
    t = Ticket(session_id=sess.id, question=question.strip()[:2000], on_shift=[g.name for g in on_shift],
              kind="message", recipient_person_id=person.id, next_deadline=deadline)
    db.add(t)
    db.flush()

    first = sess.visitor_name.split()[0] if sess.visitor_name else "there"
    say(db, sess.id, "system", f"Thanks {first}. I'll make sure a GA passes this message on to {person.name}.")
    if not on_shift:
        t.escalated_at = utcnow()  # nobody on shift: alert the lead immediately, same as a normal handoff

    mention = list(on_shift)
    if lead and lead not in mention and (settings.LEAD_ALERT_ON_EVERY_HANDOFF or not on_shift):
        mention.append(lead)
    teams.post(teams.build_card(
        title=f"Message for {person.name}",
        mention=mention,
        body_text=f"“{t.question}”",
        lines=[
            ("Visitor", sess.visitor_name or "Not given"),
            ("For", person.name),
            ("On shift", ", ".join(t.on_shift) or "Nobody"),
            ("Ticket", f"#{t.id} at {local(t.created_at).strftime('%I:%M %p')}"),
        ],
        buttons=[("Open and respond", ticket_url(t.id))],
        style="default" if on_shift else "urgent",
    ))
    db.commit()
    return t


TASK_TYPES = ("Print/copies", "Room setup", "Delivery/pickup", "Supplies/equipment", "Other")


def create_task_ticket(db, sess: ChatSession, name: str, email: str | None, task_type: str, details: str,
                       copies: int, due_at: datetime, pickup_location: str) -> Ticket:
    """"Request something from the GAs" - a task, not a handoff or a message. due_at is
    both stored for display and used directly as next_deadline, so it goes through the
    exact same reminder ladder as everything else."""
    if task_type not in TASK_TYPES:
        raise ValueError(f"Unknown task type {task_type!r}")
    on_shift = schedule.on_shift_now()
    lead = lead_ga()
    t = Ticket(session_id=sess.id, question=details.strip()[:2000], on_shift=[g.name for g in on_shift],
              kind="task", due_at=due_at, next_deadline=due_at, requester_email=(email or "").strip() or None,
              task={"type": task_type, "details": details.strip()[:2000], "copies": copies,
                    "pickup_location": pickup_location.strip()[:200]})
    db.add(t)
    db.flush()
    sess.visitor_name = name.strip()

    first = name.split()[0] if name.strip() else "there"
    say(db, sess.id, "system", f"Thanks {first}. Your request has been sent to the GA team.")
    if not on_shift:
        t.escalated_at = utcnow()

    mention = list(on_shift)
    if lead and lead not in mention and (settings.LEAD_ALERT_ON_EVERY_HANDOFF or not on_shift):
        mention.append(lead)
    teams.post(teams.build_card(
        title=f"Task request: {task_type}",
        mention=mention,
        body_text=details.strip(),
        lines=[
            ("Requested by", name.strip() or "Not given"),
            ("Copies", str(copies)),
            ("Pickup location", pickup_location.strip() or "Not given"),
            ("Due", local(due_at).strftime("%a %I:%M %p")),
            ("On shift", ", ".join(t.on_shift) or "Nobody"),
            ("Ticket", f"#{t.id}"),
        ],
        buttons=[("Open and respond", ticket_url(t.id))],
        style="default" if on_shift else "urgent",
    ))
    db.commit()
    return t


def create_approval_ticket(db, sess: ChatSession, name: str, email: str | None, department: str,
                           resource_id: str, resource_title: str) -> Ticket:
    """A request for an approval_required resource (app/approvals.py). No visitor-set
    due date, so - like a message ticket - the deadline is the end of the current shift,
    going through the exact same reminder ladder as everything else."""
    on_shift = schedule.on_shift_now()
    lead = lead_ga()
    shift_end = schedule.current_shift_end()
    deadline = _to_naive_utc(shift_end) if shift_end else utcnow() + timedelta(minutes=settings.ESCALATE_AFTER_MINUTES)
    t = Ticket(session_id=sess.id, question=f"Approval request: {resource_title}", on_shift=[g.name for g in on_shift],
              kind="approval", next_deadline=deadline, requester_email=(email or "").strip() or None,
              task={"resource_id": resource_id, "resource_title": resource_title, "department": department.strip()[:200]})
    db.add(t)
    db.flush()
    sess.visitor_name = name.strip()

    first = name.split()[0] if name.strip() else "there"
    say(db, sess.id, "system", f"Thanks {first}. Your request for {resource_title} has been sent to the GA team for approval.")
    if not on_shift:
        t.escalated_at = utcnow()

    mention = list(on_shift)
    if lead and lead not in mention and (settings.LEAD_ALERT_ON_EVERY_HANDOFF or not on_shift):
        mention.append(lead)
    teams.post(teams.build_card(
        title=f"Approval request: {resource_title}",
        mention=mention,
        lines=[
            ("Requested by", name.strip() or "Not given"),
            ("Department", department.strip() or "Not given"),
            ("On shift", ", ".join(t.on_shift) or "Nobody"),
            ("Ticket", f"#{t.id}"),
        ],
        buttons=[("Open and respond", ticket_url(t.id))],
        style="default" if on_shift else "urgent",
    ))
    db.commit()
    return t


# ---------- ownership (Reliability pack) ----------

def claim_ticket(db, ticket_id: int, ga_name: str) -> bool:
    """True if this GA just became the owner; False if someone else already claimed it
    first. Enforced by the database itself - a single conditional UPDATE - not an
    application-level lock: whichever request the database engine applies first is the
    only one whose WHERE still matches `owner IS NULL`. Only that unconditional
    owner/status write is inside the atomic statement; owner_history is a list-append,
    which must never be built from a pre-read value that could be stale under a race -
    so it's written as a separate step, and only once the atomic claim is confirmed to
    have actually succeeded."""
    t = db.get(Ticket, ticket_id)
    if t is None:
        raise ValueError("Ticket not found")
    values = {"owner": ga_name}
    if t.status == "new":
        values["status"] = "claimed"
    result = db.execute(update(Ticket).where(Ticket.id == ticket_id, Ticket.owner.is_(None)).values(**values))
    db.commit()
    if result.rowcount != 1:
        return False
    db.refresh(t)
    t.owner_history = (t.owner_history or []) + [
        {"ga": ga_name, "assigned_at": utcnow().isoformat(), "unassigned_at": None, "reason": "claimed"}]
    db.commit()
    return True


def reassign_ticket(db, t: Ticket, to_ga: str, reason: str) -> None:
    """Hands ownership to someone else outright (used by shift handoff, the 3rd-snooze
    rule, and a manual reassign) - no claim race here since the caller already holds
    (or is acting on behalf of) the current owner."""
    now = utcnow()
    history = list(t.owner_history or [])
    if history and history[-1].get("unassigned_at") is None:
        history[-1]["unassigned_at"] = now.isoformat()
    history.append({"ga": to_ga, "assigned_at": now.isoformat(), "unassigned_at": None, "reason": reason})
    t.owner = to_ga
    t.owner_history = history
    db.commit()


# ---------- GA responses from the console ----------

def _is_lead(ga_name: str) -> bool:
    lead = lead_ga()
    return bool(lead and lead.name == ga_name)


SNOOZE_OPTIONS = ("30min", "2h", "end_of_shift", "tomorrow_10am")
SNOOZE_LABELS = {"30min": "30 minutes", "2h": "2 hours", "end_of_shift": "the end of their shift",
                "tomorrow_10am": "tomorrow at 10 AM"}


def _snooze_deadline(choice: str, ga_name: str) -> datetime:
    now_local = datetime.now(TZ)
    if choice == "30min":
        return utcnow() + timedelta(minutes=30)
    if choice == "2h":
        return utcnow() + timedelta(hours=2)
    if choice == "end_of_shift":
        end = schedule.shift_end_for(ga_name, now_local)
        return _to_naive_utc(end) if end else utcnow() + timedelta(hours=4)
    if choice == "tomorrow_10am":
        tomorrow = (now_local + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        return _to_naive_utc(tomorrow)
    raise ValueError(f"Unknown snooze option {choice!r}")


def end_of_today() -> datetime:
    """A borrowed item's return deadline - today, end of day, local time."""
    end_local = datetime.now(TZ).replace(hour=23, minute=59, second=0, microsecond=0)
    return _to_naive_utc(end_local)


def ga_action(db, t: Ticket, ga_name: str, action: str, minutes: int | None = None, text: str | None = None,
             snooze_until: str | None = None, reason: str | None = None) -> None:
    """Taking any action implies ownership: an unclaimed ticket is auto-claimed to
    whoever acts on it first (claim_ticket is still atomic, so a genuine race between
    two GAs both calling an action has the same single-winner guarantee as an explicit
    claim). Once owned, only the owner or the lead may act further - everyone else gets
    a clear error rather than silently stepping on the current owner."""
    sess = db.get(ChatSession, t.session_id)
    ga_first = ga_name.split()[0]

    if t.owner is None:
        claim_ticket(db, t.id, ga_name)
        db.refresh(t)
    if t.owner != ga_name and not _is_lead(ga_name):
        raise ValueError(f"{t.owner} owns this ticket. Only they or the lead can act on it.")

    if t.first_response_at is None and action != "note":
        t.first_response_at = utcnow()
        t.responded_by = ga_name

    if action == "on_my_way":
        t.status, t.activity = "in_progress", "on_the_way"
        t.next_deadline = utcnow() + timedelta(minutes=(minutes or 0) + 5)
        t.last_reminder_step = 0
        eta = f" in about {minutes} minutes" if minutes else " shortly"
        say(db, t.session_id, "system", f"{ga_first} is on the way to the front desk and will be with you{eta}.")
    elif action == "busy":
        if snooze_until not in SNOOZE_OPTIONS:
            raise ValueError("Pick a follow-up time: 30 minutes, 2 hours, end of shift, or tomorrow 10 AM.")
        if not reason or not reason.strip():
            raise ValueError("A reason is required when snoozing.")
        t.status, t.activity = "in_progress", "busy"
        t.next_deadline = _snooze_deadline(snooze_until, ga_name)
        t.last_reminder_step = 0
        t.snooze_count += 1
        how = "call you back at the number you left" if sess.visitor_phone else \
            "get back to you. Please leave a phone number below so they can reach you"
        say(db, t.session_id, "system", f"{ga_first} is tied up with something right now and will {how}.")
        if t.snooze_count >= 3:
            lead = lead_ga()
            if lead and lead.name != t.owner:
                reassign_ticket(db, t, lead.name, f"auto-reassigned after {t.snooze_count} snoozes")
                say(db, t.session_id, "system",
                    "This has been handed to the lead GA after being delayed a few times.")
    elif action == "reply":
        if not text or not text.strip():
            raise ValueError("Reply is empty.")
        t.status, t.activity = "in_progress", "replied"
        say(db, t.session_id, "ga", text.strip()[:2000], ga_name=ga_name)
    elif action == "resolve":
        t.status = "done"
        t.resolved_at = utcnow()
        say(db, t.session_id, "system", "This request is marked as resolved. Thanks for stopping by! "
                                        "You can keep asking me questions anytime.")
    elif action in ("approve", "decline", "hand_in_person"):
        if t.kind != "approval":
            raise ValueError("That action is only for an approval request.")
        apply_approval_decision(db, t, ga_name, {"approve": "approved", "decline": "declined",
                                                 "hand_in_person": "handed_in_person"}[action], reason)
    else:
        raise ValueError(f"Unknown action {action}")
    if t.status in ("done", "cancelled"):
        _clear_task_file(db, t)
    if t.status == "done" and t.requester_email and t.confirmed is None:
        send_requester_confirmation_email(t)
    db.commit()


def apply_approval_decision(db, t: Ticket, actor: str, decision: str, reason: str | None = None) -> None:
    """Shared by ga_action's approve/decline/hand_in_person branches (actor = a GA's
    name) and the staff signed-link route in app/approvals.py (actor = a staff
    approver's name, which never goes through ga_action's owner/lead gate - the signed,
    ticket-specific link IS the authorization). Always writes one ApprovalLog row."""
    if decision == "approved":
        t.status = "done"
        t.resolved_at = utcnow()
        t.task = {**(t.task or {}), "handoff_method": "digital"}
        say(db, t.session_id, "system",
            f"Your request for {(t.task or {}).get('resource_title', 'this resource')} has been approved. "
            "Check your status link for the download - it's available for 24 hours.")
    elif decision == "declined":
        if not reason or not reason.strip():
            raise ValueError("A reason is required to decline.")
        t.status = "cancelled"
        t.cancelled_reason = reason.strip()[:500]
        say(db, t.session_id, "system", "Your request was declined. See your status link for the reason.")
    elif decision == "handed_in_person":
        t.status = "done"
        t.resolved_at = utcnow()
        t.task = {**(t.task or {}), "handoff_method": "in_person"}
        say(db, t.session_id, "system", "This was handed to you in person.")
    else:
        raise ValueError(f"Unknown decision {decision!r}")
    db.add(ApprovalLog(ticket_id=t.id, resource_id=(t.task or {}).get("resource_id", ""),
                       actor=actor, action=decision, reason=reason))
    db.commit()


def _clear_task_file(db, t: Ticket) -> None:
    """File privacy: once a task ticket is done or cancelled, its attachment is removed
    from wherever it lives - a visitor's print job or ID photo never lingers after the
    GAs no longer need it."""
    if not t.file_backend:
        return
    if t.file_backend == "supabase" and t.file_ref:
        storage.delete(t.file_ref)
    elif t.file_backend == "local" and t.file_ref:
        tf = db.get(TaskFile, int(t.file_ref))
        if tf:
            db.delete(tf)
    t.file_backend = t.file_ref = None


def purge_old_task_files(db, days: int | None = None) -> int:
    """Backstop sweep: clear any task attachment older than `days` (an admin-settable
    retention period, see app/retention.py, default 14) regardless of status, in case a
    ticket is left open indefinitely."""
    cutoff = utcnow() - timedelta(days=days if days is not None else retention.DEFAULT_FILE_DAYS)
    rows = db.scalars(select(Ticket).where(Ticket.file_backend.is_not(None),
                                           Ticket.created_at < cutoff)).all()
    for t in rows:
        _clear_task_file(db, t)
    db.commit()
    return len(rows)


def confirm_reached(db, t: Ticket, reached: bool) -> None:
    """The visitor's answer to "did a GA reach you?" once an on-my-way ETA has passed.
    Yes closes the ticket; No escalates to the lead immediately rather than waiting for
    the reminder ladder to eventually catch up on the now-overdue deadline."""
    t.confirmed, t.confirmed_at = reached, utcnow()
    if reached:
        t.status = "done"
        t.resolved_at = utcnow()
        say(db, t.session_id, "system", "Glad they found you! This request is now marked as resolved.")
    else:
        t.next_deadline = utcnow()
        t.last_reminder_step = 0
        lead = lead_ga()
        teams.post(teams.build_card(
            title="Visitor says the GA never reached them",
            mention=[lead] if lead else [],
            body_text=f"“{t.question}”",
            lines=[("Ticket", f"#{t.id}")],
            buttons=[("Open and respond", ticket_url(t.id))],
            style="urgent",
        ))
        say(db, t.session_id, "system", "Sorry about that. I've flagged this to the lead GA right away.")
    db.commit()


def status_url(ticket_id: int) -> str:
    return f"{settings.PUBLIC_BASE_URL}/status/{ticket_id}?t={tokens.status_token(ticket_id)}"


def send_requester_confirmation_email(t: Ticket) -> None:
    """Emails the requester a link to say whether their task was taken care of. Skips
    quietly if SMTP isn't configured or there's no requester email - same
    degrade-gracefully pattern as app/llm.py without a Gemini key."""
    if not (settings.SMTP_HOST and t.requester_email):
        return
    msg = MIMEText(
        "Hi,\n\nYour request to the Stevens School of Business front desk has been marked done.\n\n"
        f"Was it taken care of? Let us know here: {status_url(t.id)}\n\n"
        "Thanks,\nStevens SSB Front Desk")
    msg["Subject"] = "Your SSB front desk request is marked done"
    msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
    msg["To"] = t.requester_email
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as s:
            s.starttls()
            if settings.SMTP_USER:
                s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)
    except Exception as e:
        log.warning("Requester confirmation email failed: %s", e)


def confirm_requester(db, t: Ticket, confirmed: bool) -> None:
    """The requester's own "was this taken care of" answer on the signed /status page,
    asked once a task is done. No reopens the ticket to the lead right away rather than
    waiting for the (by now stopped) reminder ladder to notice anything is wrong."""
    t.confirmed, t.confirmed_at = confirmed, utcnow()
    if not confirmed:
        t.status, t.activity = "in_progress", None
        t.next_deadline = utcnow()
        t.last_reminder_step = 0
        lead = lead_ga()
        if lead:
            reassign_ticket(db, t, lead.name, "requester says this wasn't taken care of")
        teams.post(teams.build_card(
            title="Reopened: requester says this wasn't taken care of",
            mention=[lead] if lead else [],
            body_text=f"“{t.question}”",
            lines=[("Ticket", f"#{t.id}")],
            buttons=[("Open and respond", ticket_url(t.id))],
            style="urgent",
        ))
    db.commit()


# ---------- periodic work, called by an external cron every minute ----------

REMINDER_LABELS = {1: "Reminder", 2: "Still waiting", 3: "Escalated to lead", 4: "Team alert"}


def _ga_by_name(name: str | None):
    return next((g for g in load_team() if g.name == name), None) if name else None


def _reminder_targets(t: Ticket) -> list:
    """Steps 1-2 target the actual owner if claimed; if unclaimed, whoever is on shift
    *right now* (live lookup, not the stale on_shift snapshot from creation) - or the
    lead if nobody's on shift at all. Step 3 is always the lead, step 4 is always every
    GA in team.json - this function (and everywhere else in this module) only ever
    builds a mention list from team.json, never from the directory (SSB staff)."""
    lead = lead_ga()
    if t.owner:
        ga = _ga_by_name(t.owner)
        return [ga] if ga else ([lead] if lead else [])
    on_shift = schedule.on_shift_now()
    return on_shift if on_shift else ([lead] if lead else [])


def check_reminders(db) -> int:
    """The reminder ladder: every open ticket's next_deadline is checked against the
    current time, one tick at a time. If several steps are overdue at once (e.g. the
    server slept through some ticks), exactly one card is sent, at the *highest* overdue
    step, naming which earlier steps were missed - never one card per missed step."""
    now = utcnow()
    candidates = db.scalars(select(Ticket).where(Ticket.status.in_(OPEN_STATUSES),
                                                  Ticket.next_deadline.is_not(None))).all()
    fired = 0
    for t in candidates:
        if t.next_deadline > now:
            continue
        overdue_minutes = (now - t.next_deadline).total_seconds() / 60
        step = min(4, 1 + int(overdue_minutes // settings.REMINDER_STEP_MINUTES))
        if step <= t.last_reminder_step:
            continue
        missed = list(range(t.last_reminder_step + 1, step))
        missed_note = f" (missed step{'s' if len(missed) > 1 else ''} {', '.join(map(str, missed))})" if missed else ""
        lead_name = "Unclaimed" if not t.owner else t.owner
        targets = _reminder_targets(t)
        teams.post(teams.build_card(
            title=f"{REMINDER_LABELS[step]}{missed_note}",
            mention=targets,
            body_text=f"“{t.question}”",
            lines=[("Owner", lead_name), ("Ticket", f"#{t.id}")],
            buttons=[("Open and respond", ticket_url(t.id))],
            style="default" if step == 1 else "urgent",
        ))
        for g in targets:
            db.add(ReminderLog(ticket_id=t.id, ga_name=g.name, step=step))
        if t.escalated_at is None:
            t.escalated_at = now
        t.last_reminder_step = step
        fired += 1
    db.commit()
    return fired


def purge_old_phone_numbers(db, days: int | None = None) -> int:
    """`days` is an admin-settable retention period, see app/retention.py, default
    PHONE_RETENTION_DAYS."""
    cutoff = utcnow() - timedelta(days=days if days is not None else settings.PHONE_RETENTION_DAYS)
    rows = db.scalars(select(ChatSession).where(ChatSession.visitor_phone.is_not(None),
                                                ChatSession.created_at < cutoff)).all()
    for s in rows:
        s.visitor_phone = None
    db.commit()
    return len(rows)


def digest_data(db, day_local: datetime) -> dict:
    start_local = day_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start = start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end = start + timedelta(days=1)
    q = db.scalars(select(QueryLog).where(QueryLog.created_at >= start, QueryLog.created_at < end)).all()
    tk = db.scalars(select(Ticket).where(Ticket.created_at >= start, Ticket.created_at < end)).all()
    resp_mins = [(t.first_response_at - t.created_at).total_seconds() / 60 for t in tk if t.first_response_at]
    unanswered = [x.question for x in q if not x.confident]

    # cross-cutting, not scoped to today: whatever is open right now, system-wide
    open_now = db.scalars(select(Ticket).where(Ticket.status.in_(OPEN_STATUSES))).all()
    open_by_owner: dict[str, int] = {}
    for t in open_now:
        key = t.owner or "Unclaimed"
        open_by_owner[key] = open_by_owner.get(key, 0) + 1
    overdue = sum(1 for t in open_now if t.next_deadline and t.next_deadline <= utcnow())
    done_today = db.scalar(select(func.count()).select_from(Ticket)
                           .where(Ticket.resolved_at >= start, Ticket.resolved_at < end))

    low_or_out = db.scalars(select(Item).where(Item.active.is_(True), Item.status.in_(("low", "out")))).all()
    inventory_alerts = [{"item": it.item, "area": it.area, "status": it.status, "on_order": it.on_order}
                        for it in low_or_out]
    lend_not_returned = [{"item": t.question,
                          "due": local(t.due_at).strftime("%a %I:%M %p") if t.due_at else None}
                         for t in open_now if t.kind == "task" and (t.task or {}).get("type") == "Supplies/equipment"]
    return {
        "date": start_local.strftime("%A, %b %d"),
        "questions": len(q),
        "answered_by_bot": sum(1 for x in q if x.confident),
        "handoffs": len(tk),
        "escalated": sum(1 for t in tk if t.escalated_at and t.on_shift),
        "still_open": sum(1 for t in tk if t.status in OPEN_STATUSES),
        "avg_response_min": round(sum(resp_mins) / len(resp_mins), 1) if resp_mins else None,
        "unanswered": unanswered,
        "answered_samples": [x.question for x in q if x.confident][:10],
        "open_by_owner": open_by_owner,
        "overdue": overdue,
        "done_today": done_today,
        "inventory_alerts": inventory_alerts,
        "lend_not_returned": lend_not_returned,
    }


def send_digest(db, day_local: datetime | None = None) -> dict:
    d = digest_data(db, day_local or datetime.now(TZ))
    lead = lead_ga()
    gap_list = "\n".join(f"- {u}" for u in d["unanswered"][:15]) or "None. Nice."
    answered = "\n".join(f"- {a}" for a in d["answered_samples"]) or "None"
    owner_list = "\n".join(f"- {k}: {v}" for k, v in d["open_by_owner"].items()) or "None"
    inv_list = "\n".join(f"- {a['item']} ({a['area']}): {a['status']}" + (" - on order" if a["on_order"] else "")
                        for a in d["inventory_alerts"]) or "None"
    lend_list = "\n".join(f"- {x['item']} - due {x['due']}" for x in d["lend_not_returned"]) or "None"
    teams.post(teams.build_card(
        title=f"Virtual GA daily digest: {d['date']}",
        mention=[lead] if lead else [],
        lines=[
            ("Questions asked", str(d["questions"])),
            ("Answered by the bot", str(d["answered_by_bot"])),
            ("Handed to a GA", str(d["handoffs"])),
            ("Escalated (5 min, no reply)", str(d["escalated"])),
            ("Still open", str(d["still_open"])),
            ("Overdue right now", str(d["overdue"])),
            ("Done today", str(d["done_today"])),
            ("Avg GA response", f"{d['avg_response_min']} min" if d["avg_response_min"] is not None else "n/a"),
        ],
        body_text=f"**Open items by owner:**\n{owner_list}\n\n"
                  f"**Low/out of stock:**\n{inv_list}\n\n"
                  f"**Borrowed items not yet returned:**\n{lend_list}\n\n"
                  f"**Questions the bot couldn't answer (add these to the knowledge base):**\n{gap_list}\n\n"
                  f"**Sample of questions it handled:**\n{answered}",
        buttons=[("Open admin", f"{settings.PUBLIC_BASE_URL}/admin")],
    ))
    return d


def weekly_report_data(db, now_local: datetime) -> dict:
    """Per-GA metrics over the trailing 7 days. Reassignments-away and missed deadlines
    come from durable logs that already exist (owner_history on each ticket, and
    ReminderLog) rather than a point-in-time snapshot; snoozes/time-to-done are
    attributed to each ticket's current/final owner, since there's no separate
    per-snooze log."""
    cutoff = utcnow() - timedelta(days=7)
    names = [g.name for g in load_team()]
    reassigned_away = {n: 0 for n in names}
    for t in db.scalars(select(Ticket)).all():
        hist = t.owner_history or []
        for i in range(1, len(hist)):
            prev_ga, at = hist[i - 1].get("ga"), hist[i].get("assigned_at")
            if prev_ga in reassigned_away and at and datetime.fromisoformat(at) >= cutoff:
                reassigned_away[prev_ga] += 1

    snoozes = {n: 0 for n in names}
    time_to_done: dict[str, list] = {n: [] for n in names}
    for t in db.scalars(select(Ticket).where(Ticket.created_at >= cutoff)).all():
        if t.owner in snoozes:
            snoozes[t.owner] += t.snooze_count
    for t in db.scalars(select(Ticket).where(Ticket.status == "done", Ticket.resolved_at >= cutoff)).all():
        if t.owner in time_to_done:
            time_to_done[t.owner].append((t.resolved_at - t.created_at).total_seconds() / 60)

    missed_deadlines = {n: 0 for n in names}
    for name, count in db.execute(select(ReminderLog.ga_name, func.count())
                                  .where(ReminderLog.fired_at >= cutoff).group_by(ReminderLog.ga_name)).all():
        if name in missed_deadlines:
            missed_deadlines[name] = count

    return {
        "names": names,
        "reassigned_away": reassigned_away,
        "snoozes": snoozes,
        "missed_deadlines": missed_deadlines,
        "avg_time_to_done_min": {n: round(sum(v) / len(v), 1) if v else None for n, v in time_to_done.items()},
    }


def send_weekly_report(db, now_local: datetime | None = None) -> dict:
    d = weekly_report_data(db, now_local or datetime.now(TZ))
    lead = lead_ga()
    rows = "\n".join(
        f"- **{n}**: {d['missed_deadlines'][n]} missed deadline(s), {d['snoozes'][n]} snooze(s), "
        f"{d['reassigned_away'][n]} reassigned away, "
        f"avg time to done {d['avg_time_to_done_min'][n]} min" if d['avg_time_to_done_min'][n] is not None
        else f"- **{n}**: {d['missed_deadlines'][n]} missed deadline(s), {d['snoozes'][n]} snooze(s), "
             f"{d['reassigned_away'][n]} reassigned away, nothing closed this week"
        for n in d["names"]
    ) or "No GAs configured."
    teams.post(teams.build_card(
        title="Weekly GA report",
        lines=[],
        mention=[lead] if lead else [],
        body_text=rows,
        buttons=[("Open admin", f"{settings.PUBLIC_BASE_URL}/admin")],
    ))
    return d


def maybe_send_weekly_report(db, now: datetime) -> bool:
    """Monday only, once per ISO week - tracked in KV the same way the daily digest
    tracks its once-a-day flag."""
    if now.weekday() != 0:
        return False
    week = f"{now.isocalendar().year}-W{now.isocalendar().week}"
    if kv_get(db, "weekly_report_week") == week:
        return False
    send_weekly_report(db, now)
    kv_set(db, "weekly_report_week", week)
    return True


def monthly_export_data(db, month_start_local: datetime) -> dict:
    """The PREVIOUS calendar month's stats, aggregate counts only - never a name, email,
    or phone number, so the result is always safe to export. Top topics merges QueryLog
    (most of the month's rows - the retention job's query_log_days default, 180 days,
    is far longer than a month) with TopicCount (in case an admin shortened that period
    enough to already have rolled some of the month up)."""
    this_month_start = month_start_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start_local = (this_month_start - timedelta(days=1)).replace(day=1)
    end_local = this_month_start
    start, end = (d.astimezone(ZoneInfo("UTC")).replace(tzinfo=None) for d in (start_local, end_local))
    month_key = start_local.strftime("%Y-%m")

    q = db.scalars(select(QueryLog).where(QueryLog.created_at >= start, QueryLog.created_at < end)).all()
    tk = db.scalars(select(Ticket).where(Ticket.created_at >= start, Ticket.created_at < end)).all()
    resp_mins = [(t.first_response_at - t.created_at).total_seconds() / 60 for t in tk if t.first_response_at]

    topics: dict[str, int] = {}
    for x in q:
        cat = x.category or "Unanswered"
        topics[cat] = topics.get(cat, 0) + 1
    for row in db.scalars(select(TopicCount).where(TopicCount.month == month_key)).all():
        topics[row.category] = topics.get(row.category, 0) + row.count

    return {
        "month": month_key,
        "questions": len(q),
        "answered_by_bot_pct": round(100 * sum(1 for x in q if x.confident) / len(q), 1) if q else None,
        "handoffs": len(tk),
        "avg_response_min": round(sum(resp_mins) / len(resp_mins), 1) if resp_mins else None,
        "tasks": sum(1 for t in tk if t.kind == "task"),
        "escalations": sum(1 for t in tk if t.escalated_at and t.on_shift),
        "top_topics": sorted(topics.items(), key=lambda x: x[1], reverse=True)[:10],
    }


def monthly_export_csv(d: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["month", "questions", "answered_by_bot_pct", "handoffs", "avg_response_min",
               "tasks", "escalations"])
    w.writerow([d["month"], d["questions"], d["answered_by_bot_pct"], d["handoffs"],
               d["avg_response_min"], d["tasks"], d["escalations"]])
    w.writerow([])
    w.writerow(["top_topic", "count"])
    for topic, count in d["top_topics"]:
        w.writerow([topic, count])
    return buf.getvalue()


def maybe_run_monthly_export(db, now: datetime) -> bool:
    """The 1st of the month only, once per calendar month - same once-per-period KV
    pattern as the daily digest and weekly report."""
    if now.day != 1:
        return False
    month_key = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    if kv_get(db, "monthly_export_month") == month_key:
        return False
    data = monthly_export_data(db, now)
    db.add(MonthlyExport(month=data["month"], csv_text=monthly_export_csv(data)))
    kv_set(db, "monthly_export_month", month_key)
    db.commit()
    return True


def _next_owner_for_handoff() -> str | None:
    on_shift = schedule.on_shift_now()
    if on_shift:
        return on_shift[0].name
    lead = lead_ga()
    return lead.name if lead else None


def check_shift_ends(db) -> int:
    """15 minutes before a GA's shift ends, they get a heads-up card listing their open
    owned items (sent once per GA per shift-end, tracked in KV so it doesn't repeat every
    tick during that window). At actual shift end, an item is only reassigned if its
    deadline falls before the owner's own NEXT shift start - they genuinely can't make it
    back in time. An item they'll be back for in time stays with them (still shown in the
    heads-up card for visibility, not silently handed off).

    shift_end_for() only finds a shift while it's still active, so by the moment a shift
    has actually ended it can no longer be looked up that way. The end time is persisted
    in KV while still observable and reused once it drops out of "currently active" -
    without this, the actual-end-time branch below would never fire at all."""
    now_local = datetime.now(TZ)
    now = utcnow()
    owners = {t.owner for t in db.scalars(
        select(Ticket).where(Ticket.status.in_(OPEN_STATUSES), Ticket.owner.is_not(None))).all()}
    acted = 0
    for ga_name in owners:
        owned = db.scalars(select(Ticket).where(Ticket.status.in_(OPEN_STATUSES), Ticket.owner == ga_name)).all()
        if not owned:
            continue

        end_key = f"shiftend:{ga_name}"
        shift_end_local = schedule.shift_end_for(ga_name, now_local)
        if shift_end_local:
            shift_end_utc = _to_naive_utc(shift_end_local)
            kv_set(db, end_key, shift_end_utc.isoformat())
        else:
            stored = kv_get(db, end_key)
            if not stored:
                continue
            shift_end_utc = datetime.fromisoformat(stored)
        minutes_to_end = (shift_end_utc - now).total_seconds() / 60

        headsup_key = f"shiftend_headsup:{ga_name}:{shift_end_utc.isoformat()}"
        if 0 <= minutes_to_end <= 15 and not kv_get(db, headsup_key):
            ga = _ga_by_name(ga_name)
            lines = [(f"#{t.id}", t.question[:80]) for t in owned]
            teams.post(teams.build_card(
                title=f"Your shift ends in {int(minutes_to_end)} min - {len(owned)} open item(s)",
                mention=[ga] if ga else [],
                lines=lines,
                buttons=[("Open and hand off or reschedule", ticket_url(t.id)) for t in owned[:5]],
            ))
            kv_set(db, headsup_key, "1")
            acted += 1

        if minutes_to_end <= 0:
            next_shift = schedule.next_shift_for(ga_name, now_local)
            next_start_utc = _to_naive_utc(next_shift["start"]) if next_shift else None
            for t in owned:
                cant_make_it = t.next_deadline and (next_start_utc is None or t.next_deadline < next_start_utc)
                if cant_make_it:
                    to_ga = _next_owner_for_handoff()
                    if to_ga and to_ga != ga_name:
                        reassign_ticket(db, t, to_ga, f"shift ended for {ga_name}, deadline before their next shift")
                        say(db, t.session_id, "system", "Your request has been handed off to another GA.")
                        acted += 1
    db.commit()
    return acted


def check_task_heads_up(db) -> int:
    """30 minutes before a task's due time, a courtesy nudge goes to its owner (or
    whoever's on shift if unclaimed) - separate from the reminder ladder's escalation
    chain, which still runs independently off the same next_deadline/due_at."""
    now = utcnow()
    tasks = db.scalars(select(Ticket).where(Ticket.kind == "task", Ticket.status.in_(OPEN_STATUSES),
                                            Ticket.due_at.is_not(None))).all()
    sent = 0
    for t in tasks:
        minutes_to_due = (t.due_at - now).total_seconds() / 60
        if not (0 <= minutes_to_due <= 30):
            continue
        key = f"task_headsup:{t.id}"
        if kv_get(db, key):
            continue
        teams.post(teams.build_card(
            title=f"Task due in {int(minutes_to_due)} min",
            mention=_reminder_targets(t),
            body_text=t.question,
            lines=[("Ticket", f"#{t.id}")],
            buttons=[("Open and respond", ticket_url(t.id))],
        ))
        kv_set(db, key, "1")
        sent += 1
    db.commit()
    return sent


def check_shift_starts(db) -> int:
    """A GA newly appearing in on_shift_now() since the last tick (diffed against a KV
    snapshot) gets a start-of-shift card: what's currently open, owned by whom, and its
    deadline, and reminds them to keep the "Today at SSB" note current. The very first
    tick this app has ever run (no snapshot yet) only saves the baseline and sends
    nothing - otherwise every GA already on shift at deploy time would get a false
    "you just started" card."""
    current = {g.name for g in schedule.on_shift_now()}
    stored = kv_get(db, "on_shift_snapshot")
    kv_set(db, "on_shift_snapshot", json.dumps(sorted(current)))
    db.commit()
    if stored is None:
        return 0
    newly_started = current - set(json.loads(stored))
    if not newly_started:
        return 0
    open_tickets = db.scalars(select(Ticket).where(Ticket.status.in_(OPEN_STATUSES))).all()
    lines = [(f"#{t.id}", f"{t.owner or 'Unclaimed'} - {t.question[:50]}") for t in open_tickets[:10]]
    note = get_today_note(db)
    note_line = f"Today at SSB: {note}" if note else "No 'Today at SSB' note set yet - add one in the GA console."
    sent = 0
    for ga_name in newly_started:
        ga = _ga_by_name(ga_name)
        teams.post(teams.build_card(
            title="Shift starting: here's what's open",
            mention=[ga] if ga else [],
            lines=lines or [("Nothing open", "Quiet desk")],
            body_text=note_line,
            buttons=[("Open GA console", f"{settings.PUBLIC_BASE_URL}/ga")],
        ))
        sent += 1
    return sent


def tick(db) -> dict:
    result = {"escalated": check_reminders(db), "digest_sent": False, "phones_purged": 0, "task_files_purged": 0,
              "weekly_report_sent": False, "retention": None, "monthly_export_run": False}
    result["shift_ends_handled"] = check_shift_ends(db)
    result["shift_starts_handled"] = check_shift_starts(db)
    result["task_headsups_sent"] = check_task_heads_up(db)
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if now.hour >= settings.DIGEST_HOUR and kv_get(db, "digest_date") != today:
        send_digest(db, now)
        kv_set(db, "digest_date", today)
        result["digest_sent"] = True
        periods = retention.get_periods(db)
        result["phones_purged"] = purge_old_phone_numbers(db, days=periods["phone_days"])  # once a day is plenty
        result["task_files_purged"] = purge_old_task_files(db, days=periods["file_days"])  # once a day is plenty
        result["weekly_report_sent"] = maybe_send_weekly_report(db, now)
        result["monthly_export_run"] = maybe_run_monthly_export(db, now)
        result["retention"] = retention.run(db)
        db.commit()
    return result


def stats(db) -> dict:
    return {
        "open_tickets": db.scalar(select(func.count()).select_from(Ticket).where(Ticket.status.in_(OPEN_STATUSES))),
        "team": [{"name": g.name, "lead": g.lead, "teams_email": g.teams_email} for g in load_team()],
    }
