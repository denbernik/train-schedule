"""
Station registry for the London station picker.

Loads the static london_stations.json file and exposes typed helpers
for the Streamlit UI (selectbox options) and for RouteLeg construction.

Loaded once at module level via @lru_cache — matches the pattern used by
get_settings() in config.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.models import StationType

_DATA_PATH = Path(__file__).resolve().parent / "data" / "london_stations.json"

_MODE_TO_STATION_TYPE: dict[str, StationType] = {
    "tube":           StationType.TFL_TUBE,
    "overground":     StationType.TFL_OVERGROUND,
    "dlr":            StationType.TFL_DLR,
    "elizabeth-line": StationType.TFL_ELIZABETH,
    "national_rail":  StationType.NATIONAL_RAIL,
}

_MODE_BADGE: dict[str, str] = {
    "tube":           "Tube",
    "overground":     "Overground",
    "dlr":            "DLR",
    "elizabeth-line": "Elizabeth",
    "national_rail":  "National Rail",
}

_TFL_TYPES: frozenset[StationType] = frozenset({
    StationType.TFL_TUBE,
    StationType.TFL_OVERGROUND,
    StationType.TFL_DLR,
    StationType.TFL_ELIZABETH,
    StationType.TFL_BUS,
})


@dataclass(frozen=True)
class StationInfo:
    """Immutable station descriptor loaded from london_stations.json."""

    id: str             # e.g. "940GZZLUEPY" or "WNT"
    name: str           # e.g. "East Putney"
    mode: str           # raw mode key from JSON
    network: str        # "tfl" or "national_rail"
    station_type: StationType
    display_label: str  # e.g. "East Putney (Tube)"


@lru_cache(maxsize=1)
def load_stations() -> list[StationInfo]:
    """
    Load and return the full sorted station list from the static JSON file.

    Returns:
        List of StationInfo sorted alphabetically by display_label.

    Raises:
        FileNotFoundError: if london_stations.json does not exist.
    """
    with _DATA_PATH.open("r", encoding="utf-8") as fh:
        raw: list[dict] = json.load(fh)

    stations: list[StationInfo] = []
    for entry in raw:
        mode = entry.get("mode", "")
        station_type = _MODE_TO_STATION_TYPE.get(mode)
        if station_type is None:
            continue
        badge = _MODE_BADGE.get(mode, mode.title())
        stations.append(StationInfo(
            id=entry["id"],
            name=entry["name"],
            mode=mode,
            network=entry["network"],
            station_type=station_type,
            display_label=f"{entry['name']} ({badge})",
        ))

    stations.sort(key=lambda s: s.display_label.lower())
    return stations


def find_by_id(station_id: str) -> StationInfo | None:
    """Return a StationInfo for the given station ID, or None if not found."""
    for station in load_stations():
        if station.id == station_id:
            return station
    return None


def networks_compatible(a: StationInfo, b: StationInfo) -> bool:
    """
    Return True when two stations can form a valid single-leg route.

    TfL-to-TfL and National Rail-to-National Rail are compatible.
    Cross-network combinations are not.
    """
    return (a.station_type in _TFL_TYPES) == (b.station_type in _TFL_TYPES)


def selectbox_options() -> list[tuple[str, StationInfo]]:
    """
    Return (display_label, StationInfo) pairs sorted A-Z for st.selectbox.

    Usage:
        options = selectbox_options()
        sel = st.selectbox("Station", options, format_func=lambda o: o[0])
        info: StationInfo = sel[1]
    """
    return [(s.display_label, s) for s in load_stations()]
