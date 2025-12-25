# CONTINUITY

## Goal (incl. success criteria)
- Build a local terminal dashboard ("ServiceDash") with an amber/green retro terminal vibe (80 columns wide; can be tall, ~80 lines) that shows current status + last 24h history for:
  - OpenAI (highlight Codex), Gemini, AWS, Google App Engine, HelpScout, Slack, Anthropic/Claude, Shopify, Vercel.
  - Economic metrics (BTC price, CAD→USD FX, and a few market/commodity quotes).
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
- UI: `rich` (alt-screen live dashboard; amber/green CRT vibe; targets 80x25).
- Polling: `httpx` over HTTPS against public status endpoints.
- Persistence: local SQLite at `data/servicedash.sqlite3` (in-repo, gitignored).
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
  - Added Doomsday Clock line at the bottom with direction/velocity vs the previous statement.
- Now:
  - Tighten layout/legibility for tall terminals (optional polish).
- Next:
  - Tighten UI fit/legibility for strict 80x25 (optional polish).
  - Add export (CSV/JSON) if desired.

## Open questions (mark as UNCONFIRMED if needed)
- UNCONFIRMED: Any other specific economic metrics you want (rates/indices/yields) beyond the defaults?

## Working set (files/ids/commands)
- Files: `servicedash/`, `servicedash.json`, `requirements.txt`, `README.md`, `CONTINUITY.md`
- Commands:
  - Setup: `python3 -m venv .venv && . .venv/bin/activate && python -m pip install -r requirements.txt`
  - Run: `python -m servicedash run`
  - Snapshot: `python -m servicedash run --once --no-screen`
  - Poll (headless): `python -m servicedash poll --log`
