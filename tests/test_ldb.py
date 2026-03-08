from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.clients.ldb import (
    LdbApiError,
    _destination_from_relevant_portion,
    _extract_arrival_time,
    _has_destination_in_calling_points,
    _parse_time_value,
    call_departure_board,
    detect_service_rows,
    fetch_departures,
)
from src.models import Departure, DepartureStatus


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.ldb_access_token = "test-token"
    settings.ldb_base_url = "https://api1.raildata.org.uk/1010-live-departure-board-dep1_2"
    settings.ldb_with_details_base_url = ""
    settings.ldb_api_version = "20220120"
    settings.ldb_timeout_seconds = 30
    settings.ldb_default_num_rows = 10
    settings.ldb_default_filter_type = "to"
    settings.ldb_default_time_offset = 0
    settings.ldb_default_time_window = 120
    settings.max_departures = 10
    return settings


def test_detect_service_rows_from_train_services_root():
    payload = {"locationName": "Wandsworth Town", "trainServices": [{"std": "14:23", "etd": "On time"}]}
    path, rows = detect_service_rows(payload)
    assert path == "trainServices"
    assert len(rows) == 1
    assert rows[0]["std"] == "14:23"


def test_detect_service_rows_from_wrapped_result():
    payload = {
        "GetDepartureBoardResult": {
            "locationName": "Wandsworth Town",
            "trainServices": [{"std": "14:30", "etd": "Cancelled"}],
        }
    }
    path, rows = detect_service_rows(payload)
    assert path == "GetDepartureBoardResult.trainServices"
    assert len(rows) == 1
    assert rows[0]["etd"] == "Cancelled"


@patch("src.clients.ldb.requests.get")
@patch("src.clients.ldb.get_settings")
def test_call_departure_board_raises_structured_403(mock_settings, mock_get):
    mock_settings.return_value = _settings()
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.text = "<html>403 Forbidden</html>"
    mock_get.return_value = mock_response

    with pytest.raises(LdbApiError) as exc:
        call_departure_board(crs="WNT", filter_crs="WAT")

    assert "unauthorised" in str(exc.value).lower()


@patch("src.clients.ldb.requests.get")
@patch("src.clients.ldb.get_settings")
def test_call_departure_board_handles_non_json(mock_settings, mock_get):
    mock_settings.return_value = _settings()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.side_effect = ValueError("no json")
    mock_get.return_value = mock_response

    with pytest.raises(LdbApiError) as exc:
        call_departure_board(crs="WNT")

    assert "non-json" in str(exc.value).lower()


@patch("src.clients.ldb.call_departure_board_with_details")
@patch("src.clients.ldb.get_settings")
def test_fetch_departures_maps_on_time_service(mock_settings, mock_call):
    mock_settings.return_value = _settings()
    mock_call.return_value = {
        "locationName": "Wandsworth Town",
        "trainServices": [
            {
                "std": "14:23",
                "etd": "On time",
                "platform": "2",
                "operator": "South Western Railway",
                "isCancelled": False,
                "destination": [{"locationName": "London Waterloo", "crs": "WAT"}],
                "subsequentCallingPoints": [[
                    {"crs": "WAT", "st": "14:40", "et": "On time"},
                ]],
            }
        ],
    }

    board = fetch_departures(crs="WNT", filter_crs="WAT")

    assert not board.has_error
    assert board.station_name == "Wandsworth Town"
    assert len(board.departures) == 1
    first = board.departures[0]
    assert first.destination == "London Waterloo"
    assert first.status == DepartureStatus.ON_TIME
    assert first.platform == "2"
    assert first.delay_minutes == 0


@patch("src.clients.ldb.call_departure_board_with_details")
@patch("src.clients.ldb.get_settings")
def test_fetch_departures_maps_cancelled_service(mock_settings, mock_call):
    mock_settings.return_value = _settings()
    mock_call.return_value = {
        "locationName": "Wandsworth Town",
        "trainServices": [
            {
                "std": "14:30",
                "etd": "Cancelled",
                "platform": "1",
                "operator": "South Western Railway",
                "isCancelled": True,
                "destination": [{"locationName": "London Waterloo", "crs": "WAT"}],
                "subsequentCallingPoints": [[
                    {"crs": "WAT", "st": "14:50", "et": "Cancelled"},
                ]],
            }
        ],
    }

    board = fetch_departures(crs="WNT", filter_crs="WAT")
    assert len(board.departures) == 1
    assert board.departures[0].status == DepartureStatus.CANCELLED


# --- _extract_arrival_time tests ---

_REFERENCE_NOW = datetime(2026, 3, 8, 23, 30)
_TEST_HOUR = 0


def _dt(hour: int, minute: int, *, day_offset: int = 0) -> datetime:
    """Build a datetime near _REFERENCE_NOW for compact assertions."""
    return _REFERENCE_NOW.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=day_offset)


def test_extract_arrival_time_flat_list_shape():
    h = _TEST_HOUR
    service = {
        "subsequentCallingPoints": [[
            {"locationName": "Clapham Junction", "crs": "CLJ", "st": f"{h}:30", "et": "On time"},
            {"locationName": "London Waterloo", "crs": "WAT", "st": f"{h}:40", "et": f"{h}:42"},
        ]]
    }
    result = _extract_arrival_time(service, "WAT", _REFERENCE_NOW)
    assert result == _dt(h, 42, day_offset=1)


def test_extract_arrival_time_wrapped_shape():
    h = _TEST_HOUR
    service = {
        "subsequentCallingPoints": [
            {"callingPoint": [
                {"locationName": "Clapham Junction", "crs": "CLJ", "st": f"{h}:30", "et": f"{h}:31"},
                {"locationName": "London Waterloo", "crs": "WAT", "st": f"{h}:40", "et": f"{h}:43"},
            ]}
        ]
    }
    result = _extract_arrival_time(service, "WAT", _REFERENCE_NOW)
    assert result == _dt(h, 43, day_offset=1)


def test_extract_arrival_time_at_preferred_over_et_and_st():
    h = _TEST_HOUR
    service = {
        "subsequentCallingPoints": [[
            {"crs": "WAT", "st": f"{h}:40", "et": f"{h}:42", "at": f"{h}:41"},
        ]]
    }
    result = _extract_arrival_time(service, "WAT", _REFERENCE_NOW)
    assert result == _dt(h, 41, day_offset=1)


def test_extract_arrival_time_et_on_time_falls_back_to_st():
    h = _TEST_HOUR
    service = {
        "subsequentCallingPoints": [[
            {"crs": "WAT", "st": f"{h}:40", "et": "On time"},
        ]]
    }
    result = _extract_arrival_time(service, "WAT", _REFERENCE_NOW)
    assert result == _dt(h, 40, day_offset=1)


def test_extract_arrival_time_no_crs_match():
    h = _TEST_HOUR
    service = {
        "subsequentCallingPoints": [[
            {"crs": "CLJ", "st": f"{h}:30", "et": f"{h}:31"},
        ]]
    }
    result = _extract_arrival_time(service, "WAT", _REFERENCE_NOW)
    assert result is None


def test_extract_arrival_time_missing_keys():
    service = {
        "subsequentCallingPoints": [[
            {"crs": "WAT"},
        ]]
    }
    result = _extract_arrival_time(service, "WAT", _REFERENCE_NOW)
    assert result is None


def test_extract_arrival_time_case_insensitive_crs():
    h = _TEST_HOUR
    service = {
        "subsequentCallingPoints": [[
            {"crs": "wat", "st": f"{h}:40", "et": f"{h}:42"},
        ]]
    }
    result = _extract_arrival_time(service, "Wat", _REFERENCE_NOW)
    assert result == _dt(h, 42, day_offset=1)


def test_extract_arrival_time_near_reference_stays_same_day():
    service = {
        "subsequentCallingPoints": [[
            {"crs": "WAT", "st": "23:45", "et": "23:46"},
        ]]
    }
    result = _extract_arrival_time(service, "WAT", _REFERENCE_NOW)
    assert result == _dt(23, 46)


def test_parse_time_value_rolls_to_next_day_when_far_past_reference():
    parsed = _parse_time_value("00:15", reference_now=_REFERENCE_NOW)
    assert parsed == _dt(0, 15, day_offset=1)


# --- _has_destination_in_calling_points tests ---

def test_has_destination_true_when_crs_present():
    service = {
        "subsequentCallingPoints": [[
            {"crs": "CLJ", "st": "14:30"},
            {"crs": "VXH", "st": "14:35"},
            {"crs": "WAT", "st": "14:40"},
        ]]
    }
    assert _has_destination_in_calling_points(service, "VXH") is True


def test_has_destination_false_when_crs_absent():
    service = {
        "subsequentCallingPoints": [[
            {"crs": "PUT", "st": "14:29"},
            {"crs": "BNS", "st": "14:33"},
            {"crs": "KNG", "st": "14:59"},
        ]]
    }
    assert _has_destination_in_calling_points(service, "VXH") is False


def test_has_destination_case_insensitive():
    service = {
        "subsequentCallingPoints": [[{"crs": "vxh", "st": "14:35"}]]
    }
    assert _has_destination_in_calling_points(service, "VXH") is True


def test_has_destination_false_when_no_calling_points():
    assert _has_destination_in_calling_points({}, "VXH") is False


def test_has_destination_wrapped_shape():
    service = {
        "subsequentCallingPoints": [
            {"callingPoint": [{"crs": "VXH", "st": "14:35"}]}
        ]
    }
    assert _has_destination_in_calling_points(service, "VXH") is True


# --- fetch_departures with-details integration tests ---

@patch("src.clients.ldb.call_departure_board_with_details")
@patch("src.clients.ldb.get_settings")
def test_fetch_departures_with_details_populates_arrival_time(mock_settings, mock_call):
    mock_settings.return_value = _settings()
    mock_call.return_value = {
        "locationName": "Wandsworth Town",
        "trainServices": [
            {
                "std": "14:23",
                "etd": "On time",
                "platform": "2",
                "operator": "South Western Railway",
                "isCancelled": False,
                "destination": [{"locationName": "London Waterloo", "crs": "WAT"}],
                "subsequentCallingPoints": [[
                    {"crs": "CLJ", "st": "14:30", "et": "14:31"},
                    {"crs": "WAT", "st": "14:40", "et": "14:42"},
                ]],
            }
        ],
    }

    board = fetch_departures(crs="WNT", filter_crs="WAT")
    assert len(board.departures) == 1
    dep = board.departures[0]
    assert dep.arrival_time is not None
    assert dep.arrival_time.hour == 14
    assert dep.arrival_time.minute == 42


@patch("src.clients.ldb.call_departure_board")
@patch("src.clients.ldb.call_departure_board_with_details")
@patch("src.clients.ldb.get_settings")
def test_fetch_departures_fallback_leaves_arrival_time_none(
    mock_settings, mock_with_details, mock_basic
):
    mock_settings.return_value = _settings()
    mock_with_details.side_effect = LdbApiError("not available")
    mock_basic.return_value = {
        "locationName": "Wandsworth Town",
        "trainServices": [
            {
                "std": "14:23",
                "etd": "On time",
                "platform": "2",
                "operator": "South Western Railway",
                "isCancelled": False,
                "destination": [{"locationName": "London Waterloo", "crs": "WAT"}],
            }
        ],
    }

    board = fetch_departures(crs="WNT", filter_crs="WAT")
    assert len(board.departures) == 1
    assert board.departures[0].arrival_time is None


@patch("src.clients.ldb.call_departure_board_with_details")
@patch("src.clients.ldb.get_settings")
def test_fetch_departures_filters_out_services_without_destination_in_calling_points(
    mock_settings, mock_call
):
    mock_settings.return_value = _settings()
    mock_call.return_value = {
        "locationName": "Wandsworth Town",
        "trainServices": [
            {
                "std": "14:23",
                "etd": "On time",
                "platform": "2",
                "operator": "South Western Railway",
                "isCancelled": False,
                "destination": [{"locationName": "London Waterloo", "crs": "WAT"}],
                "subsequentCallingPoints": [[
                    {"crs": "CLJ", "st": "14:30", "et": "14:31"},
                    {"crs": "VXH", "st": "14:35", "et": "14:35"},
                    {"crs": "WAT", "st": "14:40", "et": "14:42"},
                ]],
            },
            {
                "std": "14:27",
                "etd": "On time",
                "platform": "2",
                "operator": "South Western Railway",
                "isCancelled": False,
                "destination": [{"locationName": "Kingston", "crs": "KNG"}],
                "subsequentCallingPoints": [[
                    {"crs": "PUT", "st": "14:29", "et": "On time"},
                    {"crs": "BNS", "st": "14:33", "et": "On time"},
                    {"crs": "KNG", "st": "14:59", "et": "On time"},
                ]],
            },
        ],
    }

    board = fetch_departures(crs="WNT", filter_crs="VXH")
    assert len(board.departures) == 1
    assert board.departures[0].destination == "London Waterloo"
    assert board.departures[0].arrival_time is not None


# --- display_duration model tests ---

def test_display_duration_short_trip():
    dep = Departure(
        destination="London Waterloo",
        scheduled_time=datetime(2025, 6, 15, 14, 23),
        expected_time=datetime(2025, 6, 15, 14, 23),
        status=DepartureStatus.ON_TIME,
        arrival_time=datetime(2025, 6, 15, 14, 40),
    )
    assert dep.display_duration == "17 min"


def test_display_duration_long_trip():
    dep = Departure(
        destination="Exeter St Davids",
        scheduled_time=datetime(2025, 6, 15, 10, 0),
        expected_time=datetime(2025, 6, 15, 10, 0),
        status=DepartureStatus.ON_TIME,
        arrival_time=datetime(2025, 6, 15, 12, 30),
    )
    assert dep.display_duration == "2 h 30 min"


def test_display_duration_exact_hour():
    dep = Departure(
        destination="Reading",
        scheduled_time=datetime(2025, 6, 15, 10, 0),
        expected_time=datetime(2025, 6, 15, 10, 0),
        status=DepartureStatus.ON_TIME,
        arrival_time=datetime(2025, 6, 15, 11, 0),
    )
    assert dep.display_duration == "1 h"


def test_display_duration_none_when_no_arrival():
    dep = Departure(
        destination="London Waterloo",
        scheduled_time=datetime(2025, 6, 15, 14, 23),
        expected_time=datetime(2025, 6, 15, 14, 23),
        status=DepartureStatus.ON_TIME,
    )
    assert dep.display_duration is None


# --- _destination_from_relevant_portion tests ---

def test_destination_from_relevant_portion_split_service_flat_list():
    """Split service: WNT is before the split point (Weybridge appears in both
    portions), so function returns None to let the caller use the API destination."""
    service = {
        "destination": [{"locationName": "Addlestone", "crs": "ADS"}],
        "subsequentCallingPoints": [
            [
                {"locationName": "Vauxhall", "crs": "VXH", "st": "09:05"},
                {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09"},
                {"locationName": "Clapham Junction", "crs": "CLJ", "st": "09:12"},
                {"locationName": "Weybridge", "crs": "WEY", "st": "09:45"},
            ],
            [
                {"locationName": "Weybridge", "crs": "WEY", "st": "09:45"},
                {"locationName": "Byfleet & New Haw", "crs": "BYF", "st": "09:49"},
                {"locationName": "Addlestone", "crs": "ADS", "st": "09:53"},
            ],
        ],
    }
    assert _destination_from_relevant_portion(service, "WNT") is None


def test_destination_from_relevant_portion_split_service_wrapped_shape():
    """Same pre-split scenario but with wrapped callingPoint dicts."""
    service = {
        "destination": [{"locationName": "Addlestone", "crs": "ADS"}],
        "subsequentCallingPoints": [
            {"callingPoint": [
                {"locationName": "Vauxhall", "crs": "VXH", "st": "09:05"},
                {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09"},
                {"locationName": "Clapham Junction", "crs": "CLJ", "st": "09:12"},
                {"locationName": "Weybridge", "crs": "WEY", "st": "09:45"},
            ]},
            {"callingPoint": [
                {"locationName": "Weybridge", "crs": "WEY", "st": "09:45"},
                {"locationName": "Byfleet & New Haw", "crs": "BYF", "st": "09:49"},
                {"locationName": "Addlestone", "crs": "ADS", "st": "09:53"},
            ]},
        ],
    }
    assert _destination_from_relevant_portion(service, "WNT") is None


def test_destination_from_relevant_portion_post_split_returns_terminus():
    """When filter_crs is AFTER the split point (no shared stations after it
    in other portions), return the branch terminus."""
    service = {
        "subsequentCallingPoints": [
            [
                {"locationName": "Vauxhall", "crs": "VXH", "st": "09:05"},
                {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09"},
                {"locationName": "Weybridge", "crs": "WEY", "st": "09:45"},
                {"locationName": "Byfleet & New Haw", "crs": "BYF", "st": "09:49"},
                {"locationName": "Addlestone", "crs": "ADS", "st": "09:53"},
            ],
            [
                {"locationName": "Weybridge", "crs": "WEY", "st": "09:45"},
                {"locationName": "Woking", "crs": "WOK", "st": "09:58"},
            ],
        ],
    }
    # BYF is after the split (Weybridge) — only on the Addlestone branch
    assert _destination_from_relevant_portion(service, "BYF") == "Addlestone"


def test_destination_from_relevant_portion_through_service_to_addlestone():
    """Non-split service calling at WNT and continuing all the way to Addlestone
    should still return 'Addlestone'."""
    service = {
        "destination": [{"locationName": "Addlestone", "crs": "ADS"}],
        "subsequentCallingPoints": [
            [
                {"locationName": "Vauxhall", "crs": "VXH", "st": "09:05"},
                {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09"},
                {"locationName": "Clapham Junction", "crs": "CLJ", "st": "09:12"},
                {"locationName": "Weybridge", "crs": "WEY", "st": "09:45"},
                {"locationName": "Byfleet & New Haw", "crs": "BYF", "st": "09:49"},
                {"locationName": "Addlestone", "crs": "ADS", "st": "09:53"},
            ],
        ],
    }
    assert _destination_from_relevant_portion(service, "WNT") == "Addlestone"


def test_destination_from_relevant_portion_no_matching_portion():
    """No portion contains the filter CRS; function returns None."""
    service = {
        "subsequentCallingPoints": [
            [
                {"locationName": "Putney", "crs": "PUT", "st": "09:05"},
                {"locationName": "Barnes", "crs": "BNS", "st": "09:10"},
                {"locationName": "Kingston", "crs": "KNG", "st": "09:30"},
            ],
        ],
    }
    assert _destination_from_relevant_portion(service, "WNT") is None


def test_destination_from_relevant_portion_no_calling_points():
    """Missing subsequentCallingPoints; function returns None."""
    assert _destination_from_relevant_portion({}, "WNT") is None


def test_destination_from_relevant_portion_last_point_missing_location_name():
    """Last calling point has no locationName; function returns None."""
    service = {
        "subsequentCallingPoints": [
            [
                {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09"},
                {"crs": "WEY", "st": "09:45"},  # no locationName
            ],
        ],
    }
    assert _destination_from_relevant_portion(service, "WNT") is None


def test_destination_from_relevant_portion_multi_portion_match_returns_none():
    """When filter_crs appears in multiple portions (station is before the split),
    function returns None so caller falls through to the API-level destination."""
    service = {
        "destination": [
            {"locationName": "Woking", "crs": "WOK"},
            {"locationName": "Addlestone", "crs": "ADS"},
        ],
        "subsequentCallingPoints": [
            [
                {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09"},
                {"locationName": "Clapham Junction", "crs": "CLJ", "st": "09:12"},
                {"locationName": "Weybridge", "crs": "WEY", "st": "09:45"},
                {"locationName": "Addlestone", "crs": "ADS", "st": "09:53"},
            ],
            [
                {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09"},
                {"locationName": "Clapham Junction", "crs": "CLJ", "st": "09:12"},
                {"locationName": "Weybridge", "crs": "WEY", "st": "09:45"},
                {"locationName": "Woking", "crs": "WOK", "st": "09:58"},
            ],
        ],
    }
    assert _destination_from_relevant_portion(service, "WNT") is None


def test_destination_from_relevant_portion_multi_portion_match_wrapped_returns_none():
    """Same as above but with wrapped callingPoint dict format."""
    service = {
        "subsequentCallingPoints": [
            {"callingPoint": [
                {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09"},
                {"locationName": "Clapham Junction", "crs": "CLJ", "st": "09:12"},
                {"locationName": "Addlestone", "crs": "ADS", "st": "09:53"},
            ]},
            {"callingPoint": [
                {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09"},
                {"locationName": "Clapham Junction", "crs": "CLJ", "st": "09:12"},
                {"locationName": "Woking", "crs": "WOK", "st": "09:58"},
            ]},
        ],
    }
    assert _destination_from_relevant_portion(service, "WNT") is None


@patch("src.clients.ldb.call_departure_board_with_details")
@patch("src.clients.ldb.get_settings")
def test_split_service_consistent_destination_regardless_of_origin(
    mock_settings, mock_call
):
    """The same split service should show the same destination whether queried
    from WAT or VXH. When filter_crs (WNT) is before the split point and
    appears in multiple portions, fall through to the API destination field."""
    mock_settings.return_value = _settings()

    # Simulate querying from WAT: API puts Addlestone branch first
    mock_call.return_value = {
        "locationName": "London Waterloo",
        "trainServices": [{
            "std": "09:00",
            "etd": "On time",
            "platform": "4",
            "operator": "South Western Railway",
            "isCancelled": False,
            "destination": [{"locationName": "Woking", "crs": "WOK"}],
            "subsequentCallingPoints": [
                [
                    {"locationName": "Vauxhall", "crs": "VXH", "st": "09:05", "et": "On time"},
                    {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09", "et": "On time"},
                    {"locationName": "Weybridge", "crs": "WEY", "st": "09:45", "et": "On time"},
                    {"locationName": "Addlestone", "crs": "ADS", "st": "09:53", "et": "On time"},
                ],
                [
                    {"locationName": "Vauxhall", "crs": "VXH", "st": "09:05", "et": "On time"},
                    {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09", "et": "On time"},
                    {"locationName": "Weybridge", "crs": "WEY", "st": "09:45", "et": "On time"},
                    {"locationName": "Woking", "crs": "WOK", "st": "09:58", "et": "On time"},
                ],
            ],
        }],
    }
    board_from_wat = fetch_departures(crs="WAT", filter_crs="WNT")

    # Simulate querying from VXH: API puts Woking branch first
    mock_call.return_value = {
        "locationName": "Vauxhall",
        "trainServices": [{
            "std": "09:05",
            "etd": "On time",
            "platform": "2",
            "operator": "South Western Railway",
            "isCancelled": False,
            "destination": [{"locationName": "Woking", "crs": "WOK"}],
            "subsequentCallingPoints": [
                [
                    {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09", "et": "On time"},
                    {"locationName": "Weybridge", "crs": "WEY", "st": "09:45", "et": "On time"},
                    {"locationName": "Woking", "crs": "WOK", "st": "09:58", "et": "On time"},
                ],
                [
                    {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09", "et": "On time"},
                    {"locationName": "Weybridge", "crs": "WEY", "st": "09:45", "et": "On time"},
                    {"locationName": "Addlestone", "crs": "ADS", "st": "09:53", "et": "On time"},
                ],
            ],
        }],
    }
    board_from_vxh = fetch_departures(crs="VXH", filter_crs="WNT")

    # Both should show "Woking" (the API-level destination)
    assert board_from_wat.departures[0].destination == "Woking"
    assert board_from_vxh.departures[0].destination == "Woking"
    assert board_from_wat.departures[0].destination == board_from_vxh.departures[0].destination


@patch("src.clients.ldb.call_departure_board")
@patch("src.clients.ldb.call_departure_board_with_details")
@patch("src.clients.ldb.get_settings")
def test_fetch_departures_fallback_keeps_services_without_calling_points(
    mock_settings, mock_with_details, mock_basic
):
    """When GetDepBoardWithDetails fails, services without subsequentCallingPoints
    should still be included (the basic endpoint already filtered server-side)."""
    mock_settings.return_value = _settings()
    mock_with_details.side_effect = LdbApiError("not available")
    mock_basic.return_value = {
        "locationName": "London Waterloo",
        "trainServices": [
            {
                "std": "09:00",
                "etd": "On time",
                "platform": "4",
                "operator": "South Western Railway",
                "isCancelled": False,
                "destination": [{"locationName": "Woking", "crs": "WOK"}],
            }
        ],
    }
    board = fetch_departures(crs="WAT", filter_crs="WNT")
    assert len(board.departures) == 1
    assert board.departures[0].destination == "Woking"


@patch("src.clients.ldb.call_departure_board_with_details")
@patch("src.clients.ldb.get_settings")
def test_fetch_departures_split_service_pre_split_uses_api_destination(
    mock_settings, mock_call
):
    """End-to-end: split service from WAT with filter WNT — WNT is before the
    split point (Weybridge shared), so the API-level destination is used."""
    mock_settings.return_value = _settings()
    mock_call.return_value = {
        "locationName": "London Waterloo",
        "trainServices": [
            {
                "std": "09:00",
                "etd": "On time",
                "platform": "4",
                "operator": "South Western Railway",
                "isCancelled": False,
                "destination": [{"locationName": "Woking", "crs": "WOK"}],
                "subsequentCallingPoints": [
                    [
                        {"locationName": "Vauxhall", "crs": "VXH", "st": "09:05", "et": "On time"},
                        {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09", "et": "On time"},
                        {"locationName": "Clapham Junction", "crs": "CLJ", "st": "09:12", "et": "On time"},
                        {"locationName": "Weybridge", "crs": "WEY", "st": "09:45", "et": "On time"},
                        {"locationName": "Addlestone", "crs": "ADS", "st": "09:53", "et": "On time"},
                    ],
                    [
                        {"locationName": "Weybridge", "crs": "WEY", "st": "09:45", "et": "On time"},
                        {"locationName": "Woking", "crs": "WOK", "st": "09:58", "et": "On time"},
                    ],
                ],
            }
        ],
    }

    board = fetch_departures(crs="WAT", filter_crs="WNT")
    assert len(board.departures) == 1
    assert board.departures[0].destination == "Woking"


# --- display_platform model tests ---

def _dep(**kwargs) -> Departure:
    """Helper: minimal Departure with overrideable fields."""
    defaults = dict(
        destination="London Waterloo",
        scheduled_time=datetime(2025, 6, 15, 14, 23),
        expected_time=datetime(2025, 6, 15, 14, 23),
        status=DepartureStatus.ON_TIME,
    )
    return Departure(**{**defaults, **kwargs})


# National Rail numeric / alphanumeric platforms
def test_display_platform_nr_numeric():
    assert _dep(platform="2").display_platform == "plat. 2"

def test_display_platform_nr_alphanumeric():
    assert _dep(platform="3A").display_platform == "plat. 3A"

def test_display_platform_nr_two_digits():
    assert _dep(platform="12").display_platform == "plat. 12"

# TfL live compass + platform
def test_display_platform_tfl_eastbound():
    assert _dep(platform="Eastbound - Platform 1").display_platform == "plat. 1 - E/B"

def test_display_platform_tfl_westbound():
    assert _dep(platform="Westbound - Platform 3").display_platform == "plat. 3 - W/B"

def test_display_platform_tfl_northbound():
    assert _dep(platform="Northbound - Platform 2").display_platform == "plat. 2 - N/B"

def test_display_platform_tfl_southbound():
    assert _dep(platform="Southbound - Platform 4").display_platform == "plat. 4 - S/B"

# TfL compass direction only (no platform number)
def test_display_platform_direction_only_eastbound():
    assert _dep(platform="Eastbound").display_platform == "E/B"

def test_display_platform_direction_only_westbound():
    assert _dep(platform="Westbound").display_platform == "W/B"

# TfL timetable entries — direction is surfaced, not hidden
def test_display_platform_timetable_outbound():
    assert _dep(platform="Outbound (Timetable)").display_platform == "Outbound"

def test_display_platform_timetable_inbound():
    assert _dep(platform="Inbound (Timetable)").display_platform == "Inbound"

# TfL timetable entries with compass directions (new: line-level compass map)
def test_display_platform_timetable_southbound():
    assert _dep(platform="Southbound (Timetable)").display_platform == "S/B"

def test_display_platform_timetable_northbound():
    assert _dep(platform="Northbound (Timetable)").display_platform == "N/B"

def test_display_platform_timetable_eastbound():
    assert _dep(platform="Eastbound (Timetable)").display_platform == "E/B"

def test_display_platform_timetable_westbound():
    assert _dep(platform="Westbound (Timetable)").display_platform == "W/B"

# Absent / empty
def test_display_platform_none():
    assert _dep(platform=None).display_platform is None

def test_display_platform_empty_string():
    assert _dep(platform="").display_platform is None


# --- platform integration tests (end-to-end via fetch_departures) ---

@patch("src.clients.ldb.call_departure_board_with_details")
@patch("src.clients.ldb.get_settings")
def test_platform_preserved_from_api_response(mock_settings, mock_call):
    """Platform field from GetDepBoardWithDetails passes through to Departure.platform."""
    mock_settings.return_value = _settings()
    mock_call.return_value = {
        "trainServices": [{
            "std": "09:00",
            "etd": "On time",
            "platform": "4",
            "operator": "South Western Railway",
            "isCancelled": False,
            "destination": [{"locationName": "Vauxhall", "crs": "VXH"}],
            "subsequentCallingPoints": [[
                {"locationName": "Vauxhall", "crs": "VXH", "st": "09:05", "et": "On time"},
            ]],
        }],
    }
    board = fetch_departures(crs="WAT", filter_crs="VXH")
    assert board.departures[0].platform == "4"
    assert board.departures[0].display_platform == "plat. 4"


@patch("src.clients.ldb.call_departure_board_with_details")
@patch("src.clients.ldb.get_settings")
def test_platform_none_when_absent(mock_settings, mock_call):
    """When platform key is absent (Waterloo late-allocation), platform is None."""
    mock_settings.return_value = _settings()
    mock_call.return_value = {
        "trainServices": [{
            "std": "09:00",
            "etd": "On time",
            "operator": "South Western Railway",
            "isCancelled": False,
            "destination": [{"locationName": "Vauxhall", "crs": "VXH"}],
            "subsequentCallingPoints": [[
                {"locationName": "Vauxhall", "crs": "VXH", "st": "09:05", "et": "On time"},
            ]],
        }],
    }
    board = fetch_departures(crs="WAT", filter_crs="VXH")
    assert board.departures[0].platform is None
    assert board.departures[0].display_platform is None


@patch("src.clients.ldb.call_departure_board_with_details")
@patch("src.clients.ldb.get_settings")
def test_split_service_pre_split_destination_and_platform(mock_settings, mock_call):
    """Split service: WNT is pre-split, so API destination is used. Platform preserved."""
    mock_settings.return_value = _settings()
    mock_call.return_value = {
        "trainServices": [{
            "std": "09:00",
            "etd": "On time",
            "platform": "4",
            "operator": "South Western Railway",
            "isCancelled": False,
            "destination": [{"locationName": "Woking", "crs": "WOK"}],
            "subsequentCallingPoints": [
                [
                    {"locationName": "Vauxhall", "crs": "VXH", "st": "09:05", "et": "On time"},
                    {"locationName": "Wandsworth Town", "crs": "WNT", "st": "09:09", "et": "On time"},
                    {"locationName": "Weybridge", "crs": "WEY", "st": "09:45", "et": "On time"},
                    {"locationName": "Addlestone", "crs": "ADS", "st": "09:53", "et": "On time"},
                ],
                [
                    {"locationName": "Weybridge", "crs": "WEY", "st": "09:45", "et": "On time"},
                    {"locationName": "Woking", "crs": "WOK", "st": "09:58", "et": "On time"},
                ],
            ],
        }],
    }
    board = fetch_departures(crs="WAT", filter_crs="WNT")
    dep = board.departures[0]
    assert dep.destination == "Woking"
    assert dep.platform == "4"
    assert dep.display_platform == "plat. 4"


@patch("src.clients.ldb.call_departure_board_with_details")
@patch("src.clients.ldb.get_settings")
def test_multiple_trains_mixed_platform_presence(mock_settings, mock_call):
    """Multiple trains: platform shown where present, None where absent."""
    mock_settings.return_value = _settings()
    mock_call.return_value = {
        "trainServices": [
            {
                "std": "09:00",
                "etd": "On time",
                "platform": "4",
                "operator": "South Western Railway",
                "isCancelled": False,
                "destination": [{"locationName": "Vauxhall", "crs": "VXH"}],
                "subsequentCallingPoints": [[
                    {"locationName": "Vauxhall", "crs": "VXH", "st": "09:05", "et": "On time"},
                ]],
            },
            {
                "std": "09:15",
                "etd": "On time",
                "operator": "South Western Railway",
                "isCancelled": False,
                "destination": [{"locationName": "Vauxhall", "crs": "VXH"}],
                "subsequentCallingPoints": [[
                    {"locationName": "Vauxhall", "crs": "VXH", "st": "09:20", "et": "On time"},
                ]],
            },
        ],
    }
    board = fetch_departures(crs="WAT", filter_crs="VXH")
    assert board.departures[0].platform == "4"
    assert board.departures[0].display_platform == "plat. 4"
    assert board.departures[1].platform is None
    assert board.departures[1].display_platform is None
