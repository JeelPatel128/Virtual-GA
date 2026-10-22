"""End-to-end test of the whole flow with no external services (no Gemini key, Teams captured,
calendar read from a local .ics file). Run:  python -m pytest -q tests  (or python tests/test_flow.py)"""
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
ICS = TMP / "shifts.ics"


def write_ics(events: list[tuple[str, datetime, datetime]]):
    fmt = lambda d: d.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body = "".join(
        f"BEGIN:VEVENT\r\nUID:{i}@test\r\nDTSTAMP:{fmt(s)}\r\nSUMMARY:{t}\r\nDTSTART:{fmt(s)}\r\nDTEND:{fmt(e)}\r\nEND:VEVENT\r\n"
        for i, (t, s, e) in enumerate(events))
    ICS.write_text("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n" + body + "END:VCALENDAR\r\n")


now = datetime.now(timezone.utc)
write_ics([
    ("Jeel Patel - Front Desk", now - timedelta(hours=1), now + timedelta(hours=2)),
    ("Desk: Two", now - timedelta(hours=1), now + timedelta(hours=2)),
    ("GA Three - Front Desk", now + timedelta(hours=3), now + timedelta(hours=5)),
])

os.environ.update({
    "DATABASE_URL": f"sqlite:///{TMP / 'test.db'}",
    "GEMINI_API_KEY": "",
    "TEAMS_WEBHOOK_URL": "",
    "GCAL_ICS_URL": f"file://{ICS}",
    "SCHEDULE_CACHE_MINUTES": "0",
    "ADMIN_PASSWORD": "adminpw",
    "GA_KEY": "gakey",
    "CRON_SECRET": "cronkey",
    "SECRET_KEY": "testsecret",
    "DIGEST_HOUR": "0",
    "TEAM_JSON": json.dumps([
        {"name": "Jeel Patel", "teams_email": "jp@stevens.edu", "aliases": ["JP"], "lead": True},
        {"name": "GA Two", "teams_email": "two@stevens.edu", "aliases": ["Two"], "lead": False},
        {"name": "GA Three", "teams_email": "three@stevens.edu", "aliases": [], "lead": False},
    ]),
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import db as db_module  # noqa: E402
from app import teams, tickets  # noqa: E402
from app.db import SessionLocal, Ticket  # noqa: E402
from app.main import app  # noqa: E402

A = {"X-Admin-Key": "adminpw"}


def test_migration_adds_columns_to_old_tables():
    """Simulates the real prod situation: 'sessions'/'tickets' already exist (old shape,
    pre-dating the directory feature) and must gain the new columns without create_all,
    which never alters an existing table."""
    import sqlite3
    from sqlalchemy import create_engine as _create_engine

    path = TMP / "old_schema.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, visitor_name TEXT, visitor_phone TEXT,
            phone_consent BOOLEAN, created_at TIMESTAMP
        );
        CREATE TABLE tickets (
            id INTEGER PRIMARY KEY, session_id TEXT, question TEXT, status TEXT,
            on_shift TEXT, responded_by TEXT, created_at TIMESTAMP,
            first_response_at TIMESTAMP, escalated_at TIMESTAMP, resolved_at TIMESTAMP
        );
    """)
    con.commit(); con.close()

    eng = _create_engine(f"sqlite:///{path}")
    db_module._ensure_columns(eng=eng)

    with eng.connect() as conn:
        session_cols = db_module._existing_columns(conn, "sessions")
        ticket_cols = db_module._existing_columns(conn, "tickets")
    assert {"last_person_id", "pending_query", "pending_choices"} <= session_cols
    assert {"kind", "recipient_person_id", "deadline_at"} <= ticket_cols
    assert {"owner", "owner_history", "activity", "next_deadline", "last_reminder_step",
           "snooze_count", "due_at", "cancelled_reason", "task", "file_name", "file_mime",
           "file_bytes", "requester_email", "confirmed", "confirmed_at",
           "file_backend", "file_ref"} <= ticket_cols
    eng.dispose()


def test_claim_is_atomic_only_one_wins():
    with TestClient(app) as c:
        sid = c.post("/api/session").json()["session_id"]
        tid = c.post(f"/api/session/{sid}/handoff", json={"name": "Alex", "question": "help"}).json()["ticket_id"]

        t = c.get(f"/api/ga/ticket/{tid}", headers={"X-GA-Key": "gakey"}).json()
        assert t["owner"] is None

        GA = {"X-GA-Key": "gakey"}
        r1 = c.post(f"/api/ga/ticket/{tid}/claim", headers=GA, json={"ga_name": "GA Two"})
        assert r1.status_code == 200 and r1.json()["owner"] == "GA Two", r1.text

        r2 = c.post(f"/api/ga/ticket/{tid}/claim", headers=GA, json={"ga_name": "Jeel Patel"})
        assert r2.status_code == 409, r2.text

        t = c.get(f"/api/ga/ticket/{tid}", headers={"X-GA-Key": "gakey"}).json()
        assert t["owner"] == "GA Two", "the second claim must not have overwritten the first"

        from app.db import Ticket as _Ticket
        db = SessionLocal()
        row = db.get(_Ticket, tid)
        assert row.owner_history[-1]["ga"] == "GA Two" and row.owner_history[-1]["reason"] == "claimed"
        assert len(row.owner_history) == 1, "a losing claim must not append a spurious history entry"
        # delete, not resolve: digest_data() aggregates ALL of today's tickets regardless
        # of status, and this row is disposable test fixture data (same lesson as the
        # message-ticket test above)
        db.delete(row)
        db.commit(); db.close()
    print("claim atomicity test passed")


def test_on_my_way_deadline_and_confirmation():
    with TestClient(app) as c:
        sid = c.post("/api/session").json()["session_id"]
        tid = c.post(f"/api/session/{sid}/handoff", json={"name": "Alex", "question": "help"}).json()["ticket_id"]
        GA = {"X-GA-Key": "gakey"}

        r = c.post(f"/api/ga/ticket/{tid}/action", headers=GA, json={"ga_name": "GA Two", "action": "on_my_way", "minutes": 5})
        assert r.status_code == 200

        from sqlalchemy import select
        from app.db import Ticket as _Ticket, utcnow as db_utcnow
        db = SessionLocal()
        row = db.get(_Ticket, tid)
        minutes_out = (row.next_deadline - db_utcnow()).total_seconds() / 60
        assert 9.9 <= minutes_out <= 10.1, minutes_out  # ETA (5) + 5 min buffer

        # before the deadline, no confirmation prompt
        m = c.get(f"/api/session/{sid}/messages").json()
        assert m["ticket"]["confirm_prompt"] is False, m["ticket"]

        # once the deadline has passed, the visitor is asked
        row.next_deadline = db_utcnow() - timedelta(minutes=1)
        db.commit()
        m = c.get(f"/api/session/{sid}/messages").json()
        assert m["ticket"]["confirm_prompt"] is True, m["ticket"]

        # "No" escalates right away and resets the deadline to now
        teams.sent_cards.clear()
        r = c.post(f"/api/session/{sid}/confirm_reached", json={"reached": False})
        assert r.status_code == 200
        esc = teams.sent_cards[-1]
        assert title(esc) == "Visitor says the GA never reached them"
        assert mentions(esc) == ["jp@stevens.edu"]
        m = c.get(f"/api/session/{sid}/messages").json()
        assert m["ticket"]["confirm_prompt"] is False, "must not re-prompt after an answer"
        assert m["ticket"]["status"] == "in_progress"

        db.delete(db.get(_Ticket, tid))
        db.commit(); db.close()
    print("on-my-way deadline / confirmation test passed")


def test_structured_snooze_and_third_snooze_auto_escalates():
    with TestClient(app) as c:
        sid = c.post("/api/session").json()["session_id"]
        tid = c.post(f"/api/session/{sid}/handoff", json={"name": "Alex", "question": "help"}).json()["ticket_id"]
        GA = {"X-GA-Key": "gakey"}

        # no free-text "later": a missing/invalid snooze_until is rejected
        r = c.post(f"/api/ga/ticket/{tid}/action", headers=GA,
                  json={"ga_name": "GA Two", "action": "busy", "reason": "stuff"})
        assert r.status_code == 400 and "follow-up time" in r.json()["detail"]

        # a reason is required
        r = c.post(f"/api/ga/ticket/{tid}/action", headers=GA,
                  json={"ga_name": "GA Two", "action": "busy", "snooze_until": "30min"})
        assert r.status_code == 400 and "reason is required" in r.json()["detail"]

        r = c.post(f"/api/ga/ticket/{tid}/action", headers=GA,
                  json={"ga_name": "GA Two", "action": "busy", "snooze_until": "30min", "reason": "meeting"})
        assert r.status_code == 200

        from sqlalchemy import select
        from app.db import Ticket as _Ticket, utcnow as db_utcnow
        db = SessionLocal()
        row = db.get(_Ticket, tid)
        minutes_out = (row.next_deadline - db_utcnow()).total_seconds() / 60
        assert 29 <= minutes_out <= 31, minutes_out
        assert row.snooze_count == 1 and row.owner == "GA Two"
        db.close()

        # 2nd snooze: still with GA Two, no reassignment yet
        c.post(f"/api/ga/ticket/{tid}/action", headers=GA,
              json={"ga_name": "GA Two", "action": "busy", "snooze_until": "2h", "reason": "still busy"})
        db = SessionLocal()
        row = db.get(_Ticket, tid)
        assert row.snooze_count == 2 and row.owner == "GA Two"
        db.close()

        # 3rd snooze: auto-reassigned to the lead
        c.post(f"/api/ga/ticket/{tid}/action", headers=GA,
              json={"ga_name": "GA Two", "action": "busy", "snooze_until": "tomorrow_10am", "reason": "again"})
        db = SessionLocal()
        row = db.get(_Ticket, tid)
        assert row.snooze_count == 3 and row.owner == "Jeel Patel", row.owner
        assert row.owner_history[-1]["ga"] == "Jeel Patel" and "3" in row.owner_history[-1]["reason"]
        db.delete(row)
        db.commit(); db.close()
    print("structured snooze / 3rd-snooze escalation test passed")


def test_reminder_ladder_catchup_offshift_and_autoclaim():
    with TestClient(app) as c:
        GA = {"X-GA-Key": "gakey"}
        from app.db import Ticket as _Ticket, utcnow as db_utcnow

        # --- reply on an unowned ticket auto-claims it ---
        sid = c.post("/api/session").json()["session_id"]
        tid = c.post(f"/api/session/{sid}/handoff", json={"name": "A", "question": "q"}).json()["ticket_id"]
        r = c.post(f"/api/ga/ticket/{tid}/action", headers=GA,
                  json={"ga_name": "GA Two", "action": "reply", "text": "on it"})
        assert r.status_code == 200 and r.json()["owner"] == "GA Two", r.text
        db = SessionLocal()
        db.delete(db.get(_Ticket, tid)); db.commit(); db.close()

        # --- catch-up: several missed steps at once still send exactly one card ---
        sid2 = c.post("/api/session").json()["session_id"]
        tid2 = c.post(f"/api/session/{sid2}/handoff", json={"name": "B", "question": "q2"}).json()["ticket_id"]
        db = SessionLocal()
        row2 = db.get(_Ticket, tid2)
        row2.next_deadline = db_utcnow() - timedelta(minutes=50)  # would be step 4 (3 steps missed)
        db.commit()
        teams.sent_cards.clear()
        fired = tickets.check_reminders(db)
        assert fired == 1, fired
        assert len(teams.sent_cards) == 1, "exactly one card, never one per missed step"
        card = teams.sent_cards[0]
        assert title(card) == "Team alert (missed steps 1, 2, 3)", title(card)
        db.refresh(row2)
        assert row2.last_reminder_step == 4
        db.delete(row2); db.commit(); db.close()

        # --- off-shift GA is never reminded for an unclaimed item ---
        # GA Two was on shift when this ticket was created (captured in on_shift), but the
        # calendar now shows only Jeel Patel on shift - the stale on_shift snapshot must
        # not be used for who gets reminded.
        sid3 = c.post("/api/session").json()["session_id"]
        tid3 = c.post(f"/api/session/{sid3}/handoff", json={"name": "C", "question": "q3"}).json()["ticket_id"]
        db = SessionLocal()
        row3 = db.get(_Ticket, tid3)
        assert "GA Two" in row3.on_shift, row3.on_shift  # sanity: it WAS on the creation-time snapshot
        row3.next_deadline = db_utcnow() - timedelta(minutes=1)
        db.commit()
        write_ics([("Jeel Patel - Front Desk", now - timedelta(hours=1), now + timedelta(hours=2))])  # GA Two now off
        teams.sent_cards.clear()
        tickets.check_reminders(db)
        esc = teams.sent_cards[-1]
        assert "two@stevens.edu" not in mentions(esc), esc
        assert mentions(esc) == ["jp@stevens.edu"], esc
        db.delete(db.get(_Ticket, tid3)); db.commit(); db.close()
        # restore the shared ICS for any later test that depends on GA Two being on shift
        write_ics([
            ("Jeel Patel - Front Desk", now - timedelta(hours=1), now + timedelta(hours=2)),
            ("Desk: Two", now - timedelta(hours=1), now + timedelta(hours=2)),
            ("GA Three - Front Desk", now + timedelta(hours=3), now + timedelta(hours=5)),
        ])
    print("reminder ladder catch-up / off-shift / auto-claim test passed")


def test_shift_end_headsup_and_conditional_reassignment():
    with TestClient(app) as c:
        GA = {"X-GA-Key": "gakey"}
        from app.db import Ticket as _Ticket, utcnow as db_utcnow

        # --- heads-up: fires once when a shift is 15 min or less from ending ---
        write_ics([("Desk: Two", now - timedelta(hours=2), now + timedelta(minutes=10))])
        sid = c.post("/api/session").json()["session_id"]
        tid = c.post(f"/api/session/{sid}/handoff", json={"name": "A", "question": "help A"}).json()["ticket_id"]
        c.post(f"/api/ga/ticket/{tid}/claim", headers=GA, json={"ga_name": "GA Two"})
        teams.sent_cards.clear()
        db = SessionLocal()
        assert tickets.check_shift_ends(db) == 1
        assert "shift ends in" in title(teams.sent_cards[-1])
        teams.sent_cards.clear()
        tickets.check_shift_ends(db)
        assert len(teams.sent_cards) == 0, "heads-up must fire only once per shift end"
        db.delete(db.get(_Ticket, tid)); db.commit(); db.close()

        # --- conditional reassignment: both GAs' shifts have already ended (simulated by
        # seeding KV directly, since shift_end_for can't find an ended shift after the
        # fact - this is exactly the bug the implementation works around); GA Two's next
        # shift is far away, GA Three's is coming up soon ---
        write_ics([
            ("Desk: Two", now + timedelta(hours=3), now + timedelta(hours=5)),
            ("GA Three - Front Desk", now + timedelta(minutes=10), now + timedelta(hours=2)),
        ])
        db = SessionLocal()
        from app.db import kv_set as db_kv_set
        ended_at = (db_utcnow() - timedelta(minutes=1)).isoformat()
        db_kv_set(db, "shiftend:GA Two", ended_at)
        db_kv_set(db, "shiftend:GA Three", ended_at)
        db.commit()

        sid2 = c.post("/api/session").json()["session_id"]
        tidA = c.post(f"/api/session/{sid2}/handoff", json={"name": "B", "question": "due now"}).json()["ticket_id"]
        c.post(f"/api/ga/ticket/{tidA}/claim", headers=GA, json={"ga_name": "GA Two"})
        rowA = db.get(_Ticket, tidA)
        rowA.next_deadline = db_utcnow()  # due now - before GA Two's next shift (3h away)
        db.commit()

        sid3 = c.post("/api/session").json()["session_id"]
        tidB = c.post(f"/api/session/{sid3}/handoff", json={"name": "C", "question": "due later"}).json()["ticket_id"]
        c.post(f"/api/ga/ticket/{tidB}/claim", headers=GA, json={"ga_name": "GA Three"})
        rowB = db.get(_Ticket, tidB)
        rowB.next_deadline = db_utcnow() + timedelta(hours=1)  # after GA Three's next shift (10 min away)
        db.commit()

        tickets.check_shift_ends(db)
        db.refresh(rowA); db.refresh(rowB)
        assert rowA.owner != "GA Two", "deadline before the owner's next shift must be reassigned"
        assert rowB.owner == "GA Three", "deadline after the owner's next shift must NOT be reassigned"

        db.delete(rowA); db.delete(rowB)
        db.commit(); db.close()
        write_ics([
            ("Jeel Patel - Front Desk", now - timedelta(hours=1), now + timedelta(hours=2)),
            ("Desk: Two", now - timedelta(hours=1), now + timedelta(hours=2)),
            ("GA Three - Front Desk", now + timedelta(hours=3), now + timedelta(hours=5)),
        ])
    print("shift-end heads-up / conditional reassignment test passed")


def test_shift_start_first_tick_sends_nothing_then_detects_new_shift():
    with TestClient(app) as c:
        db = SessionLocal()
        from app.db import kv_get as db_kv_get

        # first tick ever for this snapshot key: no cards, just save the baseline -
        # otherwise every GA already on shift at deploy time would get a false
        # "you just started" card
        assert db_kv_get(db, "on_shift_snapshot") is None, "test must start with no snapshot"
        teams.sent_cards.clear()
        fired = tickets.check_shift_starts(db)
        assert fired == 0, "the very first tick must send no shift-start cards"
        assert len(teams.sent_cards) == 0
        assert db_kv_get(db, "on_shift_snapshot") is not None, "baseline must now be saved"

        # a GA who wasn't on the previous snapshot but is now on shift gets a card
        write_ics([
            ("Jeel Patel - Front Desk", now - timedelta(hours=1), now + timedelta(hours=2)),
            ("Desk: Two", now - timedelta(hours=1), now + timedelta(hours=2)),
            ("GA Three - Front Desk", now - timedelta(minutes=1), now + timedelta(hours=1)),
        ])
        teams.sent_cards.clear()
        fired = tickets.check_shift_starts(db)
        assert fired == 1, fired
        card = teams.sent_cards[-1]
        assert title(card) == "Shift starting: here's what's open"
        assert mentions(card) == ["three@stevens.edu"]

        # calling again with the same on-shift set sends nothing more
        teams.sent_cards.clear()
        tickets.check_shift_starts(db)
        assert len(teams.sent_cards) == 0, "must not re-notify a GA already on the snapshot"
        db.close()
        write_ics([
            ("Jeel Patel - Front Desk", now - timedelta(hours=1), now + timedelta(hours=2)),
            ("Desk: Two", now - timedelta(hours=1), now + timedelta(hours=2)),
            ("GA Three - Front Desk", now + timedelta(hours=3), now + timedelta(hours=5)),
        ])
    print("shift-start detection test passed")


def test_storage_module_degrades_and_builds_correct_requests():
    """app/storage.py: offline tests only, no real network calls. Mocks requests.* to
    check it builds the right calls, and confirms it degrades gracefully (returns None /
    no-ops) when Supabase isn't configured - exactly the real state of this repo's .env
    today (SUPABASE_URL/SUPABASE_SERVICE_KEY are unset)."""
    from unittest.mock import patch, MagicMock
    from app import storage

    assert storage.enabled() is False, "must reflect the real (unset) settings"
    assert storage.upload(b"data", "application/pdf") is None
    assert storage.signed_url("some/path") is None
    storage.delete("some/path")  # no-op, must not raise

    with patch.object(storage.settings, "SUPABASE_URL", "https://test.supabase.co"), \
         patch.object(storage.settings, "SUPABASE_SERVICE_KEY", "test-key"):
        assert storage.enabled() is True

        with patch("app.storage.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            path = storage.upload(b"filedata", "application/pdf")
            assert path is not None
            call = mock_post.call_args
            assert f"/storage/v1/object/{storage.BUCKET}/{path}" in call.args[0]
            assert call.kwargs["headers"]["Authorization"] == "Bearer test-key"
            assert call.kwargs["data"] == b"filedata"

        with patch("app.storage.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, json=lambda: {"signedURL": "/object/sign/x?token=abc"})
            url = storage.signed_url("myfile")
            assert url == "https://test.supabase.co/storage/v1/object/sign/x?token=abc"

        with patch("app.storage.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=403, text="forbidden")
            assert storage.upload(b"x", "application/pdf") is None, "a failed upload must return None, not raise"

        with patch("app.storage.requests.delete") as mock_delete:
            mock_delete.return_value = MagicMock(status_code=200)
            storage.delete("myfile")
            assert f"/storage/v1/object/{storage.BUCKET}/myfile" in mock_delete.call_args.args[0]
    print("storage module test passed")


def test_task_request_creation_and_file_privacy():
    with TestClient(app) as c:
        GA = {"X-GA-Key": "gakey"}
        due = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

        sid = c.post("/api/session").json()["session_id"]
        teams.sent_cards.clear()
        r = c.post(f"/api/session/{sid}/task", data={
            "name": "Prof. Lee", "email": "lee@stevens.edu", "type": "Print/copies",
            "details": "Need 20 copies of the syllabus", "copies": "20", "due_at": due,
            "pickup_location": "Room 303"})
        assert r.status_code == 200, r.text
        tid = r.json()["ticket_id"]
        card = teams.sent_cards[-1]
        assert title(card) == "Task request: Print/copies"
        assert "20" in json.dumps(card) and "Room 303" in json.dumps(card)

        t = c.get(f"/api/ga/ticket/{tid}", headers=GA).json()
        assert t["kind"] == "task" and t["task"]["type"] == "Print/copies" and t["task"]["copies"] == 20
        assert t["due_at"] is not None and t["file_name"] is None

        # an invalid task type is rejected
        sid_invalid = c.post("/api/session").json()["session_id"]
        r2 = c.post(f"/api/session/{sid_invalid}/task", data={
            "name": "A", "type": "Not a real type", "details": "x", "due_at": due})
        assert r2.status_code == 400, r2.text

        from app.db import Ticket as _Ticket
        db = SessionLocal()
        db.delete(db.get(_Ticket, tid)); db.commit(); db.close()

        # --- with a file: Supabase isn't configured in this test env, so it must use
        # the local TaskFile fallback - and the file bytes must never appear in any
        # ticket-list/ticket-detail response, only via the dedicated file endpoint ---
        sid2 = c.post("/api/session").json()["session_id"]
        r = c.post(f"/api/session/{sid2}/task", data={
            "name": "Sam", "type": "Other", "details": "see attached", "due_at": due},
            files={"file": ("notes.pdf", b"%PDF-1.4 fake pdf content", "application/pdf")})
        assert r.status_code == 200, r.text
        tid2 = r.json()["ticket_id"]

        t2 = c.get(f"/api/ga/ticket/{tid2}", headers=GA).json()
        assert t2["file_name"] == "notes.pdf"
        assert "file_bytes" not in json.dumps(t2), "raw bytes must never appear in a ticket response"
        listed = c.get("/api/ga/tickets", headers=GA).json()["tickets"]
        assert "fake pdf content" not in json.dumps(listed)

        fr = c.get(f"/api/ga/ticket/{tid2}/file", headers=GA)
        assert fr.status_code == 200 and fr.content == b"%PDF-1.4 fake pdf content"
        assert fr.headers["content-type"] == "application/pdf"

        # an oversized or disallowed file type is rejected
        big = b"0" * (11 * 1024 * 1024)
        sid3 = c.post("/api/session").json()["session_id"]
        r3 = c.post(f"/api/session/{sid3}/task", data={"name": "B", "type": "Other", "details": "x", "due_at": due},
                   files={"file": ("big.pdf", big, "application/pdf")})
        assert r3.status_code == 413

        sid4 = c.post("/api/session").json()["session_id"]
        r4 = c.post(f"/api/session/{sid4}/task", data={"name": "C", "type": "Other", "details": "x", "due_at": due},
                   files={"file": ("virus.exe", b"MZ", "application/octet-stream")})
        assert r4.status_code == 400

        # neither rejected-file case must leave a dangling ticket + already-sent Teams
        # card behind - the file is validated before the ticket is ever created
        db = SessionLocal()
        assert tickets.open_ticket_for(db, sid3) is None
        assert tickets.open_ticket_for(db, sid4) is None
        from sqlalchemy import select as _select
        from app.db import TaskFile as _TaskFile
        db.delete(db.get(_Ticket, tid2))
        for tf in db.scalars(_select(_TaskFile)).all():
            db.delete(tf)
        db.commit(); db.close()
    print("task request creation / file privacy test passed")


def mentions(card):
    return [e["mentioned"]["id"] for e in card["attachments"][0]["content"]["msteams"]["entities"]]


def title(card):
    return card["attachments"][0]["content"]["body"][0]["text"]


PEOPLE_CSV_HEADER = ("name,first_name,last_name,aliases,title,type,building,room,floor,directions,"
                     "office_hours,extension,email,roles,route_topics_here,show_publicly,last_verified,review_notes")

HOURS = "Mon to Fri, 9 AM to 5 PM"
DIRECTORY_FIXTURE_CSV = PEOPLE_CSV_HEADER + "\n" + "\n".join([
    # alias "Jose" is a literal bare alias; route_topics_here=no
    f'Josa Balisi,Josa,Balisi,"Balisi | Josa | Jose",Assoc. Dir. of Academic Operations,Staff,'
    f'Babbio Center,401J,4,"Stand at the front desk. 401J is on your right.","{HOURS}",8070,,'
    f'academic operations,no,yes,2026-01-01,',
    # alias "Howie" is a literal bare alias; no directions on file (matches real data shape)
    f'Haoying Xu,Haoying,Xu,"Dr. Xu | Haoying | Howie | Howie Xu | Prof. Xu | Xu",Assistant Professor,'
    f'Faculty,Babbio Center,422A,4,,"{HOURS}",3476,,,yes,yes,2026-01-01,',
    # typo test target: "Profesor Altonen" must fuzzy-resolve here
    f'Aleksi Aaltonen,Aleksi,Aaltonen,"Aaltonen | Aleksi | Dr. Aaltonen | Prof. Aaltonen | '
    f'Professor Aaltonen | Professor Aleksi",Associate Professor,Faculty,Babbio Center,616,6,,'
    f'"{HOURS}",5423,,,yes,yes,2026-01-01,',
    # floor 4, WITH directions -> should get the map
    f'Christina Alwell,Christina,Alwell,"Alwell | Christina",Manager of Industry Capstone Program,'
    f'Staff,Babbio Center,401G,4,"Stand at the front desk. 401G is on your left.","{HOURS}",5396,,'
    f'Industry Capstone program,yes,yes,2026-01-01,',
    # synthetic: floor 6, WITH directions -> must NOT get the map (proves the floor gate)
    f'Floor Six Fixture,Floor,Fixture,"Floor Six Fixture",Research Fellow,Faculty,Babbio Center,'
    f'616B,6,"Test directions that should never trigger the 4th-floor map.","{HOURS}",9999,,,'
    f'yes,yes,2026-01-01,',
    # Altorfer, no directions
    f'Altorfer Fixture,Altorfer,Fixture,"Altorfer Fixture",Senior Lecturer,Faculty,Altorfer,404,4,,'
    f'"{HOURS}",8642,,,yes,yes,2026-01-01,',
    # Remote
    f'Remote Fixture,Remote,Fixture,"Remote Fixture",Development Officer,Staff,Remote,,,,'
    f'"{HOURS}",3487,,alumni giving,yes,yes,2026-01-01,',
    # academic advisor (graduate) vs the plain "academic advisor" below -> role-match test
    f'Lisa Burns,Lisa,Burns,Burns,Senior Graduate Academic Advisor,Staff,Babbio Center,303L,3,,'
    f'"{HOURS}",8595,,academic advisor (graduate),yes,yes,2026-01-01,',
    f'Lisa Cavanaugh,Lisa,Cavanaugh,Cavanaugh,Academic Advisor,Staff,Babbio Center,303M,3,,'
    f'"{HOURS}",8596,,academic advisor,yes,yes,2026-01-01,',
    # COPA / PhD / Dean role-match tests. Names deliberately do NOT contain "copa" anywhere
    # (name/alias) - only the roles column does - so a "COPA person" query is forced through
    # the ROLE-matching path, not an accidental name match.
    f'Priya Shah,Priya,Shah,"Priya Shah",Careers Coordinator,Staff,Babbio Center,303N,3,,'
    f'"{HOURS}",8597,,"COPA, careers, resume help, recruiters",yes,yes,2026-01-01,',
    # a second person sharing the SAME role, to test the role-group answer (list all, never
    # "did you mean") - on floor 4 with directions, so the map-attachment logic is exercised
    f'Marcus Webb,Marcus,Webb,"Marcus Webb",Corporate Outreach Manager,Staff,Babbio Center,'
    f'401K,4,"Stand at the front desk. 401K is on your right.","{HOURS}",8599,,'
    f'"COPA, careers, resume help, recruiters",yes,yes,2026-01-01,',
    f'Phd Fixture,Phd,Fixture,"Phd Fixture",PhD Program Coordinator,Staff,Babbio Center,303O,3,,'
    f'"{HOURS}",8598,,"PhD program, dissertation forms",yes,yes,2026-01-01,',
    f'GJ de Vreede,GJ,de Vreede,"Dean de Vreede | GJ | de Vreede | the Dean",Dean,Staff,Babbio Center,'
    f'408A,4,"Walk past the 401 offices. 408A is in the left corner.","{HOURS}",8565,,'
    f'Dean of the School of Business,no,yes,2026-01-01,',
    f'Megan Maher,Megan,Maher,"Maher | Megan",Executive Assistant to the Dean,Staff,Babbio Center,'
    f'401E,4,"Stand at the front desk. 401E is on your left.","{HOURS}",8548,,"Dean office",no,yes,'
    f'2026-01-01,',
    # two Chens for disambiguation tests (bare "Chen" matches last_name on both, not an alias)
    f'Jing Chen,Jing,Chen,"Dr. Chen | Jing | Prof. Chen | Professor Chen | Professor Jing",'
    f'Associate Professor,Faculty,Babbio Center,414,4,,"{HOURS}",8062,,,yes,yes,2026-01-01,',
    f'Violet Chen,Violet,Chen,"Dr. Chen | Prof. Chen | Professor Chen | Professor Violet | Violet",'
    f'Assistant Professor,Faculty,Babbio Center,629,6,,"{HOURS}",3521,,,yes,yes,2026-01-01,',
    # common-word surname, to make the common-word-gate regression test meaningful
    f'Jamie Park,Jamie,Park,"Park | Jamie",Lecturer,Faculty,Babbio Center,617,6,,"{HOURS}",'
    f'5500,,,yes,yes,2026-01-01,',
])


ROOMS_FIXTURE_CSV = "place,directions_from_front_desk\n" + "\n".join([
    'Lounge,"The Lounge is on your right, a little behind you."',
    'Washrooms,"The washrooms are on your left, past office 414."',
    'Conference Room 431,"Conference Room 431 is on your right, just ahead of the front desk."',
    '401 offices,"The 401 offices are right behind the front desk."',
    '433 offices,"The 433 offices are the block right behind the lab."',
    'Room 414,"414 is just before the washrooms."',
])


def _import_directory_fixture(c) -> None:
    r = c.post("/api/admin/directory/import", headers=A, files={"file": ("dir.csv", DIRECTORY_FIXTURE_CSV, "text/csv")})
    assert r.status_code == 200, r.text
    r = c.post("/api/admin/rooms/import", headers=A, files={"file": ("rooms.csv", ROOMS_FIXTURE_CSV, "text/csv")})
    assert r.status_code == 200, r.text


def _ask_text(c, sid: str, text: str) -> dict:
    c.post(f"/api/session/{sid}/ask", json={"text": text})
    return c.get(f"/api/session/{sid}/messages").json()["messages"][-1]


def test_directory_name_matching_and_building_formats():
    with TestClient(app) as c:
        _import_directory_fixture(c)
        sid = c.post("/api/session").json()["session_id"]

        last = _ask_text(c, sid, "Howie")
        assert "Haoying Xu" in last["text"], last
        assert "A GA can point you there" in last["text"], last  # no directions on file

        last = _ask_text(c, sid, "Jose")
        assert "Josa Balisi" in last["text"], last

        last = _ask_text(c, sid, "Profesor Altonen")
        assert "Aleksi Aaltonen" in last["text"], last

        last = _ask_text(c, sid, "where's Christina Alwell")
        assert "Christina Alwell" in last["text"] and "401G" in last["text"], last
        assert last["meta"].get("images") == ["/api/directory/floor-map"], last

        last = _ask_text(c, sid, "where's the Floor Six Fixture person")
        assert "Floor Six Fixture" in last["text"], last
        assert not last["meta"].get("images"), "map must not attach off the 4th floor"

        last = _ask_text(c, sid, "where is Altorfer Fixture")
        assert "Altorfer building" in last["text"], last

        last = _ask_text(c, sid, "where is Remote Fixture")
        assert "work remotely" in last["text"], last

        last = _ask_text(c, sid, "is Professor Nguyen in today")
        assert last["meta"].get("offer_handoff") is True, last
        assert "couldn't find" in last["text"].lower(), last
    print("directory name matching + building formats test passed")


def test_directory_import_upserts_and_preserves_edits():
    csv1 = PEOPLE_CSV_HEADER + "\n" + "\n".join([
        'Alice Example,Alice,Example,Alice,Professor,Faculty,Babbio Center,101,1,,'
        '"Mon to Fri, 9 AM to 5 PM",1111,alice@example.com,,yes,yes,2026-01-01,',
        'Bob Example,Bob,Example,Bob,Staff,Staff,Altorfer,202,2,,'
        '"Mon to Fri, 9 AM to 5 PM",2222,,,yes,yes,2026-01-01,',
    ])
    csv2 = PEOPLE_CSV_HEADER + "\n" + (
        'Alice Example,Alice,Example,Alice,Professor,Faculty,Babbio Center,101,1,,'
        '"Mon to Fri, 9 AM to 5 PM",1111,,,yes,yes,2026-01-01,'  # email blank this time
    )
    with TestClient(app) as c:
        r1 = c.post("/api/admin/directory/import", headers=A, files={"file": ("d1.csv", csv1, "text/csv")})
        assert r1.status_code == 200 and r1.json()["added"] == 2, r1.text

        r2 = c.post("/api/admin/directory/import", headers=A, files={"file": ("d2.csv", csv2, "text/csv")})
        assert r2.status_code == 200
        assert r2.json()["updated"] == 1 and r2.json()["inactivated"] == 1, r2.json()

        from sqlalchemy import select
        from app.db import Person
        db = SessionLocal()
        alice = db.scalar(select(Person).where(Person.name == "Alice Example"))
        bob = db.scalar(select(Person).where(Person.name == "Bob Example"))
        assert alice.email == "alice@example.com", "blank CSV cell must not wipe an existing value"
        assert alice.active is True
        assert bob.active is False, "a row missing from the new CSV must be hidden, not deleted"
        db.close()
    print("directory import upsert test passed")


def test_extension_hidden_unless_admin_enables_it():
    with TestClient(app) as c:
        _import_directory_fixture(c)
        sid = c.post("/api/session").json()["session_id"]

        last = _ask_text(c, sid, "what's Howie's extension")
        assert "3476" not in last["text"], last
        assert "can't share extensions" in last["text"], last

        r = c.get("/api/admin/settings", headers=A)
        assert r.json()["show_extensions"] is False
        assert c.post("/api/admin/settings", headers=A, json={"show_extensions": True}).status_code == 200
        assert c.get("/api/admin/settings", headers=A).json()["show_extensions"] is True

        last = _ask_text(c, sid, "what's Howie's extension")
        assert "3476" in last["text"], last

        c.post("/api/admin/settings", headers=A, json={"show_extensions": False})
    print("extension gating test passed")


def test_disambiguation_buttons():
    with TestClient(app) as c:
        _import_directory_fixture(c)
        sid = c.post("/api/session").json()["session_id"]

        last = _ask_text(c, sid, "Chen")
        choices = last["meta"].get("choices") or []
        names = {ch["label"].split(" (")[0] for ch in choices}
        assert names == {"Jing Chen", "Violet Chen"}, last

        # a stale/unlisted id must not be silently answered
        bad_id = max(ch["id"] for ch in choices) + 999
        r = c.post(f"/api/session/{sid}/resolve_person", json={"person_id": bad_id})
        assert r.status_code == 200
        last = c.get(f"/api/session/{sid}/messages").json()["messages"][-1]
        assert "wasn't one of the options" in last["text"], last

        # re-ask and actually pick one this time
        last = _ask_text(c, sid, "Chen")
        choices = last["meta"]["choices"]
        jing = next(ch for ch in choices if ch["label"].startswith("Jing Chen"))
        c.post(f"/api/session/{sid}/resolve_person", json={"person_id": jing["id"]})
        last = c.get(f"/api/session/{sid}/messages").json()["messages"][-1]
        assert "Jing Chen" in last["text"], last
    print("disambiguation buttons test passed")


def test_remembered_person_pronoun_followup():
    with TestClient(app) as c:
        _import_directory_fixture(c)
        sid = c.post("/api/session").json()["session_id"]

        last = _ask_text(c, sid, "Howie")
        assert "Haoying Xu" in last["text"], last

        last = _ask_text(c, sid, "is he in today")
        assert "Haoying Xu" in last["text"], last

        # a stale remembered id (e.g. after a re-import deleted/replaced it) must not crash
        from sqlalchemy import select
        from app.db import ChatSession
        db = SessionLocal()
        s = db.get(ChatSession, sid)
        s.last_person_id = 999999
        db.commit(); db.close()
        last = _ask_text(c, sid, "is he around")
        assert "999999" not in str(last)
    print("remembered person / pronoun follow-up test passed")


def test_role_matching():
    with TestClient(app) as c:
        _import_directory_fixture(c)
        sid = c.post("/api/session").json()["session_id"]

        last = _ask_text(c, sid, "my grad advisor")
        assert "Lisa Burns" in last["text"], last
        assert "Lisa Cavanaugh" not in last["text"], last

        last = _ask_text(c, sid, "COPA person")
        assert "Priya Shah" in last["text"] and "Marcus Webb" in last["text"], last
        assert not last["meta"].get("choices"), "a role shared by several people is listed, not asked about"

        last = _ask_text(c, sid, "Capstone")
        assert "Christina Alwell" in last["text"], last

        last = _ask_text(c, sid, "PhD forms")
        assert "Phd Fixture" in last["text"], last

        last = _ask_text(c, sid, "the Dean")
        assert "GJ de Vreede" in last["text"], last
    print("role matching test passed")


def test_role_match_lists_everyone_name_match_still_asks():
    """A role shared by several people is listed, with room and directions, in one answer.
    A NAME shared by several people (e.g. Chen) still gets "Did you mean?" buttons - these
    are different kinds of ambiguity and must not be handled the same way."""
    with TestClient(app) as c:
        _import_directory_fixture(c)
        sid = c.post("/api/session").json()["session_id"]

        last = _ask_text(c, sid, "COPA person")
        assert not last["meta"].get("choices"), last
        text = last["text"]
        assert "Priya Shah" in text and "Room 303N" in text, text
        assert "Marcus Webb" in text and "401K" in text, text
        assert "Stand at the front desk. 401K is on your right." in text, text
        assert last["meta"].get("images") == ["/api/directory/floor-map"], last

        last = _ask_text(c, sid, "Chen")
        assert last["meta"].get("choices"), "a shared NAME must still ask, not list"
        assert "Room" not in last["text"], "a name disambiguation prompt lists options, not locations"
    print("role-group-vs-name-disambiguation test passed")


def test_route_topics_here_excludes_from_role_matching():
    with TestClient(app) as c:
        _import_directory_fixture(c)
        sid = c.post("/api/session").json()["session_id"]

        # Josa Balisi has route_topics_here=no: a role-shaped query for her topic must not
        # surface her as "the person to handle this" (falls through, since the phrasing has
        # no title word / name signal of its own).
        last = _ask_text(c, sid, "who handles academic operations")
        assert "Josa Balisi" not in last["text"], last

        # her name/alias must still resolve normally - just not via the role path
        last = _ask_text(c, sid, "Jose")
        assert "Josa Balisi" in last["text"], last
    print("route_topics_here exclusion test passed")


def test_place_matching():
    with TestClient(app) as c:
        _import_directory_fixture(c)
        sid = c.post("/api/session").json()["session_id"]

        last = _ask_text(c, sid, "where's the lounge")
        assert "Lounge is on your right" in last["text"], last
        assert last["meta"].get("images") == ["/api/directory/floor-map"], last

        last = _ask_text(c, sid, "where's the washroom")
        assert "washrooms are on your left" in last["text"], last

        last = _ask_text(c, sid, "room 414")
        assert "414 is just before" in last["text"], last

        last = _ask_text(c, sid, "401 offices")
        assert "401 offices are right behind" in last["text"], last

        last = _ask_text(c, sid, "433 offices")
        assert "433 offices are the block" in last["text"], last
    print("place matching test passed")


FAQ_REGRESSION_QUESTIONS = [
    "can I park here",
    "oh where is the printer",
    "do you have an HDMI cable",
    "how do I register for a COPA workshop",
    "does the capstone project count for credit",
    "can I book the conference room",
    "is there wifi in the building",
    "where can I print something",
    "what are COPA walk-in hours",
    "how do I reserve a conference room for a meeting",
    "where is the nearest parking garage",
    "is the sun out today",
    "how do I connect to the printer",
    "what's the wifi password",
    "is there a charger I can borrow",
    "can I get a parking pass",
    "where do I return a library book",
    "how do I print my transcript",
    "is there a vending machine nearby",
    "where is the nearest ATM",
    "can I get a visitor badge",
    "how do I book a study room",
    "is there a lost and found",
    "where is the elevator",
    "how do I get to the fourth floor",
    "is the gym open right now",
    "where can I buy coffee",
    "how do I submit a reimbursement form",
    "is there a printer on this floor",
    "can I charge my phone here",
    "where is the nearest bathroom",
    "how do I get a guest wifi code",
    "is there a shuttle to campus",
    "where do I pick up my ID card",
    "can someone fix the projector",
    "is the HDMI cable in the drawer",
    "how long is the park open",
    "where is the sun deck",
    "can I sit in the lounge and work",
    "is there a quiet study space",
    "how do I reset my student email password",
    "where is the nearest printer that takes color",
    "can I get a campus map",
    "is there parking validation available",
    "where do I drop off a package",
    "how do I request a transcript",
    "is there a notary on campus",
    "can I use the whiteboard in the conference room",
    "where is the recycling bin",
    "how do I report a maintenance issue",
]


def test_common_word_gate_faq_regression():
    with TestClient(app) as c:
        _import_directory_fixture(c)
        sid = c.post("/api/session").json()["session_id"]

        for q in FAQ_REGRESSION_QUESTIONS:
            last = _ask_text(c, sid, q)
            assert "Jamie Park" not in last["text"], (q, last)
            assert "couldn't find anyone" not in last["text"], (q, last)
            assert not last["meta"].get("choices"), (q, last)

        # a real person must still resolve when a title word genuinely supports it
        last = _ask_text(c, sid, "is Professor Park in today")
        assert "Jamie Park" in last["text"], last
    print(f"FAQ regression test passed ({len(FAQ_REGRESSION_QUESTIONS)} questions)")


def test_message_ticket_deadline_and_escalation():
    with TestClient(app) as c:
        _import_directory_fixture(c)
        sid = c.post("/api/session").json()["session_id"]

        last = _ask_text(c, sid, "is Howie in today")
        assert last["meta"].get("availability_buttons") is True, last
        person_id = last["meta"]["person_id"]

        teams.sent_cards.clear()
        r = c.post(f"/api/session/{sid}/handoff", json={
            "name": "Alex Visitor", "question": "Is Howie in right now?",
            "message_for_person_id": person_id})
        assert r.status_code == 200, r.text
        tid = r.json()["ticket_id"]
        card = teams.sent_cards[-1]
        assert title(card) == "Message for Haoying Xu", card

        from app.db import utcnow as db_utcnow
        db = SessionLocal()
        t = db.get(Ticket, tid)
        assert t.kind == "message" and t.recipient_person_id == person_id
        assert t.next_deadline is not None
        minutes_out = (t.next_deadline - db_utcnow()).total_seconds() / 60
        assert minutes_out > 60, "deadline should be the end of the ~3hr shift window, not +5 min"

        # make it overdue and confirm the reminder ladder picks it up (call
        # check_reminders directly, not the combined /tasks/tick, so this doesn't also
        # trip the once-a-day digest flag and leak that into a later test)
        t.next_deadline = db_utcnow() - timedelta(minutes=1)
        db.commit()
        teams.sent_cards.clear()
        assert tickets.check_reminders(db) == 1
        esc = next(card for card in teams.sent_cards if title(card) == "Reminder")
        # unclaimed: step 1 targets whoever is actually on shift right now, not the lead alone
        assert set(mentions(esc)) == {"jp@stevens.edu", "two@stevens.edu"}, esc

        # delete, not just resolve: digest_data() aggregates ALL of today's tickets
        # regardless of status, and this row is disposable test fixture data that would
        # otherwise inflate test_full_flow()'s own handoffs/escalated counts for "today"
        db.delete(db.get(Ticket, tid))
        db.commit(); db.close()
    print("message ticket deadline/escalation test passed")


def test_ga_directory_search():
    with TestClient(app) as c:
        _import_directory_fixture(c)
        assert c.get("/api/ga/directory/search?q=Howie").status_code == 401
        r = c.get("/api/ga/directory/search?q=Howie", headers={"X-GA-Key": "gakey"})
        assert r.status_code == 200
        results = r.json()["results"]
        assert any(p["name"] == "Haoying Xu" for p in results), results
        xu = next(p for p in results if p["name"] == "Haoying Xu")
        assert xu["extension"] == "3476", "GA console always sees the extension"
    print("GA directory search test passed")


def test_admin_status_reports_directory_counts():
    with TestClient(app) as c:
        _import_directory_fixture(c)
        s = c.get("/api/admin/status", headers=A).json()
        assert s["people_count"] == 17, s
        assert s["rooms_count"] == 6, s
    print("admin status directory counts test passed")


def test_email_candidate_review_flow():
    with TestClient(app) as c:
        _import_directory_fixture(c)

        csv_text = "name,email,confidence,source_url\nHaoying Xu,hxu@stevens.edu,high,https://example.edu/profile/hxu\n"
        r = c.post("/api/admin/emails/import", headers=A, files={"file": ("cands.csv", csv_text, "text/csv")})
        assert r.status_code == 200 and r.json()["added"] == 1, r.text

        cands = c.get("/api/admin/emails/candidates", headers=A).json()["candidates"]
        pending = next(x for x in cands if x["name"] == "Haoying Xu")

        # reject must not touch the directory
        r = c.post(f"/api/admin/emails/candidates/{pending['id']}/reject", headers=A)
        assert r.status_code == 200

        # re-import the same candidate and approve it this time
        c.post("/api/admin/emails/import", headers=A, files={"file": ("cands2.csv", csv_text, "text/csv")})
        cands = c.get("/api/admin/emails/candidates", headers=A).json()["candidates"]
        pending = next(x for x in cands if x["name"] == "Haoying Xu")
        r = c.post(f"/api/admin/emails/candidates/{pending['id']}/approve", headers=A)
        assert r.status_code == 200, r.text

        from sqlalchemy import select
        from app.db import Person
        db = SessionLocal()
        xu = db.scalar(select(Person).where(Person.name == "Haoying Xu"))
        assert xu.email == "hxu@stevens.edu", xu.email
        db.close()

        # approving a candidate with no matching directory entry is a 409, not a crash
        csv2 = "name,email,confidence,source_url\nNobody Here,x@stevens.edu,low,\n"
        c.post("/api/admin/emails/import", headers=A, files={"file": ("c3.csv", csv2, "text/csv")})
        cands = c.get("/api/admin/emails/candidates", headers=A).json()["candidates"]
        nobody = next(x for x in cands if x["name"] == "Nobody Here")
        r = c.post(f"/api/admin/emails/candidates/{nobody['id']}/approve", headers=A)
        assert r.status_code == 409
    print("email candidate review flow test passed")


def test_find_emails_script_pure_functions():
    """tools/find_emails.py is a standalone script with no web app import - these are
    offline tests of its parsing/decoding logic only. No network calls."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import find_emails

    def cf_encode(email: str, key: int = 0x2a) -> str:
        data = bytes([key]) + bytes(b ^ key for b in email.encode())
        return data.hex()

    hexval = cf_encode("test.person@stevens.edu")
    assert find_emails.cf_decode(hexval) == "test.person@stevens.edu"

    html_attr = f'<span class="__cf_email__" data-cfemail="{hexval}">[email protected]</span>'
    assert find_emails.extract_emails(html_attr) == ["test.person@stevens.edu"]

    html_href = f'<a href="/cdn-cgi/l/email-protection#{hexval}">[email protected]</a>'
    assert find_emails.extract_emails(html_href) == ["test.person@stevens.edu"]

    # a non-stevens.edu address must never be returned, even if decodable
    other_hex = cf_encode("someone@gmail.com")
    assert find_emails.extract_emails(f'<span data-cfemail="{other_hex}"></span>') == []

    # a synthetic faculty card, matching the real site's structure
    card_html = (
        '<div class="faculty-card_cc--faculty-card__j3sBO"><div class="faculty-card_c--faculty-card__y49jx">'
        '<div class="faculty-card_name-title__WxAmJ"><h3><a href="/profile/jtest">Jamie Test</a></h3>'
        '<h4>Assistant Professor</h4></div>'
        '<div class="faculty-card_contact__w6Wob">'
        '<div class="faculty-card_contact-office__sswX5">Babbio Center 500</div>'
        f'<div class="faculty-card_contact-email__pxACB"><a href="/cdn-cgi/l/email-protection#{hexval}">'
        '[email protected]</a></div>'
        '<div class="faculty-card_contact-phone__DzSNI"><a href="tel:+12015551234">(201) 555-1234</a></div>'
        '</div></div></div>'
    )
    cards = find_emails.parse_faculty_listing(card_html)
    assert len(cards) == 1, cards
    card = cards[0]
    assert card["name"] == "Jamie Test"
    assert card["office"] == "Babbio Center 500"
    assert card["extension"] == "1234"
    assert card["email"] == "test.person@stevens.edu"

    # mismatch detection: directory says a different room/extension than the page
    fake_person = {"room": "600", "extension": "9999"}
    mismatches = []
    if card["office"] and fake_person["room"] not in card["office"]:
        mismatches.append("office")
    if card["extension"] != fake_person["extension"]:
        mismatches.append("extension")
    assert mismatches == ["office", "extension"]
    print("find_emails.py pure-function tests passed")


def test_full_flow():
    with TestClient(app) as c:
        # pages and QR
        for p in ["/", "/ga", "/admin", "/ga/ticket/1", "/healthz"]:
            assert c.get(p).status_code == 200, p
        assert c.get("/qr.png").headers["content-type"] == "image/png"

        # admin auth
        assert c.post("/api/admin/login", headers={"X-Admin-Key": "nope"}).status_code == 401
        assert c.post("/api/admin/login", headers=A).status_code == 200

        # knowledge: Q&A, pasted note, uploaded docx and text, image with description
        r = c.post("/api/admin/qa", headers=A, json={
            "question": "Where do I pick up a printed transcript request form?",
            "answer": "TEST FIXTURE: transcript request forms are in the tray next to the front desk printer."})
        assert r.status_code == 200
        c.post("/api/admin/note", headers=A, json={"title": "Desk hours (test)", "text": "TEST FIXTURE: the desk is staffed weekdays."})
        import docx
        d = docx.Document(); d.add_paragraph("TEST FIXTURE: Lost and found items are kept in a labeled box behind the desk.")
        buf = io.BytesIO(); d.save(buf)
        r = c.post("/api/admin/upload", headers=A, data={"title": "Lost and found"},
                   files={"file": ("lost.docx", buf.getvalue(),
                                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        assert r.status_code == 200, r.text
        from PIL import Image
        img = io.BytesIO(); Image.new("RGB", (40, 40), "red").save(img, format="PNG")
        r = c.post("/api/admin/upload", headers=A, data={"title": "Floor map", "description": "TEST FIXTURE: map of the fourth floor rooms"},
                   files={"file": ("map.png", img.getvalue(), "image/png")})
        assert r.status_code == 200
        r = c.post("/api/admin/upload", headers=A, data={"title": "No desc"}, files={"file": ("x.png", img.getvalue(), "image/png")})
        docs = c.get("/api/admin/docs", headers=A).json()
        assert {x["kind"] for x in docs["docs"]} == {"qa", "text", "docx", "image"}
        assert any(j["status"] == "failed" for j in docs["jobs"])  # image with no description and no AI
        assert c.post("/api/admin/upload", headers=A, files={"file": ("v.mp4", b"0" * 10, "video/mp4")}).status_code == 400

        # schedule
        s = c.get("/api/admin/schedule", headers=A).json()
        assert set(s["on_shift_now"]) == {"Jeel Patel", "GA Two"}, s

        # visitor: answered question
        sid = c.post("/api/session").json()["session_id"]
        c.post(f"/api/session/{sid}/ask", json={"text": "where is the transcript request form?"})
        msgs = c.get(f"/api/session/{sid}/messages").json()["messages"]
        assert msgs[0]["sender"] == "bot" and "Virtual GA" in msgs[0]["text"]
        assert "tray next to the front desk printer" in msgs[-1]["text"]
        assert msgs[-1]["meta"]["offer_handoff"] is False

        # image doc shows up with its picture
        c.post(f"/api/session/{sid}/ask", json={"text": "map of the fourth floor rooms"})
        last = c.get(f"/api/session/{sid}/messages").json()["messages"][-1]
        assert last["meta"]["images"], last
        assert c.get(last["meta"]["images"][0]).status_code == 200

        # visitor: unknown question -> offer a GA
        c.post(f"/api/session/{sid}/ask", json={"text": "what is the wifi password for the gym?"})
        last = c.get(f"/api/session/{sid}/messages").json()["messages"][-1]
        assert last["meta"]["offer_handoff"] is True

        # handoff validation, then success
        assert c.post(f"/api/session/{sid}/handoff", json={"name": "Alex Kim", "question": "wifi?", "phone": "201-555-0100"}).status_code == 400
        teams.sent_cards.clear()
        r = c.post(f"/api/session/{sid}/handoff", json={"name": "Alex Kim", "question": "I need help with my course registration hold",
                                                        "phone": "(201) 555-0100", "consent": True})
        assert r.status_code == 200
        tid = r.json()["ticket_id"]
        assert c.post(f"/api/session/{sid}/handoff", json={"name": "A", "question": "again"}).status_code == 409
        card = teams.sent_cards[-1]
        assert title(card) == "Visitor waiting at the front desk"
        assert set(mentions(card)) == {"jp@stevens.edu", "two@stevens.edu"}
        assert "555" not in json.dumps(card), "phone number must not be posted to the channel"
        link = card["attachments"][0]["content"]["actions"][0]["url"]
        token = link.split("t=")[1]

        # while a ticket is open, visitor messages are relayed, not answered by the bot
        n_before = len(c.get(f"/api/session/{sid}/messages").json()["messages"])
        assert c.post(f"/api/session/{sid}/ask", json={"text": "I'm by the elevator"}).json()["relayed"] is True
        assert len(c.get(f"/api/session/{sid}/messages").json()["messages"]) == n_before + 1

        # GA console access
        assert c.get(f"/api/ga/ticket/{tid}?t=wrong").status_code == 401
        t = c.get(f"/api/ga/ticket/{tid}?t={token}").json()
        assert t["visitor_phone"] == "2015550100" and t["status"] == "new"
        assert c.get("/api/ga/tickets").status_code == 401
        assert len(c.get("/api/ga/tickets", headers={"X-GA-Key": "gakey"}).json()["tickets"]) == 1
        assert c.get("/api/ga/team").status_code == 401
        assert c.get(f"/api/ga/team?tid={tid}&t={token}").json()["on_shift"] == ["Jeel Patel", "GA Two"]

        # tick before 5 minutes: no escalation; digest goes out once
        teams.sent_cards.clear()
        r = c.get("/tasks/tick?key=cronkey").json()
        assert r["escalated"] == 0 and r["digest_sent"] is True
        assert title(teams.sent_cards[-1]).startswith("Virtual GA daily digest")
        assert c.get("/tasks/tick?key=cronkey").json()["digest_sent"] is False
        assert c.get("/tasks/tick?key=bad").status_code == 401

        # a second visitor whose ticket goes unanswered for 6 minutes -> the reminder
        # ladder's step 1 fires, targeting whoever is actually on shift right now (not
        # just the lead - that's step 3, covered by the dedicated ladder tests)
        sid2 = c.post("/api/session").json()["session_id"]
        tid2 = c.post(f"/api/session/{sid2}/handoff", json={"name": "Prof. Lee", "question": "Projector in room 1 is broken"}).json()["ticket_id"]
        db = SessionLocal()
        from app.db import utcnow as db_utcnow2
        db.get(Ticket, tid2).next_deadline = db_utcnow2() - timedelta(minutes=6)
        db.commit(); db.close()
        teams.sent_cards.clear()
        assert c.get("/tasks/tick?key=cronkey").json()["escalated"] == 1
        esc = teams.sent_cards[-1]
        assert title(esc) == "Reminder" and set(mentions(esc)) == {"jp@stevens.edu", "two@stevens.edu"}
        assert c.get("/tasks/tick?key=cronkey").json()["escalated"] == 0  # only once (same step, not re-fired)

        # GA responds on ticket 1: on my way, reply, resolve
        act = lambda body: c.post(f"/api/ga/ticket/{tid}/action?t={token}", json={"ga_name": "GA Two", **body})
        assert act({"action": "on_my_way", "minutes": 5}).status_code == 200
        m = c.get(f"/api/session/{sid}/messages").json()
        assert "on the way to the front desk" in m["messages"][-1]["text"]
        assert "about 5 minutes" in m["messages"][-1]["text"]
        assert m["ticket"]["status"] == "in_progress" and m["ticket"]["activity"] == "on_the_way"
        # auto-claim made GA Two the owner; a different, non-lead GA is refused
        other = c.post(f"/api/ga/ticket/{tid}/action?t={token}", json={"ga_name": "GA Three", "action": "reply", "text": "hi"})
        assert other.status_code == 400 and "owns this ticket" in other.json()["detail"]
        act({"action": "reply", "text": "Be there in a sec, grab a seat!"})
        last = c.get(f"/api/session/{sid}/messages").json()["messages"][-1]
        assert last["sender"] == "ga" and last["ga_name"] == "GA Two"
        assert act({"action": "reply", "text": "  "}).status_code == 400
        act({"action": "resolve"})
        assert c.get(f"/api/session/{sid}/messages").json()["ticket"] is None
        assert act({"action": "reply", "text": "late"}).status_code == 409

        # GA says busy on ticket 2 -> visitor asked for a number, then adds it
        tok2 = tickets.ticket_token(tid2)
        c.post(f"/api/ga/ticket/{tid2}/action?t={tok2}",
              json={"ga_name": "Jeel Patel", "action": "busy", "snooze_until": "30min", "reason": "in a meeting"})
        assert "leave a phone number" in c.get(f"/api/session/{sid2}/messages").json()["messages"][-1]["text"]
        assert c.post(f"/api/session/{sid2}/contact", json={"phone": "201 555 0199", "consent": False}).status_code == 400
        assert c.post(f"/api/session/{sid2}/contact", json={"phone": "201 555 0199", "consent": True}).status_code == 200

        # digest numbers
        dg = c.get("/api/admin/digest", headers=A).json()
        assert dg["handoffs"] == 2 and dg["escalated"] == 1 and dg["still_open"] == 1
        assert "what is the wifi password for the gym?" in dg["unanswered"]

        # nobody on shift -> lead alerted immediately, visitor told honestly, no second escalation later
        write_ics([("GA Three - Front Desk", now + timedelta(hours=3), now + timedelta(hours=5))])
        teams.sent_cards.clear()
        sid3 = c.post("/api/session").json()["session_id"]
        c.post(f"/api/session/{sid3}/handoff", json={"name": "Sam", "question": "Is anyone here?"})
        card = teams.sent_cards[-1]
        assert title(card) == "Visitor question, no GA on shift" and mentions(card) == ["jp@stevens.edu"]
        msg = c.get(f"/api/session/{sid3}/messages").json()["messages"][-1]["text"]
        assert "No GA is on the desk right now" in msg and "next GA" in msg, msg

        # search endpoint + delete
        assert c.get("/api/admin/search?q=lost and found", headers=A).json()["hits"][0]["title"] == "Lost and found"
        first = c.get("/api/admin/docs", headers=A).json()["docs"][0]["id"]
        assert c.delete(f"/api/admin/docs/{first}", headers=A).status_code == 200
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    test_migration_adds_columns_to_old_tables()
    test_claim_is_atomic_only_one_wins()
    test_on_my_way_deadline_and_confirmation()
    test_structured_snooze_and_third_snooze_auto_escalates()
    test_reminder_ladder_catchup_offshift_and_autoclaim()
    test_shift_end_headsup_and_conditional_reassignment()
    test_shift_start_first_tick_sends_nothing_then_detects_new_shift()
    test_storage_module_degrades_and_builds_correct_requests()
    test_task_request_creation_and_file_privacy()
    test_directory_import_upserts_and_preserves_edits()
    test_directory_name_matching_and_building_formats()
    test_extension_hidden_unless_admin_enables_it()
    test_disambiguation_buttons()
    test_remembered_person_pronoun_followup()
    test_role_matching()
    test_role_match_lists_everyone_name_match_still_asks()
    test_route_topics_here_excludes_from_role_matching()
    test_place_matching()
    test_common_word_gate_faq_regression()
    test_message_ticket_deadline_and_escalation()
    test_ga_directory_search()
    test_admin_status_reports_directory_counts()
    test_email_candidate_review_flow()
    test_find_emails_script_pure_functions()
    test_full_flow()
