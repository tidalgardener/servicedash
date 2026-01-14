# Context Memory

## Domain/context essentials (facts only)
- ServiceDash is a local dashboard that polls public status endpoints and renders an amber-themed terminal UI.
- It also polls a few economic metrics (BTC, FX, market/commodity quotes) and keeps 24h history in SQLite.
- It includes “hypothetical” AGI/ASI clocks sourced from public forecasting sites (currently Metaculus + Manifold).
- It includes a Bitcoin network “health” row (block age + mempool/fee congestion) via `mempool.space`.

## Architecture notes (high level)
- `servicedash/cli.py`: CLI entry (`run`, `poll`).
- `servicedash/ui.py`: Rich UI renderer (80-col fixed width, grouped sections, paging, keybinds).
- `servicedash/sources.py`: All fetchers (Statuspage, Slack, AWS RSS, GCP incidents, markets/FX, doomsday/forecast clocks, bitcoin network health).
- `servicedash/poller.py`: Concurrency-limited polling loop + normalization into DB rows.
- `servicedash/db.py`: SQLite schema + queries for latest + 24h series.
- `servicedash/config.py`: Loads `servicedash.json`.

## Conventions (naming, style, patterns)
- `servicedash.json` is the source of truth for what gets shown; list order is preserved and can be grouped in UI.
- Service `type` drives which fetcher runs (see `servicedash/sources.py:fetch_service`).
- Local history uses SQLite at `data/servicedash.sqlite3` (gitignored).

## Non-obvious constraints (UNCONFIRMED if needed)
- “Gemini” uses Google Cloud status incidents with product id for “Vertex Gemini API” (`Z0FZJAMvEB4j3NbCJs6B`).
- Terminal must be at least `80x25` (recommended ~`80x80` for many rows).
