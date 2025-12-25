from __future__ import annotations

import asyncio
import contextlib
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
    label = status.upper()[:7]
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
    service_id: str
    name: str
    type: str
    cfg: dict[str, Any]
    latest: PollRow | None
    history: list[PollRow]


def _render_table(views: list[ServiceView]) -> Table:
    table = Table(
        box=box.SIMPLE_HEAVY,
        expand=True,
        show_header=True,
        header_style=f"bold {AMBER}",
        border_style=DIM_AMBER,
        pad_edge=False,
        row_styles=["", "on rgb(18,10,0)"],
    )
    table.add_column("Item", width=18, no_wrap=True)
    table.add_column("Now", width=12, justify="right", no_wrap=True)
    table.add_column("24h", width=8, justify="right", no_wrap=True)
    table.add_column("Gauge", width=12, no_wrap=True)
    table.add_column("Trend", width=24, no_wrap=True)
    table.add_column("Note", ratio=1, no_wrap=True)

    for v in views:
        latest = v.latest
        is_metric = v.type in {"coingecko_price", "fx_rate", "stooq_quote", "doomsday_clock", "metaculus_date", "manifold_year_market"}

        if is_metric:
            first, last, pct = _metric_change(v.history)
            lo, hi = _metric_range(v.history)
            delta_txt = "  —"
            is_date_clock = v.type in {"metaculus_date", "manifold_year_market"}
            if is_date_clock and first is not None and last is not None:
                delta_days = (last - first) / 86400.0
                delta_txt = f"{delta_days:+6.1f}d"
            elif pct is not None:
                delta_txt = f"{pct:+6.2%}"
            elif first is not None and last is not None:
                delta_txt = f"{(last - first):+7.2f}"

            if is_date_clock and first is not None and last is not None:
                delta_days = (last - first) / 86400.0
                trend_style = GREEN if delta_days > 0 else RED if delta_days < 0 else AMBER
            else:
                trend_style = GREEN if pct is not None and pct > 0 else RED if pct is not None and pct < 0 else AMBER
            if last is None:
                now_cell = Text("…", style=DIM_AMBER)
                gauge = Text(" " * 12, style=DIM_AMBER)
                trend = Text("·" * 24, style=DIM_AMBER)
            else:
                now_txt = _format_value(v, float(last))
                now_cell = Text(now_txt, style=trend_style)
                if is_date_clock:
                    gauge = Text(" " * 12, style=DIM_AMBER)
                else:
                    gauge = _range_bar(current=float(last), lo=lo, hi=hi, width=12, style=trend_style)
                trend = _value_spark(_bucket_values(v.history, hours=24, buckets=24), style=trend_style)

            note_raw = latest.message if latest else "No data yet"
            if v.type not in {"doomsday_clock", "metaculus_date", "manifold_year_market"} and lo is not None and hi is not None:
                note_raw = f"{note_raw}  lo {lo:.2f} hi {hi:.2f}".strip()
            note = _truncate(note_raw, 38)
            table.add_row(Text(_truncate(v.name, 18), style=AMBER), now_cell, Text(delta_txt, style=trend_style), gauge, trend, Text(note, style=DIM_AMBER))
            continue

        now_chip = _status_chip(latest.status) if latest else Text("…", style=DIM_AMBER)
        uptime = _uptime(v.history)
        uptime_pct = f"{int(round(uptime * 100)):>3d}%" if uptime is not None else "  —"
        gauge = _uptime_bar(uptime, width=12)
        trend = _trend_spark(_bucket_trend(v.history, hours=24, buckets=24))
        if latest and latest.latency_ms is not None:
            note_raw = f"{latest.latency_ms}ms {latest.message}"
        else:
            note_raw = latest.message if latest else "No data yet"
        note = _truncate(note_raw, 38)
        table.add_row(Text(_truncate(v.name, 18), style=AMBER), now_chip, uptime_pct, gauge, trend, Text(note, style=DIM_AMBER))

    return table


def _render_screen(
    *,
    views: list[ServiceView],
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
    pager = "" if page_count <= 1 else f"  page {page_index + 1}/{page_count} {page_mode}"
    header = Text.assemble(
        ("ServiceDash", f"bold {AMBER}"),
        ("  ", DIM_AMBER),
        (f"now {now_local}", DIM_AMBER),
        ("  ", DIM_AMBER),
        (f"last poll {last_poll}", DIM_AMBER),
        (pager, DIM_AMBER),
        ("  ", DIM_AMBER),
        ("r refresh  n/p page  q quit", DIM_AMBER),
    )

    pinned_lines: list[Text] = []
    if pinned:
        pinned_sorted = sorted(
            pinned,
            key=lambda v: (int(v.cfg.get("pin_order") or 0), v.name.lower()),
        )
        now_ts = utc_now_ts()
        for v in pinned_sorted[:4]:
            if not v.latest or v.latest.value_num is None:
                pinned_lines.append(Text(f"{v.name}: …", style=AMBER))
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

            line = f"{v.name}: {countdown} {remaining_hours:02d}h  ETA {eta_dt.date().isoformat()}{shift_txt}"
            pinned_lines.append(Text(_truncate(line, 78), style=shift_style))

    table = _render_table(views)

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
            incidents_lines.append(Text(f"- {v.name}: {_truncate(v.latest.message, 62)}", style=_status_style(v.latest.status)))
    if not incidents_lines:
        incidents_lines.append(Text("- All tracked services look operational (per current sources).", style=GREEN))

    dooms_view = next((v for v in all_views if v.type == "doomsday_clock"), None)
    dooms_line: Text | None = None
    if dooms_view and dooms_view.latest and dooms_view.latest.value_num is not None:
        msg = dooms_view.latest.message or ""
        delta_m = re.search(r"Δ\\s*([+-]?\\d+)s\\b", msg)
        delta = int(delta_m.group(1)) if delta_m else 0
        style = AMBER if delta == 0 else (GREEN if delta > 0 else RED)
        dooms_text = f"Doomsday Clock: {_truncate(msg, 74)}"
        dooms_line = Text(dooms_text, style=style)

    footer_parts: list[Text] = [Text("Incidents / Notes (non-OK):", style=f"bold {AMBER}"), *incidents_lines[:3]]
    if dooms_line is not None:
        footer_parts.append(dooms_line)
    footer = Group(*footer_parts)

    content = Group(header, *pinned_lines, table, footer)
    return Panel(content, border_style=AMBER, box=box.DOUBLE, padding=(0, 1))


def _terminal_ok() -> tuple[bool, str]:
    size = shutil.get_terminal_size(fallback=(80, 25))
    if size.columns < 80 or size.lines < 25:
        return False, f"Terminal is {size.columns}x{size.lines}; need at least 80x25 (recommended ~80x80)."
    return True, f"Terminal {size.columns}x{size.lines} (recommended ~80x80)"

def _page_size(services_count: int, *, pinned_count: int) -> int:
    size = shutil.get_terminal_size(fallback=(80, 25))
    # Rough budget for 80x25:
    # - Outer panel border: 2
    # - Header line: 1
    # - Footer title + up to 3 lines + doomsday: 5
    # - Pinned lines: pinned_count
    # - Table header + rule: 2
    overhead = 2 + 1 + pinned_count + 5 + 2
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
            for svc in services:
                latest = latest_for_service(conn, svc.id)
                history = series_for_service(conn, svc.id, since_ts=since_ts)
                views.append(
                    ServiceView(
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

                    page_size = _page_size(len(table_views), pinned_count=len(pinned))
                    page_count = max(1, (len(table_views) + page_size - 1) // page_size)
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
                    views = table_views[start : start + page_size]

                    frame = Align.center(
                        _render_screen(
                            views=views,
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
