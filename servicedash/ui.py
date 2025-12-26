from __future__ import annotations

import asyncio
import contextlib
import random
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live

from .config import load_config
from .db import PollRow, connect, init_db, latest_for_service, series_for_service
from .poller import build_services, poll_once, prune_history, record_outcomes
from .status import Status
from .timeutil import utc_now_ts


AMBER = "rgb(255,176,0)"
DIM_AMBER = "rgb(160,110,0)"
GREEN = "rgb(0,255,0)"
RED = "rgb(255,80,80)"
MATRIX_GREEN = "rgb(80,255,140)"
DIM_MATRIX = "rgb(0,110,60)"

# Panel is 80 cols wide. With a 1-col left/right padding and a 1-col border on each side,
# the usable width for single-line content is 76.
INNER_WIDTH = 76


def _status_style(status: str) -> str:
    if status == Status.OPERATIONAL.key:
        return GREEN
    if status == Status.DEGRADED.key:
        return AMBER
    if status == Status.OUTAGE.key:
        return RED
    return DIM_AMBER


def _status_chip(status: str) -> Text:
    dot = "●"
    short = {
        Status.OPERATIONAL.key: "OK",
        Status.DEGRADED.key: "DEG",
        Status.OUTAGE.key: "DOWN",
        Status.UNKNOWN.key: "UNK",
    }.get(status, status.upper()[:3])
    label = short
    return Text(f"{dot} {label}", style=_status_style(status))


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)] + "…"


def _bucket_trend(rows: list[PollRow], *, hours: int = 24, buckets: int = 24) -> list[int | None]:
    if buckets <= 0:
        return []
    now_ts = utc_now_ts()
    start_ts = now_ts - hours * 3600
    span = hours * 3600
    bucket_size = max(1, span // buckets)

    values: list[int | None] = [None] * buckets
    for r in rows:
        if r.ts < start_ts:
            continue
        idx = min(buckets - 1, max(0, (r.ts - start_ts) // bucket_size))
        if values[idx] is None:
            values[idx] = r.severity
        else:
            values[idx] = max(values[idx] or 0, r.severity)
    return values


def _trend_spark(values: list[int | None]) -> Text:
    # severity: 0 ok, 1 degraded, 2 outage, 3 unknown
    out = Text()
    for v in values:
        if v is None:
            out.append("·", style=DIM_AMBER)
        elif v <= 0:
            out.append("▁", style=GREEN)
        elif v == 1:
            out.append("▄", style=AMBER)
        elif v == 2:
            out.append("█", style=RED)
        else:
            out.append("░", style=DIM_AMBER)
    return out


def _bucket_values(rows: list[PollRow], *, hours: int = 24, buckets: int = 24) -> list[float | None]:
    if buckets <= 0:
        return []
    now_ts = utc_now_ts()
    start_ts = now_ts - hours * 3600
    span = hours * 3600
    bucket_size = max(1, span // buckets)

    values: list[float | None] = [None] * buckets
    for r in rows:
        if r.ts < start_ts or r.value_num is None:
            continue
        idx = min(buckets - 1, max(0, (r.ts - start_ts) // bucket_size))
        values[idx] = float(r.value_num)
    return values


def _value_spark(values: list[float | None], *, style: str) -> Text:
    blocks = "▁▂▃▄▅▆▇█"
    nums = [v for v in values if v is not None]
    if not nums:
        return Text("·" * len(values), style=DIM_AMBER)
    lo = min(nums)
    hi = max(nums)
    out = Text()
    if hi <= lo:
        out.append(blocks[3] * len(values), style=style)
        return out

    for v in values:
        if v is None:
            out.append("·", style=DIM_AMBER)
            continue
        idx = int(round((float(v) - lo) / (hi - lo) * (len(blocks) - 1)))
        idx = max(0, min(len(blocks) - 1, idx))
        out.append(blocks[idx], style=style)
    return out


def _metric_change(rows: list[PollRow]) -> tuple[float | None, float | None, float | None]:
    vals = [r.value_num for r in rows if r.value_num is not None]
    if len(vals) < 2:
        return None, None, None
    first = float(vals[0])
    last = float(vals[-1])
    if first == 0:
        pct = None
    else:
        pct = (last - first) / first
    return first, last, pct


def _metric_range(rows: list[PollRow]) -> tuple[float | None, float | None]:
    vals = [float(r.value_num) for r in rows if r.value_num is not None]
    if not vals:
        return None, None
    return min(vals), max(vals)


def _format_value(view: "ServiceView", value: float) -> str:
    if view.type in {"metaculus_date", "manifold_year_market"}:
        target_ts = float(value)
        now_ts = utc_now_ts()
        delta_s = target_ts - now_ts
        days = int(delta_s // 86400)
        if days >= 0:
            return f"T-{days}d"
        return f"T+{abs(days)}d"

    fmt = view.cfg.get("format")
    if not isinstance(fmt, dict):
        fmt = {}

    prefix = str(fmt.get("prefix") or "")
    suffix = str(fmt.get("suffix") or "")
    thousands = bool(fmt.get("thousands", False))

    decimals_raw = fmt.get("decimals")
    decimals: int | None
    if isinstance(decimals_raw, int):
        decimals = decimals_raw
    elif isinstance(decimals_raw, str) and decimals_raw.strip().isdigit():
        decimals = int(decimals_raw.strip())
    else:
        decimals = None

    if decimals is None:
        if view.type == "fx_rate":
            decimals = 5
        elif view.type == "coingecko_price":
            decimals = 0 if abs(value) >= 1000 else 2
        else:
            decimals = 2

    num = f"{value:,.{decimals}f}" if thousands else f"{value:.{decimals}f}"
    return f"{prefix}{num}{suffix}"


def _uptime(rows: list[PollRow]) -> float | None:
    if not rows:
        return None
    ok = sum(1 for r in rows if r.status == Status.OPERATIONAL.key)
    return ok / len(rows)


def _uptime_bar(ratio: float | None, width: int = 12) -> Text:
    if ratio is None:
        return Text(" " * width, style=DIM_AMBER)
    filled = int(round(ratio * width))
    filled = max(0, min(width, filled))
    bar = "█" * filled + "░" * (width - filled)
    style = GREEN if ratio >= 0.995 else AMBER if ratio >= 0.98 else RED
    return Text(bar, style=style)


def _range_bar(*, current: float | None, lo: float | None, hi: float | None, width: int = 12, style: str) -> Text:
    if current is None or lo is None or hi is None:
        return Text(" " * width, style=DIM_AMBER)
    if hi <= lo:
        filled = width // 2
    else:
        filled = int(round((current - lo) / (hi - lo) * width))
    filled = max(0, min(width, filled))
    bar = "█" * filled + "░" * (width - filled)
    return Text(bar, style=style)


@dataclass(frozen=True)
class ServiceView:
    order: int
    service_id: str
    name: str
    type: str
    cfg: dict[str, Any]
    latest: PollRow | None
    history: list[PollRow]

METRIC_TYPES = {
    "coingecko_price",
    "fx_rate",
    "stooq_quote",
    "doomsday_clock",
    "metaculus_date",
    "manifold_year_market",
}
DATE_CLOCK_TYPES = {"metaculus_date", "manifold_year_market"}
MARKET_METRIC_TYPES = {"coingecko_price", "fx_rate", "stooq_quote"}

COL_ITEM = 20
COL_NOW = 12
COL_24H = 8
COL_GAUGE = 12
COL_TREND = 20
COL_SEP = "│"


@dataclass(frozen=True)
class DisplayRow:
    kind: str  # "group" | "service"
    label: str
    view: ServiceView | None = None


def _group_for(view: ServiceView) -> str:
    raw = view.cfg.get("group")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()

    sid = view.service_id
    if view.type in DATE_CLOCK_TYPES or sid in {"doomsday"}:
        return "Clocks"
    if sid in {"openai", "openai_codex", "gemini", "anthropic", "claude_web", "claude_api", "claude_code"}:
        return "AI / LLMs"
    if sid in {"aws", "gae", "vercel"}:
        return "Cloud / Hosting"
    if sid in {"shopify", "helpscout", "slack"}:
        return "Ops / SaaS"
    if sid in {"cloudflare", "github", "netlify"}:
        return "Internet Core"
    if sid in {"bitcoin_network", "btc_usd"}:
        return "Markets / Crypto"
    if sid in {"cad_usd", "eur_usd", "usd_jpy"}:
        return "Markets / FX"
    if sid in {"spx", "ndx"}:
        return "Markets / Indices"
    if sid in {"gold", "silver", "copper", "wti", "ng"}:
        return "Markets / Commodities"
    if sid in {"tsla", "googl", "aapl", "msft", "nvda", "amzn", "meta"}:
        return "Markets / Equities"
    if view.type in MARKET_METRIC_TYPES:
        return "Markets"
    return "Other"


def _group_order(group: str) -> int:
    if group == "AI / LLMs":
        return 10
    if group == "Cloud / Hosting":
        return 20
    if group == "Ops / SaaS":
        return 30
    if group == "Internet Core":
        return 40
    if group.startswith("Markets / "):
        sub = group.split("/", 1)[1].strip()
        sub_order = {
            "Crypto": 0,
            "FX": 1,
            "Indices": 2,
            "Commodities": 3,
            "Equities": 4,
        }.get(sub, 9)
        return 50 + sub_order
    if group == "Markets":
        return 59
    if group == "Clocks":
        return 90
    return 999


def _episodes(rows: list[PollRow]) -> int:
    episodes = 0
    prev_ok = True
    for r in rows:
        ok = r.status == Status.OPERATIONAL.key
        if not ok and prev_ok:
            episodes += 1
        prev_ok = ok
    return episodes


def _fit(s: str, width: int, *, align: str = "left") -> str:
    s = _truncate(s, width)
    if align == "right":
        return s.rjust(width)
    return s.ljust(width)


def _fit_text(text: Text, width: int, *, align: str = "left") -> Text:
    t = text.copy()
    t.truncate(width, overflow="ellipsis")
    pad = width - len(t.plain)
    if pad <= 0:
        return t
    if align == "right":
        return Text(" " * pad) + t
    t.append(" " * pad)
    return t


def _matrix_noise(width: int, *, seed: int) -> Text:
    rng = random.Random(seed)
    chars = "0123456789abcdef"
    t = Text()
    for _ in range(width):
        if rng.random() < 0.12:
            t.append(" ", style=DIM_MATRIX)
            continue
        ch = rng.choice(chars)
        style = MATRIX_GREEN if rng.random() < 0.12 else DIM_MATRIX
        t.append(ch, style=style)
    return t


def _build_display_rows(views: list[ServiceView]) -> list[DisplayRow]:
    sorted_views = sorted(
        views,
        key=lambda v: (
            _group_order(_group_for(v)),
            _group_for(v).lower(),
            v.order,
            v.name.lower(),
        ),
    )
    rows: list[DisplayRow] = []
    last_group: str | None = None
    for v in sorted_views:
        group = _group_for(v)
        if group != last_group:
            rows.append(DisplayRow(kind="group", label=group))
            last_group = group
        rows.append(DisplayRow(kind="service", label=group, view=v))
    return rows


def _render_rows(rows: list[DisplayRow]) -> Group:
    header = Text.assemble(
        (f"{_fit('Item', COL_ITEM)}", f"bold {AMBER}"),
        (COL_SEP, DIM_AMBER),
        (f"{_fit('Now', COL_NOW, align='right')}", f"bold {AMBER}"),
        (COL_SEP, DIM_AMBER),
        (f"{_fit('24h', COL_24H, align='right')}", f"bold {AMBER}"),
        (COL_SEP, DIM_AMBER),
        (f"{_fit('Gauge', COL_GAUGE)}", f"bold {AMBER}"),
        (COL_SEP, DIM_AMBER),
        (f"{_fit('Trend', COL_TREND)}", f"bold {AMBER}"),
    )
    divider = Text("─" * INNER_WIDTH, style=DIM_AMBER)

    out_lines: list[Text] = [header, divider]
    service_row_i = 0
    for row in rows:
        if row.kind == "group":
            title = f"╞══ {row.label} "
            fill = "═" * max(0, INNER_WIDTH - len(title))
            line = Text(_fit(title + fill, INNER_WIDTH), style=f"bold {MATRIX_GREEN}")
            line.stylize(f"on rgb(0,12,0)")
            out_lines.append(line)
            continue

        v = row.view
        if v is None:
            continue
        latest = v.latest
        is_metric = v.type in METRIC_TYPES

        if is_metric:
            first, last, pct = _metric_change(v.history)
            lo, hi = _metric_range(v.history)
            delta_txt = "—"
            is_date_clock = v.type in DATE_CLOCK_TYPES
            direction_val: float | None = None
            if is_date_clock and first is not None and last is not None:
                delta_days = (last - first) / 86400.0
                delta_txt = f"{delta_days:+.1f}d"
                direction_val = delta_days
                trend_style = GREEN if delta_days > 0 else RED if delta_days < 0 else AMBER
            elif pct is not None:
                delta_txt = f"{pct:+.1%}"
                direction_val = pct
                trend_style = GREEN if pct > 0 else RED if pct < 0 else AMBER
            elif first is not None and last is not None:
                delta_val = last - first
                delta_txt = f"{delta_val:+.2f}"
                direction_val = delta_val
                trend_style = GREEN if delta_val > 0 else RED if delta_val < 0 else AMBER
            else:
                trend_style = AMBER

            arrow = "·"
            if direction_val is not None:
                arrow = "▲" if direction_val > 0 else "▼" if direction_val < 0 else "•"
            delta_disp = delta_txt if delta_txt == "—" else f"{arrow}{delta_txt}"

            now_cell = Text("…", style=DIM_AMBER)
            gauge = Text(" " * COL_GAUGE, style=DIM_AMBER)
            trend = Text("·" * COL_TREND, style=DIM_AMBER)
            if last is not None:
                now_txt = _format_value(v, float(last))
                now_cell = Text(now_txt, style=trend_style)
                if not is_date_clock:
                    gauge = _range_bar(current=float(last), lo=lo, hi=hi, width=COL_GAUGE, style=trend_style)
                trend = _value_spark(_bucket_values(v.history, hours=24, buckets=COL_TREND), style=trend_style)

            item = _fit_text(Text(v.name, style=AMBER), COL_ITEM)
            now_cell = _fit_text(now_cell, COL_NOW, align="right")
            delta = _fit_text(Text(delta_disp, style=trend_style), COL_24H, align="right")
            gauge = _fit_text(gauge, COL_GAUGE)
            trend = _fit_text(trend, COL_TREND)

            sep = Text(COL_SEP, style=DIM_AMBER)
            line = item + sep + now_cell + sep + delta + sep + gauge + sep + trend
        else:
            status = latest.status if latest else Status.UNKNOWN.key
            chip = _status_chip(status)
            if latest and latest.latency_ms is not None:
                chip.append(f" {latest.latency_ms}ms", style=DIM_AMBER)
            uptime = _uptime(v.history)
            pct = int(round(uptime * 100)) if uptime is not None else None
            eps = _episodes(v.history)
            eps_txt = str(eps) if eps <= 9 else "9+"
            uptime_txt = f"{pct:>3d}%E{eps_txt}" if pct is not None else "—"

            gauge = _uptime_bar(uptime, width=COL_GAUGE)
            trend = _trend_spark(_bucket_trend(v.history, hours=24, buckets=COL_TREND))

            item = _fit_text(Text(v.name, style=AMBER), COL_ITEM)
            now_cell = _fit_text(chip, COL_NOW, align="right")
            delta = _fit_text(Text(uptime_txt, style=_status_style(status)), COL_24H, align="right")
            gauge = _fit_text(gauge, COL_GAUGE)
            trend = _fit_text(trend, COL_TREND)

            sep = Text(COL_SEP, style=DIM_AMBER)
            line = item + sep + now_cell + sep + delta + sep + gauge + sep + trend

        row_bg = "on rgb(18,10,0)" if (service_row_i % 2 == 1) else ""
        if row_bg:
            line.stylize(row_bg)
        out_lines.append(line)
        service_row_i += 1

    return Group(*out_lines)


def _render_screen(
    *,
    rows: list[DisplayRow],
    all_views: list[ServiceView],
    pinned: list[ServiceView],
    last_poll_ts: int | None,
    page_index: int,
    page_count: int,
    page_mode: str,
) -> Panel:
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    last_poll = (
        datetime.fromtimestamp(last_poll_ts, tz=timezone.utc).astimezone().strftime("%H:%M:%S")
        if last_poll_ts
        else "—"
    )
    mode = "A" if page_mode == "(auto)" else "M" if page_mode == "(manual)" else ""
    pager = "" if page_count <= 1 else f" p{page_index + 1}/{page_count}{mode}"
    header1 = Text(_fit(f"ServiceDash  now {now_local}  poll {last_poll}{pager}", INNER_WIDTH), style=f"bold {AMBER}")

    svc_ok = svc_dg = svc_dn = svc_unk = 0
    for v in all_views:
        if bool(v.cfg.get("pin")):
            continue
        if v.type in METRIC_TYPES:
            continue
        s = v.latest.status if v.latest else Status.UNKNOWN.key
        if s == Status.OPERATIONAL.key:
            svc_ok += 1
        elif s == Status.DEGRADED.key:
            svc_dg += 1
        elif s == Status.OUTAGE.key:
            svc_dn += 1
        else:
            svc_unk += 1

    m_up = m_dn = m_flat = m_unk = 0
    for v in all_views:
        if bool(v.cfg.get("pin")):
            continue
        if v.type not in MARKET_METRIC_TYPES:
            continue
        first, last, pct = _metric_change(v.history)
        if last is None:
            m_unk += 1
            continue
        delta = pct if pct is not None else ((last - first) if (first is not None and last is not None) else None)
        if delta is None:
            m_unk += 1
        elif delta > 0:
            m_up += 1
        elif delta < 0:
            m_dn += 1
        else:
            m_flat += 1

    header2 = Text()
    header2.append("SVC ", style=DIM_AMBER)
    header2.append(f"OK{svc_ok}", style=GREEN)
    header2.append(" ", style=DIM_AMBER)
    header2.append(f"DG{svc_dg}", style=AMBER)
    header2.append(" ", style=DIM_AMBER)
    header2.append(f"DN{svc_dn}", style=RED)
    header2.append(" ", style=DIM_AMBER)
    header2.append(f"?{svc_unk}", style=DIM_AMBER)
    header2.append("  ", style=DIM_AMBER)
    header2.append("MKT ", style=DIM_AMBER)
    header2.append(f"▲{m_up}", style=GREEN)
    header2.append(" ", style=DIM_AMBER)
    header2.append(f"▼{m_dn}", style=RED)
    header2.append(" ", style=DIM_AMBER)
    header2.append(f"={m_flat}", style=AMBER)
    if m_unk:
        header2.append(" ", style=DIM_AMBER)
        header2.append(f"?{m_unk}", style=DIM_AMBER)
    header2.append("  ", style=DIM_AMBER)
    header2.append("r refresh  n/p page  q quit", style=DIM_AMBER)

    noise_len = max(0, INNER_WIDTH - len(header2.plain))
    if noise_len:
        header2 = header2 + _matrix_noise(noise_len, seed=utc_now_ts() + page_index * 101)
    header2 = _fit_text(header2, INNER_WIDTH)

    border_style = MATRIX_GREEN
    if svc_dn:
        border_style = RED
    elif svc_dg:
        border_style = AMBER
    elif svc_unk:
        border_style = DIM_AMBER

    pinned_lines: list[Text] = []
    if pinned:
        pinned_sorted = sorted(
            pinned,
            key=lambda v: (int(v.cfg.get("pin_order") or 0), v.name.lower()),
        )
        now_ts = utc_now_ts()
        for v in pinned_sorted[:4]:
            if not v.latest or v.latest.value_num is None:
                pinned_lines.append(Text(_fit(f"{v.name}: …", INNER_WIDTH), style=AMBER))
                continue

            eta_ts = float(v.latest.value_num)
            eta_dt = datetime.fromtimestamp(eta_ts, tz=timezone.utc).astimezone()
            remaining_s = eta_ts - now_ts
            remaining_days = int(remaining_s // 86400)
            remaining_hours = int((remaining_s % 86400) // 3600) if remaining_s >= 0 else 0
            countdown = f"T-{remaining_days}d" if remaining_days >= 0 else f"T+{abs(remaining_days)}d"

            shift_days: float | None = None
            vals = [float(r.value_num) for r in v.history if r.value_num is not None]
            if len(vals) >= 2:
                shift_days = (vals[-1] - vals[0]) / 86400.0

            shift_txt = ""
            shift_style = AMBER
            if shift_days is not None:
                shift_style = GREEN if shift_days > 0 else RED if shift_days < 0 else AMBER
                shift_txt = f"  ΔETA {shift_days:+.1f}d/24h"

            line = f"{v.name}: {countdown}{remaining_hours:02d}h  ETA {eta_dt.date().isoformat()}{shift_txt}"
            pinned_lines.append(Text(_fit(line, INNER_WIDTH), style=shift_style))

    table = _render_rows(rows)

    incidents_lines: list[Text] = []
    worst_first = sorted(
        [v for v in all_views if v.latest],
        key=lambda v: (v.latest.severity, v.latest.ts),  # type: ignore[union-attr]
        reverse=True,
    )
    for v in worst_first:
        if not v.latest:
            continue
        if v.latest.status != Status.OPERATIONAL.key:
            incidents_lines.append(
                Text(
                    _fit(f"- {v.name}: {v.latest.message}", INNER_WIDTH),
                    style=_status_style(v.latest.status),
                )
            )
    if not incidents_lines:
        incidents_lines.append(Text(_fit("- All tracked services look operational (per current sources).", INNER_WIDTH), style=GREEN))

    dooms_view = next((v for v in all_views if v.type == "doomsday_clock"), None)
    dooms_line: Text | None = None
    if dooms_view and dooms_view.latest and dooms_view.latest.value_num is not None:
        msg = dooms_view.latest.message or ""
        delta_m = re.search(r"Δ\\s*([+-]?\\d+)s\\b", msg)
        delta = int(delta_m.group(1)) if delta_m else 0
        style = AMBER if delta == 0 else (GREEN if delta > 0 else RED)
        dooms_line = Text(_fit(f"Doomsday Clock: {msg}", INNER_WIDTH), style=style)

    footer_parts: list[Text] = [Text("Incidents / Notes (non-OK):", style=f"bold {AMBER}"), *incidents_lines[:3]]
    if dooms_line is not None:
        footer_parts.append(dooms_line)
    footer = Group(*footer_parts)

    content = Group(header1, header2, *pinned_lines, table, footer)
    return Panel(content, border_style=border_style, box=box.DOUBLE, padding=(0, 1))


def _terminal_ok() -> tuple[bool, str]:
    size = shutil.get_terminal_size(fallback=(80, 25))
    if size.columns < 80 or size.lines < 25:
        return False, f"Terminal is {size.columns}x{size.lines}; need at least 80x25 (recommended ~80x80)."
    return True, f"Terminal {size.columns}x{size.lines} (recommended ~80x80)"

def _page_size(services_count: int, *, pinned_count: int) -> int:
    size = shutil.get_terminal_size(fallback=(80, 25))
    # Rough budget for 80x25:
    # - Outer panel border: 2
    # - Header lines: 2
    # - Footer title + up to 3 lines + doomsday: 5
    # - Pinned lines: pinned_count
    # - Table header + rule: 2
    overhead = 2 + 2 + pinned_count + 5 + 2
    return max(1, min(services_count, max(1, size.lines - overhead)))


async def run_dashboard(*, config_path: Path, screen: bool, once: bool) -> None:
    cfg = load_config(config_path)
    conn = connect(cfg.database_path)
    init_db(conn)

    services = build_services(cfg)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        headers={"User-Agent": "servicedash/0.1"},
        follow_redirects=True,
    ) as client:
        console = Console(
            width=80,
            color_system="truecolor",
            force_terminal=True,
            style=f"{AMBER} on black",
        )

        ok, term_msg = _terminal_ok()
        if not ok:
            console.print(Panel(Text(term_msg, style=AMBER), border_style=AMBER, box=box.DOUBLE))
            console.print("Resize your terminal, then re-run `python3 -m servicedash`.")
            return

        async def do_poll() -> int:
            outcomes = await poll_once(client, services)
            record_outcomes(conn, outcomes)
            prune_history(conn, cfg.retention_hours)
            return utc_now_ts()

        last_poll_ts: int | None = None
        try:
            last_poll_ts = await do_poll()
        except Exception:
            last_poll_ts = None

        def build_views() -> list[ServiceView]:
            since_ts = utc_now_ts() - int(timedelta(hours=cfg.history_hours).total_seconds())
            views: list[ServiceView] = []
            for idx, svc in enumerate(services):
                latest = latest_for_service(conn, svc.id)
                history = series_for_service(conn, svc.id, since_ts=since_ts)
                views.append(
                    ServiceView(
                        order=idx,
                        service_id=svc.id,
                        name=svc.name,
                        type=svc.type,
                        cfg=svc.cfg,
                        latest=latest,
                        history=history,
                    )
                )
            return views

        async def poll_loop() -> None:
            nonlocal last_poll_ts
            while True:
                await asyncio.sleep(cfg.poll_interval_seconds)
                try:
                    last_poll_ts = await do_poll()
                except Exception:
                    pass

        poll_task = asyncio.create_task(poll_loop())
        try:
            loop = asyncio.get_running_loop()
            key_queue: asyncio.Queue[str] = asyncio.Queue()
            manual_page: int | None = None
            current_page: int = 0

            fd: int | None = None
            old_termios: list[int] | None = None

            def _enable_keys() -> None:
                nonlocal fd, old_termios
                if not sys.stdin.isatty():
                    return
                try:
                    import termios
                    import tty

                    fd = sys.stdin.fileno()
                    old_termios = termios.tcgetattr(fd)
                    tty.setcbreak(fd)

                    def _on_stdin() -> None:
                        try:
                            ch = sys.stdin.read(1)
                        except Exception:
                            return
                        if ch:
                            key_queue.put_nowait(ch)

                    loop.add_reader(fd, _on_stdin)
                except Exception:
                    fd = None
                    old_termios = None

            def _disable_keys() -> None:
                nonlocal fd, old_termios
                if fd is None:
                    return
                try:
                    import termios

                    loop.remove_reader(fd)
                    if old_termios is not None:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_termios)
                finally:
                    fd = None
                    old_termios = None

            _enable_keys()
            with Live(
                console=console,
                screen=screen,
                auto_refresh=False,
                refresh_per_second=4,
                transient=False,
            ) as live:
                while True:
                    # Handle keypresses without blocking rendering.
                    while True:
                        try:
                            ch = key_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        ch = ch.lower()
                        if ch in {"q", "\u0003"}:
                            return
                        if ch == "r":
                            try:
                                last_poll_ts = await do_poll()
                            except Exception:
                                pass
                        if ch in {"n", "p"}:
                            if manual_page is None:
                                manual_page = current_page
                            manual_page += 1 if ch == "n" else -1

                    all_views = build_views()
                    pinned = [v for v in all_views if bool(v.cfg.get("pin"))]
                    table_views = [v for v in all_views if not bool(v.cfg.get("pin"))]
                    display_rows = _build_display_rows(table_views)

                    page_size = _page_size(len(display_rows), pinned_count=len(pinned))
                    page_count = max(1, (len(display_rows) + page_size - 1) // page_size)
                    page_index = 0
                    page_mode = ""
                    if page_count > 1:
                        auto_index = int(loop.time() // 10) % page_count
                        if manual_page is None:
                            page_index = auto_index
                            page_mode = "(auto)"
                        else:
                            page_index = manual_page % page_count
                            page_mode = "(manual)"
                    current_page = page_index
                    start = page_index * page_size
                    page_rows = display_rows[start : start + page_size]

                    frame = Align.center(
                        _render_screen(
                            rows=page_rows,
                            all_views=all_views,
                            pinned=pinned,
                            last_poll_ts=last_poll_ts,
                            page_index=page_index,
                            page_count=page_count,
                            page_mode=page_mode,
                        ),
                        vertical="top",
                    )
                    live.update(frame, refresh=True)
                    if once:
                        break
                    await asyncio.sleep(1)
        finally:
            with contextlib.suppress(Exception):
                _disable_keys()
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
            conn.close()
