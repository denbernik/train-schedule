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
        live_raw_arrivals, filter_error = _filter_arrivals_for_destination(
            raw_arrivals=all_live_raw_arrivals,
            origin_station_id=station_id,
            destination_station_id=destination_station_id,
            api_key=settings.tfl_api_key,
        )
        if filter_error:
            return _error_board(station_id, filter_error)

        live_departures = _parse_arrivals(live_raw_arrivals)
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
                if timetable_filter_error:
                    return _error_board(station_id, timetable_filter_error)
                timetable_departures = _parse_timetable_arrivals(timetable_raw_arrivals)

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
            station_name=_extract_station_name(all_live_raw_arrivals, station_id),
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
    """
    line_ids = sorted(
        {
            item.get("lineId", "").strip().lower()
            for item in live_raw_arrivals
            if isinstance(item.get("lineId"), str) and item.get("modeName") == "tube"
        }
    )
    if not line_ids:
        return []

    live_directions = sorted(
        {
            item.get("direction", "").strip().lower()
            for item in live_raw_arrivals
            if isinstance(item.get("direction"), str)
        }
    )
    directions = [d for d in live_directions if d in ("inbound", "outbound")] or [
        "outbound",
        "inbound",
    ]

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
                        "platformName": f"{direction.title()} (Timetable)",
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


def _parse_timetable_arrivals(raw_arrivals: list[dict]) -> list[Departure]:
    """
    Parse timetable-derived rows into Departure objects.

    Timetable rows are scheduled estimates, so status is NO_REPORT and delay=0.
    """
    departures: list[Departure] = []
    for arrival in raw_arrivals:
        try:
            expected_time = datetime.fromisoformat(
                str(arrival["expectedArrival"]).replace("Z", "+00:00")
            )
            departures.append(
                Departure(
                    destination=arrival.get("destinationName", "Unknown"),
                    scheduled_time=expected_time,
                    expected_time=expected_time,
                    status=DepartureStatus.NO_REPORT,
                    platform=arrival.get("platformName"),
                    operator=arrival.get("lineName"),
                    delay_minutes=0,
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