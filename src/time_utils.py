"""Shared time helpers used across models, filters, and status logic."""

from __future__ import annotations

from datetime import datetime


def minutes_until(target_time: datetime, now: datetime | None = None) -> int | None:
    """
    Return whole minutes until `target_time`, or None when already departed.

    If `now` is omitted, this uses timezone-aware "now" when `target_time`
    has tzinfo, otherwise local naive "now".
    """
    reference_now = now
    if reference_now is None:
        reference_now = (
            datetime.now(target_time.tzinfo)
            if target_time.tzinfo is not None
            else datetime.now()
        )

    delta = target_time - reference_now
    minutes = int(delta.total_seconds() / 60)
    return minutes if minutes >= 0 else None
