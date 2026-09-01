# Hermes

An AI school buddy: it watches Canvas, works out how hard and how long each
assignment actually is, and keeps its user ahead of their coursework through a
web dashboard and phone notifications.

## Mission

Hermes should be able to do anything it needs to — it is the project's job to
give it that capability, not the user's job to work around what it lacks.

Symmetrically, a Hermes user should be able to do anything they think they
should be able to do.

Balancing those two — an interface that stays intuitive and uncluttered, while
the underlying capability stays rich and unrestricted — is the standard every
change gets measured against.

### What follows from that

- **Manual entry is a universal fallback.** Any capability that depends on an
  outside system (Canvas sync, syllabus parsing, grade import) should degrade
  to "the user can just tell Hermes directly" rather than dead-end. Automation
  is the fast path, never the only path.
- **A kill switch, not a missing feature.** If a capability costs real
  resources or misbehaves, it gets a switch to turn it off — that is the
  escape hatch, rather than leaving the capability unbuilt.
- **Capability without clutter.** Fallbacks and advanced controls earn their
  place in the UI quietly (collapsed, secondary, out of the primary flow).
  Richness is not permission to crowd the interface.
- **Hermes stays free to run.** Paid dependencies are avoided where a free
  path exists; a feature that can only work behind a paywall needs a free
  alternative explored first.

## Architecture

- `main.py` — entrypoint that runs a re-analysis pass and blocks until done.
- `hermes.py` — orchestration: Canvas sync, notification checks, daily digest,
  and the APScheduler jobs that drive them.
- `config.py` — env-driven settings, loaded from `.env` (never committed).
- `database.py` — SQLite persistence; every table and query lives here.
- `modules/canvas_client.py` — Canvas REST API wrapper.
- `modules/analyzer.py` — all AI logic; Gemini primary, Groq fallback + chat.
- `modules/scheduler_engine.py` — decides *when* to nudge the user.
- `modules/notifier.py` — outbound-only Twilio SMS.
- `modules/syllabus.py`, `modules/piazza_client.py` — syllabus PDF parsing,
  optional Piazza integration.
- `web/app.py` — the dashboard and its JSON APIs; the primary interface.

Assignment data flows: Canvas sync → AI analysis against syllabus-derived
rules → SQLite → dashboard and notifications.

## Conventions

- Secrets live only in `.env` (git-ignored). Never commit tokens or keys, and
  never paste them into files, commits, or issue text.
- Due dates are absolute instants and keep the meaning their institution gave
  them. Reminder timing is computed against the user's *current* local
  timezone, so nudges arrive when they can actually act on them.
