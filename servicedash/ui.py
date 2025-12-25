from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


@dataclass(frozen=True)
class ServiceView:
    service_id: str
    name: str
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
    table.add_column("Service", width=18, no_wrap=True)
    table.add_column("Now", width=10, no_wrap=True)
    table.add_column("24h", width=5, justify="right", no_wrap=True)
    table.add_column("Gauge", width=12, no_wrap=True)
    table.add_column("Trend", width=24, no_wrap=True)
    table.add_column("Note", ratio=1, no_wrap=True)

    for v in views:
        latest = v.latest
        now_chip = _status_chip(latest.status) if latest else Text("…", style=DIM_AMBER)
        uptime = _uptime(v.history)
        uptime_pct = f"{int(round(uptime * 100)):>3d}%" if uptime is not None else "  —"
        gauge = _uptime_bar(uptime, width=12)
        trend = _trend_spark(_bucket_trend(v.history, hours=24, buckets=24))
        if latest and latest.latency_ms is not None:
            note_raw = f"{latest.latency_ms}ms {latest.message}"
        else:
            note_raw = latest.message if latest else "No data yet"
        note = _truncate(note_raw, 26)
        table.add_row(Text(_truncate(v.name, 18), style=AMBER), now_chip, uptime_pct, gauge, trend, Text(note, style=DIM_AMBER))

    return table


def _render_screen(
    *,
    views: list[ServiceView],
    all_views: list[ServiceView],
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

    footer = Group(Text("Incidents / Notes (non-OK):", style=f"bold {AMBER}"), *incidents_lines[:3])

    content = Group(header, table, footer)
    return Panel(content, border_style=AMBER, box=box.DOUBLE, padding=(0, 1))


def _terminal_ok() -> tuple[bool, str]:
    size = shutil.get_terminal_size(fallback=(80, 25))
    if size.columns < 80 or size.lines < 25:
        return False, f"Terminal is {size.columns}x{size.lines}; recommended is at least 80x25."
    return True, f"Terminal {size.columns}x{size.lines}"

def _page_size(services_count: int) -> int:
    size = shutil.get_terminal_size(fallback=(80, 25))
    # Rough budget for 80x25:
    # - Outer panel border: 2
    # - Header line: 1
    # - Footer title + up to 3 lines: 4
    # - Table header + rule: 2
    overhead = 2 + 1 + 4 + 2
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
                views.append(ServiceView(service_id=svc.id, name=svc.name, latest=latest, history=history))
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
                    page_size = _page_size(len(all_views))
                    page_count = max(1, (len(all_views) + page_size - 1) // page_size)
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
                    views = all_views[start : start + page_size]

                    frame = Align.center(
                        _render_screen(
                            views=views,
                            all_views=all_views,
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
