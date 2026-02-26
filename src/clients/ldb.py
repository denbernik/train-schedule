"""Rail Data Marketplace Live Departure Board API helpers."""

from __future__ import annotations

from collections.abc import Sequence
import json
import logging
from pathlib import Path
import time

import requests

from src.config import get_settings

logger = logging.getLogger(__name__)
_DEBUG_LOG_PATH = Path("/Users/denisbernikov/Desktop/Apps/train-schedule/.cursor/debug-c1b6ec.log")
_DEBUG_SESSION_ID = "c1b6ec"


def _debug_log(run_id: str, hypothesis_id: str, location: str, message: str, data: dict) -> None:
    # region agent log
    payload = {
        "sessionId": _DEBUG_SESSION_ID,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    try:
        _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except Exception:
        pass
    # endregion


class LdbApiError(RuntimeError):
    """Raised when an LDB request cannot be completed successfully."""


class LdbApiHttpError(LdbApiError):
    """Raised when LDB returns a non-2xx response."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


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

    # region agent log
    _debug_log(
        run_id="run1",
        hypothesis_id="H1",
        location="src/clients/ldb.py:request-build",
        message="Prepared LDB request",
        data={
            "endpoint": url,
            "crs": crs.upper(),
            "filter_crs": filter_crs.upper() if filter_crs else None,
            "params": params,
            "token_len": len(token),
        },
    )
    # endregion

    try:
        response = requests.get(
            url,
            headers={"x-apikey": token, "User-Agent": ""},
            params=params,
            timeout=settings.ldb_timeout_seconds,
        )
    except requests.Timeout as exc:
        # region agent log
        _debug_log(
            run_id="run1",
            hypothesis_id="H4",
            location="src/clients/ldb.py:requests-timeout",
            message="LDB request timed out",
            data={"endpoint": url, "timeout_seconds": settings.ldb_timeout_seconds},
        )
        # endregion
        raise LdbApiError("LDB request timed out") from exc
    except requests.RequestException as exc:
        # region agent log
        _debug_log(
            run_id="run1",
            hypothesis_id="H4",
            location="src/clients/ldb.py:requests-exception",
            message="LDB network exception",
            data={"endpoint": url, "exception_type": exc.__class__.__name__},
        )
        # endregion
        raise LdbApiError("Unable to reach LDB API") from exc

    # region agent log
    _debug_log(
        run_id="run1",
        hypothesis_id="H2",
        location="src/clients/ldb.py:response-received",
        message="LDB response received",
        data={
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type"),
            "body_prefix": response.text[:160].replace("\n", " "),
        },
    )
    # endregion

    if response.status_code >= 400:
        body_snippet = response.text[:200].strip().replace("\n", " ")
        # region agent log
        _debug_log(
            run_id="run1",
            hypothesis_id="H3",
            location="src/clients/ldb.py:http-error",
            message="LDB non-2xx status",
            data={"status_code": response.status_code, "body_snippet": body_snippet},
        )
        # endregion
        raise LdbApiHttpError(
            response.status_code,
            f"{_http_error_message(response.status_code)} | body={body_snippet}",
        )

    try:
        return response.json()
    except ValueError as exc:
        raise LdbApiError("LDB returned non-JSON response") from exc


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
        # region agent log
        _debug_log(
            run_id="run1",
            hypothesis_id="H5",
            location="src/clients/ldb.py:probe-parse",
            message="Parsed probe payload",
            data={
                "service_list_path": path,
                "service_count": len(services),
                "top_level_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
            },
        )
        # endregion
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


if __name__ == "__main__":
    result = probe_departure_board(crs="WNT", filter_crs="WAT")
    print(json.dumps(result, indent=2, default=str))
    if not result.get("ok"):
        raise SystemExit(1)
