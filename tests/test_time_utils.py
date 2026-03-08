from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.filters import filter_and_cap_departures
from src.models import Departure, DepartureStatus
from src.status import compute_action_status
from src.time_utils import minutes_until


_FIXED_NOW_UTC = datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return _FIXED_NOW_UTC.replace(tzinfo=None)
        return _FIXED_NOW_UTC.astimezone(tz)


def _dep(name: str, minutes_from_now: int) -> Departure:
    expected = _FIXED_NOW_UTC + timedelta(minutes=minutes_from_now)
    return Departure(
        destination=name,
        scheduled_time=expected,
        expected_time=expected,
        status=DepartureStatus.ON_TIME,
        operator="Test",
    )


def test_minutes_until_matches_model_property():
    dep = _dep("Soon", 9)
    with patch("src.time_utils.datetime", _FixedDateTime):
        assert minutes_until(dep.expected_time) == 9
        assert dep.minutes_until == 9


def test_minutes_until_returns_none_for_past_departure():
    dep = _dep("Gone", -1)
    with patch("src.time_utils.datetime", _FixedDateTime):
        assert minutes_until(dep.expected_time) is None
        assert dep.minutes_until is None


def test_filters_and_status_share_same_time_window_logic():
    departures = [_dep("TooSoon", 7), _dep("Rush", 9), _dep("Comfortable", 15)]

    with patch("src.time_utils.datetime", _FixedDateTime):
        visible = filter_and_cap_departures(
            departures=departures,
            walking_time_minutes=10,
            max_rows=5,
        )
        action = compute_action_status(departures, walking_time_minutes=10)

    assert [dep.destination for dep in visible] == ["Rush", "Comfortable"]
    assert action.label == "Rush to the train"
