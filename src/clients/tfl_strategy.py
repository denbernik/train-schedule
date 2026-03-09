"""Strategy helpers for TfL timetable direction selection."""

from __future__ import annotations


def directions_for_timetable_queries(live_raw_arrivals: list[dict]) -> list[str]:
    """
    Return direction query order for timetable calls.

    Live-observed directions are attempted first for efficiency, then the
    missing direction is appended so destination-aware timetable lookups
    do not miss service in one-sided live-data windows.
    """
    ordered: list[str] = []
    for item in live_raw_arrivals:
        direction = item.get("direction")
        if not isinstance(direction, str):
            continue
        normalized = direction.strip().lower()
        if normalized in ("inbound", "outbound") and normalized not in ordered:
            ordered.append(normalized)

    for fallback in ("outbound", "inbound"):
        if fallback not in ordered:
            ordered.append(fallback)

    return ordered
