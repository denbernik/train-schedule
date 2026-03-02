import streamlit as st
import streamlit.components.v1 as components

from src.config import get_settings
from src.filters import filter_and_cap_departures
from src.models import DepartureStatus, StationBoard, api_source_for
from src.refresh import fetch_national_rail_for_leg, fetch_tfl_for_leg
from src.routes import RouteLeg, load_routes
from src.station_registry import StationInfo, load_stations, networks_compatible, selectbox_options

settings = get_settings()
_TFL_MAX_RESULTS = settings.tfl_max_departures
_REFRESH_INTERVAL_SECONDS = max(5, settings.refresh_interval_seconds)
_FINAL_DISPLAY_ROWS = 5
_WALK_MIN_MINUTES = 0
_WALK_MAX_MINUTES = 99

# Station registry — loaded once, cached by @lru_cache in station_registry.py
_options: list[tuple[str, StationInfo]] = selectbox_options()
_option_idx: dict[str, int] = {info.id: i for i, (_, info) in enumerate(_options)}


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

# ── Seed session state from URL query params, falling back to route defaults ──
for _i, _route in enumerate(routes):
    _default_leg = _route.legs[0]

    # Walking time
    _ss_walk = f"walk_{_route.name}"
    if _ss_walk not in st.session_state:
        _raw_walk = st.query_params.get(f"walk_{_i}")
        try:
            _v = int(_raw_walk) if _raw_walk is not None else _route.walking_time_minutes
            st.session_state[_ss_walk] = (
                _v if _WALK_MIN_MINUTES <= _v <= _WALK_MAX_MINUTES
                else _route.walking_time_minutes
            )
        except (ValueError, TypeError):
            st.session_state[_ss_walk] = _route.walking_time_minutes

    # Departure station
    _ss_dep = f"dep_{_i}"
    if _ss_dep not in st.session_state:
        _raw_dep = st.query_params.get(_ss_dep, _default_leg.origin_station_id)
        st.session_state[_ss_dep] = (
            _raw_dep if _raw_dep in _option_idx else _default_leg.origin_station_id
        )

    # Arrival station
    _ss_arr = f"arr_{_i}"
    if _ss_arr not in st.session_state:
        _raw_arr = st.query_params.get(_ss_arr, _default_leg.destination_station_id)
        st.session_state[_ss_arr] = (
            _raw_arr if _raw_arr in _option_idx else _default_leg.destination_station_id
        )

st.title("🚂 Departure Board")
st.caption(f"Auto-refresh every {_REFRESH_INTERVAL_SECONDS}s")

# Auto-refresh via JS timer — reload preserves the full URL (including query params).
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

# Collect per-column selections for URL persistence after the loop.
_col_selections: list[tuple[StationInfo, StationInfo]] = []

for col_idx, (col, route) in enumerate(zip(columns, routes)):
    with col:
        # ── Walking time ──────────────────────────────────────────────────────
        walking_time = st.number_input(
            "🚶 min to station",
            min_value=_WALK_MIN_MINUTES,
            max_value=_WALK_MAX_MINUTES,
            step=1,
            key=f"walk_{route.name}",
        )

        # ── Departure selectbox ───────────────────────────────────────────────
        dep_init = _option_idx.get(st.session_state[f"dep_{col_idx}"], 0)
        dep_sel = st.selectbox(
            "Departure",
            _options,
            index=dep_init,
            format_func=lambda o: o[0],
            key=f"dep_{col_idx}_sel",
        )
        dep_info: StationInfo = dep_sel[1]

        # ── Arrival selectbox ─────────────────────────────────────────────────
        arr_init = _option_idx.get(st.session_state[f"arr_{col_idx}"], 0)
        arr_sel = st.selectbox(
            "Arrival",
            _options,
            index=arr_init,
            format_func=lambda o: o[0],
            key=f"arr_{col_idx}_sel",
        )
        arr_info: StationInfo = arr_sel[1]

        _col_selections.append((dep_info, arr_info))

        # ── Network compatibility check ───────────────────────────────────────
        if not networks_compatible(dep_info, arr_info):
            dep_net = "TfL" if dep_info.network == "tfl" else "National Rail"
            arr_net = "TfL" if arr_info.network == "tfl" else "National Rail"
            st.error(
                f"**Network mismatch — cannot show departures**\n\n"
                f"**{dep_info.name}** is on **{dep_net}** "
                f"but **{arr_info.name}** is on **{arr_net}**.\n\n"
                f"Select two stations on the same network."
            )
            continue

        # ── Build RouteLeg dynamically and fetch ──────────────────────────────
        dynamic_leg = RouteLeg(
            origin_station_id=dep_info.id,
            origin_name=dep_info.name,
            destination_station_id=arr_info.id,
            destination_name=arr_info.name,
            transport_mode=dep_info.station_type,
            api_source=api_source_for(dep_info.station_type),
        )

        board = _fetch_leg(dynamic_leg)
        st.subheader(f"{board.station_name} → {dynamic_leg.destination_name}")
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
                    if dynamic_leg.api_source == "tfl" and dep.status == DepartureStatus.NO_REPORT
                    else ""
                )
                if dep.display_arrival_time:
                    duration_part = f" ({dep.display_duration})" if dep.display_duration else ""
                    line = f"{dep.display_time} - {dep.display_arrival_time}{duration_part} → {dep.destination} {status}"
                else:
                    line = f"{dep.display_time} → {dep.destination}{timetable_marker} {status}"
                st.write(line)

# ── Persist all selections into the URL (survives auto-refresh and manual reload) ──
for _i, _route in enumerate(routes):
    st.query_params[f"walk_{_i}"] = str(st.session_state[f"walk_{_route.name}"])
    st.query_params[f"dep_{_i}"]  = _col_selections[_i][0].id
    st.query_params[f"arr_{_i}"]  = _col_selections[_i][1].id
