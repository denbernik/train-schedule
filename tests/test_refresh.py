import time
from unittest.mock import MagicMock, patch

from src.models import StationBoard, StationType
from src.refresh import (
    build_national_rail_cache_fetcher,
    fetch_national_rail_for_leg,
    fetch_tfl_for_leg,
)


def _board(name: str = "Test") -> StationBoard:
    return StationBoard(
        station_name=name,
        station_type=StationType.NATIONAL_RAIL,
        departures=[],
    )


def test_national_rail_cache_reuses_result_within_ttl():
    primary_calls = {"count": 0}
    fallback_calls = {"count": 0}

    def _fake_primary(**kwargs):
        primary_calls["count"] += 1
        return _board(f"LDB-{primary_calls['count']}")

    def _fake_fallback(**kwargs):
        fallback_calls["count"] += 1
        return _board(f"TA-{fallback_calls['count']}")

    cached_fetch = build_national_rail_cache_fetcher(
        ttl_seconds=30,
        primary_fetch_func=_fake_primary,
        fallback_fetch_func=_fake_fallback,
    )
    cached_fetch.clear()

    first = cached_fetch(station_code="WNT", calling_at="WAT", max_results=40)
    second = cached_fetch(station_code="WNT", calling_at="WAT", max_results=40)

    assert first.station_name == "LDB-1"
    assert second.station_name == "LDB-1"
    assert primary_calls["count"] == 1
    assert fallback_calls["count"] == 0


def test_national_rail_cache_refreshes_after_ttl_expiry():
    calls = {"primary": 0}

    def _fake_primary(**kwargs):
        calls["primary"] += 1
        return _board(f"Refresh-{calls['primary']}")

    cached_fetch = build_national_rail_cache_fetcher(
        ttl_seconds=1,
        primary_fetch_func=_fake_primary,
        fallback_fetch_func=lambda **kwargs: _board("Fallback"),
    )
    cached_fetch.clear()

    first = cached_fetch(station_code="WNT", calling_at="WAT", max_results=40)
    time.sleep(1.2)
    second = cached_fetch(station_code="WNT", calling_at="WAT", max_results=40)

    assert first.station_name == "Refresh-1"
    assert second.station_name == "Refresh-2"
    assert calls["primary"] == 2


def test_national_rail_leg_uses_prefetch_size_from_settings():
    settings = MagicMock()
    settings.national_rail_prefetch_departures = 40

    with patch("src.refresh.get_settings", return_value=settings):
        with patch("src.refresh.fetch_national_rail_cached", return_value=_board()) as mock_cached:
            fetch_national_rail_for_leg(origin_station_id="WNT", destination_station_id="WAT")

    mock_cached.assert_called_once_with(
        station_code="WNT",
        calling_at="WAT",
        max_results=40,
    )


def test_national_rail_falls_back_when_primary_errors():
    def _primary_error(**kwargs):
        return StationBoard(
            station_name="WNT",
            station_type=StationType.NATIONAL_RAIL,
            departures=[],
            error_message="LDB down",
        )

    calls = {"fallback": 0}

    def _fallback_ok(**kwargs):
        calls["fallback"] += 1
        return _board("TransportAPI")

    cached_fetch = build_national_rail_cache_fetcher(
        ttl_seconds=30,
        primary_fetch_func=_primary_error,
        fallback_fetch_func=_fallback_ok,
    )
    cached_fetch.clear()

    result = cached_fetch(station_code="WNT", calling_at="WAT", max_results=40)
    assert not result.has_error
    assert result.station_name == "TransportAPI"
    assert calls["fallback"] == 1


def test_transport_api_fallback_leaves_arrival_time_none():
    from datetime import datetime
    from src.models import Departure, DepartureStatus

    dep = Departure(
        destination="London Waterloo",
        scheduled_time=datetime(2025, 6, 15, 14, 23),
        expected_time=datetime(2025, 6, 15, 14, 23),
        status=DepartureStatus.ON_TIME,
    )
    fallback_board = StationBoard(
        station_name="TransportAPI",
        station_type=StationType.NATIONAL_RAIL,
        departures=[dep],
    )

    def _primary_error(**kwargs):
        return StationBoard(
            station_name="WNT",
            station_type=StationType.NATIONAL_RAIL,
            departures=[],
            error_message="LDB down",
        )

    def _fallback_ok(**kwargs):
        return fallback_board

    cached_fetch = build_national_rail_cache_fetcher(
        ttl_seconds=30,
        primary_fetch_func=_primary_error,
        fallback_fetch_func=_fallback_ok,
    )
    cached_fetch.clear()

    result = cached_fetch(station_code="WNT", calling_at="WAT", max_results=40)
    assert not result.has_error
    assert len(result.departures) == 1
    assert result.departures[0].arrival_time is None


def test_tfl_fetch_path_is_not_cached_in_refresh_layer():
    with patch("src.refresh.fetch_tfl", return_value=_board("TfL")) as mock_tfl:
        fetch_tfl_for_leg(
            origin_station_id="940GZZLUEPY",
            destination_station_id="940GZZLUECT",
            max_results=15,
        )
        fetch_tfl_for_leg(
            origin_station_id="940GZZLUEPY",
            destination_station_id="940GZZLUECT",
            max_results=15,
        )

    assert mock_tfl.call_count == 2
