from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Status(Enum):
    OPERATIONAL = ("operational", 0)
    DEGRADED = ("degraded", 1)
    OUTAGE = ("outage", 2)
    UNKNOWN = ("unknown", 3)

    def __init__(self, key: str, severity: int) -> None:
        self.key = key
        self.severity = severity


def worst_status(statuses: list[Status]) -> Status:
    if not statuses:
        return Status.UNKNOWN
    return max(statuses, key=lambda s: s.severity)


def status_from_statuspage_indicator(indicator: str | None) -> Status:
    ind = (indicator or "").strip().lower()
    if ind == "none":
        return Status.OPERATIONAL
    if ind in {"minor"}:
        return Status.DEGRADED
    if ind in {"major", "critical"}:
        return Status.OUTAGE
    return Status.UNKNOWN


def status_from_statuspage_component(component_status: str | None) -> Status:
    s = (component_status or "").strip().lower()
    if s == "operational":
        return Status.OPERATIONAL
    if s in {"degraded_performance", "partial_outage", "under_maintenance"}:
        return Status.DEGRADED
    if s in {"major_outage"}:
        return Status.OUTAGE
    return Status.UNKNOWN


def status_from_slack_status(status: str | None, active_incidents_count: int) -> Status:
    s = (status or "").strip().lower()
    if s == "ok" and active_incidents_count == 0:
        return Status.OPERATIONAL
    if s in {"incident", "degraded", "partial_outage", "issue"}:
        return Status.DEGRADED
    if s in {"outage", "down", "major_outage"}:
        return Status.OUTAGE
    if active_incidents_count > 0:
        return Status.DEGRADED
    return Status.UNKNOWN


def status_from_gcp_incident(status_impact: str | None, severity: str | None, has_end: bool) -> Status:
    if has_end:
        return Status.OPERATIONAL
    impact = (status_impact or "").strip().upper()
    sev = (severity or "").strip().lower()
    if "OUTAGE" in impact:
        return Status.OUTAGE
    if "DISRUPTION" in impact:
        return Status.DEGRADED
    if sev in {"high", "critical"}:
        return Status.OUTAGE
    if sev in {"low", "medium"}:
        return Status.DEGRADED
    return Status.UNKNOWN


@dataclass(frozen=True)
class NormalizedStatus:
    status: Status
    message: str
    latency_ms: int | None = None
    value_num: float | None = None
