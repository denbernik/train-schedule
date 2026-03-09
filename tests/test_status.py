from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.models import Departure, DepartureStatus
from src.status import compute_action_status


_FIXED_NOW_UTC = datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc)


class _FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return _FIXED_NOW_UTC.replace(tzinfo=None)
        return _FIXED_NOW_UTC.astimezone(tz)


def _dep(minutes_from_now: int, status: DepartureStatus = DepartureStatus.ON_TIME) -> Departure:
    expected = _FIXED_NOW_UTC + timedelta(minutes=minutes_from_now)
    return Departure(
        destination=f"D{minutes_from_now}",
        scheduled_time=expected,
        expected_time=expected,
        status=status,
        operator="Test",
    )


def test_status_avoid_completely_when_no_reachable_departure():
    with patch("src.time_utils.datetime", _FixedDateTime):
        status = compute_action_status([_dep(5)], walking_time_minutes=10)
    assert status.emoji == "⛔️"


def test_status_rush_window():
    with patch("src.time_utils.datetime", _FixedDateTime):
        status = compute_action_status([_dep(9)], walking_time_minutes=10)
    assert status.emoji == "🏃‍♂️"
    assert status.label == "Rush to the train"


def test_status_leave_now_window():
    with patch("src.time_utils.datetime", _FixedDateTime):
        status = compute_action_status([_dep(12)], walking_time_minutes=10)
    assert status.emoji == "🚶‍♂️"


def test_status_avoid_if_possible_on_moderate_cancellations():
    departures = [_dep(20)] + [_dep(21 + i, DepartureStatus.CANCELLED) for i in range(3)]
    with patch("src.time_utils.datetime", _FixedDateTime):
        status = compute_action_status(departures, walking_time_minutes=10)
    assert status.emoji == "⚠️"


def test_status_leave_in_minutes_when_comfortable_and_low_disruption():
    with patch("src.time_utils.datetime", _FixedDateTime):
        status = compute_action_status([_dep(20)], walking_time_minutes=10)
    assert status.emoji == "🫷"
    assert status.label == "Leave in 10 min"


def test_status_priority_extreme_cancellations_overrides_rush():
    departures = [_dep(9)] + [_dep(10 + i, DepartureStatus.CANCELLED) for i in range(6)]
    with patch("src.time_utils.datetime", _FixedDateTime):
        status = compute_action_status(departures, walking_time_minutes=10)
    assert status.emoji == "⛔️"
