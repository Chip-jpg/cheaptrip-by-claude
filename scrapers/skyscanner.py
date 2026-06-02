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

# Known endpoint patterns across various Skyscanner RapidAPI providers
_ENDPOINT_CANDIDATES = [
    "/api/v1/searchFlights",
    "/api/v1/flights/searchFlights",
    "/api/v2/flights/searchFlights",
    "/flights/searchFlights",
    "/flights/search-one-way",
    "/search",
]

_DEFAULT_ENDPOINT = "/api/v1/searchFlights"


class SkyscannerScraper(BaseFlightScraper):
    """Skyscanner via RapidAPI.

    On first call, probes multiple known endpoint paths to discover which one
    the subscribed API supports. Caches the working endpoint for the session.

    Override with RAPIDAPI_SKYSCANNER_ENDPOINT in .env to skip probing.
    """

    source_id = "skyscanner_api"

    def __init__(self) -> None:
        settings = get_settings()
        self._key = settings.rapidapi_key
        self._host = settings.rapidapi_skyscanner_host
        self._configured_endpoint = settings.rapidapi_skyscanner_endpoint
        self._discovered_endpoint: Optional[str] = None
        self._probed = False
        self.enabled = bool(self._key)
        self._logged_sample = False

    def _headers(self) -> dict:
        return {
            "X-RapidAPI-Key": self._key,
            "X-RapidAPI-Host": self._host,
        }

    async def _probe_endpoint(self, client: httpx.AsyncClient) -> Optional[str]:
        """Try each candidate endpoint with a lightweight test query.

        Returns the first endpoint that doesn't 404, or None if all fail.
        Any response other than 404 (200, 400, 429, 500) means the path exists.
        """
        test_params = {
            "origin": "MXP",
            "destination": "LHR",
            "date": "2026-07-01",
            "adults": "1",
            "currency": "EUR",
        }

        # If user explicitly set endpoint to something non-default, try it first
        user_overrode = self._configured_endpoint != _DEFAULT_ENDPOINT
        candidates = (
            [self._configured_endpoint] if user_overrode
            else _ENDPOINT_CANDIDATES
        )

        for endpoint in candidates:
            url = f"https://{self._host}{endpoint}"
            try:
                resp = await client.get(url, params=test_params, headers=self._headers())
                if resp.status_code != 404:
                    log.info(
                        "skyscanner_endpoint_discovered",
                        endpoint=endpoint,
                        status=resp.status_code,
                        host=self._host,
                    )
                    return endpoint
            except Exception:
                continue

        return None

    @async_retry(
        max_attempts=3, min_wait=2.0, max_wait=16.0,
        retry_on=(httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
    )
    async def _search_one_pair(
        self,
        client: httpx.AsyncClient,
        base_url: str,
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

        resp = await client.get(base_url, params=params, headers=self._headers())
        resp.raise_for_status()
        data = resp.json()

        if not self._logged_sample:
            self._logged_sample = True
            top_keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            log.info(
                "skyscanner_response_sample",
                url=base_url,
                top_level_keys=top_keys,
                preview=str(data)[:300],
            )

        results: List[RawFlightResult] = []

        itineraries = (
            data.get("data", {}).get("itineraries")
            or data.get("itineraries")
            or data.get("data", {}).get("flights")
            or data.get("flights")
            or data.get("results")
            or []
        )

        for item in itineraries:
            try:
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
        async with build_client(timeout=30.0) as client:
            # Discover endpoint on first call
            if not self._probed:
                self._probed = True
                self._discovered_endpoint = await self._probe_endpoint(client)
                if not self._discovered_endpoint:
                    log.error(
                        "skyscanner_no_working_endpoint",
                        host=self._host,
                        hint="Set RAPIDAPI_SKYSCANNER_ENDPOINT in .env — check your RapidAPI console Endpoints tab",
                    )
                    self.enabled = False
                    return results

            if not self._discovered_endpoint:
                return results

            base_url = f"https://{self._host}{self._discovered_endpoint}"
            tasks = []
            for origin in params.origins[:3]:
                for dest in params.destinations[:10]:
                    dep = params.departure_date_from
                    ret_date = dep + timedelta(days=params.nights_min + 1) if params.nights_min else None
                    tasks.append(
                        self._search_one_pair(client, base_url, origin, dest, dep, ret_date, params.adults)
                    )
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for r in gathered:
                if isinstance(r, list):
                    results.extend(r)
                elif isinstance(r, Exception):
                    log.warning("skyscanner_pair_failed", error=str(r))
        return results
