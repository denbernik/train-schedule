"""
TfL Unified API client.

Fetches real-time arrival predictions from Transport for London's API.
Used for East Putney (District Line) but works for any TfL-served station.

API docs: https://api.tfl.gov.uk/
Endpoint: GET /StopPoint/{naptanId}/Arrivals

The TfL arrivals endpoint returns predictions — estimated times until a
vehicle arrives at the stop. Unlike a timetable, these update in real-time
based on actual vehicle positions. Key quirk: there's no "scheduled time"
in the response — only timeToStation (seconds until arrival) and
expectedArrival (predicted arrival datetime). For our Departure model,
we use expectedArrival as both scheduled_time and expected_time, since
TfL doesn't distinguish between the two for live predictions.
"""

import logging
from datetime import datetime

import requests

from src.config import get_settings
from src.clients.tfl_topology import TopologyUnavailableError, TubeTopologyProvider
from src.models import Departure, DepartureStatus, StationBoard, StationType

logger = logging.getLogger(__name__)

# TfL API base URL — no trailing slash
_BASE_URL = "https://api.tfl.gov.uk"

# Request timeout in seconds.
# TfL typically responds in <500ms but we allow more for slow connections.
# A departure board that hangs for 10+ seconds is worse than one showing
# stale data, so we keep this relatively tight.
_TIMEOUT_SECONDS = 10
_topology_provider: TubeTopologyProvider | None = None


def fetch_departures(
    station_id: str | None = None,
    max_results: int | None = None,
    destination_station_id: str | None = None,
) -> StationBoard:
    """
    Fetch live arrival predictions for a TfL station.

    Args:
        station_id: NaPTAN ID of the station (e.g., "940GZZLUEPY" for East Putney).
                    Defaults to the configured station if not provided.
        max_results: Maximum number of departures to return.
                     Defaults to the configured max_departures.
        destination_station_id: Optional NaPTAN ID to keep only services that
                                pass through this station.

    Returns:
        StationBoard with departures sorted by expected arrival time.
        If the API call fails, returns a StationBoard with an error message
        and empty departures list — the display layer handles this gracefully.

    Design decision: This is a module-level function, not a class method.
    For a single-endpoint client like this, a class would add ceremony without
    value. If we later needed to manage session state, connection pooling, or
    multiple endpoints, we'd refactor into a class. YAGNI for now.
    """
    settings = get_settings()
    station_id = station_id or settings.tfl_station_id
    max_results = max_results or settings.max_departures

    try:
        raw_arrivals = _call_api(station_id, settings.tfl_api_key)
        raw_arrivals, filter_error = _filter_arrivals_for_destination(
            raw_arrivals=raw_arrivals,
            origin_station_id=station_id,
            destination_station_id=destination_station_id,
            api_key=settings.tfl_api_key,
        )
        if filter_error:
            return _error_board(station_id, filter_error)
        departures = _parse_arrivals(raw_arrivals)

        # Sort by expected time and take only what we need
        departures.sort(key=lambda d: d.expected_time)
        departures = departures[:max_results]

        logger.info(
            "TfL: fetched %d departures for station %s",
            len(departures),
            station_id,
        )

        return StationBoard(
            station_name=_extract_station_name(raw_arrivals, station_id),
            station_type=StationType.TFL_TUBE,
            departures=departures,
        )

    except requests.Timeout:
        logger.error("TfL API timeout for station %s", station_id)
        return _error_board(station_id, "TfL API timed out — showing stale data")

    except requests.RequestException as e:
        logger.error("TfL API request failed for station %s: %s", station_id, e)
        return _error_board(station_id, "Unable to reach TfL — check connection")

    except (KeyError, ValueError, TypeError) as e:
        # Parsing errors — API returned unexpected data shape
        logger.error("TfL API response parsing failed: %s", e)
        return _error_board(station_id, "Unexpected data from TfL API")


def _call_api(station_id: str, api_key: str) -> list[dict]:
    """
    Make the HTTP request to TfL's arrivals endpoint.

    Separated from parsing so we can:
    1. Test parsing independently with fixture data
    2. Swap the HTTP layer later (e.g., to async httpx) without touching parsing
    3. Keep each function focused on one thing

    Returns the raw JSON response as a list of prediction dicts.
    """
    url = f"{_BASE_URL}/StopPoint/{station_id}/Arrivals"

    # TfL accepts the key as a query parameter.
    # If no key is provided, the API still works but with lower rate limits.
    params = {}
    if api_key:
        params["app_key"] = api_key

    response = requests.get(url, params=params, timeout=_TIMEOUT_SECONDS)
    response.raise_for_status()

    return response.json()


def _get_topology_provider(api_key: str) -> TubeTopologyProvider:
    """Lazily initialise a topology provider reused across refreshes."""
    global _topology_provider
    if _topology_provider is None or _topology_provider.api_key != api_key:
        _topology_provider = TubeTopologyProvider(api_key=api_key)
    return _topology_provider


def _filter_arrivals_for_destination(
    raw_arrivals: list[dict],
    origin_station_id: str,
    destination_station_id: str | None,
    api_key: str,
) -> tuple[list[dict], str | None]:
    """
    Keep arrivals where the train route passes through destination_station_id.

    Returns filtered arrivals and optional error message.
    """
    if not destination_station_id:
        return raw_arrivals, None
    if not raw_arrivals:
        return raw_arrivals, None

    line_ids = {
        arrival.get("lineId", "").strip().lower()
        for arrival in raw_arrivals
        if isinstance(arrival.get("lineId"), str) and arrival.get("modeName") == "tube"
    }
    if not line_ids:
        return raw_arrivals, None

    provider = _get_topology_provider(api_key)
    try:
        reachable = any(
            provider.has_path(line_id, origin_station_id, destination_station_id)
            for line_id in line_ids
        )
    except TopologyUnavailableError as e:
        logger.warning("Unable to evaluate TfL destination filter: %s", e)
        return raw_arrivals, None

    if not reachable:
        return [], (
            f"Configured destination {destination_station_id} is not reachable "
            f"from {origin_station_id} on this Tube leg"
        )

    filtered: list[dict] = []
    for arrival in raw_arrivals:
        line_id = arrival.get("lineId")
        terminal_station_id = arrival.get("destinationNaptanId")
        mode_name = arrival.get("modeName")
        if mode_name != "tube":
            continue
        if not isinstance(line_id, str) or not isinstance(terminal_station_id, str):
            # No destination station id in payload: cannot safely prove pass-through.
            continue
        try:
            if provider.service_passes_through(
                line_id=line_id,
                origin_station_id=origin_station_id,
                destination_station_id=destination_station_id,
                terminal_station_id=terminal_station_id,
            ):
                filtered.append(arrival)
        except TopologyUnavailableError as e:
            logger.warning("Unable to evaluate service pass-through: %s", e)
            return raw_arrivals, None

    return filtered, None


def _parse_arrivals(raw_arrivals: list[dict]) -> list[Departure]:
    """
    Transform raw TfL API predictions into our Departure model.

    TfL response fields we use:
    - destinationName: e.g., "Richmond"
    - expectedArrival: ISO datetime, e.g., "2024-01-15T08:23:00Z"
    - timeToStation: seconds until arrival (integer)
    - platformName: e.g., "Eastbound - Platform 1"
    - lineName: e.g., "District"

    Fields we ignore (for now):
    - vehicleId, naptanId, bearing, direction — useful for a map view later
    - modeName — we already know this is TfL
    - towards — similar to destinationName but less precise

    Why we parse each arrival individually with error handling: real API
    responses occasionally include malformed entries (missing fields, null
    values). Skipping one bad entry is much better than failing the entire
    board.
    """
    departures = []

    for arrival in raw_arrivals:
        try:
            departure = _parse_single_arrival(arrival)
            departures.append(departure)
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(
                "Skipping malformed TfL arrival entry: %s — Error: %s",
                arrival.get("id", "unknown"),
                e,
            )
            continue

    return departures


def _parse_single_arrival(arrival: dict) -> Departure:
    """
    Parse one TfL prediction dict into a Departure.

    The expectedArrival field is an ISO 8601 datetime string.
    TfL returns these in UTC (Z suffix). We parse and keep as-is.
    For display, we'll format to local time in the display layer.

    Note: TfL predictions don't have a concept of "delayed" vs "on time"
    in the same way National Rail does. The prediction is always the
    current best estimate. So all TfL departures are marked ON_TIME
    unless we add logic later to compare against timetabled times.
    """
    expected_arrival = datetime.fromisoformat(
        arrival["expectedArrival"].replace("Z", "+00:00")
    )

    return Departure(
        destination=arrival["destinationName"],
        scheduled_time=expected_arrival,  # TfL doesn't separate scheduled vs expected
        expected_time=expected_arrival,
        status=DepartureStatus.ON_TIME,
        platform=arrival.get("platformName"),
        operator=arrival.get("lineName"),  # e.g., "District"
        delay_minutes=0,  # TfL predictions are always "current best estimate"
    )


def _extract_station_name(raw_arrivals: list[dict], fallback: str) -> str:
    """
    Pull the station name from the API response.

    Each arrival prediction includes stationName, so we grab it from
    the first entry. Falls back to the station ID if no arrivals exist
    (e.g., last train has departed for the night).
    """
    if raw_arrivals:
        return raw_arrivals[0].get("stationName", fallback)
    return fallback


def _error_board(station_id: str, message: str) -> StationBoard:
    """
    Construct an empty StationBoard with an error message.

    Centralised here to keep error board creation consistent.
    The display layer checks board.has_error and shows the message
    instead of trying to render an empty departure list.
    """
    return StationBoard(
        station_name=station_id,
        station_type=StationType.TFL_TUBE,
        departures=[],
        error_message=message,
    )