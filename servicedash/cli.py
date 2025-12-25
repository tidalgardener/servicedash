from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .headless import run_poller
from .ui import run_dashboard


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="servicedash",
        description="Local terminal status dashboard (retro amber vibe).",
    )
    parser.add_argument(
        "--config",
        default="servicedash.json",
        help="Path to config JSON (default: servicedash.json).",
    )

    sub = parser.add_subparsers(dest="cmd", required=False)

    run = sub.add_parser("run", help="Run the live dashboard (default).")
    run.add_argument(
        "--no-screen",
        action="store_true",
        help="Disable alternate-screen mode (useful for logs).",
    )
    run.add_argument(
        "--once",
        action="store_true",
        help="Render one frame and exit (still performs a poll first).",
    )

    poll = sub.add_parser("poll", help="Poll in a loop (no UI) to build history.")
    poll.add_argument(
        "--once",
        action="store_true",
        help="Poll once and exit.",
    )
    poll.add_argument(
        "--log",
        action="store_true",
        help="Print a short line each poll.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.exists():
        parser.error(f"Config file not found: {config_path}")

    cmd = args.cmd or "run"
    if cmd == "run":
        asyncio.run(run_dashboard(config_path=config_path, screen=not args.no_screen, once=args.once))
        return 0
    if cmd == "poll":
        asyncio.run(run_poller(config_path=config_path, once=args.once, log=args.log))
        return 0

    parser.error(f"Unknown command: {cmd}")
