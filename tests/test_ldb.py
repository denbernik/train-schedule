from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.clients.ldb import (
    LdbApiError,
    _extract_arrival_time,
    _has_destination_in_calling_points,
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

_NOW = datetime.now()
_TODAY = _NOW.date()
_TEST_HOUR = (_NOW.hour + 1) % 24


def _dt(hour: int, minute: int) -> datetime:
    """Build a datetime on _TODAY for compact assertions."""
    return datetime(_TODAY.year, _TODAY.month, _TODAY.day, hour, minute)


def test_extract_arrival_time_flat_list_shape():
    h = _TEST_HOUR
    service = {
        "subsequentCallingPoints": [[
            {"locationName": "Clapham Junction", "crs": "CLJ", "st": f"{h}:30", "et": "On time"},
            {"locationName": "London Waterloo", "crs": "WAT", "st": f"{h}:40", "et": f"{h}:42"},
        ]]
    }
    result = _extract_arrival_time(service, "WAT", _TODAY)
    assert result == _dt(h, 42)


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
    result = _extract_arrival_time(service, "WAT", _TODAY)
    assert result == _dt(h, 43)


def test_extract_arrival_time_at_preferred_over_et_and_st():
    h = _TEST_HOUR
    service = {
        "subsequentCallingPoints": [[
            {"crs": "WAT", "st": f"{h}:40", "et": f"{h}:42", "at": f"{h}:41"},
        ]]
    }
    result = _extract_arrival_time(service, "WAT", _TODAY)
    assert result == _dt(h, 41)


def test_extract_arrival_time_et_on_time_falls_back_to_st():
    h = _TEST_HOUR
    service = {
        "subsequentCallingPoints": [[
            {"crs": "WAT", "st": f"{h}:40", "et": "On time"},
        ]]
    }
    result = _extract_arrival_time(service, "WAT", _TODAY)
    assert result == _dt(h, 40)


def test_extract_arrival_time_no_crs_match():
    h = _TEST_HOUR
    service = {
        "subsequentCallingPoints": [[
            {"crs": "CLJ", "st": f"{h}:30", "et": f"{h}:31"},
        ]]
    }
    result = _extract_arrival_time(service, "WAT", _TODAY)
    assert result is None


def test_extract_arrival_time_missing_keys():
    service = {
        "subsequentCallingPoints": [[
            {"crs": "WAT"},
        ]]
    }
    result = _extract_arrival_time(service, "WAT", _TODAY)
    assert result is None


def test_extract_arrival_time_case_insensitive_crs():
    h = _TEST_HOUR
    service = {
        "subsequentCallingPoints": [[
            {"crs": "wat", "st": f"{h}:40", "et": f"{h}:42"},
        ]]
    }
    result = _extract_arrival_time(service, "Wat", _TODAY)
    assert result == _dt(h, 42)


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
