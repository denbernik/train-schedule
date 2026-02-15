import streamlit as st
from src.clients.tfl import fetch_departures

st.title("🚂 Departure Board")

board = fetch_departures()
if board.has_error:
    st.error(board.error_message)
else:
    st.subheader(board.station_name)
    for dep in board.departures:
        st.write(f"{dep.display_time} → {dep.destination} ({dep.operator})")