"""Handoffs, GA responses, 5-minute escalation to the lead, the daily digest and data cleanup."""
import hashlib
import hmac
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from . import schedule, teams
from .config import lead_ga, load_team, settings
from .db import ChatSession, Message, QueryLog, Ticket, kv_get, kv_set, utcnow

TZ = ZoneInfo(settings.TIMEZONE)
OPEN_STATUSES = ("waiting", "on_the_way", "follow_up", "replied")


def ticket_token(ticket_id: int) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"ticket:{ticket_id}".encode(), hashlib.sha256).hexdigest()[:24]


def ticket_url(ticket_id: int) -> str:
    return f"{settings.PUBLIC_BASE_URL}/ga/ticket/{ticket_id}?t={ticket_token(ticket_id)}"


def local(dt: datetime) -> datetime:
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)


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
    t = Ticket(session_id=sess.id, question=question.strip()[:2000], on_shift=[g.name for g in on_shift])
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


# ---------- GA responses from the console ----------

def ga_action(db, t: Ticket, ga_name: str, action: str, minutes: int | None = None, text: str | None = None) -> None:
    sess = db.get(ChatSession, t.session_id)
    ga_first = ga_name.split()[0]
    if t.first_response_at is None and action != "note":
        t.first_response_at = utcnow()
        t.responded_by = ga_name

    if action == "on_my_way":
        t.status = "on_the_way"
        eta = f" in about {minutes} minutes" if minutes else " shortly"
        say(db, t.session_id, "system", f"{ga_first} is on the way to the front desk and will be with you{eta}.")
    elif action == "busy":
        t.status = "follow_up"
        how = "call you back at the number you left" if sess.visitor_phone else \
            "get back to you. Please leave a phone number below so they can reach you"
        say(db, t.session_id, "system", f"{ga_first} is tied up with something right now and will {how}.")
    elif action == "reply":
        if not text or not text.strip():
            raise ValueError("Reply is empty.")
        if t.status == "waiting":
            t.status = "replied"
        say(db, t.session_id, "ga", text.strip()[:2000], ga_name=ga_name)
    elif action == "resolve":
        t.status = "resolved"
        t.resolved_at = utcnow()
        say(db, t.session_id, "system", "This request is marked as resolved. Thanks for stopping by! "
                                        "You can keep asking me questions anytime.")
    else:
        raise ValueError(f"Unknown action {action}")
    db.commit()


# ---------- periodic work, called by an external cron every minute ----------

def escalate_overdue(db) -> int:
    cutoff = utcnow() - timedelta(minutes=settings.ESCALATE_AFTER_MINUTES)
    overdue = db.scalars(select(Ticket).where(Ticket.status == "waiting", Ticket.first_response_at.is_(None),
                                              Ticket.escalated_at.is_(None), Ticket.created_at <= cutoff)).all()
    lead = lead_ga()
    for t in overdue:
        sess = db.get(ChatSession, t.session_id)
        waited = int((utcnow() - t.created_at).total_seconds() // 60)
        teams.post(teams.build_card(
            title=f"No response after {waited} minutes",
            mention=[lead] if lead else [],
            body_text=f"“{t.question}”",
            lines=[
                ("Visitor", sess.visitor_name or "Not given"),
                ("Was alerted", ", ".join(t.on_shift) or "Nobody on shift"),
                ("Ticket", f"#{t.id}, waiting since {local(t.created_at).strftime('%I:%M %p')}"),
            ],
            buttons=[("Open and respond", ticket_url(t.id))],
            style="urgent",
        ))
        say(db, t.session_id, "system", "Sorry for the wait. I've flagged this to the lead GA as well.")
        t.escalated_at = utcnow()
    db.commit()
    return len(overdue)


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


def tick(db) -> dict:
    result = {"escalated": escalate_overdue(db), "digest_sent": False, "phones_purged": 0}
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
