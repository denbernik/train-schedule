# About

## What this app does
- Streamlit departure board for London trains from your chosen nearby station pair.
- Shows trains you can realistically catch using the `min to station` walking-time control.

## APIs and stack
- Python + Streamlit frontend/runtime.
- TfL Unified API for Tube/Overground/DLR/Elizabeth departures.
- National Rail via Rail Data Marketplace LDB API, with TransportAPI fallback when LDB fails.

## Data / "database"
- No SQL database in this repo.
- Core data is JSON/config based:
  - `routes.json` (default route cards and walking times)
  - `src/data/london_stations.json` (548 stations)
  - `src/data/tfl_tube_topology_snapshot.json` (fallback route-topology snapshot)
- Runtime cache: in-memory National Rail TTL cache + local tube-topology cache under `~/.cache/train-schedule/`.

## Current status
- Defaults currently ship with 2 route cards:
  - Wandsworth Town -> Waterloo (`walk=10`)
  - East Putney -> Earl's Court (`walk=15`)
- Baseline issue was 5 failing LDB midnight-rollover tests; parser now uses a single reference time per parse to make rollover deterministic.

## Developer context
- Project owner context: beginner in software engineering and basic Python.
- Collaboration preference: concise, plain-English guidance with lightweight coaching on core technical foundations.
