# Virtual GA: notes for Claude Code

QR-code chat assistant for the Stevens School of Business front desk. It answers from a GA-maintained knowledge base, hands off to the on-shift GA through Teams, relays GA replies to the visitor, escalates to the lead GA after 5 minutes, and posts a daily digest. Owner: Jeel Patel (JP), lead GA.

## Stack
FastAPI (Python 3.12), SQLAlchemy (SQLite locally, Supabase Postgres in prod), Gemini API (answers, embeddings, image/video reading), Teams Workflows webhook (alerts), Google Calendar secret iCal link (shifts), Render free tier, cron-job.org hitting `/tasks/tick` every minute.

## Layout
- `app/main.py` routes (visitor `/api/session/*`, GA `/api/ga/*`, admin `/api/admin/*`, `/tasks/tick`)
- `app/tickets.py` handoff, GA actions, escalation, digest, phone purge
- `app/knowledge.py` extraction, chunking, embeddings, search (keyword fallback when no Gemini key)
- `app/llm.py` all Gemini calls; every function must degrade gracefully without a key
- `app/schedule.py` calendar parsing; shift = event whose title contains a GA name or alias from team.json
- `app/teams.py` Adaptive Cards; buttons are Action.OpenUrl only (no Teams bot)
- `app/static/` plain HTML/JS pages, no build step
- `tests/test_flow.py` end-to-end test, no external services

## Commands
- Run: `uvicorn app.main:app --reload`
- Test: `python tests/test_flow.py` (must pass before every commit)

## Rules
- Never commit `.env`, `team.json`, or any key, webhook URL or iCal link. Secrets live in `.env` locally and Render env vars in prod.
- Never post a visitor's phone number to Teams. It is shown only in the GA console and purged after PHONE_RETENTION_DAYS.
- The bot answers only from the knowledge base. Do not loosen the "not sure, get a GA" behaviour to make answers look better.
- No student records, grades, IDs or financial info anywhere in the system.
- Keep the HTML pages dependency-free and mobile-first (visitors use phones).
- Visitor-facing text: short, friendly, plain language, no em dashes.
- Add or update a check in `tests/test_flow.py` for every behaviour change.

See TASKS.md for the build plan and who does what.
