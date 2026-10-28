"""Virtual GA: FastAPI app. Run locally with:  uvicorn app.main:app --reload"""
import hmac
import io
import logging
import os
import re
import secrets
import tempfile
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from . import approvals, directory, inventory, knowledge, llm, retention, schedule, storage, teams, tickets, tokens
from .config import BASE_DIR, load_team, settings
from .db import (ApprovalLog, ChatSession, Doc, Item, Message, Person, QueryLog, SessionLocal, TaskFile,
                 Ticket, get_today_note, init_db, kv_get, kv_set, set_today_note, utcnow)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("virtual_ga")

STATIC = BASE_DIR / "app" / "static"
@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="Virtual GA", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def admin_audit_log(request: Request, call_next):
    """Logs every admin-authenticated write (not a read, and not /api/admin/login,
    which the rate limiter already tracks) - who/what/when, in one place rather than
    touching every admin route individually. "Who" is best-effort (X-Admin-Name, see
    admin.html) since ADMIN_PASSWORD is one shared credential, not a per-user login."""
    response = await call_next(request)
    path = request.url.path
    if (path.startswith("/api/admin/") and path != "/api/admin/login"
            and request.method in ("POST", "PUT", "DELETE") and response.status_code < 400):
        from .db import AdminActionLog
        db = SessionLocal()
        try:
            db.add(AdminActionLog(admin_name=request.headers.get("x-admin-name") or "Admin",
                                  method=request.method, path=path))
            db.commit()
        finally:
            db.close()
    return response

GREETING = ("Hi! I'm the Virtual GA for the School of Business front desk. Ask me anything, "
            "like where a room is, how to get a form, or who to talk to. If I can't help, "
            "I'll get a GA for you.")
NOT_SURE = "I'm not sure about that one, and I'd rather not guess. Want me to get a GA to help you?"
NO_TODAY_NOTE = "Nothing special today, just the regular front desk hours."
TODAY_NOTE_RE = re.compile(
    r"(happening|going on|events?)\W{0,15}today|today\W{0,15}(ssb|front desk|school of business|note)", re.I)
TASK_REDIRECT_THRESHOLD = 0.6
TASK_REDIRECT_ANSWER = ('This sounds like something our GA team can take care of directly. Tap '
                        '"Request something from the GAs" below to send us the details.')
GREETING_RE = re.compile(r"^\s*(hi|hello|hey|good (morning|afternoon|evening)|thanks|thank you|ok|okay)\W*\s*$", re.I)

jobs: dict[str, dict] = {}  # background upload status, in memory
jobs_lock = threading.Lock()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _eq(a: str | None, b: str) -> bool:
    return bool(a) and hmac.compare_digest(a, b)


def require_admin(x_admin_key: str | None = Header(None)):
    if not _eq(x_admin_key, settings.ADMIN_PASSWORD):
        raise HTTPException(401, "Wrong admin password")


def get_effective_ga_key(db) -> str:
    """A rotated key (admin "Rotate GA key" button) overrides the GA_KEY env var -
    Render env vars can't be changed from inside the running app, so the override lives
    in KV instead. Checked fresh on every request, not cached, so a rotation takes
    effect immediately for everyone."""
    return kv_get(db, "ga_key_override") or settings.GA_KEY


def require_ga(x_ga_key: str | None = Header(None), db=Depends(get_db)):
    if not (_eq(x_ga_key, get_effective_ga_key(db)) or _eq(x_ga_key, settings.ADMIN_PASSWORD)):
        raise HTTPException(401, "Wrong GA key")


LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_MINUTES = 15


def _login_rate_limit(db, kind: str, ok: bool) -> None:
    """Shared by /api/admin/login and /api/ga/login only - the explicit login check
    each console's login form makes, not every already-authenticated request (which
    would make a handful of unrelated 401s elsewhere lock everyone out). ADMIN_PASSWORD
    and GA_KEY are each one shared credential, not per-user accounts, so the lockout is
    global rather than per-caller; tracked in KV so it survives a restart."""
    locked_key, count_key = f"{kind}_login_locked_until", f"{kind}_login_fail_count"
    raw = kv_get(db, locked_key)
    if raw and datetime.fromisoformat(raw) > utcnow():
        until = datetime.fromisoformat(raw)
        raise HTTPException(429, f"Too many failed attempts. Try again after {until.strftime('%H:%M UTC')}.")
    if ok:
        kv_set(db, count_key, "0")
        db.commit()
        return
    count = int(kv_get(db, count_key) or "0") + 1
    if count >= LOGIN_MAX_FAILURES:
        kv_set(db, locked_key, (utcnow() + timedelta(minutes=LOGIN_LOCK_MINUTES)).isoformat())
        count = 0
    kv_set(db, count_key, str(count))
    db.commit()


def _staff_or_ga_key(db, x_ga_key: str | None) -> bool:
    return _eq(x_ga_key, get_effective_ga_key(db)) or _eq(x_ga_key, settings.ADMIN_PASSWORD)


def ga_action_access(ticket_id: int, t: str | None = Query(None), x_ga_key: str | None = Header(None),
                     db=Depends(get_db)):
    """The GA console's own ticket page and its claim/action/ask-staff/file endpoints -
    reached from a Teams card's "Open and respond" link, which carries a 7-day
    "ga_action"-purpose token. A status/download/staff_approve token (or anything else)
    doesn't work here, same as this doesn't work on those routes."""
    if _staff_or_ga_key(db, x_ga_key):
        return
    if not tokens.verify_token(t, "ga_action", ticket_id):
        raise HTTPException(401, "This link is not valid")


def status_token_access(ticket_id: int, t: str | None = Query(None), x_ga_key: str | None = Header(None),
                        db=Depends(get_db)):
    if _staff_or_ga_key(db, x_ga_key):
        return
    if not tokens.verify_token(t, "status", ticket_id):
        raise HTTPException(401, "This link is not valid")


def download_token_access(ticket_id: int, t: str | None = Query(None), x_ga_key: str | None = Header(None),
                          db=Depends(get_db)):
    if _staff_or_ga_key(db, x_ga_key):
        return
    if not tokens.verify_token(t, "download", ticket_id):
        raise HTTPException(401, "This link is not valid")


def staff_approve_access(ticket_id: int, t: str | None = Query(None), x_ga_key: str | None = Header(None),
                         db=Depends(get_db)):
    """GET-safe: only checks the link is currently valid (right purpose/ticket, signature
    intact, not expired, not already used). Never marks it used - the route itself does
    that, and only once a decision is actually submitted."""
    if _staff_or_ga_key(db, x_ga_key):
        return
    if not t or not tokens.check_staff_link(db, t, ticket_id):
        raise HTTPException(401, "This link isn't valid - it may have already been used or expired.")


# ---------------- pages ----------------

@app.get("/")
def visitor_page():
    return FileResponse(STATIC / "visitor.html")


@app.get("/ga")
def ga_dashboard():
    return FileResponse(STATIC / "ga.html")


@app.get("/ga/ticket/{ticket_id}")
def ga_ticket_page(ticket_id: int):
    return FileResponse(STATIC / "ga.html")


@app.get("/admin")
def admin_page():
    return FileResponse(STATIC / "admin.html")


@app.get("/status/{ticket_id}")
def status_page(ticket_id: int):
    return FileResponse(STATIC / "status.html")


@app.get("/approve/{ticket_id}")
def approve_page(ticket_id: int):
    return FileResponse(STATIC / "approve.html")


@app.get("/qr.png")
def qr_png():
    import qrcode
    img = qrcode.make(settings.PUBLIC_BASE_URL + "/", box_size=12, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------- visitor API ----------------

class AskIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class HandoffIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=1, max_length=2000)
    phone: str | None = Field(None, max_length=40)
    consent: bool = False
    message_for_person_id: int | None = None


class ContactIn(BaseModel):
    phone: str = Field(min_length=7, max_length=40)
    consent: bool


class ResolvePersonIn(BaseModel):
    person_id: int


class ConfirmReachedIn(BaseModel):
    reached: bool


class StatusConfirmIn(BaseModel):
    confirmed: bool


def _session(db, sid: str) -> ChatSession:
    s = db.get(ChatSession, sid)
    if not s:
        raise HTTPException(404, "Session expired, please refresh")
    return s


def _msg_json(m: Message) -> dict:
    return {"id": m.id, "sender": m.sender, "ga_name": m.ga_name, "text": m.text, "meta": m.meta or {},
            "at": tickets.local(m.created_at).strftime("%I:%M %p")}


def _clean_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"[^\d+]", "", phone)
    if len(re.sub(r"\D", "", digits)) < 7:
        raise HTTPException(400, "That phone number looks too short")
    return digits


@app.post("/api/session")
def new_session(db=Depends(get_db)):
    s = ChatSession()
    db.add(s)
    db.flush()
    tickets.say(db, s.id, "bot", GREETING)
    db.commit()
    return {"session_id": s.id}


@app.get("/api/session/{sid}/messages")
def get_messages(sid: str, after: int = 0, db=Depends(get_db)):
    _session(db, sid)
    msgs = db.scalars(select(Message).where(Message.session_id == sid, Message.id > after).order_by(Message.id)).all()
    t = tickets.open_ticket_for(db, sid)
    confirm_prompt = bool(t and t.status == "in_progress" and t.activity == "on_the_way"
                         and t.next_deadline and t.next_deadline <= utcnow() and t.confirmed is None)
    return {"messages": [_msg_json(m) for m in msgs],
            "ticket": {"id": t.id, "status": t.status, "activity": t.activity,
                      "confirm_prompt": confirm_prompt} if t else None}


@app.post("/api/session/{sid}/ask")
def ask(sid: str, body: AskIn, db=Depends(get_db)):
    sess = _session(db, sid)
    text = body.text.strip()
    tickets.say(db, sid, "visitor", text)

    # A GA is already handling this visitor: relay instead of answering with the bot.
    t = tickets.open_ticket_for(db, sid)
    if t:
        db.commit()
        return {"relayed": True}

    resolved = directory.resolve(db, sess, text)
    if resolved is not None:
        meta = resolved.get("meta", {})
        tickets.say(db, sid, "bot", resolved["answer"], meta=meta)
        db.add(QueryLog(session_id=sess.id, question=text, confident=True,
                        category=meta.get("category", "People & offices")))
        db.commit()
        return {"relayed": False}

    if TODAY_NOTE_RE.search(text):
        answer = get_today_note(db) or NO_TODAY_NOTE
        tickets.say(db, sid, "bot", answer)
        # "Today at SSB" is about what's happening at the front desk itself - closest of
        # the named buckets to "Places", not a person/office or inventory lookup.
        db.add(QueryLog(session_id=sess.id, question=text, confident=True, category="Places"))
        db.commit()
        return {"relayed": False}

    resource = approvals.find_requested_resource(db, text)
    if resource is not None:
        if approvals.COMPLETED_FORM_RE.search(text):
            tickets.say(db, sid, "bot", approvals.REFUSE_COMPLETED_FORM_TEXT)
        else:
            tickets.say(db, sid, "bot",
                       f"{resource.title} needs a GA's approval before I can share it - it's not something "
                       "I can send directly. Want me to request a copy for you?",
                       meta={"offer_approval_request": True, "resource_id": resource.external_id,
                            "resource_title": resource.title})
        db.add(QueryLog(session_id=sess.id, question=text, confident=True, category=resource.category or "Unanswered"))
        db.commit()
        return {"relayed": False}

    inv = inventory.visitor_answer(db, text)
    if inv is not None:
        tickets.say(db, sid, "bot", inv["answer"], meta=inv["meta"])
        db.add(QueryLog(session_id=sess.id, question=text, confident=True, category="Supplies"))
        db.commit()
        return {"relayed": False}

    hour_ago = utcnow() - timedelta(hours=1)
    asked = db.scalar(select(func.count()).select_from(QueryLog)
                      .where(QueryLog.session_id == sid, QueryLog.created_at >= hour_ago))
    if asked >= settings.MAX_BOT_QUESTIONS_PER_HOUR:
        tickets.say(db, sid, "bot", "You've asked a lot of questions this hour. Want me to get a GA instead?",
                    meta={"offer_handoff": True})
        db.commit()
        return {"relayed": False}

    history = [{"sender": m.sender, "text": m.text} for m in db.scalars(
        select(Message).where(Message.session_id == sid).order_by(Message.id.desc()).limit(7)).all()][::-1][:-1]
    hits = knowledge.search(db, text, k=5)

    # a top FAQ hit of type "Task" redirects to the task-request form instead of
    # answering normally - intercepted before the AI runs, same precedence as the
    # directory resolver above. Logged like every other match-score decision.
    if hits and hits[0].get("faq_type") == "Task" and hits[0]["score"] >= TASK_REDIRECT_THRESHOLD:
        log.info("Task-type redirect: question=%r matched doc=%r score=%.3f",
                 text, hits[0]["title"], hits[0]["score"])
        tickets.say(db, sid, "bot", TASK_REDIRECT_ANSWER, meta={"offer_task_form": True})
        db.add(QueryLog(session_id=sess.id, question=text, confident=True,
                        category=hits[0].get("category") or "Unanswered"))
        db.commit()
        return {"relayed": False}

    result = llm.answer(text, hits, history)

    if result is None:  # no Gemini key, or the API failed: keyword fallback
        if GREETING_RE.match(text):
            result = {"answer": "Hi! What can I help you with?", "confident": True, "source_ids": []}
        elif hits and hits[0]["score"] >= 0.6:
            top = hits[0]
            body = top["text"].split("\nA: ", 1)[1] if top["kind"] == "qa" and "\nA: " in top["text"] else top["text"]
            snippet = body if len(body) < 600 else body[:600].rsplit(" ", 1)[0] + "..."
            result = {"answer": snippet if top["kind"] == "qa" else f"Here's what I found ({top['title']}):\n{snippet}",
                      "confident": True,
                      "source_ids": [top["id"]]}
        else:
            result = {"answer": NOT_SURE, "confident": False, "source_ids": []}

    if not result["answer"]:
        result.update(answer=NOT_SURE, confident=False)
    used = [h for h in hits if h["id"] in result["source_ids"]]
    images = sorted({h["doc_id"] for h in used if h["kind"] == "image"})
    meta = {"sources": sorted({h["title"] for h in used}),
            "images": [f"/api/kb/image/{d}" for d in images],
            "offer_handoff": not result["confident"]}
    tickets.say(db, sid, "bot", result["answer"], meta=meta)
    # Whichever Doc actually answered keeps its own FAQ category; if none did (or it has
    # none set), the retention job's topic_counts bucket this under "Unanswered".
    category = used[0].get("category") if used else None
    db.add(QueryLog(session_id=sess.id, question=text, confident=result["confident"],
                    category=category or "Unanswered"))
    db.commit()
    return {"relayed": False}


@app.post("/api/session/{sid}/resolve_person")
def resolve_person(sid: str, body: ResolvePersonIn, db=Depends(get_db)):
    sess = _session(db, sid)
    result = directory.resolve_choice(db, sess, body.person_id)
    tickets.say(db, sid, "bot", result["answer"], meta=result.get("meta", {}))
    db.commit()
    return {"ok": True}


@app.post("/api/session/{sid}/confirm_reached")
def confirm_reached(sid: str, body: ConfirmReachedIn, db=Depends(get_db)):
    _session(db, sid)
    t = tickets.open_ticket_for(db, sid)
    if not t:
        raise HTTPException(404, "No open ticket for this session")
    tickets.confirm_reached(db, t, body.reached)
    return {"ok": True}


@app.post("/api/session/{sid}/handoff")
def handoff(sid: str, body: HandoffIn, db=Depends(get_db)):
    sess = _session(db, sid)
    if tickets.open_ticket_for(db, sid):
        raise HTTPException(409, "A GA has already been notified")
    phone = _clean_phone(body.phone)
    if phone and not body.consent:
        raise HTTPException(400, "Please tick the box so we're allowed to call you")
    sess.visitor_name = body.name.strip()
    sess.visitor_phone = phone
    sess.phone_consent = bool(phone and body.consent)
    if body.message_for_person_id:
        person = db.get(Person, body.message_for_person_id)
        if not person:
            raise HTTPException(400, "That person wasn't found.")
        t = tickets.create_message_ticket(db, sess, body.question, person)
    else:
        t = tickets.create_ticket(db, sess, body.question)
    return {"ticket_id": t.id}


ALLOWED_TASK_FILE_MIMES = ("application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@app.post("/api/session/{sid}/task")
async def create_task(sid: str, name: str = Form(...), email: str = Form(""), type: str = Form(...),
                      details: str = Form(...), copies: int = Form(1, ge=1, le=99),
                      due_at: str = Form(...), pickup_location: str = Form(""),
                      file: UploadFile | None = File(None), db=Depends(get_db)):
    sess = _session(db, sid)
    if tickets.open_ticket_for(db, sid):
        raise HTTPException(409, "A GA has already been notified")
    try:
        due_dt = datetime.fromisoformat(due_at)
    except ValueError:
        raise HTTPException(400, "That due date/time isn't valid.")
    if due_dt.tzinfo:
        due_dt = due_dt.astimezone(timezone.utc).replace(tzinfo=None)

    # Validate the file BEFORE creating the ticket: a rejected file must never leave a
    # dangling ticket + already-sent Teams card behind while the visitor sees an error.
    data = mime = None
    if file and file.filename:
        data = await file.read()
        limit = settings.TASK_FILE_MAX_MB * 1024 * 1024
        if len(data) > limit:
            raise HTTPException(413, f"File is over {settings.TASK_FILE_MAX_MB} MB")
        mime = file.content_type or "application/octet-stream"
        if not (mime.startswith("image/") or mime in ALLOWED_TASK_FILE_MIMES):
            raise HTTPException(400, "Only PDF, DOCX, or image files are allowed.")

    try:
        t = tickets.create_task_ticket(db, sess, name, email, type, details, copies, due_dt, pickup_location)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if data is not None:
        path = storage.upload(data, mime)
        if path:
            t.file_backend, t.file_ref = "supabase", path
        else:
            tf = TaskFile(ticket_id=t.id, file_bytes=data)
            db.add(tf)
            db.flush()
            t.file_backend, t.file_ref = "local", str(tf.id)
        t.file_name, t.file_mime = file.filename[:300], mime
        db.commit()

    return {"ticket_id": t.id}


class BorrowIn(BaseModel):
    item_id: str


@app.post("/api/session/{sid}/borrow")
def borrow_item(sid: str, body: BorrowIn, db=Depends(get_db)):
    sess = _session(db, sid)
    if tickets.open_ticket_for(db, sid):
        raise HTTPException(409, "A GA has already been notified")
    item = db.scalar(select(Item).where(Item.item_id == body.item_id, Item.active.is_(True)))
    if not item or item.audience != "visitors" or item.lend_or_give != "lend":
        raise HTTPException(404, "That item isn't available to borrow.")
    name = sess.visitor_name or "Visitor"
    t = tickets.create_task_ticket(db, sess, name, None, "Supplies/equipment",
                                   f"Borrow: {item.item} (borrowed by {name})", 1, tickets.end_of_today(),
                                   "Front desk")
    return {"ticket_id": t.id}


class RequestApprovalIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(None, max_length=200)
    department: str = Field("", max_length=200)
    resource_id: str


@app.post("/api/session/{sid}/request-approval")
def request_approval(sid: str, body: RequestApprovalIn, db=Depends(get_db)):
    sess = _session(db, sid)
    if tickets.open_ticket_for(db, sid):
        raise HTTPException(409, "A GA has already been notified")
    resource = db.scalars(select(Doc).where(Doc.external_id == body.resource_id,
                                            Doc.audience == "approval_required")).first()
    if not resource:
        raise HTTPException(404, "That resource isn't available to request.")
    t = tickets.create_approval_ticket(db, sess, body.name, body.email, body.department,
                                       resource.external_id, resource.title)
    return {"ticket_id": t.id}


@app.post("/api/session/{sid}/contact")
def add_contact(sid: str, body: ContactIn, db=Depends(get_db)):
    sess = _session(db, sid)
    if not body.consent:
        raise HTTPException(400, "Please tick the box so we're allowed to call you")
    sess.visitor_phone = _clean_phone(body.phone)
    sess.phone_consent = True
    tickets.say(db, sid, "system", "Got it. The GA will call you at that number.")
    db.commit()
    return {"ok": True}


@app.get("/api/kb/image/{doc_id}")
def kb_image(doc_id: int, db=Depends(get_db)):
    d = db.get(Doc, doc_id)
    if not d or d.kind != "image" or not d.image_bytes:
        raise HTTPException(404)
    return Response(d.image_bytes, media_type=d.mime or "image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/directory/floor-map")
def floor_map():
    matches = sorted((BASE_DIR / "data").glob("babbio-4th-floor-map.*"))
    if not matches:
        raise HTTPException(404)
    path = matches[0]
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "public, max-age=86400"})


# ---------------- GA console API ----------------

class ActionIn(BaseModel):
    ga_name: str = Field(min_length=1, max_length=120)
    action: str
    minutes: int | None = Field(None, ge=1, le=60)
    text: str | None = Field(None, max_length=2000)
    snooze_until: str | None = None
    reason: str | None = Field(None, max_length=500)


class ClaimIn(BaseModel):
    ga_name: str = Field(min_length=1, max_length=120)


def _ticket_json(db, t: Ticket, full: bool) -> dict:
    s = db.get(ChatSession, t.session_id)
    out = {"id": t.id, "status": t.status, "question": t.question, "visitor_name": s.visitor_name,
           "has_phone": bool(s.visitor_phone), "on_shift": t.on_shift, "responded_by": t.responded_by,
           "created_at": tickets.local(t.created_at).strftime("%a %I:%M %p"),
           "waiting_minutes": int((utcnow() - t.created_at).total_seconds() // 60),
           "escalated": bool(t.escalated_at), "owner": t.owner, "activity": t.activity,
           "kind": t.kind, "task": t.task,
           "due_at": tickets.local(t.due_at).strftime("%a %I:%M %p") if t.due_at else None,
           "file_name": t.file_name}  # metadata only - bytes are never included here
    if full:
        out["visitor_phone"] = s.visitor_phone
        msgs = db.scalars(select(Message).where(Message.session_id == t.session_id).order_by(Message.id)).all()
        out["messages"] = [_msg_json(m) for m in msgs]
    return out


@app.get("/api/ga/team")
def ga_team(tid: int | None = None, t: str | None = None, x_ga_key: str | None = Header(None),
           db=Depends(get_db)):
    ok = _eq(x_ga_key, get_effective_ga_key(db)) or _eq(x_ga_key, settings.ADMIN_PASSWORD) or \
        (tid is not None and tokens.verify_token(t, "ga_action", tid))
    if not ok:
        raise HTTPException(401, "Wrong GA key")
    return {"names": [g.name for g in load_team()], "on_shift": [g.name for g in schedule.on_shift_now()]}


class TodayNoteIn(BaseModel):
    text: str = Field("", max_length=500)


@app.get("/api/ga/today-note", dependencies=[Depends(require_ga)])
def ga_today_note(db=Depends(get_db)):
    return {"text": get_today_note(db) or ""}


@app.post("/api/ga/today-note", dependencies=[Depends(require_ga)])
def ga_set_today_note(body: TodayNoteIn, db=Depends(get_db)):
    set_today_note(db, body.text)
    db.commit()
    return {"text": get_today_note(db) or ""}


@app.get("/api/ga/directory/search", dependencies=[Depends(require_ga)])
def ga_directory_search(q: str, db=Depends(get_db)):
    results = directory.search_people(db, q)
    return {"results": [{"id": p.id, "name": p.name, "title": p.title, "building": p.building,
                         "room": p.room, "floor": p.floor, "extension": p.extension,
                         "email": p.email, "office_hours": p.office_hours} for p in results]}


def _item_json(it: Item) -> dict:
    return {"item_id": it.item_id, "item": it.item, "aliases": it.aliases, "area": it.area,
            "spot": it.spot, "quantity": it.quantity, "lend_or_give": it.lend_or_give,
            "audience": it.audience, "status": it.status, "on_order": it.on_order,
            "return_within": it.return_within, "last_counted": it.last_counted, "notes": it.notes}


@app.get("/api/ga/inventory/search", dependencies=[Depends(require_ga)])
def ga_inventory_search(q: str, db=Depends(get_db)):
    return {"results": [_item_json(it) for it in inventory.search_items(db, q)]}


@app.get("/api/ga/resources/search", dependencies=[Depends(require_ga)])
def ga_resources_search(q: str, db=Depends(get_db)):
    hits = knowledge.search(db, q, k=10, visitor=False)
    return {"results": [{"title": h["title"], "kind": h["kind"], "text": h["text"], "score": h["score"]}
                        for h in hits]}


class InventoryStatusIn(BaseModel):
    status: str


@app.post("/api/ga/inventory/{item_id}/status", dependencies=[Depends(require_ga)])
def ga_inventory_set_status(item_id: str, body: InventoryStatusIn, db=Depends(get_db)):
    if body.status not in inventory.STATUS_CHOICES:
        raise HTTPException(400, f"Status must be one of {', '.join(inventory.STATUS_CHOICES)}")
    item = db.scalar(select(Item).where(Item.item_id == item_id, Item.active.is_(True)))
    if not item:
        raise HTTPException(404)
    item.status = body.status
    item.last_counted = datetime.now(tickets.TZ).strftime("%Y-%m-%d")
    db.commit()
    return _item_json(item)


@app.get("/api/ga/tickets", dependencies=[Depends(require_ga)])
def ga_tickets(include_resolved: bool = False, db=Depends(get_db)):
    q = select(Ticket).order_by(Ticket.id.desc()).limit(50)
    if not include_resolved:
        q = q.where(Ticket.status.in_(tickets.OPEN_STATUSES))
    return {"tickets": [_ticket_json(db, t, False) for t in db.scalars(q).all()]}


@app.get("/api/ga/ticket/{ticket_id}", dependencies=[Depends(ga_action_access)])
def ga_ticket(ticket_id: int, db=Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    return _ticket_json(db, t, True)


@app.get("/api/ga/ticket/{ticket_id}/file", dependencies=[Depends(ga_action_access)])
def ga_ticket_file(ticket_id: int, db=Depends(get_db)):
    """The task's attached file - GA console only, never exposed to Teams. Bytes (for
    the local fallback) are only ever loaded here, not in any ticket-list/detail query."""
    t = db.get(Ticket, ticket_id)
    if not t or not t.file_ref:
        raise HTTPException(404)
    if t.file_backend == "supabase":
        url = storage.signed_url(t.file_ref)
        if not url:
            raise HTTPException(502, "Could not generate a file link right now.")
        return RedirectResponse(url)
    tf = db.get(TaskFile, int(t.file_ref))
    if not tf:
        raise HTTPException(404)
    return Response(tf.file_bytes, media_type=t.file_mime or "application/octet-stream",
                    headers={"Content-Disposition": f'inline; filename="{t.file_name or "file"}"'})


def _status_json(t: Ticket) -> dict:
    """Requester-facing view: status/timing only, never the visitor's phone number or
    the full GA chat transcript."""
    out = {"id": t.id, "status": t.status, "kind": t.kind, "task_type": (t.task or {}).get("type"),
          "created_at": tickets.local(t.created_at).strftime("%a %I:%M %p"),
          "due_at": tickets.local(t.due_at).strftime("%a %I:%M %p") if t.due_at else None,
          "confirm_prompt": t.status == "done" and t.confirmed is None and t.kind != "approval",
          "confirmed": t.confirmed}
    if t.kind == "approval":
        handoff = (t.task or {}).get("handoff_method")
        downloadable = (t.status == "done" and handoff == "digital" and t.resolved_at is not None
                       and utcnow() - t.resolved_at <= timedelta(hours=tokens.DOWNLOAD_WINDOW_HOURS))
        out.update(resource_title=(t.task or {}).get("resource_title"), handoff_method=handoff,
                  downloadable=downloadable, decline_reason=t.cancelled_reason if t.status == "cancelled" else None)
        if downloadable:
            dl_token = tokens.download_token(t.id, t.resolved_at)
            out["download_url"] = f"/api/status/{t.id}/download?t={dl_token}"
    return out


@app.get("/api/status/{ticket_id}", dependencies=[Depends(status_token_access)])
def get_status(ticket_id: int, db=Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    return _status_json(t)


@app.post("/api/status/{ticket_id}/confirm", dependencies=[Depends(status_token_access)])
def confirm_status(ticket_id: int, body: StatusConfirmIn, db=Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    if t.status != "done":
        raise HTTPException(409, "This request isn't marked done yet")
    if t.confirmed is not None:
        raise HTTPException(409, "This has already been answered")
    tickets.confirm_requester(db, t, body.confirmed)
    return _status_json(t)


@app.get("/api/status/{ticket_id}/download", dependencies=[Depends(download_token_access)])
def download_approval_file(ticket_id: int, db=Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t or t.kind != "approval":
        raise HTTPException(404)
    if not _status_json(t)["downloadable"]:
        raise HTTPException(403, "This link isn't available - it's expired, not approved, or was handed over in person.")
    resource_id = (t.task or {}).get("resource_id")
    doc = db.scalars(select(Doc).where(Doc.external_id == resource_id,
                                       Doc.audience == "approval_required")).first()
    if not doc or not doc.source_file:
        raise HTTPException(404, "File not available.")
    path = knowledge.RESOURCES_DIR / doc.source_file
    if not path.is_file():
        raise HTTPException(404, "File not available.")
    return FileResponse(path, filename=path.name, media_type="application/pdf")


@app.get("/api/approve/{ticket_id}", dependencies=[Depends(staff_approve_access)])
def get_approval_ticket(ticket_id: int, db=Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t or t.kind != "approval":
        raise HTTPException(404)
    return {"id": t.id, "status": t.status, "resource_title": (t.task or {}).get("resource_title"),
            "department": (t.task or {}).get("department"),
            "requested_by": db.get(ChatSession, t.session_id).visitor_name,
            "created_at": tickets.local(t.created_at).strftime("%a %I:%M %p"),
            "decline_reason": t.cancelled_reason if t.status == "cancelled" else None}


class StaffDecisionIn(BaseModel):
    decision: str  # approved | declined
    reason: str | None = None


@app.post("/api/approve/{ticket_id}", dependencies=[Depends(staff_approve_access)])
def staff_decide(ticket_id: int, body: StaffDecisionIn, t: str | None = Query(None), db=Depends(get_db)):
    tk = db.get(Ticket, ticket_id)
    if not tk or tk.kind != "approval":
        raise HTTPException(404)
    if tk.status not in tickets.OPEN_STATUSES:
        raise HTTPException(409, "This request has already been decided.")
    if body.decision not in ("approved", "declined"):
        raise HTTPException(400, "Unknown decision.")
    staff_name = approvals.get_asked_staff_name(db, tk) or "Staff approver"
    try:
        tickets.apply_approval_decision(db, tk, staff_name, body.decision, body.reason)
    except ValueError as e:
        raise HTTPException(400, str(e))
    link = tokens.check_staff_link(db, t, ticket_id) if t else None
    if link:
        tokens.consume_staff_link(db, link)
    return {"status": tk.status}


@app.post("/api/ga/ticket/{ticket_id}/claim", dependencies=[Depends(ga_action_access)])
def ga_ticket_claim(ticket_id: int, body: ClaimIn, db=Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    if not tickets.claim_ticket(db, ticket_id, body.ga_name.strip()):
        raise HTTPException(409, "Already claimed by someone else")
    return _ticket_json(db, db.get(Ticket, ticket_id), True)


@app.post("/api/ga/ticket/{ticket_id}/action", dependencies=[Depends(ga_action_access)])
def ga_ticket_action(ticket_id: int, body: ActionIn, db=Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    if t.status == "done":
        raise HTTPException(409, "Already resolved")
    try:
        tickets.ga_action(db, t, body.ga_name.strip(), body.action, body.minutes, body.text,
                         body.snooze_until, body.reason)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _ticket_json(db, t, True)


class AskStaffIn(BaseModel):
    ga_name: str
    staff_name: str


@app.post("/api/ga/ticket/{ticket_id}/ask-staff", dependencies=[Depends(ga_action_access)])
def ga_ask_staff(ticket_id: int, body: AskStaffIn, db=Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    try:
        result = approvals.ask_staff_to_approve(db, t, body.staff_name, body.ga_name.strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return result


@app.get("/api/ga/approvers", dependencies=[Depends(require_ga)])
def ga_list_approvers(db=Depends(get_db)):
    return {"names": [p.name for p in approvals.get_approvers(db)]}


# ---------------- admin API ----------------

class QAIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    answer: str = Field(min_length=1, max_length=5000)
    added_by: str | None = None


class NoteIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=200_000)
    added_by: str | None = None


class SettingsIn(BaseModel):
    show_extensions: bool


@app.post("/api/admin/login")
def admin_login(x_admin_key: str | None = Header(None), db=Depends(get_db)):
    ok = _eq(x_admin_key, settings.ADMIN_PASSWORD)
    _login_rate_limit(db, "admin", ok)
    if not ok:
        raise HTTPException(401, "Wrong admin password")
    return {"ok": True}


@app.post("/api/ga/login")
def ga_login(x_ga_key: str | None = Header(None), db=Depends(get_db)):
    ok = _eq(x_ga_key, get_effective_ga_key(db)) or _eq(x_ga_key, settings.ADMIN_PASSWORD)
    _login_rate_limit(db, "ga", ok)
    if not ok:
        raise HTTPException(401, "Wrong GA key")
    return {"ok": True}


@app.get("/api/admin/docs", dependencies=[Depends(require_admin)])
def list_docs(db=Depends(get_db)):
    docs = db.scalars(select(Doc).order_by(Doc.id.desc())).all()
    with jobs_lock:
        pending = list(jobs.values())
    return {"docs": [{"id": d.id, "title": d.title, "kind": d.kind, "added_by": d.added_by,
                      "chars": len(d.extracted_text), "preview": d.extracted_text[:300],
                      "created_at": tickets.local(d.created_at).strftime("%b %d %I:%M %p")} for d in docs],
            "jobs": pending, "ai_enabled": llm.enabled()}


@app.get("/api/admin/docs/{doc_id}", dependencies=[Depends(require_admin)])
def get_doc(doc_id: int, db=Depends(get_db)):
    d = db.get(Doc, doc_id)
    if not d:
        raise HTTPException(404)
    return {"id": d.id, "title": d.title, "kind": d.kind, "text": d.extracted_text}


@app.delete("/api/admin/docs/{doc_id}", dependencies=[Depends(require_admin)])
def delete_doc(doc_id: int, db=Depends(get_db)):
    d = db.get(Doc, doc_id)
    if not d:
        raise HTTPException(404)
    from .db import Chunk
    for c in db.scalars(select(Chunk).where(Chunk.doc_id == doc_id)).all():
        db.delete(c)
    db.delete(d)
    db.commit()
    return {"ok": True}


@app.post("/api/admin/qa", dependencies=[Depends(require_admin)])
def add_qa(body: QAIn, db=Depends(get_db)):
    text = f"Q: {body.question.strip()}\nA: {body.answer.strip()}"
    d = knowledge.add_document(db, body.question, "qa", text, body.added_by)
    return {"id": d.id}


@app.post("/api/admin/qa/{doc_id}", dependencies=[Depends(require_admin)])
def edit_qa(doc_id: int, body: QAIn, db=Depends(get_db)):
    try:
        d = knowledge.update_qa_doc(db, doc_id, body.question, body.answer, body.added_by)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"id": d.id}


@app.post("/api/admin/faq/preview", dependencies=[Depends(require_admin)])
async def faq_preview(file: UploadFile = File(...)):
    text = (await file.read()).decode("utf-8", errors="replace")
    parsed = knowledge.parse_faq_csv(text)
    rows = parsed["rows"]
    return {"missing_columns": parsed["missing_columns"], "rows": rows,
            "to_import": sum(1 for r in rows if r["will_import"]),
            "to_skip": sum(1 for r in rows if not r["will_import"])}


@app.post("/api/admin/faq/import", dependencies=[Depends(require_admin)])
async def faq_import(file: UploadFile = File(...), added_by: str = Form(""), db=Depends(get_db)):
    text = (await file.read()).decode("utf-8", errors="replace")
    result = knowledge.import_faq_csv(db, text, added_by.strip() or None)
    if result["missing_columns"]:
        raise HTTPException(400, f"Missing columns: {', '.join(result['missing_columns'])}")
    return result


@app.post("/api/admin/note", dependencies=[Depends(require_admin)])
def add_note(body: NoteIn, db=Depends(get_db)):
    d = knowledge.add_document(db, body.title, "text", body.text, body.added_by)
    return {"id": d.id}


def _process_upload(job_id: str, path: str, filename: str, mime: str, title: str, added_by: str | None,
                    extra_text: str):
    db = SessionLocal()
    try:
        with open(path, "rb") as f:
            data = f.read() if not mime.startswith("video/") else b""
        if mime.startswith("image/"):
            desc = llm.describe_image(data, mime) if llm.enabled() else ""
            text = "\n\n".join(x for x in [extra_text, desc] if x)
            if not text:
                raise ValueError("Add a description for this image (no Gemini key, so it can't be read automatically).")
            stored = data if len(data) <= 5 * 1024 * 1024 else None
            knowledge.add_document(db, title, "image", text, added_by, image_bytes=stored, mime=mime)
        elif mime.startswith("video/"):
            text = llm.transcribe_video(path, mime)
            if extra_text:
                text = extra_text + "\n\n" + text
            knowledge.add_document(db, title, "video", text, added_by)
        else:
            kind, text = knowledge.extract_text(filename, data, mime)
            if extra_text:
                text = extra_text + "\n\n" + text
            knowledge.add_document(db, title, kind, text, added_by)
        with jobs_lock:
            jobs.pop(job_id, None)
    except Exception as e:
        log.exception("Upload processing failed")
        with jobs_lock:
            jobs[job_id].update(status="failed", error=str(e))
    finally:
        db.close()
        try:
            os.remove(path)
        except OSError:
            pass


@app.post("/api/admin/upload", dependencies=[Depends(require_admin)])
async def upload(background: BackgroundTasks, file: UploadFile = File(...), title: str = Form(""),
                 description: str = Form(""), added_by: str = Form("")):
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    suffix = os.path.splitext(file.filename or "")[1]
    fd, path = tempfile.mkstemp(suffix=suffix)
    size = 0
    with os.fdopen(fd, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                os.remove(path)
                raise HTTPException(413, f"File is over {settings.MAX_UPLOAD_MB} MB")
            out.write(chunk)
    mime = file.content_type or "application/octet-stream"
    if mime.startswith("video/") and not llm.enabled():
        os.remove(path)
        raise HTTPException(400, "Video needs GEMINI_API_KEY to be set.")
    job_id = os.path.basename(path)
    with jobs_lock:
        jobs[job_id] = {"id": job_id, "title": title or file.filename, "status": "processing", "error": None}
    background.add_task(_process_upload, job_id, path, file.filename or "upload", mime,
                        title or file.filename or "Upload", added_by or None, description.strip())
    return {"job_id": job_id}


@app.post("/api/admin/jobs/{job_id}/dismiss", dependencies=[Depends(require_admin)])
def dismiss_job(job_id: str):
    with jobs_lock:
        jobs.pop(job_id, None)
    return {"ok": True}


@app.get("/api/admin/search", dependencies=[Depends(require_admin)])
def admin_search(q: str, db=Depends(get_db)):
    return {"hits": knowledge.search(db, q, k=5, visitor=False)}


@app.post("/api/admin/reembed", dependencies=[Depends(require_admin)])
def reembed(db=Depends(get_db)):
    return {"embedded": knowledge.reembed_missing(db)}


@app.post("/api/admin/directory/import", dependencies=[Depends(require_admin)])
async def import_directory(file: UploadFile = File(...), db=Depends(get_db)):
    text = (await file.read()).decode("utf-8", errors="replace")
    return directory.import_people_csv(db, text)


@app.post("/api/admin/rooms/import", dependencies=[Depends(require_admin)])
async def import_rooms(file: UploadFile = File(...), db=Depends(get_db)):
    text = (await file.read()).decode("utf-8", errors="replace")
    return directory.import_rooms_csv(db, text)


@app.post("/api/admin/inventory/import", dependencies=[Depends(require_admin)])
async def import_inventory(file: UploadFile = File(...), db=Depends(get_db)):
    text = (await file.read()).decode("utf-8", errors="replace")
    return inventory.import_inventory_csv(db, text)


@app.post("/api/admin/resources/import", dependencies=[Depends(require_admin)])
async def import_resources(file: UploadFile = File(...), db=Depends(get_db)):
    text = (await file.read()).decode("utf-8", errors="replace")
    return knowledge.import_resources_csv(db, text)


@app.post("/api/admin/emails/import", dependencies=[Depends(require_admin)])
async def import_email_candidates(file: UploadFile = File(...), db=Depends(get_db)):
    text = (await file.read()).decode("utf-8", errors="replace")
    return directory.import_email_candidates_csv(db, text)


@app.get("/api/admin/emails/candidates", dependencies=[Depends(require_admin)])
def list_email_candidates(db=Depends(get_db)):
    from .db import EmailCandidate
    rows = db.scalars(select(EmailCandidate).where(EmailCandidate.status == "pending")
                      .order_by(EmailCandidate.id)).all()
    return {"candidates": [{"id": r.id, "name": r.name, "email": r.email, "confidence": r.confidence,
                            "source_url": r.source_url} for r in rows]}


@app.post("/api/admin/emails/candidates/{cand_id}/approve", dependencies=[Depends(require_admin)])
def approve_email_candidate_ep(cand_id: int, db=Depends(get_db)):
    try:
        return directory.approve_email_candidate(db, cand_id)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/admin/emails/candidates/{cand_id}/reject", dependencies=[Depends(require_admin)])
def reject_email_candidate_ep(cand_id: int, db=Depends(get_db)):
    try:
        return directory.reject_email_candidate(db, cand_id)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/admin/settings", dependencies=[Depends(require_admin)])
def get_settings(db=Depends(get_db)):
    return {"show_extensions": directory.get_show_extensions(db)}


@app.post("/api/admin/settings", dependencies=[Depends(require_admin)])
def update_settings(body: SettingsIn, db=Depends(get_db)):
    directory.set_show_extensions(db, body.show_extensions)
    return {"show_extensions": body.show_extensions}


class RetentionIn(BaseModel):
    enabled: bool | None = None
    visitor_days: int | None = None
    query_log_days: int | None = None
    approval_log_days: int | None = None
    phone_days: int | None = None
    file_days: int | None = None


@app.get("/api/admin/retention", dependencies=[Depends(require_admin)])
def get_retention(db=Depends(get_db)):
    return {"enabled": retention.enabled(db), "periods": retention.get_periods(db),
            "last_report": retention.last_report(db)}


@app.post("/api/admin/retention", dependencies=[Depends(require_admin)])
def update_retention(body: RetentionIn, db=Depends(get_db)):
    if body.enabled is not None:
        retention.set_enabled(db, body.enabled)
    periods = body.model_dump(exclude={"enabled"}, exclude_none=True)
    if periods:
        retention.set_periods(db, **periods)
    return {"enabled": retention.enabled(db), "periods": retention.get_periods(db)}


@app.post("/api/admin/retention/run", dependencies=[Depends(require_admin)])
def run_retention_now(db=Depends(get_db)):
    """Runs immediately instead of waiting for the next daily tick - still only a
    preview unless "Enable retention" is already on, same rule as the daily run."""
    return retention.run(db)


class ApproversIn(BaseModel):
    names: list[str]


@app.get("/api/admin/approvers", dependencies=[Depends(require_admin)])
def get_approvers_setting(db=Depends(get_db)):
    return {"names": [p.name for p in approvals.get_approvers(db)]}


@app.post("/api/admin/approvers", dependencies=[Depends(require_admin)])
def set_approvers_setting(body: ApproversIn, db=Depends(get_db)):
    approvals.set_approvers(db, body.names)
    return {"names": [p.name for p in approvals.get_approvers(db)]}


@app.get("/api/admin/approval-log", dependencies=[Depends(require_admin)])
def get_approval_log(db=Depends(get_db)):
    rows = db.scalars(select(ApprovalLog).order_by(ApprovalLog.id.desc()).limit(100)).all()
    return {"entries": [{"id": r.id, "ticket_id": r.ticket_id, "resource_id": r.resource_id,
                         "actor": r.actor, "action": r.action, "reason": r.reason,
                         "at": tickets.local(r.at).strftime("%b %d %I:%M %p")} for r in rows]}


@app.get("/api/admin/audit-log", dependencies=[Depends(require_admin)])
def get_audit_log(db=Depends(get_db)):
    from .db import AdminActionLog
    rows = db.scalars(select(AdminActionLog).order_by(AdminActionLog.id.desc()).limit(200)).all()
    return {"entries": [{"id": r.id, "admin_name": r.admin_name, "method": r.method, "path": r.path,
                         "at": tickets.local(r.at).strftime("%b %d %I:%M %p")} for r in rows]}


@app.get("/api/admin/schedule", dependencies=[Depends(require_admin)])
def admin_schedule():
    return schedule.status()


@app.get("/api/admin/digest", dependencies=[Depends(require_admin)])
def admin_digest(db=Depends(get_db)):
    from datetime import datetime
    return tickets.digest_data(db, datetime.now(tickets.TZ))


@app.post("/api/admin/digest/send", dependencies=[Depends(require_admin)])
def admin_digest_send(db=Depends(get_db)):
    return tickets.send_digest(db)


@app.get("/api/admin/weekly-report", dependencies=[Depends(require_admin)])
def admin_weekly_report(db=Depends(get_db)):
    from datetime import datetime
    return tickets.weekly_report_data(db, datetime.now(tickets.TZ))


@app.post("/api/admin/weekly-report/send", dependencies=[Depends(require_admin)])
def admin_weekly_report_send(db=Depends(get_db)):
    return tickets.send_weekly_report(db)


@app.post("/api/admin/test-teams", dependencies=[Depends(require_admin)])
def test_teams():
    from .config import lead_ga
    lead = lead_ga()
    ok = teams.post(teams.build_card(
        title="Virtual GA test message", mention=[lead] if lead else [],
        body_text="If you can read this, Teams alerts are working.", lines=[],
        buttons=[("Open GA console", f"{settings.PUBLIC_BASE_URL}/ga")]))
    return {"sent": ok, "webhook_configured": bool(settings.TEAMS_WEBHOOK_URL)}


@app.get("/api/admin/status", dependencies=[Depends(require_admin)])
def admin_status(db=Depends(get_db)):
    from .db import Room, rls_status
    people_count = db.scalar(select(func.count()).select_from(Person).where(Person.active.is_(True)))
    rooms_count = db.scalar(select(func.count()).select_from(Room).where(Room.active.is_(True)))
    return {"ai_enabled": llm.enabled(), "teams_configured": bool(settings.TEAMS_WEBHOOK_URL),
            "calendar_configured": bool(settings.GCAL_ICS_URL), "public_url": settings.PUBLIC_BASE_URL,
            "people_count": people_count, "rooms_count": rooms_count,
            "tables_without_rls": rls_status(),
            "ga_key_rotated": bool(kv_get(db, "ga_key_override")),
            **tickets.stats(db)}


@app.post("/api/admin/ga-key/rotate", dependencies=[Depends(require_admin)])
def rotate_ga_key(db=Depends(get_db)):
    """Generates a new GA key and stores it as an override (Render env vars can't be
    changed from here) - shown once in the response, never retrievable again after
    this, so it must be copied out and given to the team right away. Takes effect
    immediately: every GA console currently signed in with the old key is logged out on
    its next request and needs the new one."""
    new_key = secrets.token_urlsafe(18)
    kv_set(db, "ga_key_override", new_key)
    db.commit()
    return {"ga_key": new_key}


# ---------------- cron ----------------

@app.get("/tasks/tick")
def tick(key: str = "", db=Depends(get_db)):
    """Hit this every minute from cron-job.org. It escalates overdue tickets, sends the daily digest,
    and keeps the free server awake."""
    if not _eq(key, settings.CRON_SECRET):
        raise HTTPException(401)
    return tickets.tick(db)
