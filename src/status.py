"""
Action-status banner logic for the departure board.

Each route column shows exactly one of six mutually-exclusive statuses:

  🏃‍♂️  Rush to the train   — next reachable train is in the 80–100 % walk-time window
  🚶‍♂️  Leave now           — next reachable train is in the 100–120 % (or +2 min) window
  🫷   Leave in X min      — comfortable gap; X = minutes until you need to leave
  ⚠️   Avoid if possible   — 3–5 of the next 10 upcoming trains are cancelled
  ⛔️   Avoid completely    — 6+ of next 10 cancelled, or no reachable trains at all
  🔌   No data             — API error (constructed directly in app.py)

Priority (highest → lowest): ⛔️ > 🏃‍♂️ > 🚶‍♂️ > ⚠️ > 🫷
"""

from __future__ import annotations

from dataclasses import dataclass

from src.filters import RUSH_FACTOR
from src.models import Departure
from src.time_utils import minutes_until


@dataclass
class ActionStatus:
    emoji: str
    label: str
    # Maps to the Streamlit alert function to call: "error" | "warning" | "success" | "info"
    display: str


def compute_action_status(
    raw_departures: list[Departure],
    walking_time_minutes: int,
) -> ActionStatus:
    """
    Derive the top-level action status for a single route column.

    Args:
        raw_departures:       board.departures — unfiltered, may include cancelled trains.
        walking_time_minutes: user-configured minutes to walk to the station.
    """
    walk = walking_time_minutes
    rush_floor = walk * RUSH_FACTOR  # 80 % of walk time

    # ── Step 1: cancellation rate among the next 10 upcoming raw departures ──
    upcoming = sorted(
        [d for d in raw_departures if minutes_until(d.expected_time) is not None],
        key=lambda d: d.expected_time,
    )[:10]
    cancelled_count = sum(1 for d in upcoming if d.is_cancelled)

    # ── Step 2: first non-cancelled departure reachable with a rush ──
    next_mins: int | None = None
    for dep in sorted(raw_departures, key=lambda d: d.expected_time):
        if dep.is_cancelled:
            continue
        m = minutes_until(dep.expected_time)
        if m is not None and m >= rush_floor:
            next_mins = m
            break

    # ── Step 3: classify (strict priority, top-down) ──

    # ⛔️ No service or extreme disruption
    if next_mins is None or cancelled_count >= 6:
        return ActionStatus("⛔️", "Avoid completely — no service", "error")

    # 🏃‍♂️ Rush window: 80–100 % of walk time
    if next_mins <= walk:
        return ActionStatus("🏃‍♂️", "Rush to the train", "error")

    # 🚶‍♂️ Leave-now window: 100–120 % of walk time (or walk + 2 min, whichever is higher)
    leave_now_ceiling = max(walk * 1.2, walk + 2)
    if next_mins <= leave_now_ceiling:
        return ActionStatus("🚶‍♂️", "Leave now", "warning")

    # ⚠️ Comfortable timing but route is disrupted
    if cancelled_count >= 3:
        return ActionStatus("⚠️", "Avoid if possible — cancellations probable", "warning")

    # 🫷 Calm state — show countdown to when the user must leave
    minutes_to_leave = max(0, int(next_mins - walk))
    return ActionStatus("🫷", f"Leave in {minutes_to_leave} min", "success")
