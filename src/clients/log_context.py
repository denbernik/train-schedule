"""Shared logging context formatter for transport clients."""

from __future__ import annotations


def format_log_context(
    *,
    origin: str,
    destination: str | None,
    source: str,
    fallback: str | None = None,
) -> str:
    dest = destination if destination is not None else "-"
    fb = fallback if fallback is not None else "-"
    return f"origin={origin} destination={dest} source={source} fallback={fb}"
