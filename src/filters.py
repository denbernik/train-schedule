"""Filtering helpers for route-aware departure display."""

from __future__ import annotations

from src.models import Departure
from src.time_utils import minutes_until

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
        dep_minutes = minutes_until(dep.expected_time)
        if dep_minutes is not None and dep_minutes >= walking_time_minutes * RUSH_FACTOR:
            filtered.append(dep)
    filtered.sort(key=lambda dep: dep.expected_time)
    return filtered[:max_rows]
