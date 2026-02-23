from unittest.mock import patch, MagicMock

import pytest

from src.clients.transport_api import fetch_departures, _parse_departures
from src.models import DepartureStatus


SAMPLE_RESPONSE = {
    "station_name": "Wandsworth Town",
    "departures": {
        "all": [
            {
                "aimed_departure_time": "14:23",
                "expected_departure_time": "On time",
                "status": "ON TIME",
                "platform": "2",
                "destination_name": "London Waterloo",
                "operator_name": "South Western Railway",
                "train_uid": "W12345",
            },
            {
                "aimed_departure_time": "14:30",
                "expected_departure_time": "14:35",
                "status": "LATE",
                "platform": "1",
                "destination_name": "Hounslow",
                "operator_name": "South Western Railway",
                "train_uid": "W12346",
            },
        ]
    },
}


class TestCallingAtParameter:
    """Verify calling_at is forwarded to the TransportAPI query string."""

    @patch("src.clients.transport_api.requests.get")
    @patch("src.clients.transport_api.get_settings")
    def test_calling_at_included_in_params(self, mock_settings, mock_get):
        settings = MagicMock()
        settings.transport_api_app_id = "test_id"
        settings.transport_api_app_key = "test_key"
        settings.national_rail_station_code = "WNT"
        settings.max_departures = 10
        mock_settings.return_value = settings

        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_RESPONSE
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        fetch_departures(station_code="WNT", calling_at="WAT")

        mock_get.assert_called_once()
        actual_params = mock_get.call_args.kwargs.get(
            "params", mock_get.call_args[1].get("params", {})
        )
        assert actual_params["calling_at"] == "WAT"

    @patch("src.clients.transport_api.requests.get")
    @patch("src.clients.transport_api.get_settings")
    def test_calling_at_omitted_when_none(self, mock_settings, mock_get):
        settings = MagicMock()
        settings.transport_api_app_id = "test_id"
        settings.transport_api_app_key = "test_key"
        settings.national_rail_station_code = "WNT"
        settings.max_departures = 10
        mock_settings.return_value = settings

        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_RESPONSE
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        fetch_departures(station_code="WNT")

        actual_params = mock_get.call_args.kwargs.get(
            "params", mock_get.call_args[1].get("params", {})
        )
        assert "calling_at" not in actual_params

    @patch("src.clients.transport_api.requests.get")
    @patch("src.clients.transport_api.get_settings")
    def test_waterloo_routes_putney_vs_wandsworth_town(self, mock_settings, mock_get):
        """
        From Waterloo, some services that call at Putney do not call at
        Wandsworth Town. We model that with a Windsor & Eton service.
        """
        settings = MagicMock()
        settings.transport_api_app_id = "test_id"
        settings.transport_api_app_key = "test_key"
        settings.national_rail_station_code = "WAT"
        settings.max_departures = 20
        mock_settings.return_value = settings

        putney_response = {
            "station_name": "London Waterloo",
            "departures": {
                "all": [
                    {
                        "aimed_departure_time": "14:10",
                        "expected_departure_time": "On time",
                        "status": "ON TIME",
                        "platform": "10",
                        "destination_name": "Windsor & Eton Riverside",
                        "operator_name": "South Western Railway",
                        "train_uid": "FAST1",
                    },
                    {
                        "aimed_departure_time": "14:15",
                        "expected_departure_time": "On time",
                        "status": "ON TIME",
                        "platform": "12",
                        "destination_name": "Shepperton",
                        "operator_name": "South Western Railway",
                        "train_uid": "SLOW1",
                    },
                ]
            },
        }

        wandsworth_town_response = {
            "station_name": "London Waterloo",
            "departures": {
                "all": [
                    {
                        "aimed_departure_time": "14:15",
                        "expected_departure_time": "On time",
                        "status": "ON TIME",
                        "platform": "12",
                        "destination_name": "Shepperton",
                        "operator_name": "South Western Railway",
                        "train_uid": "SLOW1",
                    }
                ]
            },
        }

        def _mock_get(url, params, timeout):
            mock_response = MagicMock()
            mock_response.raise_for_status.return_value = None
            if params.get("calling_at") == "PUT":
                mock_response.json.return_value = putney_response
            elif params.get("calling_at") == "WNT":
                mock_response.json.return_value = wandsworth_town_response
            else:
                raise AssertionError(f"Unexpected calling_at value: {params.get('calling_at')}")
            return mock_response

        mock_get.side_effect = _mock_get

        board_putney = fetch_departures(station_code="WAT", calling_at="PUT", max_results=20)
        board_wandsworth = fetch_departures(station_code="WAT", calling_at="WNT", max_results=20)

        putney_uids = {dep.operator + ":" + dep.destination for dep in board_putney.departures}
        wandsworth_uids = {dep.operator + ":" + dep.destination for dep in board_wandsworth.departures}

        assert "South Western Railway:Windsor & Eton Riverside" in putney_uids
        assert "South Western Railway:Windsor & Eton Riverside" not in wandsworth_uids


class TestParseDepartures:
    """Verify _parse_departures produces correct Departure objects."""

    def test_parses_on_time_departure(self):
        departures = _parse_departures(SAMPLE_RESPONSE)
        assert len(departures) == 2

        first = departures[0]
        assert first.destination == "London Waterloo"
        assert first.status == DepartureStatus.ON_TIME
        assert first.platform == "2"
        assert first.delay_minutes == 0

    def test_parses_delayed_departure(self):
        departures = _parse_departures(SAMPLE_RESPONSE)
        delayed = departures[1]
        assert delayed.destination == "Hounslow"
        assert delayed.status == DepartureStatus.DELAYED
        assert delayed.delay_minutes == 5

    def test_handles_empty_departures(self):
        response = {"departures": {"all": []}}
        assert _parse_departures(response) == []

    def test_handles_null_departures(self):
        response = {"departures": {"all": None}}
        assert _parse_departures(response) == []

    def test_skips_malformed_entry(self):
        response = {
            "departures": {
                "all": [
                    {"aimed_departure_time": "14:23", "expected_departure_time": "On time",
                     "status": "ON TIME", "destination_name": "London Waterloo",
                     "train_uid": "W1"},
                    {"bad_field": "missing required data"},
                ]
            }
        }
        departures = _parse_departures(response)
        assert len(departures) == 1
