"""
Core data models for the departure board.

These models define the common data structures that all API clients produce
and all display components consume. This is the contract that keeps the
system loosely coupled — clients can change their parsing logic, and display
can change its rendering, without either affecting the other.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class DepartureStatus(Enum):
    """
    Normalised departure status across all data sources.

    Why an enum rather than strings: TfL might say a train is "on time",
    RTT might use "REAL_TIME" or flag it differently. Each client maps
    source-specific statuses to these canonical values, so display logic
    only needs to handle this fixed set.
    """

    ON_TIME = "On Time"
    DELAYED = "Delayed"
    CANCELLED = "Cancelled"
    NO_REPORT = "No Report"      # When real-time data isn't available


class StationType(Enum):
    """
    Distinguishes station types for display and client routing.

    This matters because National Rail and TfL stations behave differently:
    different APIs, different data freshness, different status conventions.
    The display layer uses this to apply appropriate styling.
    """

    NATIONAL_RAIL  = "National Rail"
    TFL_TUBE       = "TfL Underground"
    TFL_BUS        = "TfL Bus"
    TFL_OVERGROUND = "TfL Overground"
    TFL_DLR        = "TfL DLR"
    TFL_ELIZABETH  = "TfL Elizabeth"


# Maps each StationType to the api_source string used by _fetch_leg().
# All TfL sub-modes share the same REST client; National Rail uses its own.
_API_SOURCE_MAP: dict[StationType, str] = {
    StationType.NATIONAL_RAIL:  "national_rail",
    StationType.TFL_TUBE:       "tfl",
    StationType.TFL_OVERGROUND: "tfl",
    StationType.TFL_DLR:        "tfl",
    StationType.TFL_ELIZABETH:  "tfl",
    StationType.TFL_BUS:        "tfl",
}


def api_source_for(station_type: StationType) -> str:
    """Return the api_source string for a given StationType."""
    return _API_SOURCE_MAP[station_type]


@dataclass
class Departure:
    """
    A single departure from a station.

    This is the core data structure of the entire app. Both TfL and RTT
    clients must produce instances of this class — that's the contract.

    Design notes:
    - scheduled_time and expected_time are datetime objects, not strings.
      Parsing happens once in the client; display can format however it wants.
    - platform is Optional because not all stations/services report platforms.
    - delay_minutes is computed from the two times rather than stored separately,
      but we store it explicitly because some APIs report delay directly and
      it's useful to have without recalculating.

    Good enough for now: All fields are simple types. At scale with a database,
    you'd add an ID field, a created_at timestamp, and possibly a source field
    for debugging which API provided the data.
    """

    destination: str
    scheduled_time: datetime
    expected_time: datetime
    status: DepartureStatus
    platform: str | None = None
    operator: str | None = None        # e.g., "South Western Railway", "District"
    delay_minutes: int = 0
    arrival_time: datetime | None = None

    @property
    def is_delayed(self) -> bool:
        """Quick check used by display layer for colour coding."""
        return self.status == DepartureStatus.DELAYED

    @property
    def is_cancelled(self) -> bool:
        return self.status == DepartureStatus.CANCELLED

    @property
    def display_time(self) -> str:
        """
        Human-readable departure time, as you'd see on a real board.
        Shows expected time in HH:MM format.
        """
        return self.expected_time.strftime("%H:%M")

    @property
    def display_arrival_time(self) -> str | None:
        """HH:MM arrival at destination, or None when unavailable."""
        if self.arrival_time is None:
            return None
        return self.arrival_time.strftime("%H:%M")

    @property
    def display_duration(self) -> str | None:
        """Human-readable journey duration from departure to arrival."""
        if self.arrival_time is None:
            return None
        total_minutes = int((self.arrival_time - self.expected_time).total_seconds() / 60)
        if total_minutes < 0:
            return None
        hours, mins = divmod(total_minutes, 60)
        if hours >= 1:
            return f"{hours} h {mins} min" if mins else f"{hours} h"
        return f"{mins} min"

    @property
    def minutes_until(self) -> int | None:
        """
        Minutes until departure from right now.
        Returns None if the departure is in the past (already gone).
        """
        delta = self.expected_time - datetime.now()
        minutes = int(delta.total_seconds() / 60)
        return minutes if minutes >= 0 else None


@dataclass
class StationBoard:
    """
    All departures from a single station, bundled with metadata.

    Why a wrapper rather than just a list of Departures: the display layer
    needs the station name, type, and when data was last fetched — not just
    the departures themselves. This avoids passing loose variables around.
    """

    station_name: str
    station_type: StationType
    departures: list[Departure] = field(default_factory=list)
    last_updated: datetime = field(default_factory=datetime.now)
    error_message: str | None = None    # Set when API call fails
    no_direct_route: bool = False       # True when no through service exists between stations

    @property
    def has_error(self) -> bool:
        return self.error_message is not None

    @property
    def departure_count(self) -> int:
        return len(self.departures)