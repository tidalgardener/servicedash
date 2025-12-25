# QA

## How to test locally (commands; TBD)
- Setup (once): `python3 -m venv .venv && . .venv/bin/activate && python -m pip install -r requirements.txt`
- Headless poll (one shot): `python -m servicedash poll --once --log`
- Smoke render: `python -m servicedash run --once --no-screen`
- Live dashboard: `python -m servicedash run`

## Test checklist
- Dashboard launches and updates without crashing.
- Polling stores history and prunes/queries the last 24 hours correctly.

## Known failing tests / flaky areas
- None (repo just bootstrapped).
