# Virtual GA: School of Business front desk

A QR-code chat assistant for the front desk. It answers common questions from a knowledge base the GA team maintains. When it can't help, it pages whichever GA is on shift in Microsoft Teams and relays their replies back to the visitor in real time.

## What it does

**For visitors** (scan the QR code, nothing to install)
- Ask questions and get answers drawn only from your knowledge base. It says "not sure" instead of guessing.
- Tap **Get a GA** to leave their name, their question and, optionally, a phone number (with consent).
- See live updates: "Riya is on the way, about 5 minutes", "tied up, will call you back", and direct replies from the GA.

**For GAs**
- A Teams card in the team channel @mentions whoever is on shift, according to the Google Calendar.
- One tap opens the GA console, with buttons for **On my way (2/5/10 min)**, **Busy, I'll follow up**, **Reply**, and **Mark resolved**.

**For you, the lead**
- You're @mentioned on every handoff (set `LEAD_ALERT_ON_EVERY_HANDOFF=false` to turn this off).
- If no GA responds within **5 minutes**, you get a red escalation card with a link to respond yourself.
- If nobody is on shift, you're alerted immediately and the visitor is told honestly.
- A **daily digest** posts at 5 PM: question counts, how many the bot answered, handoffs, escalations, average response time, and the list of questions it couldn't answer.
- The admin page (`/admin`) lets you add Q&As, upload PDFs, Word files, images and videos, check the schedule, see today's numbers, and print the QR code.

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

This one job escalates overdue tickets, sends the 5 PM digest, deletes phone numbers after 30 days, and keeps the server awake.

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
- Upload maps, flyers and form screenshots as **images**.
- Check **Today** in `/admin` (or the daily digest) for questions it couldn't answer, and click **Answer this** to turn each one into a Q&A.
- Use **Test what it finds** to check that a question pulls up the right content.

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

## Privacy and rules built in
- Phone numbers are optional, require a consent checkbox, are never posted to Teams (only shown in the console), and are deleted after 30 days.
- The bot is instructed never to discuss grades, IDs, or financial or medical details, and to hand those requests to a human.
- Visitors are limited to 30 bot questions per hour, which protects the free Gemini quota.
- Get approval from your manager before putting the QR code on the desk, since the bot speaks on behalf of SSB.

## Project layout
```
app/main.py         routes: visitor chat, GA console, admin, cron tick
app/tickets.py      handoffs, GA actions, 5-min escalation, daily digest, phone cleanup
app/knowledge.py    file reading, chunking, embeddings, search
app/llm.py          Gemini: answers, embeddings, image and video reading
app/schedule.py     Google Calendar shifts
app/teams.py        Teams Adaptive Cards
app/static/         visitor.html, ga.html, admin.html
tests/test_flow.py  full end-to-end test
```
