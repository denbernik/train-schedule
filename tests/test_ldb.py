from unittest.mock import MagicMock, patch

import pytest

from src.clients.ldb import LdbApiError, call_departure_board, detect_service_rows


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.ldb_access_token = "test-token"
    settings.ldb_base_url = "https://api1.raildata.org.uk/1010-live-departure-board-dep1_2"
    settings.ldb_api_version = "20220120"
    settings.ldb_timeout_seconds = 30
    settings.ldb_default_num_rows = 10
    settings.ldb_default_filter_type = "to"
    settings.ldb_default_time_offset = 0
    settings.ldb_default_time_window = 120
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
