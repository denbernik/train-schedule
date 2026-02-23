from unittest.mock import MagicMock, patch

import requests

from src.clients.tfl import _filter_arrivals_for_destination, fetch_departures
from src.clients.tfl_topology import TopologyUnavailableError

EAST_PUTNEY = "940GZZLUEPY"
EARLS_COURT = "940GZZLUECT"
PADDINGTON_CIRCLE = "940GZZLUPAC"
EDGWARE_ROAD_DISTRICT = "940GZZLUERC"
EDGWARE_ROAD_BAKERLOO = "940GZZLUERB"
UPMINSTER = "940GZZLUUPM"
TOWER_HILL = "940GZZLUTWH"


def _arrival(terminal_id: str, destination_name: str) -> dict:
    return {
        "id": f"arrival-{terminal_id}",
        "stationName": "East Putney Underground Station",
        "lineId": "district",
        "lineName": "District",
        "modeName": "tube",
        "destinationNaptanId": terminal_id,
        "destinationName": destination_name,
        "expectedArrival": "2026-02-23T10:00:00Z",
        "platformName": "Eastbound - Platform 1",
    }


def _arrival_in(minutes_from_now: int, terminal_id: str, destination_name: str) -> dict:
    from datetime import datetime, timedelta, timezone

    expected = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    return {
        "id": f"arrival-{terminal_id}-{minutes_from_now}",
        "stationName": "East Putney Underground Station",
        "lineId": "district",
        "lineName": "District",
        "modeName": "tube",
        "destinationNaptanId": terminal_id,
        "destinationName": destination_name,
        "expectedArrival": expected.isoformat().replace("+00:00", "Z"),
        "platformName": "Eastbound - Platform 1",
        "direction": "outbound",
    }


def _timetable_payload_from_now(minutes_from_now: list[int]) -> dict:
    from datetime import datetime, timedelta

    now = datetime.now()
    known_journeys = []
    for minute_offset in minutes_from_now:
        point = now + timedelta(minutes=minute_offset)
        known_journeys.append(
            {"hour": point.hour, "minute": point.minute, "intervalId": "int-upminster"}
        )

    return {
        "lineName": "District",
        "stops": [
            {"id": EAST_PUTNEY, "name": "East Putney Underground Station"},
            {"id": EARLS_COURT, "name": "Earl's Court Underground Station"},
            {"id": UPMINSTER, "name": "Upminster Underground Station"},
            {"id": TOWER_HILL, "name": "Tower Hill Underground Station"},
        ],
        "timetable": {
            "routes": [
                {
                    "stationIntervals": [
                        {
                            "id": "int-upminster",
                            "intervals": [
                                {"stopId": EARLS_COURT, "timeToArrival": 180},
                                {"stopId": UPMINSTER, "timeToArrival": 3600},
                            ],
                        },
                        {
                            "id": "int-tower",
                            "intervals": [
                                {"stopId": EARLS_COURT, "timeToArrival": 180},
                                {"stopId": TOWER_HILL, "timeToArrival": 1800},
                            ],
                        },
                    ],
                    "schedules": [{"knownJourneys": known_journeys}],
                }
            ]
        },
    }


RAW_ARRIVALS = [
    _arrival(UPMINSTER, "Upminster"),
    _arrival(TOWER_HILL, "Tower Hill"),
    _arrival(EDGWARE_ROAD_DISTRICT, "Edgware Road"),
]


class FakeTopologyProvider:
    def __init__(self):
        self.lines = {
            "district": [
                [EAST_PUTNEY, EARLS_COURT, PADDINGTON_CIRCLE, EDGWARE_ROAD_DISTRICT],
                [EAST_PUTNEY, EARLS_COURT, TOWER_HILL],
                [EAST_PUTNEY, EARLS_COURT, TOWER_HILL, UPMINSTER],
            ]
        }

    def has_path(self, line_id: str, origin_station_id: str, destination_station_id: str) -> bool:
        for sequence in self.lines.get(line_id.lower(), []):
            if origin_station_id in sequence and destination_station_id in sequence:
                if sequence.index(origin_station_id) < sequence.index(destination_station_id):
                    return True
        return False

    def service_passes_through(
        self,
        line_id: str,
        origin_station_id: str,
        destination_station_id: str,
        terminal_station_id: str,
    ) -> bool:
        for sequence in self.lines.get(line_id.lower(), []):
            if (
                origin_station_id in sequence
                and destination_station_id in sequence
                and terminal_station_id in sequence
            ):
                origin_index = sequence.index(origin_station_id)
                destination_index = sequence.index(destination_station_id)
                terminal_index = sequence.index(terminal_station_id)
                if origin_index < destination_index <= terminal_index:
                    return True
        return False


class UnavailableTopologyProvider:
    def has_path(self, line_id: str, origin_station_id: str, destination_station_id: str) -> bool:
        raise TopologyUnavailableError("topology unavailable")

    def service_passes_through(
        self,
        line_id: str,
        origin_station_id: str,
        destination_station_id: str,
        terminal_station_id: str,
    ) -> bool:
        raise TopologyUnavailableError("topology unavailable")


@patch("src.clients.tfl._get_topology_provider", return_value=FakeTopologyProvider())
def test_east_putney_to_earls_court_is_valid(mock_provider):
    filtered, error = _filter_arrivals_for_destination(
        raw_arrivals=RAW_ARRIVALS,
        origin_station_id=EAST_PUTNEY,
        destination_station_id=EARLS_COURT,
        api_key="test",
    )
    assert error is None
    assert len(filtered) == 3


@patch("src.clients.tfl._get_topology_provider", return_value=FakeTopologyProvider())
def test_east_putney_to_paddington_is_valid(mock_provider):
    filtered, error = _filter_arrivals_for_destination(
        raw_arrivals=RAW_ARRIVALS,
        origin_station_id=EAST_PUTNEY,
        destination_station_id=PADDINGTON_CIRCLE,
        api_key="test",
    )
    assert error is None
    assert len(filtered) == 1
    assert filtered[0]["destinationNaptanId"] == EDGWARE_ROAD_DISTRICT


@patch("src.clients.tfl._get_topology_provider", return_value=FakeTopologyProvider())
def test_east_putney_to_edgware_road_district_is_valid(mock_provider):
    filtered, error = _filter_arrivals_for_destination(
        raw_arrivals=RAW_ARRIVALS,
        origin_station_id=EAST_PUTNEY,
        destination_station_id=EDGWARE_ROAD_DISTRICT,
        api_key="test",
    )
    assert error is None
    assert len(filtered) == 1
    assert filtered[0]["destinationNaptanId"] == EDGWARE_ROAD_DISTRICT


@patch("src.clients.tfl._get_topology_provider", return_value=FakeTopologyProvider())
def test_east_putney_to_edgware_road_bakerloo_returns_error(mock_provider):
    filtered, error = _filter_arrivals_for_destination(
        raw_arrivals=RAW_ARRIVALS,
        origin_station_id=EAST_PUTNEY,
        destination_station_id=EDGWARE_ROAD_BAKERLOO,
        api_key="test",
    )
    assert filtered == []
    assert error is not None
    assert "not reachable" in error


@patch("src.clients.tfl._get_topology_provider", return_value=FakeTopologyProvider())
def test_east_putney_to_upminster_excludes_tower_hill(mock_provider):
    filtered, error = _filter_arrivals_for_destination(
        raw_arrivals=RAW_ARRIVALS,
        origin_station_id=EAST_PUTNEY,
        destination_station_id=UPMINSTER,
        api_key="test",
    )
    assert error is None
    assert len(filtered) == 1
    assert filtered[0]["destinationNaptanId"] == UPMINSTER


def test_no_destination_keeps_existing_behavior():
    filtered, error = _filter_arrivals_for_destination(
        raw_arrivals=RAW_ARRIVALS,
        origin_station_id=EAST_PUTNEY,
        destination_station_id=None,
        api_key="test",
    )
    assert error is None
    assert filtered == RAW_ARRIVALS


@patch("src.clients.tfl._get_topology_provider", return_value=UnavailableTopologyProvider())
def test_topology_unavailable_falls_back_to_unfiltered_arrivals(mock_provider):
    filtered, error = _filter_arrivals_for_destination(
        raw_arrivals=RAW_ARRIVALS,
        origin_station_id=EAST_PUTNEY,
        destination_station_id=EARLS_COURT,
        api_key="test",
    )
    assert error is None
    assert filtered == RAW_ARRIVALS


@patch("src.clients.tfl._get_topology_provider", return_value=FakeTopologyProvider())
@patch("src.clients.tfl._call_api", return_value=RAW_ARRIVALS)
@patch("src.clients.tfl.get_settings")
def test_fetch_departures_returns_explicit_error_for_invalid_leg(
    mock_settings,
    mock_call_api,
    mock_provider,
):
    settings = MagicMock()
    settings.tfl_station_id = EAST_PUTNEY
    settings.max_departures = 10
    settings.tfl_api_key = "test-key"
    mock_settings.return_value = settings

    board = fetch_departures(
        station_id=EAST_PUTNEY,
        destination_station_id=EDGWARE_ROAD_BAKERLOO,
    )

    assert board.has_error
    assert "not reachable" in (board.error_message or "")
    assert board.departures == []


@patch("src.clients.tfl._get_topology_provider", return_value=FakeTopologyProvider())
@patch("src.clients.tfl._call_timetable_api")
@patch("src.clients.tfl._call_api")
@patch("src.clients.tfl.get_settings")
def test_live_at_or_above_window_does_not_query_timetable(
    mock_settings,
    mock_call_api,
    mock_call_timetable_api,
    mock_provider,
):
    live_rows = [_arrival_in(i + 1, UPMINSTER, "Upminster") for i in range(15)]
    mock_call_api.return_value = live_rows

    settings = MagicMock()
    settings.tfl_station_id = EAST_PUTNEY
    settings.max_departures = 5
    settings.tfl_max_departures = 15
    settings.tfl_api_key = "test-key"
    mock_settings.return_value = settings

    board = fetch_departures(
        station_id=EAST_PUTNEY,
        destination_station_id=EARLS_COURT,
        max_results=15,
    )

    assert not board.has_error
    assert len(board.departures) == 15
    mock_call_timetable_api.assert_not_called()


@patch("src.clients.tfl._get_topology_provider", return_value=FakeTopologyProvider())
@patch("src.clients.tfl._call_timetable_api")
@patch("src.clients.tfl._call_api")
@patch("src.clients.tfl.get_settings")
def test_live_below_window_fills_from_timetable_to_target(
    mock_settings,
    mock_call_api,
    mock_call_timetable_api,
    mock_provider,
):
    live_rows = [_arrival_in(2, UPMINSTER, "Upminster"), _arrival_in(7, UPMINSTER, "Upminster")]
    mock_call_api.return_value = live_rows
    mock_call_timetable_api.return_value = _timetable_payload_from_now(
        [10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34]
    )

    settings = MagicMock()
    settings.tfl_station_id = EAST_PUTNEY
    settings.max_departures = 5
    settings.tfl_max_departures = 15
    settings.tfl_api_key = "test-key"
    mock_settings.return_value = settings

    board = fetch_departures(
        station_id=EAST_PUTNEY,
        destination_station_id=EARLS_COURT,
        max_results=15,
    )

    assert not board.has_error
    assert len(board.departures) == 15
    # Timetable should have been queried at least once when live rows were insufficient
    assert mock_call_timetable_api.call_count >= 1


@patch("src.clients.tfl._get_topology_provider", return_value=FakeTopologyProvider())
@patch("src.clients.tfl._call_timetable_api", side_effect=requests.RequestException("boom"))
@patch("src.clients.tfl._call_api")
@patch("src.clients.tfl.get_settings")
def test_timetable_failure_falls_back_to_live_only(
    mock_settings,
    mock_call_api,
    mock_call_timetable_api,
    mock_provider,
):
    live_rows = [_arrival_in(3, UPMINSTER, "Upminster"), _arrival_in(8, UPMINSTER, "Upminster")]
    mock_call_api.return_value = live_rows

    settings = MagicMock()
    settings.tfl_station_id = EAST_PUTNEY
    settings.max_departures = 5
    settings.tfl_max_departures = 15
    settings.tfl_api_key = "test-key"
    mock_settings.return_value = settings

    board = fetch_departures(
        station_id=EAST_PUTNEY,
        destination_station_id=EARLS_COURT,
        max_results=15,
    )

    assert not board.has_error
    assert len(board.departures) == 2
    assert mock_call_timetable_api.call_count >= 1


@patch("src.clients.tfl._get_topology_provider", return_value=FakeTopologyProvider())
@patch("src.clients.tfl._call_timetable_api")
@patch("src.clients.tfl._call_api")
@patch("src.clients.tfl.get_settings")
def test_merge_deduplicates_live_and_timetable_rows(
    mock_settings,
    mock_call_api,
    mock_call_timetable_api,
    mock_provider,
):
    live_rows = [
        _arrival_in(5, UPMINSTER, "Upminster"),
        _arrival_in(10, UPMINSTER, "Upminster"),
    ]
    mock_call_api.return_value = live_rows
    # Include a timetable journey at +10 min that should dedupe against live row.
    mock_call_timetable_api.return_value = _timetable_payload_from_now([10, 12, 14, 16, 18, 20])

    settings = MagicMock()
    settings.tfl_station_id = EAST_PUTNEY
    settings.max_departures = 5
    settings.tfl_max_departures = 15
    settings.tfl_api_key = "test-key"
    mock_settings.return_value = settings

    board = fetch_departures(
        station_id=EAST_PUTNEY,
        destination_station_id=EARLS_COURT,
        max_results=15,
    )

    keys = {
        (d.destination, d.operator, d.expected_time.strftime("%Y-%m-%d %H:%M"))
        for d in board.departures
    }
    assert len(keys) == len(board.departures)
