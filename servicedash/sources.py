from __future__ import annotations

import csv
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import StringIO
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


async def fetch_coingecko_price(
    client: httpx.AsyncClient, asset_id: str, vs_currency: str
) -> NormalizedStatus:
    asset_id = asset_id.strip()
    vs_currency = vs_currency.strip().lower()
    if not asset_id or not vs_currency:
        return NormalizedStatus(status=Status.UNKNOWN, message="Missing asset_id/vs_currency")

    started = time.perf_counter()
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={asset_id}&vs_currencies={vs_currency}&include_last_updated_at=true"
    )
    data = await _get_json(client, url)
    latency_ms = int((time.perf_counter() - started) * 1000)

    asset = data.get(asset_id) if isinstance(data, dict) else None
    if not isinstance(asset, dict) or vs_currency not in asset:
        return NormalizedStatus(status=Status.UNKNOWN, message="Unexpected CoinGecko response", latency_ms=latency_ms)

    value = float(asset[vs_currency])
    updated_at = asset.get("last_updated_at")
    note = "CoinGecko"
    if isinstance(updated_at, (int, float)):
        dt = datetime.fromtimestamp(int(updated_at), tz=timezone.utc).astimezone()
        note = f"CoinGecko @ {dt.strftime('%H:%M:%S')}"

    return NormalizedStatus(status=Status.OPERATIONAL, message=note, latency_ms=latency_ms, value_num=value)


async def fetch_fx_rate_frankfurter(
    client: httpx.AsyncClient, base: str, quote: str
) -> NormalizedStatus:
    base = base.strip().upper()
    quote = quote.strip().upper()
    if not base or not quote:
        return NormalizedStatus(status=Status.UNKNOWN, message="Missing base/quote")

    started = time.perf_counter()
    data = await _get_json(client, f"https://api.frankfurter.app/latest?from={base}&to={quote}")
    latency_ms = int((time.perf_counter() - started) * 1000)

    rates = data.get("rates") if isinstance(data, dict) else None
    if not isinstance(rates, dict) or quote not in rates:
        return NormalizedStatus(status=Status.UNKNOWN, message="Unexpected FX response", latency_ms=latency_ms)

    value = float(rates[quote])
    date = str(data.get("date") or "").strip()
    note = f"Frankfurter {date}" if date else "Frankfurter"
    return NormalizedStatus(status=Status.OPERATIONAL, message=note, latency_ms=latency_ms, value_num=value)


async def fetch_stooq_quote(client: httpx.AsyncClient, symbol: str) -> NormalizedStatus:
    symbol = symbol.strip()
    if not symbol:
        return NormalizedStatus(status=Status.UNKNOWN, message="Missing symbol")

    started = time.perf_counter()
    csv_text = await _get_text(client, f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv")
    latency_ms = int((time.perf_counter() - started) * 1000)

    reader = csv.DictReader(StringIO(csv_text))
    row = next(reader, None)
    if not row:
        return NormalizedStatus(status=Status.UNKNOWN, message="Stooq: empty", latency_ms=latency_ms)

    close = str(row.get("Close") or "").strip()
    if not close or close.upper() == "N/D":
        return NormalizedStatus(status=Status.UNKNOWN, message="Stooq: N/D", latency_ms=latency_ms)

    try:
        value = float(close)
    except ValueError:
        return NormalizedStatus(status=Status.UNKNOWN, message="Stooq: parse error", latency_ms=latency_ms)

    date = str(row.get("Date") or "").strip()
    time_s = str(row.get("Time") or "").strip()
    note = "Stooq"
    if date and time_s and date.upper() != "N/D" and time_s.upper() != "N/D":
        note = f"Stooq {date} {time_s}"
    return NormalizedStatus(status=Status.OPERATIONAL, message=note, latency_ms=latency_ms, value_num=value)


def _parse_doomsday_seconds(html: str) -> int | None:
    # Common phrasing in the Bulletin pages: "It is 89 seconds to midnight."
    m = re.search(r"\bit\s+is\s+(?:still\s+)?(\d+)\s*seconds?\s+to\s+midnight\b", html, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d+)\s*seconds?\s+to\s+midnight\b", html, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d+)\s*minutes?\s+to\s+midnight\b", html, re.I)
    if m:
        return int(m.group(1)) * 60
    return None


def _parse_doomsday_year(html: str) -> int | None:
    m = re.search(r"/doomsday-clock/(\d{4})-statement/?", html)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(20\d{2})\s+Doomsday\s+Clock\b", html, re.I)
    if m:
        return int(m.group(1))
    return None


def _parse_doomsday_published(html: str) -> datetime | None:
    # WordPress Yoast JSON-LD includes datePublished.
    m = re.search(r"\"datePublished\"\\s*:\\s*\"([^\"]+)\"", html)
    if m:
        return parse_datetime(m.group(1))
    return None


async def fetch_doomsday_clock(
    client: httpx.AsyncClient, current_url: str, previous_url: str | None
) -> NormalizedStatus:
    current_url = current_url.strip()
    if not current_url:
        return NormalizedStatus(status=Status.UNKNOWN, message="Missing current_url")

    started = time.perf_counter()
    current_html = await _get_text(client, current_url)

    current_seconds = _parse_doomsday_seconds(current_html)
    current_year = _parse_doomsday_year(current_html)
    current_published = _parse_doomsday_published(current_html)

    prev_seconds: int | None = None
    prev_year: int | None = None
    prev_published: datetime | None = None

    if previous_url:
        try:
            prev_html = await _get_text(client, previous_url)
            prev_seconds = _parse_doomsday_seconds(prev_html)
            prev_year = _parse_doomsday_year(prev_html)
            prev_published = _parse_doomsday_published(prev_html)
        except Exception:
            prev_seconds = None

    latency_ms = int((time.perf_counter() - started) * 1000)

    if current_seconds is None:
        return NormalizedStatus(status=Status.UNKNOWN, message="Doomsday parse error", latency_ms=latency_ms)

    base = f"{current_seconds}s to midnight"
    if current_year:
        base = f"{base} ({current_year})"

    if prev_seconds is None:
        return NormalizedStatus(
            status=Status.OPERATIONAL,
            message=base,
            latency_ms=latency_ms,
            value_num=float(current_seconds),
        )

    delta = int(current_seconds - prev_seconds)
    direction = "unchanged"
    if delta < 0:
        direction = "toward midnight"
    elif delta > 0:
        direction = "away from midnight"

    duration_years = 1.0
    if current_published and prev_published:
        dt = (current_published - prev_published).total_seconds()
        if dt > 0:
            duration_years = dt / (365.25 * 24 * 3600)
    rate = delta / duration_years

    prev_label = str(prev_year) if prev_year else "prev"
    msg = f"{base}; Δ {delta:+d}s vs {prev_label} ({direction}); ~{rate:+.2f}s/yr"
    return NormalizedStatus(
        status=Status.OPERATIONAL,
        message=msg,
        latency_ms=latency_ms,
        value_num=float(current_seconds),
    )


def _inverse_cdf_datetime(xs: list[datetime], cdf: list[float], p: float) -> datetime | None:
    if not xs or not cdf or len(xs) != len(cdf):
        return None
    p = float(p)
    if p <= cdf[0]:
        return xs[0]
    for i, v in enumerate(cdf):
        if v >= p:
            if i == 0:
                return xs[0]
            v0 = float(cdf[i - 1])
            v1 = float(v)
            if v1 <= v0:
                return xs[i]
            frac = (p - v0) / (v1 - v0)
            frac = max(0.0, min(1.0, frac))
            dt0 = xs[i - 1]
            dt1 = xs[i]
            delta_s = (dt1 - dt0).total_seconds() * frac
            return dt0 + timedelta(seconds=delta_s)
    return None


async def fetch_metaculus_date(
    client: httpx.AsyncClient, question_id: int, aggregation: str, quantile: float
) -> NormalizedStatus:
    if question_id <= 0:
        return NormalizedStatus(status=Status.UNKNOWN, message="Metaculus: missing question_id")

    started = time.perf_counter()
    data = await _get_json(client, f"https://www.metaculus.com/api2/questions/{question_id}/")
    latency_ms = int((time.perf_counter() - started) * 1000)

    q = data.get("question") if isinstance(data, dict) else None
    if not isinstance(q, dict):
        return NormalizedStatus(status=Status.UNKNOWN, message="Metaculus: unexpected response", latency_ms=latency_ms)

    scaling = q.get("scaling") or {}
    cr = scaling.get("continuous_range")
    aggs = q.get("aggregations") or {}
    agg = aggs.get(aggregation) if isinstance(aggs, dict) else None
    latest = agg.get("latest") if isinstance(agg, dict) else None
    cdf = latest.get("forecast_values") if isinstance(latest, dict) else None
    n = latest.get("forecaster_count") if isinstance(latest, dict) else None

    if not isinstance(cr, list) or not isinstance(cdf, list) or len(cr) != len(cdf) or len(cr) < 2:
        return NormalizedStatus(status=Status.UNKNOWN, message="Metaculus: missing aggregate CDF", latency_ms=latency_ms)

    xs: list[datetime] = []
    ys: list[float] = []
    for t, v in zip(cr, cdf):
        dt = parse_datetime(str(t))
        if dt is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        xs.append(dt.astimezone(timezone.utc))
        ys.append(fv)

    if len(xs) < 2:
        return NormalizedStatus(status=Status.UNKNOWN, message="Metaculus: parse error", latency_ms=latency_ms)

    dt = _inverse_cdf_datetime(xs, ys, quantile)
    if dt is None:
        return NormalizedStatus(status=Status.UNKNOWN, message="Metaculus: quantile not found", latency_ms=latency_ms)

    n_txt = f" n={int(n)}" if isinstance(n, int) else ""
    msg = f"Metaculus Q{question_id} q={quantile:.2f}{n_txt} ETA {dt.date().isoformat()}"
    return NormalizedStatus(status=Status.OPERATIONAL, message=msg, latency_ms=latency_ms, value_num=dt.timestamp())


def _parse_yearish(text: str) -> float | None:
    t = text.strip()
    if not t:
        return None
    if re.fullmatch(r"(19|20)\d{2}", t):
        return float(t)
    m = re.fullmatch(r"((19|20)\d{2})\s*[-–—]\s*((19|20)\d{2})", t)
    if m:
        y0 = float(m.group(1))
        y1 = float(m.group(3))
        return (y0 + y1) / 2.0
    m = re.fullmatch(r"((19|20)\d{2})s", t)
    if m:
        y = float(m.group(1))
        return y + 5.0
    return None


async def fetch_manifold_year_market(client: httpx.AsyncClient, market_id: str) -> NormalizedStatus:
    market_id = market_id.strip()
    if not market_id:
        return NormalizedStatus(status=Status.UNKNOWN, message="Manifold: missing market_id")

    started = time.perf_counter()
    data = await _get_json(client, f"https://api.manifold.markets/v0/market/{market_id}")
    latency_ms = int((time.perf_counter() - started) * 1000)

    answers = data.get("answers") if isinstance(data, dict) else None
    question = str(data.get("question") or "").strip() if isinstance(data, dict) else ""
    if not isinstance(answers, list) or not answers:
        return NormalizedStatus(status=Status.UNKNOWN, message="Manifold: missing answers", latency_ms=latency_ms)

    pairs: list[tuple[float, float]] = []
    for a in answers:
        if not isinstance(a, dict):
            continue
        y = _parse_yearish(str(a.get("text") or ""))
        p = a.get("probability")
        if y is None or not isinstance(p, (int, float)):
            continue
        pairs.append((y, float(p)))

    total_p = sum(p for _, p in pairs)
    if total_p <= 0:
        return NormalizedStatus(status=Status.UNKNOWN, message="Manifold: no parsable year probs", latency_ms=latency_ms)

    exp_year = sum(y * p for y, p in pairs) / total_p
    year_int = int(exp_year)
    frac = max(0.0, min(1.0, exp_year - year_int))
    dt = datetime(year_int, 1, 1, tzinfo=timezone.utc) + timedelta(days=frac * 365.25)

    short_q = (question[:39] + "…") if len(question) > 40 else question
    msg = f"Manifold {market_id} E[year]={exp_year:.1f} ETA {dt.date().isoformat()} ({short_q or 'Manifold'})"
    return NormalizedStatus(status=Status.OPERATIONAL, message=msg, latency_ms=latency_ms, value_num=dt.timestamp())


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
    if t == "coingecko_price":
        return await fetch_coingecko_price(
            client,
            asset_id=str(cfg.get("asset_id", "")),
            vs_currency=str(cfg.get("vs_currency", "")),
        )
    if t == "fx_rate":
        return await fetch_fx_rate_frankfurter(
            client,
            base=str(cfg.get("base", "")),
            quote=str(cfg.get("quote", "")),
        )
    if t == "stooq_quote":
        return await fetch_stooq_quote(client, symbol=str(cfg.get("symbol", "")))
    if t == "doomsday_clock":
        prev = str(cfg.get("previous_url") or "").strip() or None
        return await fetch_doomsday_clock(
            client,
            current_url=str(cfg.get("current_url", "")),
            previous_url=prev,
        )
    if t == "metaculus_date":
        try:
            qid = int(cfg.get("question_id") or 0)
        except (TypeError, ValueError):
            qid = 0
        agg = str(cfg.get("aggregation") or "recency_weighted")
        try:
            quantile = float(cfg.get("quantile") or 0.5)
        except (TypeError, ValueError):
            quantile = 0.5
        return await fetch_metaculus_date(client, question_id=qid, aggregation=agg, quantile=quantile)
    if t == "manifold_year_market":
        return await fetch_manifold_year_market(client, market_id=str(cfg.get("market_id", "")))
    return NormalizedStatus(status=Status.UNKNOWN, message=f"Unknown service type: {t}")
