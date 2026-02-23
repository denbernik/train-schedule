"""Filtering helpers for route-aware departure display."""

from __future__ import annotations

from datetime import datetime

from src.models import Departure


def filter_and_cap_departures(
    departures: list[Departure],
    walking_time_minutes: int,
    max_rows: int,
) -> list[Departure]:
    """
    Keep catchable departures and return up to `max_rows`.

    A departure is catchable when minutes_until is known and at least the
    route walking time.
    """
    filtered = []
    for dep in departures:
        minutes_until = _minutes_until(dep)
        if minutes_until is not None and minutes_until >= walking_time_minutes:
            filtered.append(dep)
    filtered.sort(key=lambda dep: dep.expected_time)
    return filtered[:max_rows]


def _minutes_until(dep: Departure) -> int | None:
    """
    Compute minutes until departure with timezone-safe `now`.
    """
    expected = dep.expected_time
    now = (
        datetime.now(expected.tzinfo)
        if expected.tzinfo is not None
        else datetime.now()
    )
    delta = expected - now
    minutes = int(delta.total_seconds() / 60)
    return minutes if minutes >= 0 else None
