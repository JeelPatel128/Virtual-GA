# Virtual GA build plan

Phase 1 code is done and tested locally. What's left is getting it live and making it actually good at answering.

Legend: **[JP]** = you do it (accounts, approvals, content, decisions). **[CC]** = Claude Code does it in this repo.

---

## Phase 0: Set up (this week)

| # | Who | Task | Done when |
|---|---|---|---|
| 0.1 | JP | Create a **private** GitHub repo `virtual-ga`, unzip this folder into it, and open Claude Code in that folder | `git status` works |
| 0.2 | CC | Init git, first commit, confirm `.gitignore` blocks `.env`, `team.json` and `*.db`, run tests | ✅ Done. Rebased onto GitHub's starter commits, tests pass, pushed to `main`. |
| 0.3 | JP | Get a **Gemini API key** (Google AI Studio) | ✅ Key in `.env`, verified working |
| 0.4 | JP | Create a **Supabase** project and copy the Session pooler URI | ✅ URI in `.env`, auth fixed, tables created |
| 0.5 | JP | In Teams, create a **Front Desk Alerts** channel and add the Workflows webhook | ✅ URL in `.env`, test card confirmed delivered to the GA group chat |
| 0.6 | JP | Get the GA calendar's **secret iCal address**. Rename shift events so each title contains the GA's name | ✅ URL in `.env`, verified working. 2 of 3 today's shift titles ("Shriya", "Arpan") don't match anyone in `team.json` yet |
| 0.7 | JP | Fill in `team.json`: 4 names, Stevens emails, calendar aliases, you as lead | ✅ Real data in place: Jeel Patel (lead), Shriya Urs, Kuhu Taneja, Yash Kakote, plus Arpan Gevariya (Josa's GA, covers front desk — see caveat below) |
| 0.8 | CC | Run locally against the real services: Gemini answers, Supabase tables, Teams test card, calendar matching. Fix whatever breaks (model names, Workflows quirks, mention format) | ✅ Gemini, Supabase, calendar read, and Teams delivery all verified. Calendar re-check with the real roster is pending (Google rate-limited the iCal feed after repeated testing today) |

**Step 5 results (2026-10-13):**
- **Supabase: PASS.** Password re-encoding issue traced to a leftover `[YOUR-PASSWORD]`-style artifact; after JP reset the password, connected and created all 7 tables (`chunks`, `docs`, `kv`, `messages`, `query_log`, `sessions`, `tickets`).
- **Gemini: PASS.** Real API calls succeeded (embeddings + `gemini-2.5-flash` generation). Reworded question answered correctly with high confidence; unrelated question correctly triggered "not sure." Current model names in `.env.example` are valid for this key — no change needed.
- **Calendar: PASS** (initial check, before real roster). Connects and reads today's 3 shifts. "Jeel" matched "Jeel Patel" (placeholder data); "Shriya" and "Arpan" were unmatched since `team.json` still had placeholders at that point.
- **Teams: PASS (delivery).** Test card sent via `/api/admin/test-teams` and confirmed by JP as posted to the GA group chat. The `@mention` target was still the placeholder lead email at that point, so it likely did not ping anyone for real — recheck now that `team.json` has the real lead email.
- **Known limitation — Arpan / event color:** Google Calendar's "secret address in iCal format" export does not include per-event color at all (confirmed: Google's API exposes `colorId` but the iCal/ICS export drops it). So the brown-vs-blue convention (front desk vs. Josa) described by JP is invisible to this app — matching is title-text only. Arpan was added to `team.json` per JP's decision, which means **any calendar event titled "Arpan" will currently show him as on-shift for front desk, including Josa shifts**, and could get him paged by mistake. Fix needed: Arpan's Josa-shift events need a title that does not contain "Arpan" (e.g. use initials "AG" or omit the name), reserving the plain name for actual front-desk coverage.
- **Tests:** `tests/test_flow.py` — ALL CHECKS PASSED (run four times across this session).
- **Python 3.12 (render.yaml pin):** requirements install cleanly and the app imports fine on 3.12.7, tested in an isolated venv alongside the local 3.14 environment. No 3.14-only code.

## Phase 1: Deploy (this week)

| # | Who | Task | Done when |
|---|---|---|---|
| 1.1 | JP | Create a Render account and connect GitHub | |
| 1.2 | CC | Check `render.yaml`, and list the exact env vars to paste into Render | |
| 1.3 | JP | Deploy the Blueprint and paste the env vars (including `TEAM_JSON`) | App URL opens |
| 1.4 | JP | Set up a cron-job.org job hitting `/tasks/tick?key=...` every minute | Render log shows ticks |
| 1.5 | CC | Smoke-test the live URL: chat, handoff, Teams card, console, escalation (with the timer temporarily set to 1 min) | Full loop works on a phone |

## Phase 2: Content (next 1 to 2 weeks, the part that matters most)

| # | Who | Task | Done when |
|---|---|---|---|
| 2.1 | JP | Ask SSB staff for their top 10 weekly questions and the docs and links they point people to | Shared sheet of Q&As |
| 2.2 | CC | Build a **bulk CSV import** (question, answer, tags) on the admin page, plus an **edit Q&A** button (right now you can only delete) | Import 50 rows in one go |
| 2.3 | JP | Write down 25 real test questions people have asked at the desk, with the correct answer for each | `tests/eval_questions.csv` |
| 2.4 | CC | Build an **eval script** that runs those questions through the bot and reports correct, wrong and "not sure". Tune retrieval and the prompt until wrong answers are near zero | Report: X/25 correct, 0 confidently wrong |
| 2.5 | JP | Upload the floor maps, flyers and any how-to videos | |

## Phase 2b: Directory / person lookup (2026-10-21, built in 17 small commits)

| # | Who | Task | Done when |
|---|---|---|---|
| 2b.1 | CC | Person resolver: exact/fuzzy name & alias match, role match ("my grad advisor", "COPA person"), common-word gate so a bare word that's also a real name ("Park", "Sun") needs title/phrase support, "Did you mean?" buttons for a shared name, session memory for pronoun follow-ups ("is she in?") | ✅ Done. 50-question synthetic FAQ regression (no false matches) plus all of the original spec's named test cases pass |
| 2b.2 | CC | Building-specific answers (Babbio w/ & w/o directions + 4th-floor map, Altorfer, Remote), extensions hidden unless admin enables `show_extensions`, `route_topics_here=no` keeps a person findable by name but out of role-based routing | ✅ Done |
| 2b.3 | CC | Place lookups against `babbio4_rooms.csv` (lounge, washrooms, room 4xx, 401/433 offices) | ✅ Done |
| 2b.4 | CC | "Leave a message for X": pages whoever's on shift, deadline is the end of the current shift (not 5 min), still escalates through the normal chain so it can't be forgotten | ✅ Done |
| 2b.5 | CC | Admin **Directory** tab: CSV upload (upsert by name/place - a blank cell never erases an edit; a missing row is hidden, not deleted), GA console directory search (always shows extension/email) | ✅ Done |
| 2b.6 | CC | `tools/find_emails.py`: standalone, polite scraper for public stevens.edu emails, admin review/approve screen before anything reaches the live directory | ✅ Built. See the real run's results below once it's been executed |
| 2b.7 | JP | Upload the real `data/ssb_directory.csv` and `data/babbio4_rooms.csv` through the admin **Directory** tab (they're gitignored - not in git history, and not auto-loaded) | ⏳ Not yet done - the 105-row/20-row real CSVs are sitting in `data/` but nothing has imported them into a running database yet |
| 2b.8 | JP | For Staff/PhD-student rows (no public stevens.edu listing page), hand-fill `tools/known_profile_urls.csv` (`name,url`) with whichever profile/bio pages you know of, if you want the email finder to cover them - anyone left out just gets skipped, not guessed | ⏳ Optional, your call |
| 2b.9 | JP | Review and approve/reject each row in **Review emails** after running `tools/find_emails.py` | ⏳ Pending the real run |

**Known limitation carried over:** the Arpan Josa-vs-front-desk calendar-color issue from Phase 0 (`#phase-0`) is unrelated to this feature but still unresolved - his calendar titling still needs a fix on the Google Calendar side.

## Phase 3: Pilot (internal, before the QR goes on the desk)

| # | Who | Task | Done when |
|---|---|---|---|
| 3.1 | JP | **Get Josa's approval** once she's back. Show her the pilot, not just the idea | Approved |
| 3.2 | JP | Share `/ga` and `GA_KEY` with the 3 GAs. Each picks their name in the console | |
| 3.3 | JP + GAs | Spend one week acting as fake visitors and fake GAs. Note every confusing moment | List of issues |
| 3.4 | CC | Fix the pilot issues. Add `added_by` from a name field on the admin page so you know who added what | |
| 3.5 | JP | Print the QR code and put it on the desk | Live |

## Phase 4: After launch (portfolio-worthy)

| # | Who | Task |
|---|---|---|
| 4.1 | CC | Analytics page: response time by GA, busiest hours heatmap, top question topics, % answered by the bot week over week |
| 4.2 | JP | After 4 weeks, write the impact summary (questions handled, average response time, GA hours saved) for your resume and portfolio |

---

## Things only JP can provide
1. Gemini API key
2. Supabase connection URI
3. Teams Workflows webhook URL (and confirmation that Workflows isn't blocked at Stevens)
4. Google Calendar secret iCal link, with shift events titled by GA name
5. `team.json` contents: names, Stevens Teams emails, calendar aliases
6. Render and cron-job.org accounts
7. Staff FAQs and 25 real test questions with correct answers
8. Josa's approval before the QR code goes public

**Never paste keys, webhook URLs or the iCal link into a chat.** Put them in `.env` locally and in Render's environment variables.
