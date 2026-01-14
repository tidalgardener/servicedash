# Session Handoff

## Ledger Snapshot (Goal / Now / Next / Open Questions)
- Goal: Local terminal dashboard (80 cols; tall ok) with retro amber/green/Matrix vibe that aggregates SaaS status + markets + 24h history (SQLite).
- Now: App is running end-to-end; UI is grouped and fixed-width; docs are synced for handoff.
- Next (priority):
  1) Optional: export last 24h to JSON/CSV.
  2) Optional: improve paging so section headers don't split from their first row.
  3) Optional: add more indicators (VIX, yields, DXY, DNS/root/CDN status, etc. — UNCONFIRMED sources).
- Open Questions (UNCONFIRMED):
  - Preferred polling interval (default 5 min).
  - Prefer AGI/ASI clocks from Metaculus only, or Manifold acceptable for one.
  - Any preferred group order/naming for sections.

## Project purpose + current state
- `servicedash` polls public status endpoints + market data and renders a single-screen terminal dashboard with 24h history.
- UI shows grouped sections, per-row gauges/sparklines, and a Matrix-style header summary/noise line.

## At the moment we stopped…
- Core code is stable; latest “UI/indicators” commit: `3683917d4ea2c1b09b47f3c4fe4df39852754cdf`.
- Latest docs/ledger sync commit: `475b4ea`.
- Ran local checks:
  - `python -m py_compile servicedash/*.py`
  - `python -m servicedash poll --once --log`
  - `python -m servicedash run --once --no-screen`
- Observed: Stooq can intermittently timeout/return empty for some symbols, which shows as `unknown` in the dashboard.

## What changed recently (files/components)
- `servicedash/ui.py`: grouped sections + fixed-width 80-col-safe rows; header health summary; Matrix “noise”; extra indicators (latency in-row, episode count `%E#`, delta arrows).
- `servicedash/sources.py`: added Bitcoin network “health” row via `mempool.space` (block age + mempool/fee congestion).
- `servicedash.json`: default config expanded (services + markets + clocks).
- Docs: `CONTINUITY.md`, `README.md`, `START_HERE.md`, `TODO.md`, `NEXT_STEPS.md`, `QA.md`, `PLAYBOOKS.md`, `DEPLOYMENT.md`.

## Decisions (what/why)
- Python + `rich` for a full-screen-ish terminal dashboard (fast iteration, cross-platform).
- SQLite for local history storage (simple, zero infra).
- Grouped UI layout (faster scanning across many services/metrics; override via per-service `"group"`).

## Commands to run (build/test/dev)
- Setup: `python3 -m venv .venv && . .venv/bin/activate && python -m pip install -r requirements.txt`
- Headless poller (build history): `python -m servicedash poll --log`
- Live dashboard: `python -m servicedash run`
- Smoke checks:
  - `python -m py_compile servicedash/*.py`
  - `python -m servicedash poll --once --log`
  - `python -m servicedash run --once --no-screen`

## Known issues / risks / gotchas
- Terminal must be at least `80x25` (recommended ~`80x80`).
- Market quotes via Stooq can intermittently fail/timeout (shows as `unknown`).

## Working set
- Branch: `main`
- HEAD commit (run): `git rev-parse HEAD`
- Remote: `git@github.com:tidalgardener/servicedash.git`
- Key files:
  - `servicedash/ui.py`
  - `servicedash/sources.py`
  - `servicedash.json`
  - `data/servicedash.sqlite3` (local, gitignored)

## Paste-into-next-session prompt
You are working in `/Users/babakm/project/servicedash`. Please:
1) Read `START_HERE.md`, then `CONTINUITY.md` (canonical), then `README.md`.
2) Run smoke checks: `python -m py_compile servicedash/*.py && python -m servicedash poll --once --log && python -m servicedash run --once --no-screen`.
3) Implement next tasks from `TODO.md` (priority: export last 24h; improve paging so group headers stick with first row; optionally add more indicators).
4) Keep the UI 80-col safe, preserve local-only constraints, and update `CONTINUITY.md` + `SESSION_HANDOFF.md` as you go.
