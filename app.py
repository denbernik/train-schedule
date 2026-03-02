import streamlit as st
import streamlit.components.v1 as components

from src.config import get_settings
from src.filters import filter_and_cap_departures
from src.models import DepartureStatus, StationBoard
from src.refresh import fetch_national_rail_for_leg, fetch_tfl_for_leg
from src.routes import RouteLeg, load_routes

settings = get_settings()
_TFL_MAX_RESULTS = settings.tfl_max_departures
_REFRESH_INTERVAL_SECONDS = max(5, settings.refresh_interval_seconds)
_FINAL_DISPLAY_ROWS = 5
_WALK_MIN_MINUTES = 0
_WALK_MAX_MINUTES = 99


def _fetch_leg(leg: RouteLeg) -> StationBoard:
    if leg.api_source in ("national_rail", "transport_api"):
        return fetch_national_rail_for_leg(
            origin_station_id=leg.origin_station_id,
            destination_station_id=leg.destination_station_id,
        )
    if leg.api_source == "tfl":
        return fetch_tfl_for_leg(
            origin_station_id=leg.origin_station_id,
            destination_station_id=leg.destination_station_id,
            max_results=_TFL_MAX_RESULTS,
        )
    raise ValueError(f"Unknown api_source: {leg.api_source}")


routes = load_routes()

# Seed session-state walking times from URL query params (persisted across
# reloads) or fall back to the route defaults on very first visit.
for _i, _route in enumerate(routes):
    _ss_key = f"walk_{_route.name}"
    if _ss_key not in st.session_state:
        _raw = st.query_params.get(f"walk_{_i}")
        if _raw is not None:
            try:
                _stored = int(_raw)
                if _WALK_MIN_MINUTES <= _stored <= _WALK_MAX_MINUTES:
                    st.session_state[_ss_key] = _stored
                else:
                    st.session_state[_ss_key] = _route.walking_time_minutes
            except (ValueError, TypeError):
                st.session_state[_ss_key] = _route.walking_time_minutes
        else:
            st.session_state[_ss_key] = _route.walking_time_minutes

st.title("🚂 Departure Board")
st.caption(f"Auto-refresh every {_REFRESH_INTERVAL_SECONDS}s")

# Streamlit 1.54 in this project doesn't expose a native autorefresh helper.
# Inject a tiny reload timer so new departures appear without manual refresh.
components.html(
    f"""
    <script>
      setTimeout(function() {{
        window.parent.location.reload();
      }}, {_REFRESH_INTERVAL_SECONDS * 1000});
    </script>
    """,
    height=0,
    width=0,
)

columns = st.columns(len(routes))

for col, route in zip(columns, routes):
    with col:
        walk_key = f"walk_{route.name}"
        walking_time = st.number_input(
            "🚶 min to station",
            min_value=_WALK_MIN_MINUTES,
            max_value=_WALK_MAX_MINUTES,
            step=1,
            key=walk_key,
        )
        for leg in route.legs:
            board = _fetch_leg(leg)
            st.subheader(f"{board.station_name} → {leg.destination_name}")
            if board.has_error:
                st.error(board.error_message)
            else:
                visible_departures = filter_and_cap_departures(
                    departures=board.departures,
                    walking_time_minutes=int(walking_time),
                    max_rows=_FINAL_DISPLAY_ROWS,
                )
                for dep in visible_departures:
                    status = f"({dep.status.value})" if dep.is_delayed or dep.is_cancelled else ""
                    timetable_marker = (
                        " *"
                        if leg.api_source == "tfl" and dep.status == DepartureStatus.NO_REPORT
                        else ""
                    )
                    if dep.display_arrival_time:
                        duration_part = f" ({dep.display_duration})" if dep.display_duration else ""
                        line = f"{dep.display_time} - {dep.display_arrival_time}{duration_part} → {dep.destination} {status}"
                    else:
                        line = f"{dep.display_time} → {dep.destination}{timetable_marker} {status}"
                    st.write(line)

# Persist current walking times into the URL so they survive page reloads.
for _i, _route in enumerate(routes):
    st.query_params[f"walk_{_i}"] = str(st.session_state[f"walk_{_route.name}"])