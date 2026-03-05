# Learnings

## TfL Destination Filter: "No Route" on Suspended Branches

### What happened
King's Cross → Angel showed no route (previously: a confusing error with raw NaPTAN IDs).

### Root cause
The Northern line Bank branch is suspended Mon–Thu after 22:00 (planned works). During those hours, the TfL arrivals API returns **zero Northern line arrivals** for `940GZZLUKSX` — only Circle/H&C/Metropolitan/Piccadilly/Victoria trains appear. `_filter_arrivals_for_destination` builds `line_ids` from live arrivals only, so "northern" is absent. It then checks if Angel is reachable via the lines that *do* have arrivals — and it isn't. `reachable = False` → previously returned an error board.

### Key insight
The code was *logically correct* — no service genuinely existed — but it reported a planned suspension as a data/config error, exposing raw NaPTAN IDs to the user.

### Fix (`src/clients/tfl.py`)
Changed `if not reachable` from returning a `filter_error` to returning `([], None)`. Empty departures flow through to `compute_action_status`, which shows ⛔ **"Avoid completely — no service"** — accurate and user-friendly.

### TfL API behaviour to remember
- `940GZZLUKSX` is the correct hub NaPTAN ID for King's Cross St Pancras Underground and is used in Northern line route sequences.
- The arrivals endpoint `/StopPoint/{id}/Arrivals` only returns arrivals for lines *currently running* at that stop — not all lines that *normally* serve it.
- During branch suspensions, affected stations simply disappear from live arrival feeds for that line.
