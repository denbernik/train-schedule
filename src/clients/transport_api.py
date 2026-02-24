"""
TransportAPI client for National Rail departures.

Fetches live departure data from TransportAPI's Rail Information service.
Used for Wandsworth Town (National Rail) but works for any UK rail station.

API endpoint: GET /v3/uk/train/station/{station_code}/live.json
Docs: https://developer.transportapi.com/docs

Authentication: app_id and app_key as query parameters or HTTP headers.
We use query parameters for simplicity.

IMPORTANT — Rate limits:
    Free plan:  30 requests/day  (not viable for real-time refresh)
    Home plan:  300 requests/day (£5/month — enough for ~2 min refresh intervals)
    Paid plans: higher limits available

For a departure board refreshing every 30 seconds, you'd need ~2,880 requests/day.
We set a longer default refresh for this client and recommend upgrading to the
Home plan (300/day) or switching to RTT/Darwin if you need faster updates.

The response format nests departures inside:
    response["departures"]["all"] -> list of departure dicts

Each departure dict contains:
    - aimed_departure_time: scheduled time, e.g., "14:23"
    - expected_departure_time: live estimate, e.g., "14:25" or "On time" or None
    - status: e.g., "ON TIME", "LATE", "CANCELLED", "EARLY", "NO REPORT"
    - platform: e.g., "2" or None
    - destination_name: e.g., "London Waterloo"
    - operator_name: e.g., "South Western Railway"
    - train_uid: unique train identifier
"""

import logging
from datetime import datetime, date

import requests

from src.config import get_settings
from src.models import Departure, DepartureStatus, StationBoard, StationType

logger = logging.getLogger(__name__)

_BASE_URL = "https://transportapi.com/v3/uk/train/station"


def fetch_departures(
    station_code: str | None = None,
    max_results: int | None = None,
    calling_at: str | None = None,
) -> StationBoard:
    """
    Fetch live departures from a National Rail station via TransportAPI.

    Args:
        station_code: 3-letter CRS code (e.g., "WNT" for Wandsworth Town).
                      Defaults to configured station.
        max_results: Max departures to return. Defaults to configured value.
        calling_at: Optional 3-letter CRS code to return only services that
                    call at that station.

    Returns:
        StationBoard with departures sorted by scheduled departure time.
        Returns a StationBoard with error_message on failure.
    """
    settings = get_settings()
    station_code = station_code or settings.national_rail_station_code
    max_results = max_results or settings.max_departures

    try:
        raw_response = _call_api(
            station_code=station_code,
            app_id=settings.transport_api_app_id,
            app_key=settings.transport_api_app_key,
            calling_at=calling_at,
            timeout_seconds=settings.transport_api_timeout_seconds,
        )
        departures = _parse_departures(raw_response)

        # Already sorted by time from the API, but enforce it
        departures.sort(key=lambda d: d.expected_time)
        departures = departures[:max_results]

        station_name = raw_response.get("station_name", station_code)

        logger.info(
            "TransportAPI: fetched %d departures for %s (%s)",
            len(departures),
            station_name,
            station_code,
        )

        return StationBoard(
            station_name=station_name,
            station_type=StationType.NATIONAL_RAIL,
            departures=departures,
        )

    except requests.Timeout:
        logger.error("TransportAPI timeout for station %s", station_code)
        return _error_board(station_code, "TransportAPI timed out")

    except requests.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else "unknown"
        logger.error(
            "TransportAPI HTTP %s for station %s: %s",
            status_code,
            station_code,
            e,
        )
        # Surface specific, actionable messages for common errors
        if e.response is not None and e.response.status_code == 401:
            return _error_board(station_code, "Invalid TransportAPI credentials — check .env")
        if e.response is not None and e.response.status_code == 429:
            return _error_board(station_code, "TransportAPI rate limit reached — try again later")
        return _error_board(station_code, f"TransportAPI error (HTTP {status_code})")

    except requests.RequestException as e:
        logger.error("TransportAPI request failed for station %s: %s", station_code, e)
        return _error_board(station_code, "Unable to reach TransportAPI")

    except (KeyError, ValueError, TypeError) as e:
        logger.error("TransportAPI response parsing failed: %s", e)
        return _error_board(station_code, "Unexpected data from TransportAPI")


def _call_api(
    station_code: str,
    app_id: str,
    app_key: str,
    calling_at: str | None = None,
    timeout_seconds: int = 3600,
) -> dict:
    """
    Make the HTTP request to TransportAPI's live departures endpoint.

    We request Darwin-enriched data (darwin=true) for better real-time
    accuracy. Without this flag, status fields may not reflect live
    conditions.
    """
    url = f"{_BASE_URL}/{station_code}/live.json"

    params = {
        "app_id": app_id,
        "app_key": app_key,
        "darwin": "true",           # Use Darwin feed for live status
        "train_status": "passenger", # Only passenger services, not freight
    }
    if calling_at:
        params["calling_at"] = calling_at

    response = requests.get(url, params=params, timeout=timeout_seconds)
    response.raise_for_status()

    return response.json()


def _parse_departures(raw_response: dict) -> list[Departure]:
    """
    Transform TransportAPI response into Departure objects.

    The response nests departures at: response["departures"]["all"]
    Each entry has both scheduled ("aimed") and live ("expected") times.

    TransportAPI returns times as HH:MM strings (not ISO datetimes),
    so we combine them with today's date to create proper datetime objects.
    Edge case: a departure at 00:05 shown at 23:55 is tomorrow — we handle
    this by checking if the resulting time is in the past and adding a day
    if needed.
    """
    departures_data = raw_response.get("departures", {}).get("all", [])

    if departures_data is None:
        # API returns null for "all" when there are no departures
        return []

    departures = []
    today = date.today()

    for dep in departures_data:
        try:
            departure = _parse_single_departure(dep, today)
            departures.append(departure)
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(
                "Skipping malformed TransportAPI departure: %s — Error: %s",
                dep.get("train_uid", "unknown"),
                e,
            )
            continue

    return departures


def _parse_single_departure(dep: dict, today: date) -> Departure:
    """
    Parse one TransportAPI departure dict into a Departure.

    Time handling:
    - aimed_departure_time: always present, format "HH:MM"
    - expected_departure_time: can be "HH:MM", "On time", or None

    If expected is "On time" or missing, we use the scheduled time.
    If expected is a time string, we parse it as the live estimate.
    """
    aimed_time_str = dep["aimed_departure_time"]  # e.g., "14:23"
    scheduled_time = _parse_time(aimed_time_str, today)

    # Parse expected time — this has several possible values
    expected_str = dep.get("expected_departure_time")
    if expected_str and expected_str not in ("On time", "on time"):
        expected_time = _parse_time(expected_str, today)
    else:
        expected_time = scheduled_time

    # Calculate delay
    delay_seconds = (expected_time - scheduled_time).total_seconds()
    delay_minutes = max(0, int(delay_seconds / 60))

    # Map status string to our enum
    status = _map_status(dep.get("status", ""), delay_minutes)

    return Departure(
        destination=dep.get("destination_name", "Unknown"),
        scheduled_time=scheduled_time,
        expected_time=expected_time,
        status=status,
        platform=dep.get("platform"),
        operator=dep.get("operator_name"),
        delay_minutes=delay_minutes,
    )


def _parse_time(time_str: str, today: date) -> datetime:
    """
    Parse a HH:MM string into a datetime using today's date.

    Handles the midnight rollover edge case: if the resulting datetime
    is more than 6 hours in the past, assume it's tomorrow. This handles
    services like the 00:05 showing up on the board at 23:50, without
    incorrectly bumping afternoon services viewed in the morning.
    """
    hours, minutes = map(int, time_str.split(":"))
    result = datetime(today.year, today.month, today.day, hours, minutes)

    # Midnight rollover: if time appears to be far in the past,
    # it's probably tomorrow
    now = datetime.now()
    if (now - result).total_seconds() > 6 * 3600:
        from datetime import timedelta
        result += timedelta(days=1)

    return result


def _map_status(status_str: str, delay_minutes: int) -> DepartureStatus:
    """
    Map TransportAPI status strings to our DepartureStatus enum.

    TransportAPI can return various status strings depending on whether
    Darwin data is enabled. Common values:
    - "ON TIME", "EARLY" -> ON_TIME
    - "LATE" -> DELAYED
    - "CANCELLED" -> CANCELLED
    - "NO REPORT", "" -> NO_REPORT

    We also cross-check: if status says "ON TIME" but delay_minutes > 0,
    we trust the calculated delay (the times don't lie).
    """
    status_upper = status_str.strip().upper()

    if status_upper == "CANCELLED":
        return DepartureStatus.CANCELLED

    if status_upper == "LATE" or delay_minutes > 0:
        return DepartureStatus.DELAYED

    if status_upper in ("ON TIME", "EARLY", "STARTS HERE"):
        return DepartureStatus.ON_TIME

    if status_upper in ("NO REPORT", "OFF ROUTE", ""):
        return DepartureStatus.NO_REPORT

    # Unknown status — log it so we can add handling later
    logger.warning("Unknown TransportAPI status: '%s'", status_str)
    return DepartureStatus.NO_REPORT


def _error_board(station_code: str, message: str) -> StationBoard:
    """Construct an empty StationBoard with an error message."""
    return StationBoard(
        station_name=station_code,
        station_type=StationType.NATIONAL_RAIL,
        departures=[],
        error_message=message,
    )