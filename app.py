import html as _html
import streamlit as st
import streamlit.components.v1 as components

from src.config import get_settings
from src.filters import filter_and_cap_departures
from src.models import DepartureStatus, StationBoard, api_source_for
from src.refresh import fetch_national_rail_for_leg, fetch_tfl_for_leg
from src.routes import Route, RouteLeg, load_routes
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


def _validated_station_id(
    station_id: str | None,
    fallback_station_id: str,
) -> str:
    if station_id is None:
        return fallback_station_id
    return station_id if station_id in _option_idx else fallback_station_id


def _validated_walk_minutes(raw_value: str | None, fallback: int) -> int:
    try:
        value = int(raw_value) if raw_value is not None else fallback
    except (TypeError, ValueError):
        return fallback
    if _WALK_MIN_MINUTES <= value <= _WALK_MAX_MINUTES:
        return value
    return fallback


def _seed_session_state_from_query(route_list: list[Route]) -> None:
    """Seed session state from URL query params with safe fallback defaults."""
    for route_idx, route in enumerate(route_list):
        default_leg = route.legs[0]

        walk_key = f"walk_{route.name}"
        if walk_key not in st.session_state:
            st.session_state[walk_key] = _validated_walk_minutes(
                st.query_params.get(f"walk_{route_idx}"),
                route.walking_time_minutes,
            )

        dep_key = f"dep_{route_idx}"
        if dep_key not in st.session_state:
            st.session_state[dep_key] = _validated_station_id(
                st.query_params.get(dep_key, default_leg.origin_station_id),
                default_leg.origin_station_id,
            )

        arr_key = f"arr_{route_idx}"
        if arr_key not in st.session_state:
            st.session_state[arr_key] = _validated_station_id(
                st.query_params.get(arr_key, default_leg.destination_station_id),
                default_leg.destination_station_id,
            )


def _render_page_chrome() -> None:
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


def _persist_query_params(
    route_list: list[Route],
    selections: list[tuple[StationInfo, StationInfo]],
) -> None:
    """Persist current controls to the URL so refresh/reload keeps state."""
    for route_idx, route in enumerate(route_list):
        st.query_params[f"walk_{route_idx}"] = str(st.session_state[f"walk_{route.name}"])
        st.query_params[f"dep_{route_idx}"] = selections[route_idx][0].id
        st.query_params[f"arr_{route_idx}"] = selections[route_idx][1].id


routes = load_routes()

# Maps each ActionStatus.display value to the corresponding Streamlit alert function.
_STATUS_DISPLAY = {
    "error":   st.error,
    "warning": st.warning,
    "success": st.success,
    "info":    st.info,
}

_seed_session_state_from_query(routes)
_render_page_chrome()

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
    /* ── Departure card styles ────────────────────────────────────────── */
    .dep-card {
        border-left: 3px solid rgba(100, 200, 100, 0.5);
        padding: 8px 10px 6px 10px;
        margin-bottom: 6px;
        border-radius: 0 6px 6px 0;
        background: rgba(255, 255, 255, 0.03);
    }
    .dep-card--delayed {
        border-left-color: rgba(255, 180, 50, 0.8);
        background: rgba(255, 180, 50, 0.06);
    }
    .dep-card--cancelled {
        border-left-color: rgba(255, 70, 70, 0.8);
        background: rgba(255, 70, 70, 0.06);
    }
    .dep-card--timetable {
        border-left-color: rgba(150, 150, 150, 0.35);
        background: rgba(255, 255, 255, 0.015);
    }
    .dep-main {
        display: flex;
        align-items: baseline;
        gap: 4px;
        flex-wrap: wrap;
    }
    .dep-time {
        font-family: 'SF Mono', 'Menlo', 'Consolas', monospace;
        font-size: 0.95em;
        font-weight: 600;
        white-space: nowrap;
    }
    .dep-dest {
        font-weight: 500;
        flex: 1;
        min-width: 0;
    }
    .dep-dest--cancelled {
        text-decoration: line-through;
        opacity: 0.5;
    }
    .dep-mins {
        font-size: 0.75em;
        font-weight: 600;
        padding: 1px 6px;
        border-radius: 8px;
        background: rgba(100, 200, 100, 0.15);
        color: rgba(100, 220, 100, 0.9);
        white-space: nowrap;
        flex-shrink: 0;
    }
    .dep-mins--rush {
        background: rgba(255, 70, 70, 0.15);
        color: rgba(255, 120, 120, 0.9);
    }
    .dep-mins--soon {
        background: rgba(255, 180, 50, 0.15);
        color: rgba(255, 200, 80, 0.9);
    }
    .dep-details {
        display: flex;
        align-items: baseline;
        gap: 6px;
        margin-top: 2px;
        font-size: 0.78em;
        opacity: 0.55;
        flex-wrap: wrap;
    }
    .dep-sep {
        opacity: 0.35;
    }
    .dep-plat {
        font-weight: 600;
        opacity: 1.0;
        padding: 0 5px;
        border-radius: 4px;
        background: rgba(255, 255, 255, 0.07);
        white-space: nowrap;
    }
    .dep-status {
        font-weight: 600;
    }
    .dep-status--delayed {
        color: rgba(255, 180, 50, 0.9);
        opacity: 1.0;
    }
    .dep-status--cancelled {
        color: rgba(255, 70, 70, 0.9);
        opacity: 1.0;
    }
    .dep-timetable-tag {
        font-size: 0.85em;
        opacity: 0.5;
        font-style: italic;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

def _render_departure_html(
    dep: "Departure",
    is_tfl: bool,
    walking_time: int,
    plat_override: str | None = None,
) -> str:
    """Build styled HTML for a single departure card."""
    e = _html.escape

    # ── Card modifier class ──
    is_timetable = is_tfl and dep.status == DepartureStatus.NO_REPORT
    if dep.is_cancelled:
        card_cls = "dep-card dep-card--cancelled"
    elif dep.is_delayed:
        card_cls = "dep-card dep-card--delayed"
    elif is_timetable:
        card_cls = "dep-card dep-card--timetable"
    else:
        card_cls = "dep-card"

    # ── Main line: departure time → destination + minutes badge ──
    time_part = e(dep.display_time)
    dest_cls = "dep-dest dep-dest--cancelled" if dep.is_cancelled else "dep-dest"
    dest_text = e(dep.destination)
    if is_timetable:
        dest_text += ' <span class="dep-timetable-tag">sched.</span>'

    # ── Minutes-until badge ──
    mins = dep.minutes_until
    mins_html = ""
    if mins is not None and not dep.is_cancelled:
        if mins <= walking_time:
            mins_cls = "dep-mins dep-mins--rush"
        elif mins <= walking_time + 3:
            mins_cls = "dep-mins dep-mins--soon"
        else:
            mins_cls = "dep-mins"
        mins_html = f'<span class="{mins_cls}">{mins} min</span>'

    # ── Details line: arrival info + platform + status + operator ──
    details_parts: list[str] = []

    if dep.display_arrival_time:
        arr_text = f"arr. {e(dep.display_arrival_time)}"
        if dep.display_duration:
            arr_text += f" ({e(dep.display_duration)})"
        details_parts.append(f'<span>{arr_text}</span>')

    plat = plat_override if plat_override is not None else dep.display_platform
    if plat:
        details_parts.append(f'<span class="dep-plat">{e(plat)}</span>')

    if dep.is_delayed:
        details_parts.append(f'<span class="dep-status dep-status--delayed">{e(dep.status.value)}</span>')
    elif dep.is_cancelled:
        details_parts.append(f'<span class="dep-status dep-status--cancelled">{e(dep.status.value)}</span>')

    if dep.operator:
        details_parts.append(f'<span>{e(dep.operator)}</span>')

    details_html = ' <span class="dep-sep">·</span> '.join(details_parts)
    details_line = f'<div class="dep-details">{details_html}</div>' if details_html else ""

    return (
        f'<div class="{card_cls}">'
        f'  <div class="dep-main">'
        f'    <span class="dep-time">{time_part}</span>'
        f'    <span style="opacity:0.4">→</span>'
        f'    <span class="{dest_cls}">{dest_text}</span>'
        f'    {mins_html}'
        f'  </div>'
        f'  {details_line}'
        f'</div>'
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
        is_tfl = dynamic_leg.api_source == "tfl"
        cards_html: list[str] = []
        for dep in visible_departures:
            plat_override: str | None = None
            if dep.display_platform is None and not is_tfl:
                plat_override = "plat. TBD"
            cards_html.append(
                _render_departure_html(dep, is_tfl, walking_time, plat_override)
            )
        if cards_html:
            st.markdown("\n".join(cards_html), unsafe_allow_html=True)

# ── Persist all selections into the URL (survives auto-refresh and manual reload) ──
_persist_query_params(routes, _col_selections)
