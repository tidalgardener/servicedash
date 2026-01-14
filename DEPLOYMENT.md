# Deployment

## Environments (TBD)
- Local-only.

## Build steps (TBD)
- Local setup:
  - `python3 -m venv .venv`
  - `. .venv/bin/activate`
  - `python -m pip install -r requirements.txt`

## Deploy steps (TBD)
- Not applicable.

## Rollback (TBD)
- Not applicable.

## Operational notes / gotchas
- Recommended: run `python -m servicedash poll --log` in a long-running terminal to keep 24h history filled, then view with `python -m servicedash run`.
