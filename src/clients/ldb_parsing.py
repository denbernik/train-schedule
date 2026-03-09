"""Parsing helpers for LDB payload mapping."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from src.models import DepartureStatus


def detect_service_rows(payload: dict) -> tuple[str, list[dict]]:
    """Find where service rows live in the response body."""
    candidate_paths: list[tuple[str, ...]] = [
        ("trainServices",),
        ("GetDepartureBoardResult", "trainServices"),
        ("getDepartureBoardResult", "trainServices"),
        ("result", "trainServices"),
        ("departures", "all"),
    ]

    for path in candidate_paths:
        node = nested_get(payload, path)
        if isinstance(node, list):
            rows = [item for item in node if isinstance(item, dict)]
            return (".".join(path), rows)

    return ("<not-found>", [])


def nested_get(data: dict, path: Sequence[str]) -> object:
    current: object = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def is_time_value(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split(":")
    if len(parts) != 2:
        return False
    return parts[0].isdigit() and parts[1].isdigit()


def parse_time_value(value: str, reference_now: datetime) -> datetime:
    hours, minutes = map(int, value.split(":"))
    parsed = reference_now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if (reference_now - parsed).total_seconds() > 6 * 3600:
        parsed += timedelta(days=1)
    return parsed


def map_status(service: dict, expected_raw: str, delay_minutes: int) -> DepartureStatus:
    if service.get("isCancelled") is True:
        return DepartureStatus.CANCELLED

    text = str(expected_raw).strip().lower()
    if "cancel" in text:
        return DepartureStatus.CANCELLED
    if text in {"on time", "starts here", "early"}:
        return DepartureStatus.ON_TIME
    if text in {"no report"}:
        return DepartureStatus.NO_REPORT
    if "delayed" in text or "late" in text:
        return DepartureStatus.DELAYED
    if is_time_value(expected_raw):
        return DepartureStatus.DELAYED if delay_minutes > 0 else DepartureStatus.ON_TIME
    return DepartureStatus.NO_REPORT
