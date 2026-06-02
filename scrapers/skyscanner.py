from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import List, Optional

import httpx

from config import get_settings
from scrapers.base import BaseFlightScraper, build_client
from storage.models import RawFlightResult, ScraperParams
from utils.logging_config import get_logger
from utils.retry import async_retry

log = get_logger(__name__)


class SkyscannerScraper(BaseFlightScraper):
    """Skyscanner via RapidAPI.

    Endpoint is configurable via RAPIDAPI_SKYSCANNER_ENDPOINT in .env.
    If you see 404s, open the "Endpoints" tab in your RapidAPI console and
    set that value to the correct path (e.g. /api/v2/flights/search).
    """

    source_id = "skyscanner_api"

    def __init__(self) -> None:
        settings = get_settings()
        self._key = settings.rapidapi_key
        self._host = settings.rapidapi_skyscanner_host
        self._endpoint = settings.rapidapi_skyscanner_endpoint
        self._base_url = f"https://{self._host}{self._endpoint}"
        self.enabled = bool(self._key)
        self._logged_sample = False  # log one raw response per session to aid debugging

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

        resp = await client.get(self._base_url, params=params, headers=self._headers())
        resp.raise_for_status()
        data = resp.json()

        # Log one raw sample per session so endpoint/format issues are visible
        if not self._logged_sample:
            self._logged_sample = True
            top_keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            log.info(
                "skyscanner_response_sample",
                url=self._base_url,
                top_level_keys=top_keys,
                preview=str(data)[:300],
            )

        results: List[RawFlightResult] = []

        # Try the most common response shapes
        itineraries = (
            data.get("data", {}).get("itineraries")          # shape A: {data:{itineraries:[]}}
            or data.get("itineraries")                        # shape B: {itineraries:[]}
            or data.get("data", {}).get("flights")            # shape C: {data:{flights:[]}}
            or data.get("flights")                            # shape D: {flights:[]}
            or data.get("results")                            # shape E: {results:[]}
            or []
        )

        for item in itineraries:
            try:
                # Shape A/B (Skyscanner-style)
                price_raw = (
                    item.get("price", {}).get("raw")
                    or item.get("price", {}).get("amount")
                    or item.get("minPrice")
                    or item.get("total")
                    or 0
                )
                if not price_raw or float(price_raw) <= 0:
                    continue

                legs = item.get("legs") or item.get("segments") or []
                airline = None
                if legs:
                    carriers = legs[0].get("carriers", {}).get("marketing", [])
                    if carriers:
                        airline = carriers[0].get("name")
                    elif legs[0].get("airline"):
                        airline = legs[0]["airline"]

                results.append(
                    RawFlightResult(
                        origin=origin,
                        destination=destination,
                        price=float(price_raw),
                        currency="EUR",
                        departure_date=dep_date,
                        return_date=return_date,
                        airline=airline,
                        booking_url=item.get("deeplink") or item.get("url") or item.get("bookingUrl"),
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
            for origin in params.origins[:3]:
                for dest in params.destinations[:10]:
                    dep = params.departure_date_from
                    ret_date = dep + timedelta(days=params.nights_min + 1) if params.nights_min else None
                    tasks.append(
                        self._search_one_pair(client, origin, dest, dep, ret_date, params.adults)
                    )
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for r in gathered:
                if isinstance(r, list):
                    results.extend(r)
                elif isinstance(r, Exception):
                    log.warning("skyscanner_pair_failed", error=str(r))
        return results
