"""Pure app-flow helpers used by Streamlit orchestration in app.py."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence

from src.filters import filter_and_cap_departures
from src.models import Departure, DepartureStatus, StationBoard
from src.routes import Route
from src.station_registry import StationInfo, networks_compatible
from src.status import ActionStatus, compute_action_status


def _validated_station_id(
    station_id: object,
    fallback_station_id: str,
    option_idx: Mapping[str, int],
) -> str:
    if not isinstance(station_id, str):
        return fallback_station_id
    return station_id if station_id in option_idx else fallback_station_id


def _validated_walk_minutes(
    raw_value: object,
    fallback: int,
    walk_min_minutes: int,
    walk_max_minutes: int,
) -> int:
    try:
        value = int(raw_value) if raw_value is not None else fallback
    except (TypeError, ValueError):
        return fallback
    if walk_min_minutes <= value <= walk_max_minutes:
        return value
    return fallback


def seed_route_state(
    session_state: MutableMapping[str, object],
    query_params: Mapping[str, object],
    routes: Sequence[Route],
    option_idx: Mapping[str, int],
    walk_min_minutes: int,
    walk_max_minutes: int,
) -> None:
    """Seed route-related state from query params using safe defaults."""
    for route_idx, route in enumerate(routes):
        default_leg = route.legs[0]

        walk_key = f"walk_{route.name}"
        if walk_key not in session_state:
            session_state[walk_key] = _validated_walk_minutes(
                query_params.get(f"walk_{route_idx}"),
                route.walking_time_minutes,
                walk_min_minutes,
                walk_max_minutes,
            )

        dep_key = f"dep_{route_idx}"
        if dep_key not in session_state:
            session_state[dep_key] = _validated_station_id(
                query_params.get(dep_key, default_leg.origin_station_id),
                default_leg.origin_station_id,
                option_idx,
            )

        arr_key = f"arr_{route_idx}"
        if arr_key not in session_state:
            session_state[arr_key] = _validated_station_id(
                query_params.get(arr_key, default_leg.destination_station_id),
                default_leg.destination_station_id,
                option_idx,
            )


def persist_route_state(
    query_params: MutableMapping[str, str],
    session_state: Mapping[str, object],
    routes: Sequence[Route],
    selections: Sequence[tuple[StationInfo, StationInfo]],
) -> None:
    """Persist route-related controls to query params."""
    for route_idx, route in enumerate(routes):
        query_params[f"walk_{route_idx}"] = str(session_state[f"walk_{route.name}"])
        query_params[f"dep_{route_idx}"] = selections[route_idx][0].id
        query_params[f"arr_{route_idx}"] = selections[route_idx][1].id


def station_pair_validation_error(dep_info: StationInfo, arr_info: StationInfo) -> str | None:
    """Return a user-facing validation error for an invalid station pair."""
    if not networks_compatible(dep_info, arr_info):
        dep_net = "TfL" if dep_info.network == "tfl" else "National Rail"
        arr_net = "TfL" if arr_info.network == "tfl" else "National Rail"
        return (
            "**Network mismatch — cannot show departures**\n\n"
            f"**{dep_info.name}** is on **{dep_net}** "
            f"but **{arr_info.name}** is on **{arr_net}**.\n\n"
            "Select two stations on the same network."
        )

    if dep_info.network == "tfl" and dep_info.mode != arr_info.mode:
        dep_service = dep_info.display_label.split("(")[-1].rstrip(")")
        arr_service = arr_info.display_label.split("(")[-1].rstrip(")")
        return (
            "**Service type mismatch — cannot show departures**\n\n"
            f"**{dep_info.name}** is **{dep_service}** "
            f"but **{arr_info.name}** is **{arr_service}**.\n\n"
            "Select two stations on the same service."
        )

    return None


def status_for_board(board: StationBoard, walking_time_minutes: int) -> ActionStatus | None:
    """Compute action status when board contains valid departures."""
    if board.has_error or board.no_direct_route:
        return None
    return compute_action_status(board.departures, walking_time_minutes)


def prepare_visible_departure_rows(
    board: StationBoard,
    api_source: str,
    walking_time_minutes: int,
    max_rows: int,
) -> list[tuple[Departure, str | None]]:
    """Return filtered departures with presentation-specific platform overrides."""
    visible_departures = filter_and_cap_departures(
        departures=board.departures,
        walking_time_minutes=walking_time_minutes,
        max_rows=max_rows,
    )
    is_tfl = api_source == "tfl"
    rows: list[tuple[Departure, str | None]] = []
    for dep in visible_departures:
        plat_override: str | None = None
        if dep.display_platform is None and not is_tfl and dep.status != DepartureStatus.CANCELLED:
            plat_override = "plat. TBD"
        rows.append((dep, plat_override))
    return rows
