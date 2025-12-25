from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ServiceConfig:
    id: str
    name: str
    type: str
    cfg: dict[str, Any]


@dataclass(frozen=True)
class AppConfig:
    poll_interval_seconds: int
    history_hours: int
    retention_hours: int
    database_path: Path
    services: list[ServiceConfig]


def load_config(path: Path) -> AppConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))

    poll_interval_seconds = int(raw.get("poll_interval_seconds", 300))
    history_hours = int(raw.get("history_hours", 24))
    retention_hours = int(raw.get("retention_hours", max(24, history_hours)))

    db_path = Path(raw.get("database_path", "data/servicedash.sqlite3"))
    if not db_path.is_absolute():
        db_path = (path.parent / db_path).resolve()

    services_raw = raw.get("services", [])
    if not isinstance(services_raw, list) or not services_raw:
        raise ValueError("Config must include a non-empty 'services' list.")

    services: list[ServiceConfig] = []
    for i, svc in enumerate(services_raw):
        if not isinstance(svc, dict):
            raise ValueError(f"Service config at index {i} must be an object.")
        sid = str(svc.get("id", "")).strip()
        name = str(svc.get("name", "")).strip()
        stype = str(svc.get("type", "")).strip()
        if not sid or not name or not stype:
            raise ValueError(f"Service config at index {i} must include 'id', 'name', and 'type'.")
        cfg = {k: v for k, v in svc.items() if k not in {"id", "name", "type"}}
        services.append(ServiceConfig(id=sid, name=name, type=stype, cfg=cfg))

    return AppConfig(
        poll_interval_seconds=poll_interval_seconds,
        history_hours=history_hours,
        retention_hours=retention_hours,
        database_path=db_path,
        services=services,
    )

