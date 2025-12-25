from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

import httpx

from .config import AppConfig
from .db import PollRow, insert_poll, prune_before
from .sources import Service, fetch_service
from .status import NormalizedStatus, Status
from .timeutil import utc_now_ts


def build_services(cfg: AppConfig) -> list[Service]:
    return [Service(id=s.id, name=s.name, type=s.type, cfg=s.cfg) for s in cfg.services]


@dataclass(frozen=True)
class PollOutcome:
    service: Service
    status: NormalizedStatus


async def poll_once(
    client: httpx.AsyncClient, services: list[Service], *, concurrency: int = 8
) -> list[PollOutcome]:
    sem = asyncio.Semaphore(concurrency)

    async def _one(svc: Service) -> PollOutcome:
        async with sem:
            try:
                return PollOutcome(service=svc, status=await fetch_service(client, svc))
            except Exception as e:
                return PollOutcome(
                    service=svc,
                    status=NormalizedStatus(status=Status.UNKNOWN, message=f"Fetch error: {type(e).__name__}"),
                )

    return await asyncio.gather(*[_one(s) for s in services])


def record_outcomes(conn, outcomes: list[PollOutcome]) -> None:
    ts = utc_now_ts()
    for o in outcomes:
        insert_poll(
            conn,
            PollRow(
                ts=ts,
                service_id=o.service.id,
                service_name=o.service.name,
                status=o.status.status.key,
                severity=o.status.status.severity,
                message=o.status.message,
                latency_ms=o.status.latency_ms,
            ),
        )


def prune_history(conn, retention_hours: int) -> int:
    cutoff = utc_now_ts() - int(timedelta(hours=retention_hours).total_seconds())
    return prune_before(conn, cutoff_ts=cutoff)
