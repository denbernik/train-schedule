import streamlit as st
import streamlit.components.v1 as components

from src.config import get_settings
from src.filters import filter_and_cap_departures
from src.models import DepartureStatus, StationBoard, api_source_for
from src.refresh import fetch_national_rail_for_leg, fetch_tfl_for_leg
from src.routes import RouteLeg, load_routes
from src.station_registry import StationInfo, load_stations, networks_compatible, selectbox_options
from src.status import ActionStatus, compute_action_status

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

# Maps each ActionStatus.display value to the corresponding Streamlit alert function.
_STATUS_DISPLAY = {
    "error":   st.error,
    "warning": st.warning,
    "success": st.success,
    "info":    st.info,
}

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
# Also patches selectbox inputs so they clear-on-focus: the current value is
# auto-selected when the dropdown opens, meaning the user can type immediately
# to search without manually deleting the existing text first.
# A guard flag prevents duplicate listeners across Streamlit reruns.
components.html(
    f"""
    <script>
      setTimeout(function() {{
        window.parent.location.reload();
      }}, {_REFRESH_INTERVAL_SECONDS * 1000});

      (function() {{
        var doc = window.parent.document;
        if (doc.__stSelectClearAttached) return;
        doc.__stSelectClearAttached = true;
        // On every mousedown inside a baseweb Select, wait long enough for
        // the component to open and populate the search input, then select
        // all that text so the user can just start typing from scratch.
        doc.addEventListener('mousedown', function(e) {{
          var container = e.target.closest('[data-baseweb="select"]');
          if (!container) return;
          setTimeout(function() {{
            var inp = container.querySelector('input');
            if (inp && inp.value) {{ inp.select(); }}
          }}, 80);
        }}, true);
      }})();
    </script>
    """,
    height=0,
    width=0,
)

def _swap_stations(idx: int) -> None:
    """Swap departure and arrival station selections for route column `idx`."""
    dep_val = st.session_state.get(f"dep_{idx}_sel")
    arr_val = st.session_state.get(f"arr_{idx}_sel")
    if dep_val is None or arr_val is None:
        return
    st.session_state[f"dep_{idx}_sel"] = arr_val
    st.session_state[f"arr_{idx}_sel"] = dep_val
    # Keep the ID persistence keys in sync
    st.session_state[f"dep_{idx}"] = arr_val[1].id
    st.session_state[f"arr_{idx}"] = dep_val[1].id


def _walk_dec(route_name: str) -> None:
    v = int(st.session_state.get(f"walk_{route_name}", 0))
    st.session_state[f"walk_{route_name}"] = max(_WALK_MIN_MINUTES, v - 1)


def _walk_inc(route_name: str) -> None:
    v = int(st.session_state.get(f"walk_{route_name}", 0))
    st.session_state[f"walk_{route_name}"] = min(_WALK_MAX_MINUTES, v + 1)


# ── Layout CSS ──────────────────────────────────────────────────────────────
# The inner two-column layout (left controls + right pickers) must never stack.
# :has([data-baseweb="select"]) identifies that specific block (it contains the
# Departure/Arrival selectboxes), so fixed-width rules only apply there and not
# to other nested columns (e.g. the −/+ stepper buttons).
st.markdown(
    """
    <style>
    /* Left+right block: no stacking, left col fixed at 70px, right fills rest */
    [data-testid="stColumn"] [data-testid="stHorizontalBlock"]:has([data-baseweb="select"]) {
        flex-wrap: nowrap !important;
        flex-direction: row !important;
    }
    [data-testid="stColumn"] [data-testid="stHorizontalBlock"]:has([data-baseweb="select"])
        > [data-testid="stColumn"]:first-child {
        flex: 0 0 70px !important;
        width: 70px !important;
        min-width: 70px !important;
        max-width: 70px !important;
    }
    [data-testid="stColumn"] [data-testid="stHorizontalBlock"]:has([data-baseweb="select"])
        > [data-testid="stColumn"]:last-child {
        flex: 1 1 auto !important;
        min-width: 0 !important;
        max-width: none !important;
    }
    /* Other nested blocks (e.g. −/+ stepper): no stacking, equal flexible columns */
    [data-testid="stColumn"] [data-testid="stHorizontalBlock"]:not(:has([data-baseweb="select"])) {
        flex-wrap: nowrap !important;
        flex-direction: row !important;
    }
    [data-testid="stColumn"] [data-testid="stHorizontalBlock"]:not(:has([data-baseweb="select"]))
        > [data-testid="stColumn"] {
        min-width: 0 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

columns = st.columns(len(routes))

# Collect per-column selections for URL persistence after the loop.
_col_selections: list[tuple[StationInfo, StationInfo]] = []

for col_idx, (col, route) in enumerate(zip(columns, routes)):
    with col:
        # Read walking time from session state (pre-seeded from URL params above)
        walking_time = int(st.session_state.get(f"walk_{route.name}", route.walking_time_minutes))

        # ── Two-column layout: fixed-width left controls + flexible right pickers ──
        _left_col, _right_col = st.columns([1, 6])

        # ── Right col: Departure ──────────────────────────────────────────────
        dep_init = _option_idx.get(st.session_state[f"dep_{col_idx}"], 0)
        with _right_col:
            dep_sel = st.selectbox(
                "Departure",
                _options,
                index=dep_init,
                format_func=lambda o: o[0],
                key=f"dep_{col_idx}_sel",
            )
        dep_info: StationInfo = dep_sel[1]

        # ── Left col: swap button (centred between Departure and Arrival) ──────
        with _left_col:
            # Spacer pushes the button down to the visual midpoint between the
            # two selectboxes (label ~20px + input ~38px = ~58px per selectbox;
            # centre is roughly at 20px label + 19px = ~39px from top).
            st.markdown('<div style="height:70px"></div>', unsafe_allow_html=True)
            st.button(
                "⇅",
                key=f"swap_{col_idx}",
                on_click=_swap_stations,
                args=(col_idx,),
                help="Swap departure ↔ arrival",
                use_container_width=True,
            )

        # ── Right col: Arrival ────────────────────────────────────────────────
        arr_init = _option_idx.get(st.session_state[f"arr_{col_idx}"], 0)
        with _right_col:
            arr_sel = st.selectbox(
                "Arrival",
                _options,
                index=arr_init,
                format_func=lambda o: o[0],
                key=f"arr_{col_idx}_sel",
            )
        arr_info: StationInfo = arr_sel[1]

        _col_selections.append((dep_info, arr_info))

        # ── Left col: walking-time stepper (bottom) ───────────────────────────
        with _left_col:
            st.markdown(
                f'<p style="text-align:center;font-size:0.65em;color:var(--text-color,#888);'
                f'margin:0 0 1px;line-height:1">min to station</p>'
                f'<p style="text-align:center;font-size:1.6em;font-weight:bold;'
                f'margin:0 0 2px;line-height:1">{walking_time}</p>',
                unsafe_allow_html=True,
            )
            _dec_col, _inc_col = st.columns(2, gap="small")
            with _dec_col:
                st.button("−", key=f"wd_{col_idx}", on_click=_walk_dec,
                          args=(route.name,), use_container_width=True)
            with _inc_col:
                st.button("＋", key=f"wi_{col_idx}", on_click=_walk_inc,
                          args=(route.name,), use_container_width=True)

        # ── Network compatibility check ───────────────────────────────────────
        if not networks_compatible(dep_info, arr_info):
            dep_net = "TfL" if dep_info.network == "tfl" else "National Rail"
            arr_net = "TfL" if arr_info.network == "tfl" else "National Rail"
            with _right_col:
                st.error(
                    f"**Network mismatch — cannot show departures**\n\n"
                    f"**{dep_info.name}** is on **{dep_net}** "
                    f"but **{arr_info.name}** is on **{arr_net}**.\n\n"
                    f"Select two stations on the same network."
                )
            continue

        # ── Service type compatibility check (within TfL) ────────────────────
        if dep_info.network == "tfl" and dep_info.mode != arr_info.mode:
            with _right_col:
                st.error(
                    f"**Service type mismatch — cannot show departures**\n\n"
                    f"**{dep_info.name}** is **{dep_info.display_label.split('(')[-1].rstrip(')')}** "
                    f"but **{arr_info.name}** is **{arr_info.display_label.split('(')[-1].rstrip(')')}**.\n\n"
                    f"Select two stations on the same service."
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

        # ── Right col: status ─────────────────────────────────────────────────
        with _right_col:
            if board.has_error:
                _STATUS_DISPLAY["info"]("🔌 No data — check the National Rail app or TfL Go")
                st.error(board.error_message)
            elif board.no_direct_route:
                st.error(
                    "**No direct service** — these stations are not on the same route.\n\n"
                    "Select two stations with a through train between them."
                )
            else:
                action = compute_action_status(board.departures, walking_time)
                _STATUS_DISPLAY[action.display](f"{action.emoji} {action.label}")

        if board.has_error or board.no_direct_route:
            continue

        # ── Departures list (full-width within route column) ──────────────────
        visible_departures = filter_and_cap_departures(
            departures=board.departures,
            walking_time_minutes=walking_time,
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
