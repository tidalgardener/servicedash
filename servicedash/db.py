from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PollRow:
    ts: int
    service_id: str
    service_name: str
    status: str
    severity: int
    message: str
    latency_ms: int | None
    value_num: float | None


SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  service_id TEXT NOT NULL,
  service_name TEXT NOT NULL,
  status TEXT NOT NULL,
  severity INTEGER NOT NULL,
  message TEXT NOT NULL,
  latency_ms INTEGER,
  value_num REAL
);

CREATE INDEX IF NOT EXISTS idx_polls_service_ts ON polls(service_id, ts);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Backward compatible migrations for existing DBs.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(polls)")}
    if "value_num" not in cols:
        conn.execute("ALTER TABLE polls ADD COLUMN value_num REAL")
    conn.commit()


def insert_poll(conn: sqlite3.Connection, row: PollRow) -> None:
    conn.execute(
        """
        INSERT INTO polls(ts, service_id, service_name, status, severity, message, latency_ms, value_num)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.ts,
            row.service_id,
            row.service_name,
            row.status,
            row.severity,
            row.message,
            row.latency_ms,
            row.value_num,
        ),
    )
    conn.commit()


def prune_before(conn: sqlite3.Connection, cutoff_ts: int) -> int:
    cur = conn.execute("DELETE FROM polls WHERE ts < ?", (cutoff_ts,))
    conn.commit()
    return int(cur.rowcount or 0)


def latest_for_service(conn: sqlite3.Connection, service_id: str) -> PollRow | None:
    row = conn.execute(
        """
        SELECT ts, service_id, service_name, status, severity, message, latency_ms, value_num
        FROM polls
        WHERE service_id = ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (service_id,),
    ).fetchone()
    return PollRow(**dict(row)) if row else None


def series_for_service(conn: sqlite3.Connection, service_id: str, since_ts: int) -> list[PollRow]:
    rows = conn.execute(
        """
        SELECT ts, service_id, service_name, status, severity, message, latency_ms, value_num
        FROM polls
        WHERE service_id = ? AND ts >= ?
        ORDER BY ts ASC
        """,
        (service_id, since_ts),
    ).fetchall()
    return [PollRow(**dict(r)) for r in rows]
