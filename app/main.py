"""Virtual GA: FastAPI app. Run locally with:  uvicorn app.main:app --reload"""
import hmac
import io
import logging
import os
import re
import tempfile
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from . import directory, knowledge, llm, schedule, storage, teams, tickets
from .config import BASE_DIR, load_team, settings
from .db import ChatSession, Doc, Message, Person, QueryLog, SessionLocal, TaskFile, Ticket, init_db, utcnow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("virtual_ga")

STATIC = BASE_DIR / "app" / "static"
@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="Virtual GA", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

GREETING = ("Hi! I'm the Virtual GA for the School of Business front desk. Ask me anything, "
            "like where a room is, how to get a form, or who to talk to. If I can't help, "
            "I'll get a GA for you.")
NOT_SURE = "I'm not sure about that one, and I'd rather not guess. Want me to get a GA to help you?"
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


def require_ga(x_ga_key: str | None = Header(None)):
    if not (_eq(x_ga_key, settings.GA_KEY) or _eq(x_ga_key, settings.ADMIN_PASSWORD)):
        raise HTTPException(401, "Wrong GA key")


def ticket_access(ticket_id: int, t: str | None = Query(None), x_ga_key: str | None = Header(None)):
    ok = _eq(t, tickets.ticket_token(ticket_id)) or _eq(x_ga_key, settings.GA_KEY) or \
        _eq(x_ga_key, settings.ADMIN_PASSWORD)
    if not ok:
        raise HTTPException(401, "This link is not valid")


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
        tickets.say(db, sid, "bot", resolved["answer"], meta=resolved.get("meta", {}))
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
    db.add(QueryLog(session_id=sess.id, question=text, confident=result["confident"]))
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
def ga_team(tid: int | None = None, t: str | None = None, x_ga_key: str | None = Header(None)):
    ok = _eq(x_ga_key, settings.GA_KEY) or _eq(x_ga_key, settings.ADMIN_PASSWORD) or \
        (tid is not None and _eq(t, tickets.ticket_token(tid)))
    if not ok:
        raise HTTPException(401, "Wrong GA key")
    return {"names": [g.name for g in load_team()], "on_shift": [g.name for g in schedule.on_shift_now()]}


@app.get("/api/ga/directory/search", dependencies=[Depends(require_ga)])
def ga_directory_search(q: str, db=Depends(get_db)):
    results = directory.search_people(db, q)
    return {"results": [{"id": p.id, "name": p.name, "title": p.title, "building": p.building,
                         "room": p.room, "floor": p.floor, "extension": p.extension,
                         "email": p.email, "office_hours": p.office_hours} for p in results]}


@app.get("/api/ga/tickets", dependencies=[Depends(require_ga)])
def ga_tickets(include_resolved: bool = False, db=Depends(get_db)):
    q = select(Ticket).order_by(Ticket.id.desc()).limit(50)
    if not include_resolved:
        q = q.where(Ticket.status.in_(tickets.OPEN_STATUSES))
    return {"tickets": [_ticket_json(db, t, False) for t in db.scalars(q).all()]}


@app.get("/api/ga/ticket/{ticket_id}", dependencies=[Depends(ticket_access)])
def ga_ticket(ticket_id: int, db=Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    return _ticket_json(db, t, True)


@app.get("/api/ga/ticket/{ticket_id}/file", dependencies=[Depends(ticket_access)])
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
    return {"id": t.id, "status": t.status, "kind": t.kind, "task_type": (t.task or {}).get("type"),
            "created_at": tickets.local(t.created_at).strftime("%a %I:%M %p"),
            "due_at": tickets.local(t.due_at).strftime("%a %I:%M %p") if t.due_at else None,
            "confirm_prompt": t.status == "done" and t.confirmed is None,
            "confirmed": t.confirmed}


@app.get("/api/status/{ticket_id}", dependencies=[Depends(ticket_access)])
def get_status(ticket_id: int, db=Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    return _status_json(t)


@app.post("/api/status/{ticket_id}/confirm", dependencies=[Depends(ticket_access)])
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


@app.post("/api/ga/ticket/{ticket_id}/claim", dependencies=[Depends(ticket_access)])
def ga_ticket_claim(ticket_id: int, body: ClaimIn, db=Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404)
    if not tickets.claim_ticket(db, ticket_id, body.ga_name.strip()):
        raise HTTPException(409, "Already claimed by someone else")
    return _ticket_json(db, db.get(Ticket, ticket_id), True)


@app.post("/api/ga/ticket/{ticket_id}/action", dependencies=[Depends(ticket_access)])
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


@app.post("/api/admin/login", dependencies=[Depends(require_admin)])
def admin_login():
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
    return {"hits": knowledge.search(db, q, k=5)}


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
    from .db import Room
    people_count = db.scalar(select(func.count()).select_from(Person).where(Person.active.is_(True)))
    rooms_count = db.scalar(select(func.count()).select_from(Room).where(Room.active.is_(True)))
    return {"ai_enabled": llm.enabled(), "teams_configured": bool(settings.TEAMS_WEBHOOK_URL),
            "calendar_configured": bool(settings.GCAL_ICS_URL), "public_url": settings.PUBLIC_BASE_URL,
            "people_count": people_count, "rooms_count": rooms_count,
            **tickets.stats(db)}


# ---------------- cron ----------------

@app.get("/tasks/tick")
def tick(key: str = "", db=Depends(get_db)):
    """Hit this every minute from cron-job.org. It escalates overdue tickets, sends the daily digest,
    and keeps the free server awake."""
    if not _eq(key, settings.CRON_SECRET):
        raise HTTPException(401)
    return tickets.tick(db)
