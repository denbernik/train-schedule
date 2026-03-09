import json

from src.models import StationType
from src.station_registry import StationInfo
import src.station_registry as registry


def _write_station_data(path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_stations_sorts_maps_and_skips_unknown_mode(tmp_path, monkeypatch):
    data = [
        {"id": "X1", "name": "Zulu", "mode": "unknown", "network": "tfl"},
        {"id": "940GZZLUEPY", "name": "East Putney", "mode": "tube", "network": "tfl"},
        {"id": "WNT", "name": "Wandsworth Town", "mode": "national_rail", "network": "national_rail"},
    ]
    data_path = tmp_path / "stations.json"
    _write_station_data(data_path, data)

    monkeypatch.setattr(registry, "_DATA_PATH", data_path)
    registry.load_stations.cache_clear()

    stations = registry.load_stations()

    assert len(stations) == 2
    assert [s.display_label for s in stations] == ["East Putney (Tube)", "Wandsworth Town (National Rail)"]
    assert stations[0].station_type == StationType.TFL_TUBE
    assert stations[1].station_type == StationType.NATIONAL_RAIL

    registry.load_stations.cache_clear()


def test_networks_compatible_tfl_vs_nr():
    tfl_a = StationInfo(
        id="940GZZLUEPY",
        name="East Putney",
        mode="tube",
        network="tfl",
        station_type=StationType.TFL_TUBE,
        display_label="East Putney (Tube)",
    )
    tfl_b = StationInfo(
        id="940GZZLUECT",
        name="Earl's Court",
        mode="tube",
        network="tfl",
        station_type=StationType.TFL_TUBE,
        display_label="Earl's Court (Tube)",
    )
    nr = StationInfo(
        id="WNT",
        name="Wandsworth Town",
        mode="national_rail",
        network="national_rail",
        station_type=StationType.NATIONAL_RAIL,
        display_label="Wandsworth Town (National Rail)",
    )

    assert registry.networks_compatible(tfl_a, tfl_b)
    assert not registry.networks_compatible(tfl_a, nr)


def test_selectbox_options_returns_label_and_object_pairs(tmp_path, monkeypatch):
    data = [
        {"id": "WNT", "name": "Wandsworth Town", "mode": "national_rail", "network": "national_rail"},
        {"id": "940GZZLUEPY", "name": "East Putney", "mode": "tube", "network": "tfl"},
    ]
    data_path = tmp_path / "stations.json"
    _write_station_data(data_path, data)

    monkeypatch.setattr(registry, "_DATA_PATH", data_path)
    registry.load_stations.cache_clear()

    options = registry.selectbox_options()

    assert len(options) == 2
    assert options[0][0] == "East Putney (Tube)"
    assert isinstance(options[0][1], StationInfo)

    registry.load_stations.cache_clear()
