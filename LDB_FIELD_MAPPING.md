# LDB JSON Field Mapping (Phase 1)

This note maps expected Live Departure Board fields to the app's domain model in `src/models.py`.
It is based on:

- Rail Data Marketplace product specification for `GetDepartureBoard`,
- National Rail LDBWS JSON docs,
- current model contract used by `src/clients/transport_api.py`.

## Request Shape (validated)

- Endpoint: `/LDBWS/api/20220120/GetDepartureBoard/{crs}`
- Header: `x-apikey`
- Query params used by probe:
  - `numRows`
  - `filterCrs`
  - `filterType`
  - `timeOffset`
  - `timeWindow`

## Response Mapping Targets

| App model target | LDB expected field(s) | Notes |
|---|---|---|
| `StationBoard.station_name` | `locationName` | From station board root object. |
| `StationBoard.last_updated` | `generatedAt` | ISO datetime expected; fallback to now if absent. |
| `Departure.destination` | `destination[].locationName` (or equivalent display string) | Destination can be a list in LDB. Parser should flatten to a readable string. |
| `Departure.scheduled_time` | `std` | Time string like `HH:MM` or text in some cases. |
| `Departure.expected_time` | `etd` / `atd` | If `atd` exists, use actual departure. Otherwise use `etd`; if textual status, fall back to `std`. |
| `Departure.status` | `isCancelled`, `etd` text, `cancelReason`, `delayReason` | `isCancelled=true` -> Cancelled. `etd` text like `On time` / `Delayed` / `Cancelled` should map to enum. |
| `Departure.platform` | `platform` | Optional field. |
| `Departure.operator` | `operator` | String value. |
| `Departure.delay_minutes` | derived from `std` vs `etd/atd` when both are concrete times | Keep `0` if expected/actual is textual. |

## Ambiguities To Resolve In Phase 2

1. Confirm exact root key for service rows from live payload:
   - `trainServices`, `GetDepartureBoardResult.trainServices`, or another wrapper.
2. Confirm whether destination is always list-typed vs simple string in this product's payload.
3. Confirm exact time text variants returned by this gateway (`On time`, `No report`, `Cancelled`, etc.).

## Current Probe Outcome

- Connectivity reached endpoint but returned `HTTP 403` in this environment.
- This indicates credentials/product access or network policy issue, not a code parsing failure.
