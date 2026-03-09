"""Merge helpers for combining TfL live and timetable departures."""

from __future__ import annotations

from src.models import Departure, DepartureStatus


def merge_departures_live_first(
    live_departures: list[Departure],
    timetable_departures: list[Departure],
    max_results: int,
    tolerance_seconds: int,
) -> list[Departure]:
    """Merge departures in live-first mode, filling gaps from timetable rows."""
    live_sorted = sorted(live_departures, key=lambda d: d.expected_time)
    if len(live_sorted) >= max_results:
        return live_sorted[:max_results]

    merged = list(live_sorted)
    seen = {_departure_dedupe_key(dep) for dep in merged}

    for dep in sorted(timetable_departures, key=lambda d: d.expected_time):
        if len(merged) >= max_results:
            break
        live_match_index = _find_live_boundary_match_index(
            merged_departures=merged,
            timetable_dep=dep,
            tolerance_seconds=tolerance_seconds,
        )
        if live_match_index is not None:
            live_dep = merged[live_match_index]
            # Guardrail for occasional API skew: if TT says earlier than live,
            # keep live and drop TT.
            if dep.expected_time < live_dep.expected_time:
                continue

            old_key = _departure_dedupe_key(live_dep)
            seen.discard(old_key)
            merged[live_match_index] = dep
            seen.add(_departure_dedupe_key(dep))
            continue

        key = _departure_dedupe_key(dep)
        if key in seen:
            continue
        seen.add(key)
        merged.append(dep)

    merged.sort(key=lambda d: d.expected_time)
    return merged[:max_results]


def _departure_dedupe_key(dep: Departure) -> tuple[str, str, str]:
    return (
        dep.destination.strip().lower(),
        (dep.operator or "").strip().lower(),
        dep.expected_time.strftime("%Y-%m-%d %H:%M"),
    )


def _find_live_boundary_match_index(
    merged_departures: list[Departure],
    timetable_dep: Departure,
    tolerance_seconds: int,
) -> int | None:
    target_destination = timetable_dep.destination.strip().lower()
    target_operator = (timetable_dep.operator or "").strip().lower()

    for index, existing in enumerate(merged_departures):
        if existing.status == DepartureStatus.NO_REPORT:
            continue
        if existing.destination.strip().lower() != target_destination:
            continue
        if (existing.operator or "").strip().lower() != target_operator:
            continue
        delta_seconds = abs((timetable_dep.expected_time - existing.expected_time).total_seconds())
        if delta_seconds <= tolerance_seconds:
            return index

    return None
