# CONTINUITY

## Goal (incl. success criteria)
- Build a local terminal dashboard ("ServiceDash") with an amber/green retro terminal vibe (80 columns wide; can be tall, ~80 lines) that shows current status + last 24h history for:
  - OpenAI (highlight Codex), Gemini, AWS, Google App Engine, HelpScout, Slack, Anthropic/Claude, Shopify, Vercel.
  - Economic metrics (BTC price, FX, indices, commodities, key equities).
  - Hypothetical “countdown clocks” for AGI and ASI/Singularity (based on public forecasting sources).
- Success criteria:
  - `servicedash` runs locally and renders a single-screen dashboard in terminal.
  - Polls public status sources over HTTPS and persists results locally.
  - Shows last 24h uptime/trend per service and a short incident summary.

## Constraints/Assumptions
- Local-only utility; no AWS credentials or AWS APIs.
- Everything lives in this folder (code + local data); no external infra.
- Network access is allowed for polling public status endpoints.
- Some providers might not expose an official status API for every requested product (UNCONFIRMED for “Gemini”); must be configurable.

## Key decisions
- Python 3 (tested with the system Python 3.9 on macOS).
- UI: `rich` (alt-screen live dashboard; amber/green CRT vibe with Matrix accents; targets 80x25).
- Polling: `httpx` over HTTPS against public status endpoints.
- Refresh cadence: polls on startup, then every `poll_interval_seconds` (default `300`); manual refresh via `r`.
- Persistence: local SQLite at `data/servicedash.sqlite3` (in-repo, gitignored).
- Layout: fixed-width 80-col-safe rows with group headers; grouping can be overridden via `servicedash.json` per-service `"group"`.
- “Gemini” source: Google Cloud status product “Vertex Gemini API” (product id `Z0FZJAMvEB4j3NbCJs6B`).
- “Claude” source: split into component rows from `status.anthropic.com` (claude.ai / Claude API / Claude Code).
- Optional headless poller mode: `python -m servicedash poll` for background-ish history collection.
- Economic metrics sources:
  - BTC/USD: CoinGecko simple price API.
  - CAD→USD: Frankfurter FX API.
  - Market/commodities/stocks: Stooq CSV quotes.
- Internet health sources:
  - Cloudflare/GitHub/Netlify: Statuspage-compatible `api/v2/summary.json`.
- Doomsday Clock:
  - Scrape Bulletin “Doomsday Clock Statement” pages for seconds-to-midnight and compare vs previous statement.
- AGI/ASI clocks:
  - Use Metaculus (AGI date question) and a public Manifold market (ASI year question) as *hypothetical* sources.

## State: Done / Now / Next
- Done:
  - Bootstrapped docs + git repo + default config (`servicedash.json`).
  - Implemented polling sources (Statuspage, Slack API, Google Cloud incidents.json, AWS RSS).
  - Implemented SQLite storage and 24h trend/uptime rendering.
  - Implemented the amber terminal dashboard (`python -m servicedash run`).
  - Added a headless poller command (`python -m servicedash poll`) for ongoing history collection.
  - Split Claude into separate component rows (claude.ai / Claude API / Claude Code).
  - Added auto-paging when service count exceeds screen height.
  - Added keybinds in the UI (r refresh, n/p page, q quit).
  - Added Shopify + Vercel status sources.
  - Added internet-critical status sources (Cloudflare, GitHub, Netlify).
  - Added market metrics (BTC, FX, indices, commodities, mega-cap stocks) with numeric history storage and 24h deltas.
  - Added Bitcoin network health (blocks age + mempool/fee congestion) via `mempool.space` API.
  - Added Doomsday Clock line at the bottom with direction/velocity vs the previous statement.
  - Added AGI and ASI/Singularity “countdown clocks” pinned at the top (Metaculus + Manifold sources).
  - Smoke-checked: `python -m py_compile servicedash/*.py`, `python -m servicedash poll --once --log`, and `python -m servicedash run --once --no-screen`.
  - UI refresh: grouped sections + fixed-width (80-col safe) layout + “Matrix” header noise + extra indicators (latency in-row, episode count, delta arrows).
- Now:
  - Docs/handoff sync; ready for incremental UX polish.
- Next:
  - Tighten UI fit/legibility for strict 80x25 after grouping (optional polish).
  - Add export (CSV/JSON) if desired.
  - Optional: improve paging so group headers don't split from their first row.
  - Optional: add more “internet health” providers and market/rates indicators.

## Open questions (mark as UNCONFIRMED if needed)
- UNCONFIRMED: Any other specific economic metrics you want (rates/indices/yields) beyond the defaults?
- UNCONFIRMED: Prefer the AGI/ASI clocks to come from Metaculus only, or is Manifold acceptable as a source for one of them?
- UNCONFIRMED: What polling interval do you want (default 5 min)?
- UNCONFIRMED: OK using `mempool.space` as the Bitcoin network health source?
- UNCONFIRMED: Prefer a specific group order/naming for the dashboard sections?

## Working set (files/ids/commands)
- Branch: `main`
- HEAD commit (run): `git rev-parse HEAD`
- Last UI/indicator commit: `3683917d4ea2c1b09b47f3c4fe4df39852754cdf`
- Files: `servicedash/`, `servicedash/ui.py`, `servicedash/sources.py`, `servicedash.json`, `requirements.txt`, `README.md`, `CONTINUITY.md`
- Commands:
  - Setup: `python3 -m venv .venv && . .venv/bin/activate && python -m pip install -r requirements.txt`
  - Run: `python -m servicedash run`
  - Snapshot: `python -m servicedash run --once --no-screen`
  - Poll (headless): `python -m servicedash poll --log`
