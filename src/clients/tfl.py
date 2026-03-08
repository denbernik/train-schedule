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
from datetime import datetime, timedelta

import requests

from src.config import get_settings
from src.clients.tfl_topology import TopologyUnavailableError, TubeTopologyProvider
from src.models import Departure, DepartureStatus, StationBoard, StationType

logger = logging.getLogger(__name__)

# TfL API base URL — no trailing slash
_BASE_URL = "https://api.tfl.gov.uk"

_topology_provider: TubeTopologyProvider | None = None
_TIMETABLE_HORIZON_HOURS = 12
_LIVE_TT_TOLERANCE_SECONDS = 60

# Line-level compass mapping: line_id → {direction → compass}
# Fixed geographic facts — TfL's inbound/outbound is consistent per line.
# Dynamic extraction from live arrivals takes priority (see _compass_cache).
_LINE_COMPASS: dict[str, dict[str, str]] = {
    "bakerloo":         {"inbound": "Southbound", "outbound": "Northbound"},
    "central":          {"inbound": "Westbound",  "outbound": "Eastbound"},
    "circle":           {"inbound": "Westbound",  "outbound": "Eastbound"},
    "district":         {"inbound": "Eastbound",  "outbound": "Westbound"},
    "hammersmith-city":  {"inbound": "Westbound",  "outbound": "Eastbound"},
    "jubilee":          {"inbound": "Westbound",  "outbound": "Eastbound"},
    "metropolitan":     {"inbound": "Southbound", "outbound": "Northbound"},
    "northern":         {"inbound": "Southbound", "outbound": "Northbound"},
    "piccadilly":       {"inbound": "Eastbound",  "outbound": "Westbound"},
    "victoria":         {"inbound": "Southbound", "outbound": "Northbound"},
    "waterloo-city":    {"inbound": "Westbound",  "outbound": "Eastbound"},
    "elizabeth":        {"inbound": "Westbound",  "outbound": "Eastbound"},
}

# Populated at runtime from live arrivals' platformName field.
# Overrides _LINE_COMPASS when available (self-correcting).
_compass_cache: dict[str, dict[str, str]] = {}

# Persist station names across refreshes so the board keeps showing a human-readable
# name even when the live arrivals list is temporarily empty (last train gone, etc.).
_station_name_cache: dict[str, str] = {}


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
    max_results = max_results or getattr(settings, "tfl_max_departures", settings.max_departures)
    timeout_seconds = settings.tfl_timeout_seconds

    try:
        all_live_raw_arrivals = _call_api(station_id, settings.tfl_api_key, timeout_seconds)
        _update_compass_cache(all_live_raw_arrivals)
        live_raw_arrivals, filter_error = _filter_arrivals_for_destination(
            raw_arrivals=all_live_raw_arrivals,
            origin_station_id=station_id,
            destination_station_id=destination_station_id,
            api_key=settings.tfl_api_key,
        )
        if filter_error == _NO_DIRECT_ROUTE:
            return StationBoard(
                station_name=_extract_station_name(
                    all_live_raw_arrivals, station_id, settings.tfl_api_key, timeout_seconds,
                ),
                station_type=StationType.TFL_TUBE,
                no_direct_route=True,
            )
        if filter_error:
            return _error_board(station_id, filter_error)

        # Build destination arrival lookup for arrival_time matching
        destination_arrival_map: dict[tuple[str, str], datetime] = {}
        timetable_journey_minutes: int | None = None
        if destination_station_id:
            destination_arrival_map = _build_destination_arrival_map(
                destination_station_id=destination_station_id,
                api_key=settings.tfl_api_key,
                timeout_seconds=timeout_seconds,
            )
            timetable_journey_minutes = _fetch_timetable_journey_minutes(
                origin_station_id=station_id,
                destination_station_id=destination_station_id,
                live_raw_arrivals=all_live_raw_arrivals,
                api_key=settings.tfl_api_key,
                timeout_seconds=timeout_seconds,
            )

        live_departures = _parse_arrivals(
            live_raw_arrivals, destination_arrival_map, timetable_journey_minutes,
        )
        timetable_departures: list[Departure] = []

        if destination_station_id and len(live_departures) < max_results:
            timetable_raw_arrivals = _fetch_timetable_candidates(
                origin_station_id=station_id,
                live_raw_arrivals=all_live_raw_arrivals,
                api_key=settings.tfl_api_key,
                timeout_seconds=timeout_seconds,
            )

            if timetable_raw_arrivals:
                timetable_raw_arrivals, timetable_filter_error = _filter_arrivals_for_destination(
                    raw_arrivals=timetable_raw_arrivals,
                    origin_station_id=station_id,
                    destination_station_id=destination_station_id,
                    api_key=settings.tfl_api_key,
                )
                if timetable_filter_error == _NO_DIRECT_ROUTE:
                    return StationBoard(
                        station_name=_extract_station_name(
                            all_live_raw_arrivals, station_id, settings.tfl_api_key, timeout_seconds,
                        ),
                        station_type=StationType.TFL_TUBE,
                        no_direct_route=True,
                    )
                if timetable_filter_error:
                    return _error_board(station_id, timetable_filter_error)
                timetable_departures = _parse_timetable_arrivals(
                    timetable_raw_arrivals, timetable_journey_minutes,
                )

        departures = _merge_departures_live_first(
            live_departures=live_departures,
            timetable_departures=timetable_departures,
            max_results=max_results,
        )

        logger.info(
            "TfL: fetched %d departures for station %s (live=%d timetable=%d)",
            len(departures),
            station_id,
            len(live_departures),
            len(timetable_departures),
        )

        return StationBoard(
            station_name=_extract_station_name(
                all_live_raw_arrivals, station_id, settings.tfl_api_key, timeout_seconds,
            ),
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


def _call_api(station_id: str, api_key: str, timeout_seconds: int) -> list[dict]:
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

    response = requests.get(url, params=params, timeout=timeout_seconds)
    response.raise_for_status()

    return response.json()


def _build_destination_arrival_map(
    destination_station_id: str,
    api_key: str,
    timeout_seconds: int,
) -> dict[tuple[str, str], datetime]:
    """
    Fetch arrivals at the destination station and build a vehicleId lookup.

    Returns a mapping of (vehicleId, lineId) -> expectedArrival datetime.
    Both keys are lowercased for consistent matching.

    If the API call fails, returns an empty dict — departures will simply
    show no arrival time (same as current behavior).
    """
    try:
        raw_arrivals = _call_api(destination_station_id, api_key, timeout_seconds)
    except (requests.Timeout, requests.RequestException) as e:
        logger.warning(
            "TfL destination arrivals fetch failed for %s: %s",
            destination_station_id,
            e,
        )
        return {}

    lookup: dict[tuple[str, str], datetime] = {}
    for arrival in raw_arrivals:
        vehicle_id = arrival.get("vehicleId")
        line_id = arrival.get("lineId")
        expected_arrival_raw = arrival.get("expectedArrival")

        if not isinstance(vehicle_id, str) or not vehicle_id.strip():
            continue
        if not isinstance(line_id, str) or not line_id.strip():
            continue
        if not isinstance(expected_arrival_raw, str):
            continue

        try:
            expected_arrival = datetime.fromisoformat(
                expected_arrival_raw.replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            continue

        key = (vehicle_id.strip().lower(), line_id.strip().lower())
        # If multiple predictions exist for same vehicle, keep the earliest.
        if key not in lookup or expected_arrival < lookup[key]:
            lookup[key] = expected_arrival

    logger.debug(
        "TfL destination arrival map for %s: %d entries",
        destination_station_id,
        len(lookup),
    )
    return lookup


def _fetch_timetable_journey_minutes(
    origin_station_id: str,
    destination_station_id: str,
    live_raw_arrivals: list[dict],
    api_key: str,
    timeout_seconds: int,
) -> int | None:
    """
    Look up the scheduled journey time from origin to destination via stationIntervals.

    The TfL timetable endpoint returns stationIntervals with timeToArrival
    (minutes from origin) for each stop along the route. We find the destination
    stop and return its timeToArrival value.

    Returns minutes as an int, or None if unavailable.
    Falls back to StopPoint API when live arrivals provide no line IDs.
    """
    line_ids = _resolve_tube_line_ids(
        live_raw_arrivals, origin_station_id, api_key, timeout_seconds,
    )
    if not line_ids:
        return None

    directions = _directions_for_timetable_queries(live_raw_arrivals)

    dest_lower = destination_station_id.strip().lower()

    for line_id in line_ids:
        for direction in directions:
            try:
                payload = _call_timetable_api(
                    line_id=line_id,
                    stop_id=origin_station_id,
                    direction=direction,
                    api_key=api_key,
                    timeout_seconds=timeout_seconds,
                )
            except (requests.RequestException, ValueError, TypeError) as e:
                logger.warning(
                    "TfL timetable journey time lookup failed for %s/%s: %s",
                    line_id,
                    direction,
                    e,
                )
                continue

            timetable = payload.get("timetable", {})
            for route in timetable.get("routes", []):
                if not isinstance(route, dict):
                    continue
                for si in route.get("stationIntervals", []):
                    if not isinstance(si, dict):
                        continue
                    for interval in si.get("intervals", []):
                        if not isinstance(interval, dict):
                            continue
                        stop_id = interval.get("stopId", "")
                        if isinstance(stop_id, str) and stop_id.strip().lower() == dest_lower:
                            time_val = interval.get("timeToArrival")
                            if isinstance(time_val, (int, float)) and time_val > 0:
                                minutes = int(time_val)
                                logger.debug(
                                    "TfL timetable journey time %s -> %s: %d min",
                                    origin_station_id,
                                    destination_station_id,
                                    minutes,
                                )
                                return minutes

    logger.warning(
        "Could not determine TfL timetable journey time from %s to %s",
        origin_station_id,
        destination_station_id,
    )
    return None


def _call_timetable_api(
    line_id: str,
    stop_id: str,
    direction: str,
    api_key: str,
    timeout_seconds: int,
) -> dict:
    """
    Request scheduled timetable data for a line/stop/direction.

    Endpoint:
      GET /Line/{lineId}/Timetable/{stopId}?direction={inbound|outbound}
    """
    url = f"{_BASE_URL}/Line/{line_id}/Timetable/{stop_id}"
    params = {"direction": direction}
    if api_key:
        params["app_key"] = api_key

    response = requests.get(url, params=params, timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()


def _fetch_timetable_candidates(
    origin_station_id: str,
    live_raw_arrivals: list[dict],
    api_key: str,
    timeout_seconds: int,
) -> list[dict]:
    """
    Build arrival-like timetable candidates from known journeys.

    We query relevant line IDs discovered in live arrivals, for directions seen
    in the payload. If direction data is missing, query both directions.
    Falls back to StopPoint API when live arrivals provide no line IDs
    (e.g. last train has departed for the night).
    """
    line_ids = _resolve_tube_line_ids(
        live_raw_arrivals, origin_station_id, api_key, timeout_seconds,
    )
    if not line_ids:
        return []

    directions = _directions_for_timetable_queries(live_raw_arrivals)

    candidates: list[dict] = []
    for line_id in line_ids:
        for direction in directions:
            try:
                payload = _call_timetable_api(
                    line_id=line_id,
                    stop_id=origin_station_id,
                    direction=direction,
                    api_key=api_key,
                    timeout_seconds=timeout_seconds,
                )
                candidates.extend(
                    _parse_timetable_response_to_arrivals(
                        payload=payload,
                        line_id=line_id,
                        direction=direction,
                        origin_station_id=origin_station_id,
                    )
                )
            except requests.RequestException as e:
                logger.warning(
                    "TfL timetable request failed for %s/%s at %s: %s",
                    line_id,
                    direction,
                    origin_station_id,
                    e,
                )
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(
                    "TfL timetable parse failed for %s/%s at %s: %s",
                    line_id,
                    direction,
                    origin_station_id,
                    e,
                )

    return candidates


def _directions_for_timetable_queries(live_raw_arrivals: list[dict]) -> list[str]:
    """
    Return direction query order for timetable calls.

    Why query both directions:
    - Late at night or during disruption, live arrivals at the origin station
      may exist in only one direction.
    - The user's destination can still be served in the opposite direction.

    We keep live-observed direction(s) first for efficiency, then append the
    missing direction so destination-aware timetable lookups do not miss service.
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


def _update_compass_cache(live_raw_arrivals: list[dict]) -> None:
    """
    Extract (line_id, direction) → compass from live platformName strings.

    Live arrivals include both ``direction`` ("inbound"/"outbound") and
    ``platformName`` ("Southbound - Platform 4").  Parsing the compass word
    from platformName gives us a verified mapping that we cache at module
    level for use in timetable entries.
    """
    for arrival in live_raw_arrivals:
        line_id = (arrival.get("lineId") or "").strip().lower()
        direction = (arrival.get("direction") or "").strip().lower()
        platform_name = arrival.get("platformName") or ""
        if (
            line_id
            and direction in ("inbound", "outbound")
            and " - Platform " in platform_name
        ):
            compass = platform_name.split(" - Platform ", 1)[0].strip()
            if compass.lower() in ("northbound", "southbound", "eastbound", "westbound"):
                _compass_cache.setdefault(line_id, {})[direction] = compass


def _timetable_platform_name(line_id: str, direction: str) -> str:
    """
    Return a compass-labelled platformName for a timetable entry.

    Resolution order:
    1. _compass_cache — populated at runtime from live arrivals (self-correcting)
    2. _LINE_COMPASS — hardcoded geographic fallback
    3. Raw direction.title() — last resort ("Inbound" / "Outbound")
    """
    compass = _compass_cache.get(line_id, {}).get(direction)
    if not compass:
        compass = _LINE_COMPASS.get(line_id, {}).get(direction)
    if compass:
        return f"{compass} (Timetable)"
    return f"{direction.title()} (Timetable)"


def _parse_timetable_response_to_arrivals(
    payload: dict,
    line_id: str,
    direction: str,
    origin_station_id: str,
) -> list[dict]:
    """
    Convert timetable knownJourneys into arrival-like dictionaries.

    These rows are later filtered with the same pass-through logic used for
    live arrivals.
    """
    timetable = payload.get("timetable", {})
    routes = timetable.get("routes", [])
    if not isinstance(routes, list):
        return []

    stop_name_map = _extract_stop_name_map(payload)
    line_name = payload.get("lineName", line_id.title())
    origin_name = stop_name_map.get(origin_station_id, origin_station_id)
    now = datetime.now().astimezone()
    horizon = now + timedelta(hours=_TIMETABLE_HORIZON_HOURS)

    arrivals: list[dict] = []
    for route in routes:
        if not isinstance(route, dict):
            continue
        terminal_by_interval = _build_interval_terminal_map(route)
        schedules = route.get("schedules", [])
        if not isinstance(schedules, list):
            continue

        for schedule in schedules:
            if not isinstance(schedule, dict):
                continue
            known_journeys = schedule.get("knownJourneys", [])
            if not isinstance(known_journeys, list):
                continue

            for journey in known_journeys:
                if not isinstance(journey, dict):
                    continue

                terminal_station_id = terminal_by_interval.get(str(journey.get("intervalId")))
                if not terminal_station_id:
                    continue

                departure_dt = _next_departure_datetime(
                    hour=journey.get("hour"),
                    minute=journey.get("minute"),
                    now=now,
                )
                if departure_dt is None:
                    continue
                if departure_dt > horizon:
                    continue

                destination_name = stop_name_map.get(terminal_station_id, terminal_station_id)
                row_id = (
                    f"tt-{line_id}-{direction}-{journey.get('intervalId')}-"
                    f"{departure_dt.strftime('%Y%m%d%H%M')}-{terminal_station_id}"
                )
                arrivals.append(
                    {
                        "id": row_id,
                        "stationName": origin_name,
                        "lineId": line_id,
                        "lineName": line_name,
                        "modeName": "tube",
                        "destinationNaptanId": terminal_station_id,
                        "destinationName": destination_name,
                        "expectedArrival": departure_dt.isoformat(),
                        "platformName": _timetable_platform_name(line_id, direction),
                        "direction": direction,
                    }
                )

    return arrivals


def _build_interval_terminal_map(route: dict) -> dict[str, str]:
    """
    Map interval IDs to terminal stop IDs using max timeToArrival.
    """
    station_intervals = route.get("stationIntervals", [])
    if not isinstance(station_intervals, list):
        return {}

    mapping: dict[str, str] = {}
    for station_interval in station_intervals:
        if not isinstance(station_interval, dict):
            continue
        interval_id_raw = station_interval.get("id")
        if interval_id_raw is None:
            continue
        interval_id = str(interval_id_raw)
        intervals = station_interval.get("intervals", [])
        if not isinstance(intervals, list) or not intervals:
            continue

        best_stop_id: str | None = None
        best_time = -1
        for interval in intervals:
            if not isinstance(interval, dict):
                continue
            stop_id = interval.get("stopId")
            time_to_arrival = interval.get("timeToArrival")
            if not isinstance(stop_id, str):
                continue
            if not isinstance(time_to_arrival, (int, float)):
                continue
            time_value = float(time_to_arrival)
            if time_value > best_time:
                best_time = time_value
                best_stop_id = stop_id

        if best_stop_id:
            mapping[interval_id] = best_stop_id

    return mapping


def _extract_stop_name_map(payload: dict) -> dict[str, str]:
    """Extract id->name mapping from timetable payload station/stop sections."""
    name_map: dict[str, str] = {}

    for key in ("stops", "stations"):
        items = payload.get(key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            station_id = item.get("id") or item.get("stationId")
            name = item.get("name")
            if isinstance(station_id, str) and isinstance(name, str):
                name_map[station_id] = name

    return name_map


def _next_departure_datetime(
    hour: int | str | None,
    minute: int | str | None,
    now: datetime,
) -> datetime | None:
    """Convert timetable hour/minute to next datetime from now (today/tomorrow)."""
    try:
        hour_int = int(hour)
        minute_int = int(minute)
    except (TypeError, ValueError):
        return None

    if minute_int < 0 or minute_int >= 60 or hour_int < 0:
        return None

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidate = day_start + timedelta(hours=hour_int, minutes=minute_int)
    while candidate < now:
        candidate += timedelta(days=1)
    return candidate


def _get_topology_provider(api_key: str) -> TubeTopologyProvider:
    """Lazily initialise a topology provider reused across refreshes."""
    global _topology_provider
    if _topology_provider is None or _topology_provider.api_key != api_key:
        _topology_provider = TubeTopologyProvider(api_key=api_key)
    return _topology_provider


# TfL modes whose line sequences are tracked by TubeTopologyProvider.
# Arrivals on these modes are subject to destination pass-through filtering;
# arrivals on other modes (e.g. bus) are returned unfiltered.
_TOPOLOGY_MODES: frozenset[str] = frozenset({"tube", "dlr", "elizabeth-line", "overground"})

# Sentinel returned by _filter_arrivals_for_destination when stations have no
# through service (different lines or different branches of the same line).
_NO_DIRECT_ROUTE = "_no_direct_route"


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
        if isinstance(arrival.get("lineId"), str)
        and arrival.get("modeName") in _TOPOLOGY_MODES
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
        # Distinguish service suspension from genuinely unconnected stations.
        # Sequence-membership check: if both stations appear in a single cached
        # route sequence, a through train can serve them — so this is a suspension.
        # If no single sequence contains both, no direct service exists (different
        # lines or different branches of the same line).
        try:
            if not provider.has_direct_connection(origin_station_id, destination_station_id):
                return [], _NO_DIRECT_ROUTE  # no through service → show error
        except Exception:
            pass  # can't determine; fall through to ⛔
        return [], None  # through service exists but suspended, or unknown → ⛔

    filtered: list[dict] = []
    for arrival in raw_arrivals:
        line_id = arrival.get("lineId")
        terminal_station_id = arrival.get("destinationNaptanId")
        mode_name = arrival.get("modeName")
        if mode_name not in _TOPOLOGY_MODES:
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

    # Case B: had live topology-tracked arrivals but none pass through destination.
    # Happens for different-branch pairs (e.g. Angel → Charing Cross) where
    # has_path returns True via the merged graph but no actual through train exists.
    if not filtered and line_ids:
        try:
            if not provider.has_direct_connection(origin_station_id, destination_station_id):
                return [], _NO_DIRECT_ROUTE
        except Exception:
            pass

    return filtered, None


def _parse_arrivals(
    raw_arrivals: list[dict],
    destination_arrival_map: dict[tuple[str, str], datetime] | None = None,
    timetable_journey_minutes: int | None = None,
) -> list[Departure]:
    """
    Transform raw TfL API predictions into our Departure model.

    TfL response fields we use:
    - destinationName: e.g., "Richmond"
    - expectedArrival: ISO datetime, e.g., "2024-01-15T08:23:00Z"
    - timeToStation: seconds until arrival (integer)
    - platformName: e.g., "Eastbound - Platform 1"
    - lineName: e.g., "District"
    - vehicleId: used to match arrival predictions at the destination station

    Fields we ignore (for now):
    - naptanId, bearing, direction — useful for a map view later
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
            departure = _parse_single_arrival(
                arrival, destination_arrival_map, timetable_journey_minutes,
            )
            departures.append(departure)
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(
                "Skipping malformed TfL arrival entry: %s — Error: %s",
                arrival.get("id", "unknown"),
                e,
            )
            continue

    return departures


def _parse_timetable_arrivals(
    raw_arrivals: list[dict],
    timetable_journey_minutes: int | None = None,
) -> list[Departure]:
    """
    Parse timetable-derived rows into Departure objects.

    Timetable rows are scheduled estimates, so status is NO_REPORT and delay=0.
    When timetable_journey_minutes is provided, sets arrival_time using
    the TfL-published journey duration from stationIntervals.
    """
    departures: list[Departure] = []
    for arrival in raw_arrivals:
        try:
            expected_time = datetime.fromisoformat(
                str(arrival["expectedArrival"]).replace("Z", "+00:00")
            )
            arrival_time: datetime | None = None
            if timetable_journey_minutes is not None:
                arrival_time = expected_time + timedelta(minutes=timetable_journey_minutes)
            departures.append(
                Departure(
                    destination=_clean_destination(arrival.get("destinationName", "Unknown")),
                    scheduled_time=expected_time,
                    expected_time=expected_time,
                    status=DepartureStatus.NO_REPORT,
                    platform=arrival.get("platformName"),
                    operator=arrival.get("lineName"),
                    delay_minutes=0,
                    arrival_time=arrival_time,
                )
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(
                "Skipping malformed TfL timetable row: %s — Error: %s",
                arrival.get("id", "unknown"),
                e,
            )
            continue

    return departures


def _parse_single_arrival(
    arrival: dict,
    destination_arrival_map: dict[tuple[str, str], datetime] | None = None,
    timetable_journey_minutes: int | None = None,
) -> Departure:
    """
    Parse one TfL prediction dict into a Departure.

    The expectedArrival field is an ISO 8601 datetime string.
    TfL returns these in UTC (Z suffix). We parse and keep as-is.
    For display, we'll format to local time in the display layer.

    Arrival time resolution priority:
    1. vehicleId match from destination_arrival_map (real-time)
    2. timetable_journey_minutes offset from TfL stationIntervals (scheduled)

    Note: TfL predictions don't have a concept of "delayed" vs "on time"
    in the same way National Rail does. The prediction is always the
    current best estimate. So all TfL departures are marked ON_TIME
    unless we add logic later to compare against timetabled times.
    """
    expected_arrival = datetime.fromisoformat(
        arrival["expectedArrival"].replace("Z", "+00:00")
    )

    # Look up arrival time at destination via vehicleId matching
    arrival_time: datetime | None = None
    if destination_arrival_map:
        vehicle_id = arrival.get("vehicleId")
        line_id = arrival.get("lineId")
        if isinstance(vehicle_id, str) and isinstance(line_id, str):
            key = (vehicle_id.strip().lower(), line_id.strip().lower())
            arrival_time = destination_arrival_map.get(key)
            # Sanity check: arrival at destination must be after departure from origin
            if arrival_time is not None and arrival_time <= expected_arrival:
                arrival_time = None

    # Fall back to timetable journey offset when vehicleId match unavailable
    if arrival_time is None and timetable_journey_minutes is not None:
        arrival_time = expected_arrival + timedelta(minutes=timetable_journey_minutes)

    return Departure(
        destination=_clean_destination(arrival["destinationName"]),
        scheduled_time=expected_arrival,  # TfL doesn't separate scheduled vs expected
        expected_time=expected_arrival,
        status=DepartureStatus.ON_TIME,
        platform=arrival.get("platformName"),
        operator=arrival.get("lineName"),  # e.g., "District"
        delay_minutes=0,  # TfL predictions are always "current best estimate"
        arrival_time=arrival_time,
    )


def _extract_station_name(
    raw_arrivals: list[dict],
    station_id: str,
    api_key: str,
    timeout_seconds: int,
) -> str:
    """
    Resolve a human-readable station name, with three-tier fallback.

    1. stationName from the live arrivals response (cheapest — already fetched).
    2. Module-level cache populated by a previous successful response.
    3. GET /StopPoint/{id} → commonName (one extra call, only when cache is cold).

    Falls back to the raw station_id only if all sources fail. This ensures
    the board always shows "East Putney Underground Station" rather than
    "940GZZLUEPY" when the last train of the night has departed.
    """
    if raw_arrivals:
        name = raw_arrivals[0].get("stationName") or station_id
        _station_name_cache[station_id] = name
        return name

    if station_id in _station_name_cache:
        return _station_name_cache[station_id]

    try:
        url = f"{_BASE_URL}/StopPoint/{station_id}"
        params: dict = {}
        if api_key:
            params["app_key"] = api_key
        resp = requests.get(url, params=params, timeout=timeout_seconds)
        resp.raise_for_status()
        name = resp.json().get("commonName") or station_id
        _station_name_cache[station_id] = name
        return name
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.warning("TfL StopPoint name lookup failed for %s: %s", station_id, e)
        return station_id


def _resolve_tube_line_ids(
    live_raw_arrivals: list[dict],
    station_id: str,
    api_key: str,
    timeout_seconds: int,
) -> list[str]:
    """
    Return the tube line IDs serving a station, with StopPoint fallback.

    Primary source: lineId fields from the live arrivals payload.
    Fallback: GET /StopPoint/{id} → lines[], used when live arrivals are
    absent (e.g. last train gone for the night). This ensures timetable
    candidates and journey-time lookups still work outside service hours.
    """
    line_ids = sorted(
        {
            item.get("lineId", "").strip().lower()
            for item in live_raw_arrivals
            if isinstance(item.get("lineId"), str) and item.get("modeName") == "tube"
        }
    )
    if line_ids:
        return line_ids

    try:
        url = f"{_BASE_URL}/StopPoint/{station_id}"
        params: dict = {}
        if api_key:
            params["app_key"] = api_key
        resp = requests.get(url, params=params, timeout=timeout_seconds)
        resp.raise_for_status()
        lines = resp.json().get("lines", [])
        raw_ids = {
            line.get("id", "").strip().lower()
            for line in lines
            if isinstance(line.get("id"), str) and line.get("id", "").strip()
        }
        # Filter to known tube lines only — StopPoint returns ALL modes
        # (buses, NR, DLR, etc.) and querying timetables for non-tube lines
        # produces floods of 404 errors.
        fallback_ids = sorted(raw_ids & set(_LINE_COMPASS))
        if fallback_ids:
            logger.debug(
                "TfL StopPoint line fallback for %s: %s", station_id, fallback_ids
            )
        return fallback_ids
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.warning("TfL StopPoint line lookup failed for %s: %s", station_id, e)
        return []


def _merge_departures_live_first(
    live_departures: list[Departure],
    timetable_departures: list[Departure],
    max_results: int,
) -> list[Departure]:
    """
    Merge departures in live-first mode, filling gaps from timetable rows.
    """
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
            tolerance_seconds=_LIVE_TT_TOLERANCE_SECONDS,
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
    """Stable dedupe key for live+timetable merge."""
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
    """
    Find a matching live departure near timetable boundary.

    Match conditions:
    - existing row is live (status != NO_REPORT)
    - same normalized destination and operator
    - absolute time delta <= tolerance_seconds
    """
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


def _clean_destination(name: str) -> str:
    """Strip trailing ' Underground Station' suffix from TfL destination names."""
    suffix = " Underground Station"
    if name.lower().endswith(suffix.lower()):
        return name[: -len(suffix)]
    return name


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
