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

from app import teams, tickets  # noqa: E402
from app.db import SessionLocal, Ticket  # noqa: E402
from app.main import app  # noqa: E402

A = {"X-Admin-Key": "adminpw"}


def mentions(card):
    return [e["mentioned"]["id"] for e in card["attachments"][0]["content"]["msteams"]["entities"]]


def title(card):
    return card["attachments"][0]["content"]["body"][0]["text"]


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
    test_full_flow()
