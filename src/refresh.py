"""Source-specific refresh orchestration helpers."""

from __future__ import annotations

from collections.abc import Callable
import time

from src.clients.tfl import fetch_departures as fetch_tfl
from src.clients.transport_api import fetch_departures as fetch_national_rail
from src.config import get_settings
from src.models import StationBoard


def build_transport_cache_fetcher(
    ttl_seconds: int,
    fetch_func: Callable[..., StationBoard] | None = None,
):
    """Build a cached TransportAPI fetch function with a configurable TTL."""
    fetch_impl = fetch_func or fetch_national_rail
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

        board = fetch_impl(
            station_code=station_code,
            calling_at=calling_at,
            max_results=max_results,
        )
        cache[key] = (now, board)
        return board

    def _clear() -> None:
        cache.clear()

    _cached_fetch.clear = _clear  # type: ignore[attr-defined]

    return _cached_fetch


_settings = get_settings()
fetch_transport_api_cached = build_transport_cache_fetcher(
    ttl_seconds=_settings.transport_api_refresh_seconds
)


def fetch_transport_for_leg(origin_station_id: str, destination_station_id: str) -> StationBoard:
    """
    Fetch National Rail departures for a route leg using hourly cache policy.
    """
    settings = get_settings()
    return fetch_transport_api_cached(
        station_code=origin_station_id,
        calling_at=destination_station_id,
        max_results=settings.transport_api_prefetch_departures,
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
