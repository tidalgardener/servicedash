from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .status import (
    NormalizedStatus,
    Status,
    status_from_gcp_incident,
    status_from_slack_status,
    status_from_statuspage_component,
    status_from_statuspage_indicator,
    worst_status,
)
from .timeutil import parse_datetime


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    type: str
    cfg: dict[str, Any]


async def _get_json(client: httpx.AsyncClient, url: str) -> Any:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


async def _get_text(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.text


def _match_any(name: str, patterns: list[str]) -> bool:
    n = name.lower()
    for p in patterns:
        if not p:
            continue
        if p.lower() in n:
            return True
    return False


async def fetch_statuspage_overall(client: httpx.AsyncClient, base_url: str) -> NormalizedStatus:
    started = time.perf_counter()
    summary = await _get_json(client, f"{base_url.rstrip('/')}/api/v2/summary.json")
    latency_ms = int((time.perf_counter() - started) * 1000)

    status_obj = summary.get("status") or {}
    status = status_from_statuspage_indicator(status_obj.get("indicator"))

    incidents = summary.get("incidents") or []
    active = [i for i in incidents if str(i.get("status", "")).lower() not in {"resolved", "postmortem"}]
    if active:
        top = active[0]
        message = f"{len(active)} active: {top.get('name', 'incident')}"
    else:
        message = str(status_obj.get("description") or "").strip() or status.key

    return NormalizedStatus(status=status, message=message, latency_ms=latency_ms)


async def fetch_statuspage_component(
    client: httpx.AsyncClient, base_url: str, component_match: list[str]
) -> NormalizedStatus:
    started = time.perf_counter()
    summary = await _get_json(client, f"{base_url.rstrip('/')}/api/v2/summary.json")
    latency_ms = int((time.perf_counter() - started) * 1000)

    components = summary.get("components") or []
    matched = [c for c in components if _match_any(str(c.get("name", "")), component_match)]
    if not matched:
        return NormalizedStatus(
            status=Status.UNKNOWN, message=f"No components matched: {', '.join(component_match) or '∅'}", latency_ms=latency_ms
        )

    statuses = [status_from_statuspage_component(c.get("status")) for c in matched]
    status = worst_status(statuses)

    parts: list[str] = []
    for c in matched[:3]:
        parts.append(f"{c.get('name')}: {c.get('status')}")
    extra = "" if len(matched) <= 3 else f" (+{len(matched) - 3} more)"
    message = "; ".join(parts) + extra
    return NormalizedStatus(status=status, message=message, latency_ms=latency_ms)


async def fetch_slack(client: httpx.AsyncClient, current_url: str, history_url: str | None) -> NormalizedStatus:
    started = time.perf_counter()
    current = await _get_json(client, current_url)
    latency_ms = int((time.perf_counter() - started) * 1000)

    active_incidents = current.get("active_incidents") or []
    status = status_from_slack_status(current.get("status"), len(active_incidents))

    msg_parts: list[str] = []
    if active_incidents:
        msg_parts.append(f"{len(active_incidents)} active incident(s)")
    else:
        msg_parts.append("No active incidents")

    if history_url:
        try:
            history = await _get_json(client, history_url)
            now = datetime.now(timezone.utc)
            since = now - timedelta(hours=24)
            recent = 0
            if isinstance(history, list):
                for item in history:
                    created = parse_datetime(str(item.get("date_created") or "")) if isinstance(item, dict) else None
                    if created and created >= since:
                        recent += 1
            msg_parts.append(f"{recent} in last 24h")
        except Exception:
            msg_parts.append("history: error")

    return NormalizedStatus(status=status, message="; ".join(msg_parts), latency_ms=latency_ms)


async def fetch_aws_rss(client: httpx.AsyncClient, rss_url: str) -> NormalizedStatus:
    started = time.perf_counter()
    xml_text = await _get_text(client, rss_url)
    latency_ms = int((time.perf_counter() - started) * 1000)

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return NormalizedStatus(status=Status.UNKNOWN, message="RSS parse error", latency_ms=latency_ms)

    items = root.findall("./channel/item")
    if not items:
        return NormalizedStatus(status=Status.OPERATIONAL, message="No active events", latency_ms=latency_ms)

    titles = []
    for it in items[:10]:
        title = (it.findtext("title") or "").strip()
        if title:
            titles.append(title)

    active = [t for t in titles if "RESOLVED" not in t.upper()]
    if active:
        return NormalizedStatus(status=Status.DEGRADED, message=f"Active: {active[0]}", latency_ms=latency_ms)
    return NormalizedStatus(status=Status.OPERATIONAL, message=f"{len(titles)} event(s) (all resolved)", latency_ms=latency_ms)


async def fetch_gcp_incidents(
    client: httpx.AsyncClient, incidents_url: str, product_ids: list[str]
) -> NormalizedStatus:
    started = time.perf_counter()
    incidents = await _get_json(client, incidents_url)
    latency_ms = int((time.perf_counter() - started) * 1000)

    if not isinstance(incidents, list):
        return NormalizedStatus(status=Status.UNKNOWN, message="Unexpected incidents JSON shape", latency_ms=latency_ms)

    product_ids = [p for p in product_ids if p]
    if not product_ids:
        return NormalizedStatus(status=Status.UNKNOWN, message="No product_ids configured", latency_ms=latency_ms)

    matched: list[dict[str, Any]] = []
    for inc in incidents:
        if not isinstance(inc, dict):
            continue
        affected = inc.get("affected_products") or []
        for p in affected:
            if isinstance(p, dict) and p.get("id") in product_ids:
                matched.append(inc)
                break

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)

    active: list[dict[str, Any]] = []
    recent_total = 0
    for inc in matched:
        begin = parse_datetime(str(inc.get("begin") or ""))
        if begin and begin >= since:
            recent_total += 1
        end = parse_datetime(str(inc.get("end") or ""))
        if end is None:
            active.append(inc)

    if not active:
        return NormalizedStatus(
            status=Status.OPERATIONAL, message=f"No active incidents; {recent_total} in last 24h", latency_ms=latency_ms
        )

    statuses: list[Status] = []
    for inc in active:
        statuses.append(
            status_from_gcp_incident(
                str(inc.get("status_impact") or ""),
                str(inc.get("severity") or ""),
                has_end=False,
            )
        )
    status = worst_status(statuses)
    top = active[0]
    desc = str(top.get("external_desc") or "").strip() or "Active incident"
    return NormalizedStatus(
        status=status,
        message=f"{len(active)} active: {desc}",
        latency_ms=latency_ms,
    )


async def fetch_service(client: httpx.AsyncClient, service: Service) -> NormalizedStatus:
    t = service.type
    cfg = service.cfg
    if t == "statuspage":
        return await fetch_statuspage_overall(client, base_url=str(cfg.get("base_url", "")))
    if t == "statuspage_component":
        return await fetch_statuspage_component(
            client,
            base_url=str(cfg.get("base_url", "")),
            component_match=list(cfg.get("component_match") or []),
        )
    if t == "slack":
        return await fetch_slack(
            client,
            current_url=str(cfg.get("current_url", "")),
            history_url=str(cfg.get("history_url") or "") or None,
        )
    if t == "aws_rss":
        return await fetch_aws_rss(client, rss_url=str(cfg.get("rss_url", "")))
    if t == "gcp_incidents":
        return await fetch_gcp_incidents(
            client,
            incidents_url=str(cfg.get("incidents_url", "")),
            product_ids=list(cfg.get("product_ids") or []),
        )
    return NormalizedStatus(status=Status.UNKNOWN, message=f"Unknown service type: {t}")

