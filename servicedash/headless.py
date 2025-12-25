from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import httpx

from .config import load_config
from .db import connect, init_db
from .poller import build_services, poll_once, prune_history, record_outcomes


async def run_poller(*, config_path: Path, once: bool, log: bool) -> None:
    cfg = load_config(config_path)
    conn = connect(cfg.database_path)
    init_db(conn)

    services = build_services(cfg)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        headers={"User-Agent": "servicedash/0.1"},
        follow_redirects=True,
    ) as client:
        try:
            while True:
                outcomes = await poll_once(client, services)
                record_outcomes(conn, outcomes)
                pruned = prune_history(conn, cfg.retention_hours)
                if log:
                    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
                    worst = max(outcomes, key=lambda o: o.status.status.severity)
                    msg = f"{ts} polled {len(outcomes)} services; worst={worst.service.name}={worst.status.status.key}"
                    if pruned:
                        msg += f"; pruned={pruned}"
                    print(msg, flush=True)

                if once:
                    break
                await asyncio.sleep(cfg.poll_interval_seconds)
        finally:
            conn.close()

