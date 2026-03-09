import json

import pytest

from src.routes import load_routes


def _write_routes(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_routes_rejects_non_list_root(tmp_path):
    file_path = tmp_path / "routes.json"
    _write_routes(file_path, {"name": "bad"})
    with pytest.raises(TypeError):
        load_routes(file_path)


def test_load_routes_rejects_leg_count_outside_one_or_two(tmp_path):
    file_path = tmp_path / "routes.json"
    _write_routes(
        file_path,
        [
            {
                "name": "BadRoute",
                "walking_time_minutes": 10,
                "legs": [],
            }
        ],
    )
    with pytest.raises(ValueError):
        load_routes(file_path)


def test_load_routes_rejects_invalid_transport_mode(tmp_path):
    file_path = tmp_path / "routes.json"
    _write_routes(
        file_path,
        [
            {
                "name": "BadMode",
                "walking_time_minutes": 10,
                "legs": [
                    {
                        "origin_station_id": "WNT",
                        "origin_name": "Wandsworth Town",
                        "destination_station_id": "WAT",
                        "destination_name": "Waterloo",
                        "api_source": "national_rail",
                        "transport_mode": "NOT_A_MODE",
                    }
                ],
            }
        ],
    )
    with pytest.raises(ValueError):
        load_routes(file_path)


def test_load_routes_rejects_wrong_walking_time_type(tmp_path):
    file_path = tmp_path / "routes.json"
    _write_routes(
        file_path,
        [
            {
                "name": "WrongType",
                "walking_time_minutes": "10",
                "legs": [
                    {
                        "origin_station_id": "WNT",
                        "origin_name": "Wandsworth Town",
                        "destination_station_id": "WAT",
                        "destination_name": "Waterloo",
                        "api_source": "national_rail",
                        "transport_mode": "NATIONAL_RAIL",
                    }
                ],
            }
        ],
    )
    with pytest.raises(TypeError):
        load_routes(file_path)


def test_load_routes_accepts_enum_names_and_values(tmp_path):
    file_path = tmp_path / "routes.json"
    payload = [
        {
            "name": "NameMode",
            "walking_time_minutes": 8,
            "legs": [
                {
                    "origin_station_id": "WNT",
                    "origin_name": "Wandsworth Town",
                    "destination_station_id": "WAT",
                    "destination_name": "Waterloo",
                    "api_source": "national_rail",
                    "transport_mode": "NATIONAL_RAIL",
                }
            ],
        },
        {
            "name": "ValueMode",
            "walking_time_minutes": 12,
            "legs": [
                {
                    "origin_station_id": "940GZZLUEPY",
                    "origin_name": "East Putney",
                    "destination_station_id": "940GZZLUECT",
                    "destination_name": "Earl's Court",
                    "api_source": "tfl",
                    "transport_mode": "TfL Underground",
                }
            ],
        },
    ]
    _write_routes(file_path, payload)

    routes = load_routes(file_path)

    assert len(routes) == 2
    assert routes[0].legs[0].transport_mode.name == "NATIONAL_RAIL"
    assert routes[1].legs[0].transport_mode.value == "TfL Underground"
