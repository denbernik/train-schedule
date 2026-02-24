import time
from unittest.mock import MagicMock, patch

from src.models import StationBoard, StationType
from src.refresh import (
    build_transport_cache_fetcher,
    fetch_tfl_for_leg,
    fetch_transport_for_leg,
)


def _board(name: str = "Test") -> StationBoard:
    return StationBoard(
        station_name=name,
        station_type=StationType.NATIONAL_RAIL,
        departures=[],
    )


def test_transport_cache_reuses_result_within_ttl():
    calls = {"count": 0}

    def _fake_fetch(**kwargs):
        calls["count"] += 1
        return _board(f"WNT-{calls['count']}")

    cached_fetch = build_transport_cache_fetcher(ttl_seconds=30, fetch_func=_fake_fetch)
    cached_fetch.clear()

    first = cached_fetch(station_code="WNT", calling_at="WAT", max_results=40)
    second = cached_fetch(station_code="WNT", calling_at="WAT", max_results=40)

    assert first.station_name == "WNT-1"
    assert second.station_name == "WNT-1"
    assert calls["count"] == 1


def test_transport_cache_refreshes_after_ttl_expiry():
    calls = {"count": 0}

    def _fake_fetch(**kwargs):
        calls["count"] += 1
        return _board(f"Refresh-{calls['count']}")

    cached_fetch = build_transport_cache_fetcher(ttl_seconds=1, fetch_func=_fake_fetch)
    cached_fetch.clear()

    first = cached_fetch(station_code="WNT", calling_at="WAT", max_results=40)
    time.sleep(1.2)
    second = cached_fetch(station_code="WNT", calling_at="WAT", max_results=40)

    assert first.station_name == "Refresh-1"
    assert second.station_name == "Refresh-2"
    assert calls["count"] == 2


def test_transport_leg_uses_prefetch_size_from_settings():
    settings = MagicMock()
    settings.transport_api_prefetch_departures = 40

    with patch("src.refresh.get_settings", return_value=settings):
        with patch("src.refresh.fetch_transport_api_cached", return_value=_board()) as mock_cached:
            fetch_transport_for_leg(origin_station_id="WNT", destination_station_id="WAT")

    mock_cached.assert_called_once_with(
        station_code="WNT",
        calling_at="WAT",
        max_results=40,
    )


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
