from __future__ import annotations

import asyncio
from typing import List, Tuple

from scrapers.base import BaseFlightScraper, BaseHotelScraper
from scrapers.booking_com import BookingComScraper
from scrapers.going import GoingScraper
from scrapers.google_flights import GoogleFlightsScraper
from scrapers.holiday_pirates import HolidayPiratesFlightScraper, HolidayPiratesHotelScraper
from scrapers.secret_flying import SecretFlyingScraper
from scrapers.skyscanner import SkyscannerScraper
from storage.models import RawFlightResult, RawHotelResult, ScraperParams
from utils.logging_config import get_logger

log = get_logger(__name__)


def build_flight_scrapers() -> List[BaseFlightScraper]:
    return [
        SkyscannerScraper(),
        GoogleFlightsScraper(),
        SecretFlyingScraper(),
        HolidayPiratesFlightScraper(),
        GoingScraper(),
    ]


def build_hotel_scrapers() -> List[BaseHotelScraper]:
    return [
        BookingComScraper(),
        HolidayPiratesHotelScraper(),
    ]


class ScraperAggregator:
    """
    Runs all scrapers concurrently.
    Any scraper failure is isolated — pipeline continues with remaining results.
    """

    def __init__(self) -> None:
        self._flight_scrapers = build_flight_scrapers()
        self._hotel_scrapers = build_hotel_scrapers()

    async def collect_flights(
        self, params: ScraperParams
    ) -> Tuple[List[RawFlightResult], dict]:
        """Returns (results, stats) where stats maps source → count."""
        tasks = [s.safe_scrape(params) for s in self._flight_scrapers]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        results: List[RawFlightResult] = []
        stats: dict = {}
        for scraper, outcome in zip(self._flight_scrapers, gathered):
            if isinstance(outcome, list):
                stats[scraper.source_id] = len(outcome)
                results.extend(outcome)
            else:
                stats[scraper.source_id] = 0
                log.error(
                    "aggregator_flight_error",
                    source=scraper.source_id,
                    error=str(outcome),
                )

        log.info("flights_collected", total=len(results), sources=stats)
        return results, stats

    async def collect_hotels(
        self, params: ScraperParams
    ) -> Tuple[List[RawHotelResult], dict]:
        tasks = [s.safe_scrape(params) for s in self._hotel_scrapers]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        results: List[RawHotelResult] = []
        stats: dict = {}
        for scraper, outcome in zip(self._hotel_scrapers, gathered):
            if isinstance(outcome, list):
                stats[scraper.source_id] = len(outcome)
                results.extend(outcome)
            else:
                stats[scraper.source_id] = 0
                log.error(
                    "aggregator_hotel_error",
                    source=scraper.source_id,
                    error=str(outcome),
                )

        log.info("hotels_collected", total=len(results), sources=stats)
        return results, stats
