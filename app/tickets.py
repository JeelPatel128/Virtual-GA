"""Handoffs, GA responses, 5-minute escalation to the lead, the daily digest and data cleanup."""
import hashlib
import hmac
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from . import schedule, teams
from .config import lead_ga, load_team, settings
from .db import ChatSession, Message, QueryLog, Ticket, kv_get, kv_set, utcnow

TZ = ZoneInfo(settings.TIMEZONE)
# Unified across all three kinds (visitor/message/task). `activity` carries the old
# visitor-facing nuance (on_the_way/busy/replied) that used to live in `status` itself.
OPEN_STATUSES = ("new", "claimed", "in_progress")


def ticket_token(ticket_id: int) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"ticket:{ticket_id}".encode(), hashlib.sha256).hexdigest()[:24]


def ticket_url(ticket_id: int) -> str:
    return f"{settings.PUBLIC_BASE_URL}/ga/ticket/{ticket_id}?t={ticket_token(ticket_id)}"


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
    else:
        raise ValueError(f"Unknown action {action}")
    db.commit()


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
        teams.post(teams.build_card(
            title=f"{REMINDER_LABELS[step]}{missed_note}",
            mention=_reminder_targets(t),
            body_text=f"“{t.question}”",
            lines=[("Owner", lead_name), ("Ticket", f"#{t.id}")],
            buttons=[("Open and respond", ticket_url(t.id))],
            style="default" if step == 1 else "urgent",
        ))
        if t.escalated_at is None:
            t.escalated_at = now
        t.last_reminder_step = step
        fired += 1
    db.commit()
    return fired


def purge_old_phone_numbers(db) -> int:
    cutoff = utcnow() - timedelta(days=settings.PHONE_RETENTION_DAYS)
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
    }


def send_digest(db, day_local: datetime | None = None) -> dict:
    d = digest_data(db, day_local or datetime.now(TZ))
    lead = lead_ga()
    gap_list = "\n".join(f"- {u}" for u in d["unanswered"][:15]) or "None. Nice."
    answered = "\n".join(f"- {a}" for a in d["answered_samples"]) or "None"
    teams.post(teams.build_card(
        title=f"Virtual GA daily digest: {d['date']}",
        mention=[lead] if lead else [],
        lines=[
            ("Questions asked", str(d["questions"])),
            ("Answered by the bot", str(d["answered_by_bot"])),
            ("Handed to a GA", str(d["handoffs"])),
            ("Escalated (5 min, no reply)", str(d["escalated"])),
            ("Still open", str(d["still_open"])),
            ("Avg GA response", f"{d['avg_response_min']} min" if d["avg_response_min"] is not None else "n/a"),
        ],
        body_text=f"**Questions the bot couldn't answer (add these to the knowledge base):**\n{gap_list}\n\n"
                  f"**Sample of questions it handled:**\n{answered}",
        buttons=[("Open admin", f"{settings.PUBLIC_BASE_URL}/admin")],
    ))
    return d


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


def tick(db) -> dict:
    result = {"escalated": check_reminders(db), "digest_sent": False, "phones_purged": 0}
    result["shift_ends_handled"] = check_shift_ends(db)
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    if now.hour >= settings.DIGEST_HOUR and kv_get(db, "digest_date") != today:
        send_digest(db, now)
        kv_set(db, "digest_date", today)
        result["digest_sent"] = True
        result["phones_purged"] = purge_old_phone_numbers(db)  # once a day is plenty
        db.commit()
    return result


def stats(db) -> dict:
    return {
        "open_tickets": db.scalar(select(func.count()).select_from(Ticket).where(Ticket.status.in_(OPEN_STATUSES))),
        "team": [{"name": g.name, "lead": g.lead, "teams_email": g.teams_email} for g in load_team()],
    }
