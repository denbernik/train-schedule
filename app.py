import streamlit as st

from src.clients.tfl import fetch_departures as fetch_tfl
from src.clients.transport_api import fetch_departures as fetch_national_rail
from src.models import StationBoard
from src.routes import RouteLeg, load_routes


def _fetch_leg(leg: RouteLeg) -> StationBoard:
    if leg.api_source == "transport_api":
        return fetch_national_rail(
            station_code=leg.origin_station_id,
            calling_at=leg.destination_station_id,
        )
    if leg.api_source == "tfl":
        return fetch_tfl(station_id=leg.origin_station_id)
    raise ValueError(f"Unknown api_source: {leg.api_source}")


routes = load_routes()

st.title("🚂 Departure Board")

columns = st.columns(len(routes))

for col, route in zip(columns, routes):
    with col:
        for leg in route.legs:
            board = _fetch_leg(leg)
            st.subheader(f"{board.station_name} → {leg.destination_name}")
            if board.has_error:
                st.error(board.error_message)
            else:
                for dep in board.departures:
                    status = f"({dep.status.value})" if dep.is_delayed or dep.is_cancelled else ""
                    st.write(f"{dep.display_time} → {dep.destination} {status}")