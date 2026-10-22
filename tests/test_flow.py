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
    eng.dispose()


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
    # COPA / PhD / Dean role-match tests
    f'Copa Fixture,Copa,Fixture,"Copa Fixture",Careers Coordinator,Staff,Babbio Center,303N,3,,'
    f'"{HOURS}",8597,,"COPA, careers, resume help, recruiters",yes,yes,2026-01-01,',
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
        assert "Copa Fixture" in last["text"], last

        last = _ask_text(c, sid, "Capstone")
        assert "Christina Alwell" in last["text"], last

        last = _ask_text(c, sid, "PhD forms")
        assert "Phd Fixture" in last["text"], last

        last = _ask_text(c, sid, "the Dean")
        assert "GJ de Vreede" in last["text"], last
    print("role matching test passed")


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
        assert t.deadline_at is not None
        minutes_out = (t.deadline_at - db_utcnow()).total_seconds() / 60
        assert minutes_out > 60, "deadline should be the end of the ~3hr shift window, not +5 min"

        # make it overdue and confirm the normal escalation chain picks it up (call
        # escalate_overdue directly, not the combined /tasks/tick, so this doesn't also
        # trip the once-a-day digest flag and leak that into a later test)
        t.deadline_at = db_utcnow() - timedelta(minutes=1)
        db.commit()
        teams.sent_cards.clear()
        assert tickets.escalate_overdue(db) == 1
        esc = next(card for card in teams.sent_cards if title(card).startswith("No response after"))
        assert mentions(esc) == ["jp@stevens.edu"], esc

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
        assert s["people_count"] == 16, s
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
        assert t["visitor_phone"] == "2015550100" and t["status"] == "waiting"
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

        # a second visitor whose ticket goes unanswered for 6 minutes -> lead gets escalated
        sid2 = c.post("/api/session").json()["session_id"]
        tid2 = c.post(f"/api/session/{sid2}/handoff", json={"name": "Prof. Lee", "question": "Projector in room 1 is broken"}).json()["ticket_id"]
        db = SessionLocal()
        db.get(Ticket, tid2).created_at -= timedelta(minutes=6)
        db.commit(); db.close()
        teams.sent_cards.clear()
        assert c.get("/tasks/tick?key=cronkey").json()["escalated"] == 1
        esc = teams.sent_cards[-1]
        assert title(esc).startswith("No response after 6 minutes") and mentions(esc) == ["jp@stevens.edu"]
        assert c.get("/tasks/tick?key=cronkey").json()["escalated"] == 0  # only once
        assert "flagged this to the lead" in c.get(f"/api/session/{sid2}/messages").json()["messages"][-1]["text"]

        # GA responds on ticket 1: on my way, reply, resolve
        act = lambda body: c.post(f"/api/ga/ticket/{tid}/action?t={token}", json={"ga_name": "GA Two", **body})
        assert act({"action": "on_my_way", "minutes": 5}).status_code == 200
        m = c.get(f"/api/session/{sid}/messages").json()
        assert "on the way to the front desk" in m["messages"][-1]["text"]
        assert "about 5 minutes" in m["messages"][-1]["text"]
        assert m["ticket"]["status"] == "on_the_way"
        act({"action": "reply", "text": "Be there in a sec, grab a seat!"})
        last = c.get(f"/api/session/{sid}/messages").json()["messages"][-1]
        assert last["sender"] == "ga" and last["ga_name"] == "GA Two"
        assert act({"action": "reply", "text": "  "}).status_code == 400
        act({"action": "resolve"})
        assert c.get(f"/api/session/{sid}/messages").json()["ticket"] is None
        assert act({"action": "reply", "text": "late"}).status_code == 409

        # GA says busy on ticket 2 -> visitor asked for a number, then adds it
        tok2 = tickets.ticket_token(tid2)
        c.post(f"/api/ga/ticket/{tid2}/action?t={tok2}", json={"ga_name": "Jeel Patel", "action": "busy"})
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
    test_directory_import_upserts_and_preserves_edits()
    test_directory_name_matching_and_building_formats()
    test_extension_hidden_unless_admin_enables_it()
    test_disambiguation_buttons()
    test_remembered_person_pronoun_followup()
    test_role_matching()
    test_route_topics_here_excludes_from_role_matching()
    test_place_matching()
    test_common_word_gate_faq_regression()
    test_message_ticket_deadline_and_escalation()
    test_ga_directory_search()
    test_admin_status_reports_directory_counts()
    test_email_candidate_review_flow()
    test_full_flow()
