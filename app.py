import streamlit as st
from src.clients.tfl import fetch_departures as fetch_tfl
from src.clients.transport_api import fetch_departures as fetch_national_rail

st.title("🚂 Departure Board")

col1, col2 = st.columns(2)

with col1:
    tfl_board = fetch_tfl()
    st.subheader(tfl_board.station_name)
    if tfl_board.has_error:
        st.error(tfl_board.error_message)
    else:
        for dep in tfl_board.departures:
            st.write(f"{dep.display_time} → {dep.destination}")

with col2:
    nr_board = fetch_national_rail()
    st.subheader(nr_board.station_name)
    if nr_board.has_error:
        st.error(nr_board.error_message)
    else:
        for dep in nr_board.departures:
            status = f"({dep.status.value})" if dep.is_delayed or dep.is_cancelled else ""
            st.write(f"{dep.display_time} → {dep.destination} {status}")