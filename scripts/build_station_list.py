#!/usr/bin/env python3
"""
One-time script to build src/data/london_stations.json.

Run from the project root whenever the London network changes significantly
(new station opens, NaPTAN ID changes, etc.):

    python scripts/build_station_list.py

Requires TFL_API_KEY in .env or environment (optional but recommended to
avoid anonymous rate limits).

Output: src/data/london_stations.json  — flat JSON array, one entry per station:
    {"id": "940GZZLUEPY", "name": "East Putney", "mode": "tube", "network": "tfl"}
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OUTPUT_PATH = _REPO_ROOT / "src" / "data" / "london_stations.json"

_TFL_BASE = "https://api.tfl.gov.uk"
_TFL_MODES = ["tube", "overground", "elizabeth-line", "dlr"]

# Suffixes TfL appends to commonName that we strip for clean display labels.
_NAME_SUFFIXES = [
    " Underground Station",
    " DLR Station",
    " Rail Station",
    " Overground Station",
    " Elizabeth line Station",
    " (Elizabeth line)",
]

# ─────────────────────────────────────────────────────────────────────────────
# London-area National Rail stations
# Format: (CRS code, display name)
# Source: National Rail CRS codes for Greater London and inner commuter belt.
# ─────────────────────────────────────────────────────────────────────────────
_NATIONAL_RAIL_STATIONS: list[tuple[str, str]] = [
    ("ABW", "Abbey Wood"),
    ("ACT", "Acton Central"),
    ("AHD", "Ashtead"),
    ("BAL", "Balham"),
    ("BAT", "Battersea Power Station"),
    ("BFR", "London Blackfriars"),
    ("BKH", "Blackheath"),
    ("BKJ", "Birkbeck"),
    ("BMH", "Bournemouth"),
    ("BRX", "Brixton"),
    ("BTN", "Brighton"),
    ("CAT", "Caterham"),
    ("CBH", "Cobham & Stoke D'Abernon"),
    ("CHX", "London Charing Cross"),
    ("CLJ", "Clapham Junction"),
    ("CST", "London Cannon Street"),
    ("CTK", "City Thameslink"),
    ("CTN", "Clapham North"),
    ("CWU", "Chessington South"),
    ("DAG", "Dagenham Dock"),
    ("DFD", "Deptford"),
    ("EAD", "Earlsfield"),
    ("EAL", "Ealing Broadway"),
    ("ELD", "Elmers End"),
    ("ELP", "Elephant & Castle"),
    ("EPS", "Epsom"),
    ("EUS", "London Euston"),
    ("EWE", "East Worthing"),
    ("FLT", "Feltham"),
    ("FPK", "Finsbury Park"),
    ("FST", "London Fenchurch Street"),
    ("GNH", "Greenwich"),
    ("GPO", "Gipsy Hill"),
    ("GTW", "London Gatwick Airport"),
    ("HHB", "Hammersmith"),
    ("HMC", "Hampton Court"),
    ("HNH", "Herne Hill"),
    ("HOP", "Honor Oak Park"),
    ("HOW", "Horwich Parkway"),
    ("HTN", "Herne Bay"),
    ("HWW", "Harrow-on-the-Hill"),
    ("KGX", "London King's Cross"),
    ("KNG", "Kingston"),
    ("LBG", "London Bridge"),
    ("LEW", "Lewisham"),
    ("LGW", "London Gatwick Airport"),
    ("LHD", "Leatherhead"),
    ("LMO", "Loughborough Junction"),
    ("LST", "London Liverpool Street"),
    ("LVC", "London Victoria"),
    ("MTC", "Mitcham Junction"),
    ("MYB", "London Marylebone"),
    ("NEW", "New Eltham"),
    ("NOR", "Norwood Junction"),
    ("NRB", "New Eltham"),
    ("NWX", "New Cross"),
    ("NYM", "New Cross Gate"),
    ("PAD", "London Paddington"),
    ("PET", "Peterborough"),
    ("QRP", "Queens Road Peckham"),
    ("RDH", "Redhill"),
    ("RMD", "Richmond"),
    ("SAJ", "St Johns"),
    ("SHP", "Shepherd's Bush"),
    ("SRH", "South Ruislip"),
    ("STC", "Streatham Common"),
    ("STJ", "Streatham"),
    ("STP", "London St Pancras International"),
    ("SUP", "Surbiton"),
    ("SUR", "Sutton"),
    ("TON", "Tonbridge"),
    ("TWI", "Twickenham"),
    ("VIC", "London Victoria"),
    ("VXH", "Vauxhall"),
    ("WAT", "London Waterloo"),
    ("WIM", "Wimbledon"),
    ("WNT", "Wandsworth Town"),
    ("WPL", "Wandsworth Road"),
    ("WSB", "Woolwich Arsenal"),
]


def _load_api_key() -> str:
    api_key = os.environ.get("TFL_API_KEY", "")
    if not api_key:
        env_path = _REPO_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("TFL_API_KEY="):
                    api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    return api_key


def _fetch_line_ids(mode: str, api_key: str) -> list[str]:
    params: dict = {"app_key": api_key} if api_key else {}
    resp = requests.get(f"{_TFL_BASE}/Line/Mode/{mode}", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return [line["id"] for line in data if isinstance(line, dict) and "id" in line]


def _fetch_line_stop_points(line_id: str, api_key: str) -> list[dict]:
    params: dict = {"app_key": api_key} if api_key else {}
    resp = requests.get(f"{_TFL_BASE}/Line/{line_id}/StopPoints", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _clean_name(raw: str) -> str:
    for suffix in _NAME_SUFFIXES:
        if raw.endswith(suffix):
            return raw[: -len(suffix)]
    return raw


def _parse_tfl_stop(stop: dict, mode: str) -> dict | None:
    # /Line/{id}/StopPoints uses "stopType"; filter to station hubs only.
    if stop.get("stopType") not in {"NaptanMetroStation", "NaptanRailStation"}:
        return None

    station_id = (stop.get("naptanId") or stop.get("id") or "").strip()
    raw_name = (stop.get("commonName") or stop.get("name") or "").strip()
    if not station_id or not raw_name:
        return None

    return {
        "id": station_id,
        "name": _clean_name(raw_name),
        "mode": mode,
        "network": "tfl",
    }


def build_tfl_stations(api_key: str) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()  # deduplicates within TfL modes by NaPTAN ID

    for mode in _TFL_MODES:
        try:
            line_ids = _fetch_line_ids(mode, api_key)
            print(f"  {mode}: {len(line_ids)} line(s) → ", end="", flush=True)
        except requests.RequestException as exc:
            print(f"\n  WARNING: failed to get {mode} lines: {exc}")
            continue

        mode_count = 0
        for line_id in line_ids:
            try:
                stops = _fetch_line_stop_points(line_id, api_key)
                time.sleep(0.2)
            except requests.RequestException as exc:
                print(f"\n  WARNING: failed to get stops for {line_id}: {exc}")
                continue

            for stop in stops:
                parsed = _parse_tfl_stop(stop, mode)
                if parsed is None or parsed["id"] in seen:
                    continue
                seen.add(parsed["id"])
                results.append(parsed)
                mode_count += 1

        print(f"{mode_count} unique stations")

    return results


def build_national_rail_stations() -> list[dict]:
    seen: set[str] = set()
    results: list[dict] = []
    for crs, name in _NATIONAL_RAIL_STATIONS:
        if crs in seen:
            continue
        seen.add(crs)
        results.append({"id": crs, "name": name, "mode": "national_rail", "network": "national_rail"})
    return results


def main() -> None:
    api_key = _load_api_key()
    print("Building London station list…")
    print(f"TfL API key: {'set' if api_key else 'not set (anonymous rate limits apply)'}\n")

    print("Fetching TfL stations:")
    tfl = build_tfl_stations(api_key)

    print(f"\nLoading {len(_NATIONAL_RAIL_STATIONS)} National Rail stations")
    nr = build_national_rail_stations()

    all_stations = tfl + nr
    all_stations.sort(key=lambda s: s["name"].lower())

    _OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _OUTPUT_PATH.open("w", encoding="utf-8") as fh:
        json.dump(all_stations, fh, indent=2, ensure_ascii=False)

    print(f"\nWrote {len(all_stations)} stations to {_OUTPUT_PATH.relative_to(_REPO_ROOT)}")
    print(f"  TfL: {len(tfl)}  |  National Rail: {len(nr)}")


if __name__ == "__main__":
    main()
