from __future__ import annotations

import asyncio
from datetime import date
from typing import List, Optional

import httpx

from config import get_settings
from scrapers.base import BaseFlightScraper, build_client
from storage.models import RawFlightResult, ScraperParams
from utils.logging_config import get_logger
from utils.retry import async_retry

log = get_logger(__name__)

_RAPIDAPI_BASE = "https://skyscanner50.p.rapidapi.com/api/v1"


class SkyscannerScraper(BaseFlightScraper):
    """Skyscanner via RapidAPI skyscanner50."""

    source_id = "skyscanner_api"

    def __init__(self) -> None:
        settings = get_settings()
        self._key = settings.rapidapi_key
        self._host = settings.rapidapi_skyscanner_host
        self.enabled = bool(self._key)

    def _headers(self) -> dict:
        return {
            "X-RapidAPI-Key": self._key,
            "X-RapidAPI-Host": self._host,
        }

    @async_retry(max_attempts=3, min_wait=2.0, max_wait=16.0)
    async def _search_one_pair(
        self,
        client: httpx.AsyncClient,
        origin: str,
        destination: str,
        dep_date: date,
        return_date: Optional[date],
        adults: int,
    ) -> List[RawFlightResult]:
        params: dict = {
            "origin": origin,
            "destination": destination,
            "date": dep_date.strftime("%Y-%m-%d"),
            "adults": str(adults),
            "currency": "EUR",
        }
        if return_date:
            params["returnDate"] = return_date.strftime("%Y-%m-%d")

        resp = await client.get(
            f"{_RAPIDAPI_BASE}/searchFlights",
            params=params,
            headers=self._headers(),
        )
        resp.raise_for_status()
        data = resp.json()

        results: List[RawFlightResult] = []
        itineraries = data.get("data", {}).get("itineraries", [])
        for item in itineraries:
            try:
                price_raw = item.get("price", {}).get("raw", 0)
                if price_raw <= 0:
                    continue
                legs = item.get("legs", [])
                if not legs:
                    continue
                airline = None
                carriers = legs[0].get("carriers", {}).get("marketing", [])
                if carriers:
                    airline = carriers[0].get("name")

                results.append(
                    RawFlightResult(
                        origin=origin,
                        destination=destination,
                        price=float(price_raw),
                        currency="EUR",
                        departure_date=dep_date,
                        return_date=return_date,
                        airline=airline,
                        booking_url=item.get("deeplink"),
                        source=self.source_id,
                        extra={"raw": item},
                    )
                )
            except Exception as exc:
                log.warning("skyscanner_parse_error", error=str(exc))
        return results

    async def scrape(self, params: ScraperParams) -> List[RawFlightResult]:
        results: List[RawFlightResult] = []
        tasks = []
        async with build_client(timeout=30.0) as client:
            for origin in params.origins[:3]:  # rate-limit pairs per call
                for dest in params.destinations[:10]:
                    dep = params.departure_date_from
                    ret_date = None
                    if params.nights_min:
                        from datetime import timedelta
                        ret_date = dep + timedelta(days=params.nights_min + 1)
                    tasks.append(
                        self._search_one_pair(
                            client, origin, dest, dep, ret_date, params.adults
                        )
                    )
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for r in gathered:
                if isinstance(r, list):
                    results.extend(r)
                elif isinstance(r, Exception):
                    log.warning("skyscanner_pair_failed", error=str(r))
        return results
