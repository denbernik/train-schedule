from datetime import datetime, timedelta, timezone

from src.app_logic import (
    persist_route_state,
    prepare_visible_departure_rows,
    seed_route_state,
    station_pair_validation_error,
    status_for_board,
)
from src.models import Departure, DepartureStatus, StationBoard, StationType
from src.routes import Route, RouteLeg
from src.station_registry import StationInfo


def _sample_route() -> Route:
    return Route(
        name="Wandsworth Town to Waterloo",
        legs=[
            RouteLeg(
                origin_station_id="WNT",
                origin_name="Wandsworth Town",
                destination_station_id="VXH",
                destination_name="Vauxhall",
                transport_mode=StationType.NATIONAL_RAIL,
                api_source="national_rail",
            )
        ],
        walking_time_minutes=10,
    )


def _station(
    station_id: str,
    name: str,
    mode: str,
    network: str,
    station_type: StationType,
    label: str,
) -> StationInfo:
    return StationInfo(
        id=station_id,
        name=name,
        mode=mode,
        network=network,
        station_type=station_type,
        display_label=label,
    )


def _dep(minutes_from_now: int, *, platform: str | None = None) -> Departure:
    now = datetime.now(timezone.utc)
    expected = now + timedelta(minutes=minutes_from_now)
    return Departure(
        destination="London Waterloo",
        scheduled_time=expected,
        expected_time=expected,
        status=DepartureStatus.ON_TIME,
        platform=platform,
        operator="South Western Railway",
    )


def test_seed_route_state_applies_defaults_and_fallbacks():
    routes = [_sample_route()]
    session_state: dict[str, object] = {}
    query_params = {
        "walk_0": "999",  # out of allowed range -> fallback
        "dep_0": "BAD",   # unknown station id -> fallback
        "arr_0": "WAT",   # known station id -> keep
    }
    option_idx = {"WNT": 0, "VXH": 1, "WAT": 2}

    seed_route_state(
        session_state=session_state,
        query_params=query_params,
        routes=routes,
        option_idx=option_idx,
        walk_min_minutes=0,
        walk_max_minutes=99,
    )

    assert session_state["walk_Wandsworth Town to Waterloo"] == 10
    assert session_state["dep_0"] == "WNT"
    assert session_state["arr_0"] == "WAT"


def test_seed_route_state_keeps_existing_values():
    routes = [_sample_route()]
    session_state: dict[str, object] = {
        "walk_Wandsworth Town to Waterloo": 15,
        "dep_0": "WAT",
        "arr_0": "WNT",
    }

    seed_route_state(
        session_state=session_state,
        query_params={},
        routes=routes,
        option_idx={"WNT": 0, "VXH": 1, "WAT": 2},
        walk_min_minutes=0,
        walk_max_minutes=99,
    )

    assert session_state["walk_Wandsworth Town to Waterloo"] == 15
    assert session_state["dep_0"] == "WAT"
    assert session_state["arr_0"] == "WNT"


def test_persist_route_state_writes_expected_query_keys():
    routes = [_sample_route()]
    session_state = {"walk_Wandsworth Town to Waterloo": 12}
    query_params: dict[str, str] = {}

    selections = [
        (
            _station("WNT", "Wandsworth Town", "national_rail", "national_rail", StationType.NATIONAL_RAIL, "Wandsworth Town (National Rail)"),
            _station("WAT", "London Waterloo", "national_rail", "national_rail", StationType.NATIONAL_RAIL, "London Waterloo (National Rail)"),
        )
    ]

    persist_route_state(
        query_params=query_params,
        session_state=session_state,
        routes=routes,
        selections=selections,
    )

    assert query_params == {"walk_0": "12", "dep_0": "WNT", "arr_0": "WAT"}


def test_station_pair_validation_error_for_network_and_service_mismatch():
    dep_nr = _station("WNT", "Wandsworth Town", "national_rail", "national_rail", StationType.NATIONAL_RAIL, "Wandsworth Town (National Rail)")
    arr_tfl = _station("940GZZLUEPY", "East Putney", "tube", "tfl", StationType.TFL_TUBE, "East Putney (Tube)")
    assert "Network mismatch" in station_pair_validation_error(dep_nr, arr_tfl)

    dep_tube = _station("940GZZLUEPY", "East Putney", "tube", "tfl", StationType.TFL_TUBE, "East Putney (Tube)")
    arr_dlr = _station("940GZZDLABR", "Abbey Road", "dlr", "tfl", StationType.TFL_DLR, "Abbey Road (DLR)")
    assert "Service type mismatch" in station_pair_validation_error(dep_tube, arr_dlr)


def test_station_pair_validation_error_none_for_valid_pair():
    dep = _station("WNT", "Wandsworth Town", "national_rail", "national_rail", StationType.NATIONAL_RAIL, "Wandsworth Town (National Rail)")
    arr = _station("WAT", "London Waterloo", "national_rail", "national_rail", StationType.NATIONAL_RAIL, "London Waterloo (National Rail)")
    assert station_pair_validation_error(dep, arr) is None


def test_status_for_board_returns_none_for_error_or_no_direct_route():
    err_board = StationBoard(station_name="A", station_type=StationType.NATIONAL_RAIL, departures=[], error_message="boom")
    no_route_board = StationBoard(station_name="A", station_type=StationType.TFL_TUBE, departures=[], no_direct_route=True)

    assert status_for_board(err_board, 10) is None
    assert status_for_board(no_route_board, 10) is None


def test_prepare_visible_departure_rows_applies_tbd_platform_for_nr_only():
    board = StationBoard(
        station_name="Wandsworth Town",
        station_type=StationType.NATIONAL_RAIL,
        departures=[_dep(20, platform=None)],
    )

    nr_rows = prepare_visible_departure_rows(
        board=board,
        api_source="national_rail",
        walking_time_minutes=10,
        max_rows=5,
    )
    tfl_rows = prepare_visible_departure_rows(
        board=board,
        api_source="tfl",
        walking_time_minutes=10,
        max_rows=5,
    )

    assert len(nr_rows) == 1
    assert nr_rows[0][1] == "plat. TBD"
    assert len(tfl_rows) == 1
    assert tfl_rows[0][1] is None
