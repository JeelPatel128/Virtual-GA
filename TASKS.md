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
- **Teams: PASS (delivery).** Test card sent via `/api/admin/test-teams` and confirmed by JP as posted to the GA group chat. The `@mention` target was still the placeholder lead email at that point, so it likely did not ping anyone for real — recheck now that `team.json` has the real lead email. **Rechecked 2026-10-22 with the real lead email (jpatel109@stevens.edu): card posted AND the @mention pinged JP for real.** Also reran the calendar check: all 3 of today's shifts now match a real GA (Shriya Urs, Jeel Patel, Arpan Gevariya) — the unmatched-shift gap above is resolved.
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
| 2.2 | CC | Build a **bulk CSV import** (question, answer, tags) on the admin page, plus an **edit Q&A** button (right now you can only delete) | ✅ Superseded by the richer 12-column FAQ bulk import built as Reliability pack group 13 (`#reliability-pack`) — same two asks (bulk import + edit button), bigger schema |
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

## Reliability pack (2026-10-22 spec, built 2026-10-22, 15 groups / 18 commits)

Manager requirements: GA work never falls back to the office manager, and a forgotten
item keeps getting reminded about until it's done or reassigned. **Groups 1–8 are on
`main`** (pushed, piloted). **Groups 9–15 are on `reliability-pack-2`** only, pending JP's
pilot results — not merged to `main` yet.

| # | Group | Branch | Status |
|---|---|---|---|
| 1 | Status/ownership/deadline schema + migration | `main` | ✅ Done, verified against Supabase |
| 2 | Claim mechanism (DB-atomic, single owner) | `main` | ✅ Done |
| 3 | Status model cutover (`new → claimed → in_progress → done\|cancelled`) | `main` | ✅ Done |
| 4 | "On my way" deadline + visitor confirmation prompt | `main` | ✅ Done |
| 5 | Structured snooze (30 min / 2 h / end of shift / tomorrow 10 AM + reason, 3rd auto-reassigns) | `main` | ✅ Done |
| 6 | Reminder ladder (4 steps, catch-up sends one card naming missed steps) | `main` | ✅ Done |
| 7 | Shift end: heads-up + conditional reassignment (only if the owner won't be back in time) | `main` | ✅ Done |
| 8 | Shift start: on-shift diff, first-tick-ever sends nothing | `main` | ✅ Done |
| 9 | Task requests: visitor form, file upload (Supabase Storage/local fallback), 30-min heads-up | `reliability-pack-2` | ✅ Done |
| 10 | File privacy: delete on done/cancelled, 14-day sweep | `reliability-pack-2` | ✅ Done |
| 11 | Requester confirmation page (signed `/status/{id}`, optional SMTP email) | `reliability-pack-2` | ✅ Done |
| 12 | Reports: digest additions (open by owner, overdue, done today) + weekly GA report | `reliability-pack-2` | ✅ Done |
| 13 | FAQ bulk import (12-column CSV, upsert by id, Task-type redirect) | `reliability-pack-2` | ✅ Done. **Needs a real-data check**: JP is placing the real 50-row `data/virtual-ga-faq.csv` (gitignored) — expected 9 import (8 "Draft, verify" + 1 "Suggested wording"), 41 skipped as "Needs answer". Verified so far against a synthetic fixture only; run the import against the real file once it lands. |
| 14 | "Today at SSB" note (KV-backed, midnight America/New_York expiry) | `reliability-pack-2` | ✅ Done |
| 15 | Docs (this update) + final test run | `reliability-pack-2` | ✅ Done |

**Design choices worth knowing about:**
- Every open thing — a visitor handoff, a message for someone specific, a task request —
  is the same `Ticket`, one status/ownership/deadline model, not three parallel systems.
- The reminder ladder and every alert in this pack only ever mention `team.json` names,
  never the SSB staff directory (`app/directory.py`) — a dedicated test enforces this.
- Task files: Supabase Storage (private bucket, signed URLs, GA-console only) if
  `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` are set, else a local DB table — same
  degrade-gracefully pattern as Gemini without a key.

## Inventory and GA resources (2026-10-23 spec, built 2026-10-23, `reliability-pack-2`)

Not merged to `main`. Two features, both reusing existing machinery rather than building
parallel systems: inventory piggybacks on the task-ticket/reminder-ladder pack above, and
GA resources are just `Doc` rows in the existing knowledge base.

| # | Item | Status |
|---|---|---|
| 1 | `Item` table + admin CSV import (upsert by `item_id`, same rules as the directory) | ✅ Done, verified against Supabase (264 real rows imported) |
| 2 | Visitor "do you have X" matching: in stock/low → offer; out → "we're out" + on order; ga_only → answered as not stocked; Josa's area excluded entirely | ✅ Done. Checking against the real 264-row `ssb_inventory.csv` (not just the test fixture) caught a real cross-match bug — "an HDMI cable" was matching the unrelated, in-stock "Power cables" since both just share the word "cable." Fixed by scoring every candidate by word overlap and keeping only the top-scoring tie, not anything with any overlap at all. |
| 3 | Overdue loans use the existing reminder ladder | ✅ Done - "Borrow it" creates a normal task ticket (due today end of day, borrower named in its text), no new escalation mechanism needed |
| 4 | GA console: inventory search (everything, incl. Josa's/ga_only) + one-tap in stock/low/out buttons | ✅ Done |
| 5 | Daily digest: low/out stock, borrowed items not yet returned | ✅ Done |
| 6 | GA resources: index each PDF (or its CSV `summary` as a fallback, before the real files exist) into the knowledge base, tagged `audience`/`seasonal`/`linked_faq_ids` | ✅ Done. 5 real PDFs now live under `data/resources/` (committed, not gitignored) and all 9 resources are re-imported with their real extracted text, verified against Supabase |
| 7 | `audience=ga_only`/`seasonal=yes` resources excluded from every visitor search; searchable from the GA console/admin only | ✅ Done - `knowledge.search()`'s new `visitor` flag |

**FAQ re-import**: the real `virtual-ga-faq.csv` (50 rows) was imported against the real
Supabase DB — 12 imported, 38 skipped as "Needs answer", matching Jeel's expected split
exactly.

## Approval-gated resources (2026-10-27 spec, built 2026-10-27, `reliability-pack-2`)

Not merged to `main`. A `Doc.audience` of `"approval_required"` (new value, on top of the
`public`/`ga_only` pair above) is never shown, attached, linked or quoted to a visitor in
any form — `knowledge.search()`'s visitor gating is an allowlist (only `"public"` is
visitor-facing), so this is structurally excluded regardless of matching precision
elsewhere. Currently used for RES06, the ISSS On-Campus Employment Verification Form.

| # | Item | Status |
|---|---|---|
| 1 | New `audience="approval_required"` value, excluded from visitor search | ✅ Done |
| 2 | Visitor can ask for the resource and request a copy; never upload or send a completed one (reply explains why, points to ISSS/in person) | ✅ Done - no file-upload path exists on this flow at all, so there's nothing to refuse structurally either; a text mention of a completed form is refused too |
| 3 | Request creates `kind="approval"` ticket: normal Teams card, claim, reminder ladder (team.json only) | ✅ Done - `tickets.create_approval_ticket`, deadline = end of current shift (no visitor-set due date), same ladder as everything else |
| 4 | GA console: Approve / Decline (reason required) / Hand in person; approved → signed 24h download link on the requester's status page, tied to that one ticket | ✅ Done. Reuses existing Ticket fields throughout (`cancelled_reason` for the decline reason, `resolved_at` as the approval timestamp, `task` JSON for the handoff method) - no new Ticket columns |
| 5 | Staff approvers setting (default Josa Balisi/Yadilsa Gomez/Megan Maher, matched from the directory); GA can ask one, who gets a signed approve/decline link by email (or the GA forwards it manually if SMTP isn't set); staff never auto-notified or added to the ladder | ✅ Done. **Blocker found**: the real `ssb_directory.csv` has never actually been imported into Supabase (`people` table has 0 rows - flagged back in Phase 2b, still open), so none of the 3 default approvers resolve to a real person yet. Added "Yadi" as an alias for Yadilsa Gomez in the local CSV so it's ready the moment the import happens; didn't run that 105-row import myself since it's a separate decision outside this task. Until then, an admin can point the approvers setting at any name already in the directory. |
| 6 | Audit log (who approved/declined/handed over/was asked, when, ticket, resource), visible in admin | ✅ Done - new `ApprovalLog` table, admin viewer under Setup |
| 7 | Tests: 5 phrasings never leak the file; link fails for another ticket and after 24h; decline shows the reason; staff never auto-notified; log is written | ✅ Done, all pass |

Also re-imported `ga_resources.csv` (RES06 → `approval_required`) and `virtual-ga-faq.csv`
(C4's answer no longer offers the file directly) against the real Supabase DB - same 12/38
FAQ split as before, RES06 confirmed excluded from a live visitor search and present in a
GA/admin one.

## Sustainability pack (2026-10-22, spec from outside this repo — pending)

JP's spec not pasted yet. Named scope so far: admin-managed settings, health checks, content
expiry, runbook.

| Named item | Exists today? | What's actually there |
|---|---|---|
| Admin-managed settings | ⚠️ Partial | One setting (`show_extensions`) is stored in the `KV` table and toggleable from `/admin`. Everything else (Gemini key, Teams webhook, etc.) is a `.env`/Render env var, not editable from the admin page. |
| Health checks | ⚠️ Partial | `/healthz` is a trivial liveness check (`{"ok": true}`, no dependency checks). `/api/admin/status` separately reports `ai_enabled`/`teams_configured`/`calendar_configured`, but it's admin-authenticated, not a plain health endpoint. |
| Content expiry | ❌ No | No mechanism flags or expires stale knowledge-base Q&As or directory rows. The directory's `last_verified` column (a per-row date) exists as raw data but nothing acts on it yet. |
| Runbook | ❌ No | README.md is setup instructions, not an incident/operational runbook. |

**FAQ bulk import:** ✅ Built as Reliability pack group 13 (`#reliability-pack`,
`reliability-pack-2` branch), superseding Phase 2.2 above with the full 12-column schema.

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
