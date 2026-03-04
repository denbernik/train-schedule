"""
Application configuration.

Single source of truth for all settings. Values are loaded from environment
variables (with .env file support), validated on startup, and accessed
throughout the app via the get_settings() function.

Why pydantic-settings rather than just os.getenv():
- Type validation: catches misconfiguration at startup, not at runtime
- Defaults: sensible defaults documented in one place
- .env support: built-in, no extra code needed
- Immutability: settings are frozen after load, preventing accidental mutation

Usage anywhere in the app:
    from src.config import get_settings
    settings = get_settings()
    print(settings.tfl_api_key)
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All application configuration in one place.

    Environment variable names are case-insensitive. So TFL_API_KEY,
    tfl_api_key, and Tfl_Api_Key in your .env file all work.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Freeze settings after creation — prevents accidental mutation
        frozen=True,
    )

    # --- TfL API (East Putney, District Line) ---
    # Optional because TfL works without a key, just with lower rate limits.
    # Register at https://api-portal.tfl.gov.uk/ for higher limits.
    tfl_api_key: str = ""

    # TfL station ID for East Putney — this is the NaPTAN ID used by the API.
    # You can look up other station IDs at https://api.tfl.gov.uk/StopPoint/Search/{name}
    tfl_station_id: str = "940GZZLUEPY"  # East Putney

    # --- TransportAPI (Wandsworth Town, National Rail) ---
    # Register at https://developer.transportapi.com/ for credentials.
    transport_api_app_id: str = ""
    transport_api_app_key: str = ""
    ldb_access_token: str = ""

    # Station code for Wandsworth Town — this is the CRS (Computer Reservation System) code.
    # Look up codes at https://www.nationalrail.co.uk/stations_destinations/default.aspx
    national_rail_station_code: str = "WNT"  # Wandsworth Town

    # --- Display settings ---
    # How many departures to show per station (default fallback)
    max_departures: int = 5

    # TfL board is denser than National Rail, so allow a larger default window.
    # This controls "next N trains" for TfL legs.
    tfl_max_departures: int = 15

    # --- HTTP timeouts ---
    # Keep TfL relatively tight so the board remains responsive.
    tfl_timeout_seconds: int = 30

    # TransportAPI free limits are strict; allow a much longer timeout window.
    transport_api_timeout_seconds: int = 3600

    # --- Rail Data Marketplace LDB API ---
    ldb_base_url: str = "https://api1.raildata.org.uk/1010-live-departure-board-dep1_2"
    ldb_with_details_base_url: str = ""
    ldb_api_version: str = "20220120"
    ldb_timeout_seconds: int = 30
    ldb_default_num_rows: int = 10
    ldb_default_filter_type: str = "to"
    ldb_default_time_offset: int = 0
    ldb_default_time_window: int = 480

    # Refresh cadence for TransportAPI-backed rows (seconds).
    # We cache National Rail responses to avoid burning low daily quotas.
    transport_api_refresh_seconds: int = 3600

    # Refresh cadence for National Rail board rows (seconds).
    # LDB has better limits than TransportAPI; 60s keeps data fresh.
    national_rail_refresh_seconds: int = 60

    # How many National Rail departures to prefetch each refresh cycle.
    # UI still applies catchability filter and final 5-row cap.
    transport_api_prefetch_departures: int = 40
    national_rail_prefetch_departures: int = 40

    # How often to refresh data, in seconds.
    # Keep this reasonably low so East Putney eastbound services appear quickly.
    # 60s is a good balance between freshness and API usage.
    refresh_interval_seconds: int = 60

    # --- App metadata ---
    app_title: str = "🚂 Departure Board"
    app_description: str = "Real-time departures from Wandsworth Town & East Putney"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Returns the app settings, creating them once and caching forever.

    Why lru_cache rather than a module-level variable:
    - Lazy initialisation: settings aren't loaded until first access,
      which means import errors from missing .env don't break test imports.
    - Still effectively a singleton: called many times, created once.
    - Easy to override in tests: you can clear the cache and provide
      different settings.

    In tests, reset with: get_settings.cache_clear()
    """
    return Settings()