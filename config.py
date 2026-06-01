from __future__ import annotations

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    # AI
    anthropic_api_key: str = Field(default="")

    # Skyscanner via RapidAPI
    rapidapi_key: str = Field(default="")
    rapidapi_skyscanner_host: str = Field(default="skyscanner50.p.rapidapi.com")

    # Currency
    exchange_rate_api_key: str = Field(default="")

    # Storage
    database_url: str = Field(default="sqlite+aiosqlite:///./data/travel_deals.db")

    # Scheduler
    scrape_interval_minutes: int = Field(default=90)
    digest_hour: int = Field(default=8)
    digest_minute: int = Field(default=0)

    # Alert thresholds
    europe_trip_max_eur: float = Field(default=120.0)
    longhaul_trip_max_eur: float = Field(default=450.0)
    hotel_discount_min_pct: float = Field(default=60.0)
    flight_discount_min_pct: float = Field(default=60.0)
    instant_alerts_per_hour: int = Field(default=5)

    # Logging
    log_level: str = Field(default="INFO")
    log_file: str = Field(default="./logs/engine.log")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# ── Airport layers ────────────────────────────────────────────────────────────

LAYER_1_AIRPORTS = ["MXP", "LIN", "BGY"]

LAYER_2_AIRPORTS = ["VCE", "VRN", "BLQ", "FCO", "CIA"]

LAYER_3_HUBS = ["LHR", "LGW", "AMS", "CDG", "FRA", "MAD", "BCN", "DUB"]

ALL_ORIGIN_AIRPORTS = LAYER_1_AIRPORTS + LAYER_2_AIRPORTS + LAYER_3_HUBS

# Primary search destinations — popular short/long haul from Italy
POPULAR_DESTINATIONS = [
    # Europe
    "KRK", "WAW", "PRG", "BUD", "LIS", "ATH", "DUB", "CPH", "ARN",
    "HEL", "OSL", "VIE", "ZRH", "BRU", "EDI", "GVA", "NCE", "MRS",
    "OPO", "SEV", "MAH", "IBZ", "PMI", "TFS", "ACE", "LPA",
    # Long-haul
    "JFK", "EWR", "LAX", "MIA", "ORD", "BOS", "YYZ", "YVR",
    "NRT", "HND", "ICN", "HKG", "BKK", "SIN", "KUL", "CGK",
    "DXB", "AUH", "DOH", "TLV", "CAI",
    "GRU", "EZE", "BOG", "LIM", "SCL",
    "JNB", "CPT", "NBO",
    "SYD", "MEL", "AKL",
]

# Repositioning threshold constants
REPOSITIONING_MIN_SAVING_EUR = 80.0
REPOSITIONING_MIN_SAVING_PCT = 0.25
REPOSITIONING_MIN_LAYOVER_HOURS = 2.0

# Deduplication constants
DEDUP_PRICE_TOLERANCE_PCT = 0.05
DEDUP_DATE_TOLERANCE_DAYS = 2
DEDUP_WINDOW_DAYS = 7

# Time decay windows (hours)
DECAY_INSTANT_CUTOFF = 6
DECAY_DIGEST_CUTOFF = 24
