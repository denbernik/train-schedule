"""Rail Data Marketplace Live Departure Board API client."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
import json
import logging

import requests

from src.config import get_settings
from src.models import Departure, DepartureStatus, StationBoard, StationType

logger = logging.getLogger(__name__)


class LdbApiError(RuntimeError):
    """Raised when an LDB request cannot be completed successfully."""


class LdbApiHttpError(LdbApiError):
    """Raised when LDB returns a non-2xx response."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def fetch_departures(
    crs: str,
    *,
    num_rows: int | None = None,
    filter_crs: str | None = None,
    filter_type: str | None = None,
    time_offset: int | None = None,
    time_window: int | None = None,
) -> dict:
    """Fetch and parse live departures for a CRS from LDB."""
    settings = get_settings()
    max_results = num_rows if num_rows is not None else settings.max_departures
    call_kwargs = dict(
        crs=crs,
        num_rows=max_results,
        filter_crs=filter_crs,
        filter_type=filter_type,
        time_offset=time_offset,
        time_window=time_window,
    )
    try:
        destination_crs: str | None = None
        if filter_crs:
            try:
                payload = call_departure_board_with_details(**call_kwargs)
                destination_crs = filter_crs
            except LdbApiError:
                logger.warning(
                    "GetDepBoardWithDetails failed for %s; falling back to GetDepartureBoard",
                    crs,
                )
                payload = call_departure_board(**call_kwargs)
                destination_crs = filter_crs
        else:
            payload = call_departure_board(**call_kwargs)

        departures = _parse_departures(
            payload,
            destination_crs=destination_crs,
            reference_now=datetime.now(),
        )
        departures.sort(key=lambda dep: dep.expected_time)
        return StationBoard(
            station_name=payload.get("locationName", crs),
            station_type=StationType.NATIONAL_RAIL,
            departures=departures[:max_results],
        )
    except LdbApiError as exc:
        logger.error("LDB fetch failed for %s: %s", crs, exc)
        return _error_board(crs, str(exc))


def call_departure_board(
    crs: str,
    *,
    num_rows: int | None = None,
    filter_crs: str | None = None,
    filter_type: str | None = None,
    time_offset: int | None = None,
    time_window: int | None = None,
) -> dict:
    """Call GetDepartureBoard and return parsed JSON payload."""
    settings = get_settings()
    token = settings.ldb_access_token.strip()
    if not token:
        raise LdbApiError("Missing LDB access token. Set LDB_ACCESS_TOKEN in .env")

    url = (
        f"{settings.ldb_base_url.rstrip('/')}"
        f"/LDBWS/api/{settings.ldb_api_version}/GetDepartureBoard/{crs.upper()}"
    )
    params = {
        "numRows": num_rows if num_rows is not None else settings.ldb_default_num_rows,
        "filterType": filter_type or settings.ldb_default_filter_type,
        "timeOffset": time_offset if time_offset is not None else settings.ldb_default_time_offset,
        "timeWindow": time_window if time_window is not None else settings.ldb_default_time_window,
    }
    if filter_crs:
        params["filterCrs"] = filter_crs.upper()

    try:
        response = requests.get(
            url,
            headers={"x-apikey": token, "User-Agent": ""},
            params=params,
            timeout=settings.ldb_timeout_seconds,
        )
    except requests.Timeout as exc:
        raise LdbApiError("LDB request timed out") from exc
    except requests.RequestException as exc:
        raise LdbApiError("Unable to reach LDB API") from exc

    if response.status_code >= 400:
        body_snippet = response.text[:200].strip().replace("\n", " ")
        raise LdbApiHttpError(
            response.status_code,
            f"{_http_error_message(response.status_code)} | body={body_snippet}",
        )

    try:
        return response.json()
    except ValueError as exc:
        raise LdbApiError("LDB returned non-JSON response") from exc


def call_departure_board_with_details(
    crs: str,
    *,
    num_rows: int | None = None,
    filter_crs: str | None = None,
    filter_type: str | None = None,
    time_offset: int | None = None,
    time_window: int | None = None,
) -> dict:
    """Call GetDepBoardWithDetails and return parsed JSON payload."""
    settings = get_settings()
    token = settings.ldb_access_token.strip()
    if not token:
        raise LdbApiError("Missing LDB access token. Set LDB_ACCESS_TOKEN in .env")

    base_url = settings.ldb_with_details_base_url.strip() or settings.ldb_base_url
    url = (
        f"{base_url.rstrip('/')}"
        f"/LDBWS/api/{settings.ldb_api_version}/GetDepBoardWithDetails/{crs.upper()}"
    )
    params = {
        "numRows": num_rows if num_rows is not None else settings.ldb_default_num_rows,
        "filterType": filter_type or settings.ldb_default_filter_type,
        "timeOffset": time_offset if time_offset is not None else settings.ldb_default_time_offset,
        "timeWindow": time_window if time_window is not None else settings.ldb_default_time_window,
    }
    if filter_crs:
        params["filterCrs"] = filter_crs.upper()

    try:
        response = requests.get(
            url,
            headers={"x-apikey": token, "User-Agent": ""},
            params=params,
            timeout=settings.ldb_timeout_seconds,
        )
    except requests.Timeout as exc:
        raise LdbApiError("LDB with-details request timed out") from exc
    except requests.RequestException as exc:
        raise LdbApiError("Unable to reach LDB API (with-details)") from exc

    if response.status_code >= 400:
        body_snippet = response.text[:200].strip().replace("\n", " ")
        raise LdbApiHttpError(
            response.status_code,
            f"{_http_error_message(response.status_code)} | body={body_snippet}",
        )

    try:
        return response.json()
    except ValueError as exc:
        raise LdbApiError("LDB with-details returned non-JSON response") from exc


def probe_departure_board(crs: str, *, filter_crs: str | None = None) -> dict:
    """
    Fetch a board and return high-signal diagnostics for schema validation.

    The output intentionally avoids headers and secrets.
    """
    try:
        settings = get_settings()
        payload = call_departure_board(crs=crs, filter_crs=filter_crs)
        path, services = detect_service_rows(payload)
        first = services[0] if services else {}
        logger.info(
            "LDB probe: crs=%s status=200 services=%d path=%s",
            crs.upper(),
            len(services),
            path,
        )
        return {
            "ok": True,
            "status_code": 200,
            "endpoint": (
                f"{settings.ldb_base_url.rstrip('/')}/LDBWS/api/"
                f"{settings.ldb_api_version}/GetDepartureBoard/{crs.upper()}"
            ),
            "query_params": {
                "numRows": settings.ldb_default_num_rows,
                "filterCrs": filter_crs.upper() if filter_crs else None,
                "filterType": settings.ldb_default_filter_type,
                "timeOffset": settings.ldb_default_time_offset,
                "timeWindow": settings.ldb_default_time_window,
            },
            "top_level_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
            "service_list_path": path,
            "service_count": len(services),
            "first_service_keys": sorted(first.keys()) if isinstance(first, dict) else [],
            "first_service_sample": _service_preview(first),
        }
    except LdbApiHttpError as exc:
        return {"ok": False, "status_code": exc.status_code, "error": str(exc)}
    except LdbApiError as exc:
        return {"ok": False, "error": str(exc)}


def _extract_arrival_time(
    service: dict,
    destination_crs: str,
    reference_now: datetime,
) -> datetime | None:
    """Extract arrival time at destination_crs from subsequentCallingPoints."""
    calling_points_raw = service.get("subsequentCallingPoints")
    if not isinstance(calling_points_raw, list):
        return None

    target = destination_crs.upper()
    points: list[dict] = []
    for item in calling_points_raw:
        if isinstance(item, list):
            points.extend(p for p in item if isinstance(p, dict))
        elif isinstance(item, dict):
            inner = item.get("callingPoint")
            if isinstance(inner, list):
                points.extend(p for p in inner if isinstance(p, dict))

    for point in points:
        crs = point.get("crs", "")
        if not isinstance(crs, str) or crs.upper() != target:
            continue
        for key in ("at", "et", "st"):
            value = point.get(key)
            if _is_time_value(value):
                return _parse_time_value(value, reference_now=reference_now)
        return None

    return None


def _has_destination_in_calling_points(service: dict, destination_crs: str) -> bool:
    """Check whether destination_crs appears in subsequentCallingPoints."""
    calling_points_raw = service.get("subsequentCallingPoints")
    if not isinstance(calling_points_raw, list):
        return False

    target = destination_crs.upper()
    for item in calling_points_raw:
        if isinstance(item, list):
            for p in item:
                if isinstance(p, dict) and isinstance(p.get("crs"), str) and p["crs"].upper() == target:
                    return True
        elif isinstance(item, dict):
            inner = item.get("callingPoint")
            if isinstance(inner, list):
                for p in inner:
                    if isinstance(p, dict) and isinstance(p.get("crs"), str) and p["crs"].upper() == target:
                        return True
    return False


def _parse_departures(
    payload: dict,
    *,
    destination_crs: str | None = None,
    reference_now: datetime | None = None,
) -> list[Departure]:
    path, services = detect_service_rows(payload)
    if path == "<not-found>":
        return []

    departures: list[Departure] = []
    reference_now = reference_now or datetime.now()
    for service in services:
        has_calling_points = isinstance(service.get("subsequentCallingPoints"), list)
        if destination_crs and has_calling_points and not _has_destination_in_calling_points(service, destination_crs):
            logger.debug(
                "Skipping service %s: %s not in subsequentCallingPoints",
                service.get("std", "?"),
                destination_crs,
            )
            continue
        try:
            departures.append(
                _parse_service(
                    service,
                    reference_now=reference_now,
                    destination_crs=destination_crs,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Skipping malformed LDB service: %s", exc)
    return departures


def _parse_service(
    service: dict,
    reference_now: datetime,
    *,
    destination_crs: str | None = None,
) -> Departure:
    scheduled_time = _parse_time_value(service["std"], reference_now=reference_now)

    expected_raw = service.get("atd") or service.get("etd") or service["std"]
    expected_time = (
        _parse_time_value(expected_raw, reference_now=reference_now)
        if _is_time_value(expected_raw)
        else scheduled_time
    )
    delay_minutes = max(0, int((expected_time - scheduled_time).total_seconds() / 60))

    if destination_crs:
        destination = (
            _destination_from_relevant_portion(service, destination_crs)
            or _destination_name(service)
        )
    else:
        destination = _destination_name(service)

    status = _map_status(service=service, expected_raw=expected_raw, delay_minutes=delay_minutes)

    arrival_time = None
    if destination_crs:
        arrival_time = _extract_arrival_time(service, destination_crs, reference_now)

    return Departure(
        destination=destination,
        scheduled_time=scheduled_time,
        expected_time=expected_time,
        status=status,
        platform=service.get("platform"),
        operator=service.get("operator"),
        delay_minutes=delay_minutes,
        arrival_time=arrival_time,
    )



def _destination_from_relevant_portion(service: dict, filter_crs: str) -> str | None:
    """
    Return the locationName of the last calling point in the portion containing
    filter_crs, handling both flat-list and wrapped-dict schemas.

    For split services (multiple portions in subsequentCallingPoints), this
    returns the terminus of the relevant portion rather than the train's overall
    final destination — but only when filter_crs is clearly AFTER the split
    point (i.e. on a specific branch).

    When filter_crs is before the split point, returns None so the caller falls
    through to the service-level destination.  A station is detected as
    pre-split when a station that appears AFTER it in the matching portion also
    exists in another portion (indicating the split hasn't happened yet).
    """
    calling_points_raw = service.get("subsequentCallingPoints")
    if not isinstance(calling_points_raw, list):
        return None

    target = filter_crs.upper()

    # Parse all portions.
    portions: list[list[dict]] = []
    for item in calling_points_raw:
        if isinstance(item, list):
            portion = [p for p in item if isinstance(p, dict)]
        elif isinstance(item, dict):
            inner = item.get("callingPoint")
            portion = [p for p in inner if isinstance(p, dict)] if isinstance(inner, list) else []
        else:
            continue
        if portion:
            portions.append(portion)

    # Find portions containing filter_crs.
    matching_indices: list[int] = []
    for idx, portion in enumerate(portions):
        if any(isinstance(p.get("crs"), str) and p["crs"].upper() == target for p in portion):
            matching_indices.append(idx)

    if not matching_indices:
        return None

    # Multiple portions contain filter_crs — ambiguous, use API destination.
    if len(matching_indices) > 1:
        return None

    matched = portions[matching_indices[0]]

    # Single portion overall — no split, return its terminus.
    if len(portions) == 1:
        last = matched[-1]
        name = last.get("locationName")
        return name if isinstance(name, str) else None

    # Split service: determine if filter_crs is before or after the split.
    # Collect CRS codes from all OTHER portions.
    other_crs: set[str] = set()
    for idx, portion in enumerate(portions):
        if idx != matching_indices[0]:
            for p in portion:
                crs = p.get("crs")
                if isinstance(crs, str):
                    other_crs.add(crs.upper())

    # Find position of filter_crs in the matching portion.
    filter_pos = -1
    for i, p in enumerate(matched):
        crs = p.get("crs")
        if isinstance(crs, str) and crs.upper() == target:
            filter_pos = i
            break

    # If any station AFTER filter_crs also appears in another portion,
    # filter_crs is before the split — fall through to API destination.
    if filter_pos >= 0:
        for i in range(filter_pos + 1, len(matched)):
            crs = matched[i].get("crs")
            if isinstance(crs, str) and crs.upper() in other_crs:
                return None

    # filter_crs is after the split — return this portion's terminus.
    last = matched[-1]
    name = last.get("locationName")
    return name if isinstance(name, str) else None


def _destination_name(service: dict) -> str:
    destination = service.get("destination")
    if isinstance(destination, list):
        names = [
            item.get("locationName")
            for item in destination
            if isinstance(item, dict) and isinstance(item.get("locationName"), str)
        ]
        if names:
            return " / ".join(names)
    return "Unknown"


def _map_status(service: dict, expected_raw: str, delay_minutes: int) -> DepartureStatus:
    if service.get("isCancelled") is True:
        return DepartureStatus.CANCELLED

    text = str(expected_raw).strip().lower()
    if "cancel" in text:
        return DepartureStatus.CANCELLED
    if text in {"on time", "starts here", "early"}:
        return DepartureStatus.ON_TIME
    if text in {"no report"}:
        return DepartureStatus.NO_REPORT
    if "delayed" in text or "late" in text:
        return DepartureStatus.DELAYED
    if _is_time_value(expected_raw):
        return DepartureStatus.DELAYED if delay_minutes > 0 else DepartureStatus.ON_TIME
    return DepartureStatus.NO_REPORT


def _is_time_value(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split(":")
    if len(parts) != 2:
        return False
    return parts[0].isdigit() and parts[1].isdigit()


def _parse_time_value(value: str, reference_now: datetime) -> datetime:
    hours, minutes = map(int, value.split(":"))
    parsed = reference_now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if (reference_now - parsed).total_seconds() > 6 * 3600:
        parsed += timedelta(days=1)
    return parsed


def detect_service_rows(payload: dict) -> tuple[str, list[dict]]:
    """Find where service rows live in the response body."""
    candidate_paths: list[tuple[str, ...]] = [
        ("trainServices",),
        ("GetDepartureBoardResult", "trainServices"),
        ("getDepartureBoardResult", "trainServices"),
        ("result", "trainServices"),
        ("departures", "all"),
    ]

    for path in candidate_paths:
        node = _nested_get(payload, path)
        if isinstance(node, list):
            rows = [item for item in node if isinstance(item, dict)]
            return (".".join(path), rows)

    return ("<not-found>", [])


def _nested_get(data: dict, path: Sequence[str]) -> object:
    current: object = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _http_error_message(status_code: int) -> str:
    if status_code in (401, 403):
        return "Invalid LDB credentials or unauthorised product access"
    if status_code == 404:
        return "LDB endpoint not found (path/version mismatch)"
    if status_code == 429:
        return "LDB rate limit reached"
    return f"LDB request failed (HTTP {status_code})"


def _service_preview(service: dict) -> dict:
    if not isinstance(service, dict):
        return {}
    preview_keys = (
        "std",
        "etd",
        "platform",
        "operator",
        "operatorCode",
        "serviceType",
        "isCancelled",
        "cancelReason",
        "delayReason",
        "destination",
        "destination_name",
        "expected_departure_time",
        "status",
    )
    return {key: service.get(key) for key in preview_keys if key in service}


def _error_board(station_code: str, message: str) -> StationBoard:
    return StationBoard(
        station_name=station_code,
        station_type=StationType.NATIONAL_RAIL,
        departures=[],
        error_message=message,
    )


if __name__ == "__main__":
    result = probe_departure_board(crs="WNT", filter_crs="WAT")
    print(json.dumps(result, indent=2, default=str))
    if not result.get("ok"):
        raise SystemExit(1)
