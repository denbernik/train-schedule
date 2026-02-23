from datetime import datetime, timedelta

from src.filters import filter_and_cap_departures
from src.models import Departure, DepartureStatus


def _dep(destination: str, minutes_from_now: int) -> Departure:
    now = datetime.now()
    expected = now + timedelta(minutes=minutes_from_now)
    return Departure(
        destination=destination,
        scheduled_time=expected,
        expected_time=expected,
        status=DepartureStatus.ON_TIME,
        operator="Test",
    )


def test_filter_drops_departures_below_walking_threshold():
    departures = [
        _dep("TooSoon", 5),
        _dep("CatchableA", 12),
        _dep("CatchableB", 20),
    ]

    result = filter_and_cap_departures(
        departures=departures,
        walking_time_minutes=10,
        max_rows=5,
    )

    assert [dep.destination for dep in result] == ["CatchableA", "CatchableB"]


def test_filter_applies_before_cap():
    departures = [
        _dep("TooSoon", 2),
        _dep("Keep1", 11),
        _dep("Keep2", 14),
        _dep("Keep3", 18),
    ]

    result = filter_and_cap_departures(
        departures=departures,
        walking_time_minutes=10,
        max_rows=2,
    )

    # If capping happened first, result might include TooSoon.
    # Expected behavior is filter first, then cap.
    assert [dep.destination for dep in result] == ["Keep1", "Keep2"]


def test_filter_caps_to_five_rows():
    departures = [_dep(f"D{i}", i + 20) for i in range(8)]

    result = filter_and_cap_departures(
        departures=departures,
        walking_time_minutes=10,
        max_rows=5,
    )

    assert len(result) == 5
