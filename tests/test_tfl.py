from unittest.mock import MagicMock, patch

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
