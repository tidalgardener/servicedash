# ServiceDash

## Purpose
- Local terminal dashboard (retro amber/green vibe) that aggregates status across multiple third-party services and keeps the last 24 hours of history.

## Quickstart
```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m servicedash run
```

Keys: `r` refresh, `n`/`p` page, `q` quit.

Snapshot mode (renders one frame and exits):
```bash
python -m servicedash run --once --no-screen
```

Headless poller (keeps history building without the UI):
```bash
python -m servicedash poll --log
```

## Configuration
- `servicedash.json` controls what services are tracked, polling interval, and the local SQLite DB path.
- Default DB location: `data/servicedash.sqlite3` (created automatically).
- Default refresh: `poll_interval_seconds: 300` (5 minutes). Press `r` to force an immediate refresh.
- Defaults include:
  - Status sources: OpenAI/Codex, Gemini, AWS, GAE, HelpScout, Slack, Anthropic/Claude, Shopify, Vercel.
  - Internet-critical: Cloudflare, GitHub, Netlify.
  - Markets: Bitcoin network health (mempool/fees), BTC/USD, CAD→USD, EUR/USD, USD/JPY, SPX/NDX, gold, silver, copper, WTI, nat gas, plus a few large-cap stocks (TSLA/GOOGL/AAPL/MSFT/NVDA/AMZN/META).
  - Doomsday Clock (seconds to midnight + direction/velocity vs previous statement).
  - Hypothetical AGI/ASI countdown clocks (Metaculus + Manifold community forecasts).

## Development workflow
- Run the dashboard: `python -m servicedash run`
- Edit config: `servicedash.json`

## Testing
- Basic syntax check: `python -m py_compile servicedash/*.py`
- Basic smoke run: `python -m servicedash run --once --no-screen`

## Deployment
- Not applicable (local utility).

## Repo structure (high level)
- `servicedash/` — Python app (polling, storage, UI)
- `servicedash.json` — default config (services + intervals)
- `data/` — local SQLite DB (gitignored)

## Known issues / gotchas
- AWS RSS can be empty when there are no active events (this is expected).
