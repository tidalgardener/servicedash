# Session Handoff

## Ledger Snapshot (Goal / Now / Next / Open Questions)
- Goal: Build a local terminal status dashboard with 24h history.
- Now: Core app is implemented; optional polish.
- Next: Decide if “Claude” should be split (API vs Code) and iterate UI fit.
- Open Questions:
  - UNCONFIRMED: Add more Claude rows (Console, etc.)?

## What changed (files/components)
- Added the Python app under `servicedash/` (polling, storage, UI).
- Added default config `servicedash.json` and dependencies in `requirements.txt`.
 - Added `python -m servicedash poll` for headless history polling.

## Decisions (what/why)
- Python + `rich` for a full-screen-ish 80x25 retro terminal dashboard.
- SQLite for local history storage.

## Commands to run (build/test/dev)
- Setup: `python3 -m venv .venv && . .venv/bin/activate && python -m pip install -r requirements.txt`
- Run: `python -m servicedash run`
- Snapshot: `python -m servicedash run --once --no-screen`
 - Headless poller: `python -m servicedash poll --log`

## Next steps (priority order)
- Optional: tighten layout for strict 80x25 terminals.
- Optional: add export (CSV/JSON) and manual refresh keybind.

## Known issues / risks / gotchas
- Terminal must be at least 80x25; smaller terminals will refuse to start.
