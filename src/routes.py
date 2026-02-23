"""
Route configuration models and JSON loader.

These dataclasses define user-configured commute routes independently from
API client implementations. A route is a sequence of one or two legs where
each leg declares:
- the origin station to query
- the destination to filter against in departure results
- the transport mode and client source to use

Design choices:
- Dataclasses match the rest of the project (simple, explicit, lightweight).
- Route legs store both destination ID and destination name. The name is used
  for current departure filtering/display; the ID preserves structured data for
  future features such as onward-leg planning and arrivals queries.
- `transport_mode` is a `StationType` enum, but the model remains mode-agnostic
  because any `StationType` value can be configured (including bus later).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.models import StationType

_DEFAULT_ROUTES_PATH = Path(__file__).resolve().parent.parent / "routes.json"


@dataclass
class RouteLeg:
    """
    A single transport segment within a commute route.

    `origin_station_id` and `destination_station_id` are backend identifiers:
    NaPTAN for TfL and CRS for National Rail. `destination_name` is kept
    separately because APIs expose human-readable destination text that we
    currently filter with substring matching.
    """

    origin_station_id: str
    origin_name: str
    destination_station_id: str
    destination_name: str
    transport_mode: StationType
    api_source: str


@dataclass
class Route:
    """
    A complete commute path with one or two legs.

    The model intentionally does not assume one transport mode. A route can
    combine rail, tube, bus, or any future mode represented by `StationType`.
    """

    name: str
    legs: list[RouteLeg]
    walking_time_minutes: int


def load_routes(path: str | Path | None = None) -> list[Route]:
    """
    Load route definitions from JSON and return typed `Route` objects.

    Args:
        path: Optional override path for tests or alternate configs.
              Defaults to `<project-root>/routes.json`.

    Returns:
        List of parsed `Route` objects.

    Raises:
        FileNotFoundError: If the JSON file does not exist.
        ValueError: If JSON is invalid or required fields are malformed.
        TypeError: If field types are not as expected.
    """

    routes_path = Path(path) if path is not None else _DEFAULT_ROUTES_PATH

    with routes_path.open("r", encoding="utf-8") as file:
        raw_data = json.load(file)

    if not isinstance(raw_data, list):
        raise TypeError("routes.json root must be a list of route objects")

    routes: list[Route] = []
    for index, route_data in enumerate(raw_data):
        if not isinstance(route_data, dict):
            raise TypeError(f"Route at index {index} must be an object")

        name = _require_str(route_data, "name", index)
        walking_time = _require_int(route_data, "walking_time_minutes", index)

        legs_data = route_data.get("legs")
        if not isinstance(legs_data, list):
            raise TypeError(f"Route '{name}' field 'legs' must be a list")
        if len(legs_data) not in (1, 2):
            raise ValueError(f"Route '{name}' must contain 1 or 2 legs")

        legs: list[RouteLeg] = []
        for leg_index, leg_data in enumerate(legs_data):
            if not isinstance(leg_data, dict):
                raise TypeError(
                    f"Route '{name}' leg at index {leg_index} must be an object"
                )
            legs.append(_parse_leg(route_name=name, leg_index=leg_index, leg_data=leg_data))

        routes.append(Route(name=name, legs=legs, walking_time_minutes=walking_time))

    return routes


def _parse_leg(route_name: str, leg_index: int, leg_data: dict) -> RouteLeg:
    """Parse a single raw leg object into a typed `RouteLeg`."""

    origin_station_id = _require_str(leg_data, "origin_station_id", route_name)
    origin_name = _require_str(leg_data, "origin_name", route_name)
    destination_station_id = _require_str(leg_data, "destination_station_id", route_name)
    destination_name = _require_str(leg_data, "destination_name", route_name)
    api_source = _require_str(leg_data, "api_source", route_name)
    transport_mode_raw = _require_str(leg_data, "transport_mode", route_name)
    transport_mode = _parse_station_type(transport_mode_raw, route_name, leg_index)

    return RouteLeg(
        origin_station_id=origin_station_id,
        origin_name=origin_name,
        destination_station_id=destination_station_id,
        destination_name=destination_name,
        transport_mode=transport_mode,
        api_source=api_source,
    )


def _parse_station_type(raw_value: str, route_name: str, leg_index: int) -> StationType:
    """
    Parse station type from JSON.

    Accepts either enum names (e.g., "NATIONAL_RAIL") or enum values
    (e.g., "National Rail") so config remains readable and resilient.
    """

    if raw_value in StationType.__members__:
        return StationType[raw_value]

    for station_type in StationType:
        if station_type.value == raw_value:
            return station_type

    valid_options = ", ".join([*StationType.__members__.keys()])
    raise ValueError(
        f"Route '{route_name}' leg {leg_index} has invalid transport_mode "
        f"'{raw_value}'. Use one of: {valid_options}"
    )


def _require_str(data: dict, key: str, context: str | int) -> str:
    """Read required string field with basic type checking."""

    value = data.get(key)
    if not isinstance(value, str):
        raise TypeError(f"{context}: field '{key}' must be a string")
    if not value.strip():
        raise ValueError(f"{context}: field '{key}' cannot be empty")
    return value


def _require_int(data: dict, key: str, context: str | int) -> int:
    """Read required integer field with basic type checking."""

    value = data.get(key)
    if not isinstance(value, int):
        raise TypeError(f"{context}: field '{key}' must be an integer")
    return value
