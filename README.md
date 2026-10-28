# Virtual GA: School of Business front desk

A QR-code chat assistant for the front desk. It answers common questions from a knowledge base the GA team maintains. When it can't help, it pages whichever GA is on shift in Microsoft Teams and relays their replies back to the visitor in real time.

## What it does

**For visitors** (scan the QR code, nothing to install)
- Ask questions and get answers drawn only from your knowledge base. It says "not sure" instead of guessing.
- Ask "do you have a calculator?" or "can I borrow an HDMI cable?" and get a straight answer from the real front-desk inventory - with a one-tap **Borrow it** button for anything lendable.
- Tap **Get a GA** to leave their name, their question and, optionally, a phone number (with consent).
- Tap **Request something from the GAs** for a print job, room setup, delivery or anything else that isn't a question - with an optional file attachment, a due date, and a copy count.
- See live updates: "on the way, about 5 minutes", "tied up, will call you back", and direct replies from the GA. Once a task is marked done, a signed link lets the requester say whether it was actually taken care of.

**For GAs**
- A Teams card in the team channel @mentions whoever is on shift, according to the Google Calendar.
- One tap opens the GA console, with **Claim** (so exactly one GA owns an item - a second claim attempt is rejected), **On my way (2/5/10 min)**, a structured **Busy, follow up in...** picker (30 min / 2 h / end of shift / tomorrow 10 AM, with a required reason), **Reply**, and **Mark resolved**.
- The console's **Today at SSB** field is where you note anything happening today (an event, a closure) that visitors might ask about - it clears itself at midnight.
- The console also has **Inventory** search (every item, including Josa's personal stock and GA-only supplies, with one-tap in stock/low/out buttons) and **Resources** search (every guide, flyer and form, including ones never shown to a visitor).

**For you, the lead**
- No GA work ever falls back to the office manager: every open item (a visitor handoff, a message for someone specific, or a task request) has a single owner and a deadline, and a forgotten one keeps getting reminded about - first the owner, then more urgently, then you, then the whole team - until it's done or reassigned. The exact same reminder chain also runs when nobody has claimed an item yet, targeting whoever is actually on shift at that moment.
- A GA's open items are never silently dropped: ending a shift hands off anything that can't wait for their next shift, and a new shift starting gets a card listing what's open.
- A **daily digest** posts at 5 PM: question counts, how many the bot answered, handoffs, overdue items, items closed that day, low/out of stock supplies, borrowed items not yet returned, and the list of questions it couldn't answer. A **weekly report** posts Monday mornings: missed deadlines, snoozes, reassignments and average time-to-done, per GA.
- The admin page (`/admin`) lets you add Q&As (one at a time or in bulk via CSV), upload PDFs, Word files, images and videos, check the schedule, see today's and this week's numbers, and print the QR code.

## How it's built (all free tiers)

| Piece | Tool |
|---|---|
| App and API | Python, FastAPI |
| Hosting | Render (free web service) |
| Database | Supabase Postgres (free) |
| AI answers, image and video reading, search | Google Gemini API (free tier) |
| GA alerts | Teams Workflows webhook |
| Shift schedule | Google Calendar secret iCal link |
| Keep-alive, escalation and digest timer | cron-job.org (free) |

Images are read by AI (all visible text plus a description) and the picture is shown to visitors when it's relevant. Videos are transcribed by Gemini. Everything is stored as searchable text.

> **Why GAs reply in a web console, not inside Teams:** two-way replies inside Teams require registering a Teams bot in Azure, which needs Stevens IT admin approval. This design gets you Teams alerts plus a one-tap console with no IT ticket. If IT approves a bot later, the console logic can be reused.

---

## Setup (about 45 minutes)

### 1. Gemini API key (5 min)
1. Go to Google AI Studio and click **Get API key**, then create a key.
2. Save it as `GEMINI_API_KEY`.

> On the free tier, Google may use your inputs to improve its products. Only add information that is already public. **Never add student records, grades or IDs.**

### 2. Supabase database (10 min)
1. Create a free project at supabase.com.
2. Go to **Project Settings, then Database, then Connection string**, and copy the **Session pooler** URI. Render's free tier needs the pooler, not the direct connection.
3. Replace `[YOUR-PASSWORD]` in the URI and save it as `DATABASE_URL`. The app creates its own tables on first start.

### 3. Teams channel alerts (10 min)
1. In Teams, pick (or create) a channel for the GA team, such as **Front Desk Alerts**.
2. Open the channel's **...** menu, choose **Workflows**, and pick the template **"Post to a channel when a webhook request is received"**.
3. Choose your team and channel, then copy the URL it gives you. Save it as `TEAMS_WEBHOOK_URL`.
4. After deploying, open `/admin`, go to **Setup**, and click **Send a Teams test**.

> If Workflows is blocked in the Stevens tenant, ask IT to allow the Teams Workflows template, or tell me and I'll switch alerts to email.
> Check that the @mention in the test card actually notifies you. If the mention shows as plain text, double-check the `teams_email` values in `team.json`.

### 4. Google Calendar shifts (5 min)
1. In Google Calendar, open **Settings**, select the GA work calendar, and go to **Integrate calendar**.
2. Copy **"Secret address in iCal format"** and save it as `GCAL_ICS_URL`. Keep it secret, since anyone with the link can read the calendar.
3. **Naming rule:** each shift event's title must contain the GA's name or one of their aliases from `team.json`, for example `Riya - Front Desk`. Recurring events work.
4. After deploying, the **Schedule** tab in `/admin` shows today's events and flags any that don't match a GA.

> If the secret address option is missing, your Google Workspace admin has disabled it. Tell me and I'll switch to a service account using the Calendar API.

### 5. Your team list
Copy `team.example.json` and fill in real names, Stevens Teams emails, and the aliases used in calendar titles. Set `"lead": true` for yourself. On Render, paste the whole JSON into the `TEAM_JSON` environment variable (don't commit names and emails to a public repo).

### 6. Deploy to Render (10 min)
1. Push this folder to a **private** GitHub repo.
2. In Render, choose **New, then Blueprint**, and pick the repo. It reads `render.yaml`.
3. Fill in the environment variables: `PUBLIC_BASE_URL` (your `https://....onrender.com` URL), `ADMIN_PASSWORD`, `GA_KEY` (share this with your GAs), `DATABASE_URL`, `GEMINI_API_KEY`, `TEAMS_WEBHOOK_URL`, `GCAL_ICS_URL`, and `TEAM_JSON`. `SECRET_KEY` and `CRON_SECRET` are generated for you; copy `CRON_SECRET` for the next step.

### 7. The 1-minute timer (5 min)
Render's free tier sleeps after about 15 idle minutes. A sleeping server can't escalate tickets and makes visitors wait for it to wake up, so:
1. Create a free account at cron-job.org.
2. Add a job that GETs `https://YOUR-APP.onrender.com/tasks/tick?key=YOUR_CRON_SECRET` **every minute**.

This one job runs the reminder ladder, shift handoff/start, task heads-ups, the 5 PM digest, Monday's weekly report, the 1st-of-the-month stats export, the daily retention job (see "Data and privacy" below), deletes phone numbers after 30 days and old task files after 14, and keeps the server awake.

> Free-tier limits (Render hours, Gemini requests per day, Supabase pausing) change over time. Check each provider's current limits before launch.

### 8. Go live
1. Open `/admin` and add your first 15 to 20 Q&As (see "Getting good answers" below).
2. On the **Setup** tab, download the QR code, print it, and put it on the desk.
3. Share `/ga` and the `GA_KEY` with your GAs, and have each of them pick their name once in the console.

---

## Getting good answers

The bot is only as good as what you feed it. Ask SSB staff:

> "What are the 10 questions you answer most often each week, and what documents or links do you point people to?"

Then:
- **Add each one as a Q&A**, with one question and one clear answer. This works better than long documents.
- Already have a spreadsheet? **FAQ bulk import** on the Knowledge tab reads a CSV with
  exact columns `id,category,type,question,variants,asked_by,times_mentioned,answer,source,
  handled_by,changes_each_semester,status`, matched by header name so a reordered file
  still works. It shows a preview (what will import, what will be skipped) before you
  confirm. Rows with `status` = "Needs answer" are skipped. Re-importing the same `id`
  updates that row instead of creating a duplicate. `variants` (alternate phrasings,
  separated by ` | `) make a Q&A findable by more than one way of asking, without
  cluttering the displayed answer. A row with `type` = "Task" offers the visitor the
  task-request form instead of answering normally, once its match is confident enough.
- Click **Edit** on any existing Q&A to update it in place, rather than deleting and re-adding.
- Upload maps, flyers and form screenshots as **images**.
- Check **Today** in `/admin` (or the daily digest) for questions it couldn't answer, and click **Answer this** to turn each one into a Q&A.
- Use **Test what it finds** to check that a question pulls up the right content.

## Directory: finding a person or a place

Any question about a specific SSB person or a Babbio 4th-floor place is answered by the
directory resolver (`app/directory.py`), not the general knowledge base, and runs before
the AI - it works even with no `GEMINI_API_KEY` set. It never invents a person or an
office: an unmatched name gets "I couldn't find anyone by that name, want a GA?" and a
shared name (e.g. two people named Chen) gets "Did you mean ...?" buttons, never a silent
guess.

**Loading the data.** `data/ssb_directory.csv` and `data/babbio4_rooms.csv` are gitignored
(they're not meant to live in git history) and are never read from disk by the running
app. On the **Directory** tab in `/admin`, upload each CSV; this upserts by name/place -
a blank cell never erases an existing value, and a row missing from a new upload is hidden,
not deleted, so nothing already resolved mid-conversation or already email-approved goes
stale.

**What it matches on:** exact name/alias, then a fuzzy match (typos, a first or last name
alone, "Prof"/"Dr"/"Dean" prefixes), then the person's `roles` column ("my grad advisor",
"COPA person", "Capstone"). A bare common English word that happens to also be a real name
in the data (e.g. "Park", "Sun", "Lee") only counts as a match when the question also has a
title word or a clear "where is X" / "is X in" / "X's office" phrasing around it - not on
its own, so an unrelated question never gets hijacked. `route_topics_here = no` keeps a
person's location answerable by name while excluding them from "who handles this topic"
role matches. Extensions are hidden from visitors unless the **Setup** tab's
`show_extensions` toggle is on; the GA console's directory search always shows them.

**Leaving a message for someone specific:** asking if someone is in gets "Ask a GA to
check" or "Leave a message" buttons (never "they're in right now" - office hours only). A
left message still pages whoever's on shift in Teams ("Message for `<name>`"), but its
escalation deadline is the end of the current shift instead of 5 minutes, since nobody
needs to drop what they're doing - it still can't be forgotten, since it goes through the
same escalation chain as every other ticket.

**Finding emails.** All 105 rows start with an empty `email` column. `tools/find_emails.py`
is a separate script you run on your own Mac (not part of the deployed app, no network
calls happen from the live site): `python3 tools/find_emails.py`. It reads the real SSB
faculty listing page (politely - checks robots.txt, waits 2 seconds between requests,
caches pages so a re-run resumes) and writes `data/email_candidates.csv` with a confidence
level and source URL per row, flagging any office/extension that doesn't match the CSV.
Staff and PhD-student rows have no public listing page, so hand-fill
`tools/known_profile_urls.csv` (`name,url`, gitignored) for the ones you want covered -
anyone left out gets nothing rather than a guess. Upload the output on the **Directory**
tab's **Import found emails** form, then approve or reject each one under **Review
emails** - nothing reaches the live directory unapproved.

## Task requests, the reminder ladder, and reports

Every open item - a visitor handoff, a message left for someone specific, or a task
request - is the same underlying `Ticket`, unified under one status model
(`new → claimed → in_progress → done | cancelled`) and one ownership/deadline/reminder
mechanism, so nothing needs a parallel system to stay on top of.

**Claim** is enforced by the database itself (a single conditional update), not an
application-level lock, so two GAs can never both end up owning the same item. Taking any
action on an unclaimed item claims it automatically; after that, only the owner or the
lead can act on it.

**The reminder ladder** fires in four steps as an item goes further overdue: a reminder to
the owner (or whoever's on shift, if unclaimed), a more urgent one, an alert to you as
lead, then the whole team - each step firing exactly once, even if the server missed
several ticks in a row (it names which steps it missed rather than sending one card per
step). Nothing in this chain ever notifies anyone outside `team.json` - the SSB staff
directory is never used for alerts.

**Task requests** support one optional file attachment (PDF, Word, or an image, 10 MB by
default - `TASK_FILE_MAX_MB`). Files live in Supabase Storage if `SUPABASE_URL` and
`SUPABASE_SERVICE_KEY` are set (a private bucket, signed URLs generated only for the GA
console, never posted to Teams); otherwise they fall back to a local database table. A
file is deleted the moment its ticket is marked done or cancelled, with a 14-day sweep as
a backstop for anything left open. The GA team gets a heads-up card 30 minutes before a
task is due.

**Requester confirmation:** once a task is marked done, its requester (if they left an
email) can get a link - by email if `SMTP_HOST` is set, otherwise just the signed
`/status/{id}` page itself - asking "was this taken care of?" Saying no reopens it to you
as lead immediately, rather than waiting for a deadline to lapse.

**Shift handoff:** 15 minutes before a GA's shift ends, they get a card listing their open
items. At the actual end of shift, an item is only reassigned if its deadline falls before
the owner's own next shift - if they'll be back in time, it stays with them. A newly
started shift gets a card showing what's currently open.

## Inventory and GA resources

**Inventory** (`data/ssb_inventory.csv`, gitignored, loaded through the admin **Directory**
tab's inventory import, same upsert-by-key/replace rules as the people and rooms CSVs -
by `item_id`, blank cells never erase an edit, a row missing from a new upload is hidden,
not deleted) answers "do you have a calculator?" or "can I borrow an HDMI cable?" directly,
without the AI. It only fires on an explicit ask ("do you have...", "is there...", "can I
borrow/get..."), never on a bare mention, and matches on the item's name and its `aliases`
column (pipe-separated). When several items tie for the best match (e.g. several kinds of
toner for a generic "do you have toner?"), all of them are considered together, so the
answer still reflects reality even though the visitor didn't name the specific one.

A visitor only ever hears about `audience=visitors` items, and never anything whose `area`
starts with "Josa" (her personal stock) - that's excluded before matching even runs, not
just hidden after. A `ga_only` item is answered as if it isn't stocked at all, so a visitor
can't tell "really out" apart from "GA-only and we won't say." An item marked `lend_or_give
= lend` and currently in stock gets a one-tap **Borrow it** button, which creates a normal
task ticket due today end of day - it goes through the exact same reminder ladder as
everything else, so an unreturned loan can't be forgotten, and the GA console's Inventory
tab has one-tap in stock/low/out buttons to keep status current.

**GA resources** (`data/ga_resources.csv`, gitignored metadata + PDFs under
`data/resources/`, which *are* committed since they're ordinary app content, not private
data) are indexed into the same knowledge base as every other Q&A and upload - a resource
is just a `Doc` with `audience` (`public` or `ga_only`) and `seasonal` set. A `ga_only` or
`seasonal` resource is excluded from every visitor-facing search; the admin **Test what it
finds** tool and the GA console's own Resources search both see everything. Importing a
resource whose PDF hasn't been placed under `data/resources/` yet falls back to just the
CSV's own `summary` column, so the library is fully usable before the real files exist -
re-importing later upgrades it automatically once they're added.

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env              # fill in what you have; everything is optional locally
cp team.example.json team.json
uvicorn app.main:app --reload
```
- Visitor chat: http://localhost:8000
- GA console: http://localhost:8000/ga
- Admin: http://localhost:8000/admin

Without a Gemini key, the bot falls back to keyword search, so you can try the whole flow before setting anything up.

To run the end-to-end test, which needs no external services:

```bash
python tests/test_flow.py
```

## Data and privacy

**What's collected, and for how long.** A visitor sees a one-line notice under the chat
box linking to `/privacy`, a short page covering all of this in plain language.
- Phone numbers are optional, require a consent checkbox, are never posted to Teams (only
  shown in the console), and are deleted after 30 days (`PHONE_RETENTION_DAYS`, also an
  admin-settable period - see below).
- A task's attached file is deleted as soon as it's marked done or cancelled (14-day sweep
  as a backstop), and a signed GA-console link is the only way to open one - it's never
  posted to Teams.
- A visitor's name and email are removed after 180 days (replaced with "Visitor #<id>" on
  their session/ticket) - except an approval-gated resource request (e.g. an employment
  verification form), kept for 1 year for audit purposes before the same anonymizing happens.
- A question's own text is kept for 180 days, then deleted - but not lost: its topic
  (an FAQ's category, "People & offices" for a directory answer, "Places" for a room
  lookup, "Supplies" for inventory, or "Unanswered") and month are rolled into a running
  count first, so trend reporting survives even once the question itself is gone.
- The bot is instructed never to discuss grades, IDs, or financial or medical details, and
  to hand those requests to a human - and never asks for a password, Social Security
  number, or payment detail in the chat at all.
- Visitors are limited to 30 bot questions per hour, which protects the free Gemini quota.
- Every reminder, escalation and report stays inside `team.json` - the SSB staff directory
  is never used for alerts.
- Josa's personal inventory and anything marked GA-only (stock or a resource) is never
  shown to a visitor, in any form.

**The retention job** (`app/retention.py`) runs once a day and is **preview mode by
default**: it computes what it would anonymize or delete - counts per table, plus a few
masked examples (e.g. "A\*\*\* S\*\*\*\*\*\*\*") - without changing anything, visible on the
admin **Setup** tab. Turning on **Enable retention** there makes the next run (and every
one after) real. All the periods above (visitor info, question log, approval requests,
phone numbers, task files) are admin-settable from that same panel, not fixed constants.

**Monthly export:** on the 1st of each month, an anonymized CSV (questions, answered-by-bot
%, handoffs, avg response, tasks, escalations, top topics) is saved and downloadable from
the admin Digest tab. It never contains a name, email, or phone number - only aggregate
counts.

**Database security:** every Supabase table has row-level security turned on with no
policies - the app always connects as the table owner, which bypasses RLS regardless, so
this is a backstop against the Supabase anon/authenticated roles (never used or exposed by
this app) ever reading a table directly through some other route, like PostgREST. The
**Setup** tab flags any table where RLS is unexpectedly off.

**Access controls:**
- `/api/admin/login` and `/api/ga/login` lock out for 15 minutes after 5 failed attempts.
- Every admin action (not a plain read) is logged - who (a name typed at sign-in, not a
  verified login, since there's one shared admin password), what, and when - visible on
  the **Setup** tab.
- The **Setup** tab has a **Rotate GA key** button for when the shared `GA_KEY` needs to
  change without a Render redeploy - it logs out every GA console signed in with the old
  key immediately, and shows the new one once, to copy out to the team.

Get approval from your manager before putting the QR code on the desk, since the bot
speaks on behalf of SSB.

## Project layout
```
app/main.py         routes: visitor chat, GA console, admin, requester status page, cron tick
app/tickets.py      claim, status model, reminder ladder, shift handoff, confirmation, digest, weekly report, monthly export
app/knowledge.py    file reading, chunking, embeddings, search, FAQ bulk import, GA resources
app/directory.py    person/place resolver: name, fuzzy, role matching; CSV upsert import
app/inventory.py    front-desk inventory: CSV upsert import, "do you have X" matching, borrowing
app/storage.py      task-file attachments: Supabase Storage, with a local DB table fallback
app/retention.py    preview-mode anonymization/rollup job, admin-settable periods
app/tokens.py       purpose-scoped signed links (status, download, staff approve, GA console)
app/llm.py          Gemini: answers, embeddings, image and video reading
app/schedule.py     Google Calendar shifts
app/teams.py        Teams Adaptive Cards
app/static/         visitor.html, ga.html, admin.html, status.html, approve.html, privacy.html
tools/find_emails.py  standalone script: finds public stevens.edu emails for the directory
tests/test_flow.py  full end-to-end test
```
