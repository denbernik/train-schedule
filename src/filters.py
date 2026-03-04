"""Filtering helpers for route-aware departure display."""

from __future__ import annotations

from datetime import datetime

from src.models import Departure

# Trains within RUSH_FACTOR × walking_time are included in the display list
# so the user can see them alongside the 🏃 banner and decide to run for it.
RUSH_FACTOR = 0.8


def filter_and_cap_departures(
    departures: list[Departure],
    walking_time_minutes: int,
    max_rows: int,
) -> list[Departure]:
    """
    Keep catchable (or rush-catchable) departures and return up to `max_rows`.

    A departure is shown when minutes_until is known and at least
    RUSH_FACTOR × walking_time_minutes (80 %).  Trains between 80–100 % of
    walk time are "rush" trains; the action-status banner signals them with 🏃.
    """
    filtered = []
    for dep in departures:
        if dep.is_cancelled:
            continue
        minutes_until = _minutes_until(dep)
        if minutes_until is not None and minutes_until >= walking_time_minutes * RUSH_FACTOR:
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
