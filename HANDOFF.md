# Handoff — Korea semester

Working state as of the start of the study-abroad semester. Delete this file
once its threads are closed.

## Context

The user is studying abroad in South Korea this semester and returns to OSU
(Carmen) next semester. The host institution also uses Canvas — a different
instance from OSU's — and a Canvas access token has been generated for it.
This is the first time Hermes is being used against live coursework, so the
priority is getting it running and finding out what actually breaks.

## Branching

`korea` is the working trunk for this semester — treat it as main. New work
branches off `korea`, and merges back into `korea`. Nothing goes to `main`
unless the user explicitly asks for it.

## FIRST: help the user set up `.env`

This has not been done yet and blocks everything else. The app will not start
without it — `Config.validate()` (`config.py:41`) exits unless both
`CANVAS_TOKEN` and `GEMINI_API_KEY` are present.

Walk the user through it explicitly:

1. Copy the template: `cp .env.example .env`. The `.env` file is git-ignored
   (`.gitignore:2`) and must never be committed.
2. `CANVAS_TOKEN=` — generated from Canvas under
   *Account → Settings → New Access Token*. The user has one ready.
3. `CANVAS_BASE_URL=` — **must be changed.** It defaults to
   `https://osu.instructure.com` (`config.py:8`), which is wrong this
   semester. It needs the host institution's Canvas host.
4. `GEMINI_API_KEY=` — required, free from
   https://aistudio.google.com/app/apikey.
5. Everything else (Twilio, Piazza) is optional; Hermes runs dashboard-only
   without them.

Then verify the connection before anything else — the Settings page has a
Canvas test endpoint (`/api/test-canvas`, `web/app.py:3117`), or run a sync
and read `logs/hermes.log`.

## Open threads

### 1. Timezone correctness (highest value, do before relying on reminders)

The agreed principle: **a due date is an absolute instant and keeps the
meaning its institution gave it; reminder timing is computed against the
user's current local timezone**, so nudges arrive when they can act on them.
Right now the code does neither reliably.

- `modules/scheduler_engine.py` — every comparison strips the UTC offset off
  the Canvas timestamp and compares it against naive `datetime.now()`
  (lines 22, 25, 66-67, 76, 101-104). `is_within_active_hours()` (line 86)
  gates all notifications on the raw server clock.
- `modules/analyzer.py` — same pattern in the prompt-building and
  study-plan code (`now = datetime.now()` at lines 308, 566, 785, 869, 1067,
  1099, with `.replace(tzinfo=None)` on due dates throughout).
- `web/app.py` — converts Canvas timestamps to Eastern for display
  (`_from_canvas_time`, line 30) but then compares that against naive
  `datetime.now()` in `_fmt_due` (line 39). `EASTERN` is hardcoded at line 11.
- `hermes.py:842` — the APScheduler cron is pinned to `America/New_York`,
  which is inconsistent with the server-local gating inside the jobs it runs.

Direction discussed: stop stripping `tzinfo`, keep aware UTC instants
end-to-end, and add a user-configurable local timezone (`Asia/Seoul` now,
`America/New_York` next semester) that drives active-hours gating, reminder
timing, and "time remaining" display. Consider showing due times in both the
course's zone and the user's, since a 1pm Ohio deadline is 2am in Seoul and
the ambiguity is dangerous.

### 2. Manual entry fallback

Canvas works this semester, so this is a fallback rather than the primary
path — but per the mission in `CLAUDE.md`, it should exist (e.g. a professor
hands out a paper assignment that never appears in Canvas).

Currently there is no manual path at all:

- `database.py:340` — `upsert_assignment()` is keyed on `canvas_id` and is
  only ever called from Canvas sync.
- `upsert_course()` (`database.py:251`) is likewise Canvas-keyed, so a
  manually added assignment has no course to attach to unless courses can be
  added manually too.
- `web/app.py` has no create routes for either — only edit/toggle endpoints
  on existing rows.

Everything downstream (analysis, notifications, dashboard) just reads the
assignments table, so manually created rows would flow through the existing
pipeline for free. Syllabus input should support both PDF upload and a
pasted-text fallback, kept visually quiet per the UI principle in
`CLAUDE.md`.

### 3. Phone notifications — broken, and must stay free

The user considers working phone notifications essential, but has never
gotten the Twilio path to function. It is gated behind `Config.sms_enabled()`
(`config.py:50`) so it currently no-ops silently.

This collides with the "Hermes stays free" principle — Twilio is not free
past its trial credit. Before sinking effort into debugging Twilio
specifically, evaluate free push alternatives (ntfy.sh, Pushover, etc.), which
would also sidestep international SMS delivery entirely.

### 4. Canvas sync assumptions worth re-checking

Written against OSU's Canvas; the host instance may differ in what it
exposes. Watch for: whether syllabi are attached as files vs. the course
syllabus body (`modules/canvas_client.py:134-150`), whether assignment groups
and weights come through (`:193`), and whether grades are visible
(`:164`). Piazza is almost certainly unused this semester.
