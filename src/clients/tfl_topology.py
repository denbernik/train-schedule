"""
Tube topology provider for TfL pass-through filtering.

This module provides a hybrid data source for Tube stop sequences:
1) Persistent on-disk cache (preferred)
2) Live refresh from TfL Line Route Sequence endpoints
3) Versioned in-repo snapshot fallback

The cache stores station IDs in ordered sequences per line. Filtering logic can
then answer questions such as "does a train to terminal T pass through station X
after origin O?" without making an API call for every arrival row.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.tfl.gov.uk"
_TIMEOUT_SECONDS = 10
_CACHE_VERSION = 1
_DEFAULT_TTL_DAYS = 7
_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "tfl_tube_topology_snapshot.json"


class TopologyUnavailableError(Exception):
    """Raised when topology data cannot be loaded from any source."""


def _default_cache_path() -> Path:
    """Default persistent cache path for Tube topology data."""
    return Path.home() / ".cache" / "train-schedule" / "tfl_tube_topology_cache.json"


class TubeTopologyProvider:
    """
    Provides ordered stop sequences for Tube lines with hybrid caching.

    Data contract:
    {
        "version": 1,
        "fetched_at": "ISO timestamp",
        "lines": {
            "district": [["940GZZLUEPY", "..."], ...]
        }
    }
    """

    def __init__(
        self,
        api_key: str,
        cache_path: Path | None = None,
        ttl_days: int = _DEFAULT_TTL_DAYS,
    ) -> None:
        self.api_key = api_key
        self.cache_path = cache_path or _default_cache_path()
        self.ttl = timedelta(days=ttl_days)
        self._snapshot_data = self._load_snapshot_data()

    def has_path(self, line_id: str, origin_station_id: str, destination_station_id: str) -> bool:
        """True if line graph has a path between origin and destination."""
        sequences = self._get_line_sequences(line_id)
        graph = self._build_graph(sequences)
        return self._distance(graph, origin_station_id, destination_station_id) is not None

    def has_direct_connection(self, origin_station_id: str, destination_station_id: str) -> bool:
        """Return True if any cached route sequence contains BOTH stations.

        A shared sequence means a through train can serve both without a change.
        This correctly handles branch pairs (e.g. Angel on Bank branch vs Charing
        Cross on Charing Cross branch — no single sequence contains both).

        Uses only disk cache or snapshot — no API calls.
        Returns False (conservative) if cache is empty.
        """
        cache = self._load_disk_cache() or self._load_snapshot_data()
        if not cache:
            return False
        for sequences in cache.get("lines", {}).values():
            for seq in sequences:
                seq_set = set(seq)
                if origin_station_id in seq_set and destination_station_id in seq_set:
                    return True
        return False

    def service_passes_through(
        self,
        line_id: str,
        origin_station_id: str,
        destination_station_id: str,
        terminal_station_id: str,
    ) -> bool:
        """
        True if a service to `terminal_station_id` passes through destination.

        We consider destination a pass-through point if it lies on at least one
        shortest path from origin to terminal in the line graph.
        """
        sequences = self._get_line_sequences(line_id)
        graph = self._build_graph(sequences)

        origin_to_terminal = self._distance(graph, origin_station_id, terminal_station_id)
        origin_to_destination = self._distance(graph, origin_station_id, destination_station_id)
        destination_to_terminal = self._distance(graph, destination_station_id, terminal_station_id)
        if (
            origin_to_terminal is None
            or origin_to_destination is None
            or destination_to_terminal is None
        ):
            return False

        return origin_to_terminal == (origin_to_destination + destination_to_terminal)

    def _get_line_sequences(self, line_id: str) -> list[list[str]]:
        """
        Resolve sequences with priority:
        fresh disk cache -> refresh line from API -> stale disk cache -> snapshot.
        """
        normalized = line_id.strip().lower()
        cache_data = self._load_disk_cache()

        if self._is_fresh(cache_data):
            sequences = self._extract_line_sequences(cache_data, normalized)
            if sequences:
                return sequences

        refreshed = self._refresh_line_sequences(normalized, cache_data)
        if refreshed:
            return refreshed

        stale_sequences = self._extract_line_sequences(cache_data, normalized)
        if stale_sequences:
            logger.warning("Using stale Tube topology cache for line '%s'", normalized)
            return stale_sequences

        snapshot_sequences = self._extract_line_sequences(self._snapshot_data, normalized)
        if snapshot_sequences:
            logger.warning("Using snapshot Tube topology for line '%s'", normalized)
            return snapshot_sequences

        raise TopologyUnavailableError(f"No topology sequences available for line '{normalized}'")

    def _refresh_line_sequences(self, line_id: str, cache_data: dict | None) -> list[list[str]]:
        """Fetch inbound/outbound route sequences for one line and persist."""
        try:
            inbound = self._fetch_line_sequences(line_id, "inbound")
            outbound = self._fetch_line_sequences(line_id, "outbound")
            merged = self._dedupe_sequences(inbound + outbound)
            if not merged:
                return []

            updated = cache_data or {"version": _CACHE_VERSION, "lines": {}}
            updated["version"] = _CACHE_VERSION
            updated["fetched_at"] = datetime.utcnow().isoformat()
            updated.setdefault("lines", {})[line_id] = merged
            self._save_disk_cache(updated)
            return merged
        except requests.RequestException as e:
            logger.warning("TfL topology refresh failed for line '%s': %s", line_id, e)
            return []
        except (ValueError, TypeError, KeyError) as e:
            logger.warning("TfL topology parse failed for line '%s': %s", line_id, e)
            return []

    def _fetch_line_sequences(self, line_id: str, direction: str) -> list[list[str]]:
        url = f"{_BASE_URL}/Line/{line_id}/Route/Sequence/{direction}"
        params = {}
        if self.api_key:
            params["app_key"] = self.api_key

        response = requests.get(url, params=params, timeout=_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()

        stop_point_sequences = payload.get("stopPointSequences", [])
        sequences: list[list[str]] = []
        for sequence in stop_point_sequences:
            stops = sequence.get("stopPoint", [])
            station_ids = [stop.get("id") for stop in stops if isinstance(stop.get("id"), str)]
            if station_ids:
                sequences.append(station_ids)
        return sequences

    def _load_snapshot_data(self) -> dict:
        if not _SNAPSHOT_PATH.exists():
            return {"version": _CACHE_VERSION, "lines": {}}
        with _SNAPSHOT_PATH.open("r", encoding="utf-8") as file:
            return json.load(file)

    def _load_disk_cache(self) -> dict | None:
        if not self.cache_path.exists():
            return None
        try:
            with self.cache_path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except (OSError, json.JSONDecodeError):
            logger.warning("Unable to read Tube topology cache at %s", self.cache_path)
            return None

    def _save_disk_cache(self, data: dict) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cache_path.open("w", encoding="utf-8") as file:
                json.dump(data, file)
        except OSError as e:
            logger.warning("Unable to persist Tube topology cache at %s: %s", self.cache_path, e)

    def _is_fresh(self, cache_data: dict | None) -> bool:
        if not cache_data:
            return False
        fetched_at = cache_data.get("fetched_at")
        if not isinstance(fetched_at, str):
            return False
        try:
            fetched_dt = datetime.fromisoformat(fetched_at)
        except ValueError:
            return False
        return datetime.utcnow() - fetched_dt <= self.ttl

    @staticmethod
    def _extract_line_sequences(cache_data: dict | None, line_id: str) -> list[list[str]]:
        if not cache_data:
            return []
        lines = cache_data.get("lines", {})
        sequences = lines.get(line_id, [])
        if not isinstance(sequences, list):
            return []
        parsed: list[list[str]] = []
        for sequence in sequences:
            if isinstance(sequence, list):
                ids = [station_id for station_id in sequence if isinstance(station_id, str)]
                if ids:
                    parsed.append(ids)
        return parsed

    @staticmethod
    def _dedupe_sequences(sequences: list[list[str]]) -> list[list[str]]:
        deduped: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()
        for sequence in sequences:
            key = tuple(sequence)
            if key not in seen:
                seen.add(key)
                deduped.append(sequence)
        return deduped

    @staticmethod
    def _build_graph(sequences: list[list[str]]) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = {}
        for sequence in sequences:
            for station_id in sequence:
                graph.setdefault(station_id, set())
            for index in range(len(sequence) - 1):
                a = sequence[index]
                b = sequence[index + 1]
                graph[a].add(b)
                graph[b].add(a)
        return graph

    @staticmethod
    def _distance(graph: dict[str, set[str]], start: str, end: str) -> int | None:
        if start not in graph or end not in graph:
            return None
        if start == end:
            return 0

        visited = {start}
        frontier = {start}
        distance = 0
        while frontier:
            distance += 1
            next_frontier: set[str] = set()
            for node in frontier:
                for neighbor in graph.get(node, set()):
                    if neighbor == end:
                        return distance
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_frontier.add(neighbor)
            frontier = next_frontier

        return None
