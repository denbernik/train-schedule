"""Source-specific refresh orchestration helpers."""

from __future__ import annotations

from collections.abc import Callable
import time

from src.clients.ldb import fetch_departures as fetch_ldb
from src.clients.tfl import fetch_departures as fetch_tfl
from src.clients.transport_api import fetch_departures as fetch_transport_api
from src.config import get_settings
from src.models import StationBoard


def _fetch_ldb_for_leg(station_code: str, calling_at: str, max_results: int) -> StationBoard:
    return fetch_ldb(
        crs=station_code,
        filter_crs=calling_at,
        num_rows=max_results,
    )


def _fetch_transport_api_for_leg(
    station_code: str, calling_at: str, max_results: int
) -> StationBoard:
    return fetch_transport_api(
        station_code=station_code,
        calling_at=calling_at,
        max_results=max_results,
    )


def build_national_rail_cache_fetcher(
    ttl_seconds: int,
    primary_fetch_func: Callable[..., StationBoard] | None = None,
    fallback_fetch_func: Callable[..., StationBoard] | None = None,
):
    """Build a cached National Rail fetcher (LDB primary, TransportAPI fallback)."""
    primary_fetch = primary_fetch_func or _fetch_ldb_for_leg
    fallback_fetch = fallback_fetch_func or _fetch_transport_api_for_leg
    cache: dict[tuple[str, str, int], tuple[float, StationBoard]] = {}

    def _cached_fetch(
        station_code: str,
        calling_at: str,
        max_results: int,
    ) -> StationBoard:
        key = (station_code, calling_at, max_results)
        now = time.time()
        cached = cache.get(key)
        if cached:
            fetched_at, board = cached
            if now - fetched_at < ttl_seconds:
                return board

        board = primary_fetch(
            station_code=station_code,
            calling_at=calling_at,
            max_results=max_results,
        )
        if board.has_error:
            fallback_board = fallback_fetch(
                station_code=station_code,
                calling_at=calling_at,
                max_results=max_results,
            )
            if not fallback_board.has_error:
                board = fallback_board
        cache[key] = (now, board)
        return board

    def _clear() -> None:
        cache.clear()

    _cached_fetch.clear = _clear  # type: ignore[attr-defined]

    return _cached_fetch


_settings = get_settings()
fetch_national_rail_cached = build_national_rail_cache_fetcher(
    ttl_seconds=_settings.national_rail_refresh_seconds
)


def fetch_national_rail_for_leg(origin_station_id: str, destination_station_id: str) -> StationBoard:
    """
    Fetch National Rail departures for a route leg via LDB with TransportAPI fallback.
    """
    settings = get_settings()
    return fetch_national_rail_cached(
        station_code=origin_station_id,
        calling_at=destination_station_id,
        max_results=settings.national_rail_prefetch_departures,
    )


def fetch_transport_for_leg(origin_station_id: str, destination_station_id: str) -> StationBoard:
    """Backward-compatible alias for older api_source config."""
    return fetch_national_rail_for_leg(
        origin_station_id=origin_station_id,
        destination_station_id=destination_station_id,
    )


def fetch_tfl_for_leg(
    origin_station_id: str,
    destination_station_id: str,
    max_results: int,
) -> StationBoard:
    """
    Fetch TfL departures directly (no app-level cache).
    """
    return fetch_tfl(
        station_id=origin_station_id,
        destination_station_id=destination_station_id,
        max_results=max_results,
    )
