from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import httpx

from config import get_settings
from scrapers.base import BaseFlightScraper, build_client
from storage.models import RawFlightResult, ScraperParams
from utils.logging_config import get_logger
from utils.retry import async_retry

log = get_logger(__name__)

_DEFAULT_ENDPOINT = "/api/v1/searchFlights"

# Each RapidAPI Skyscanner provider uses a different endpoint + parameter scheme.
# We probe combinations of (endpoint, param_scheme) until one returns 200.
_ENDPOINT_CANDIDATES = [
    "/flights/searchFlights",
    "/api/v1/searchFlights",
    "/api/v1/flights/searchFlights",
    "/api/v2/flights/searchFlights",
    "/flights/search-one-way",
    "/search",
]

# Different parameter naming conventions used by various Skyscanner RapidAPI providers
_PARAM_SCHEMES: List[Dict[str, str]] = [
    # Scheme A: originSkyId / destinationSkyId (skyscanner-flights-travel-api)
    {
        "origin_key": "originSkyId",
        "dest_key": "destinationSkyId",
        "date_key": "date",
        "return_key": "returnDate",
        "adults_key": "adults",
        "currency_key": "currency",
    },
    # Scheme B: origin / destination (skyscanner50, sky-scrapper)
    {
        "origin_key": "origin",
        "dest_key": "destination",
        "date_key": "date",
        "return_key": "returnDate",
        "adults_key": "adults",
        "currency_key": "currency",
    },
    # Scheme C: fromEntityId / toEntityId
    {
        "origin_key": "fromEntityId",
        "dest_key": "toEntityId",
        "date_key": "departDate",
        "return_key": "returnDate",
        "adults_key": "adults",
        "currency_key": "currency",
    },
]


def _build_params(
    scheme: Dict[str, str],
    origin: str,
    destination: str,
    dep_date: date,
    return_date: Optional[date],
    adults: int,
) -> dict:
    params = {
        scheme["origin_key"]: origin,
        scheme["dest_key"]: destination,
        scheme["date_key"]: dep_date.strftime("%Y-%m-%d"),
        scheme["adults_key"]: str(adults),
        scheme["currency_key"]: "EUR",
    }
    if return_date:
        params[scheme["return_key"]] = return_date.strftime("%Y-%m-%d")
    return params


class SkyscannerScraper(BaseFlightScraper):
    """Skyscanner via RapidAPI.

    On first call, probes combinations of endpoint paths and parameter naming
    schemes to discover the correct API contract. Caches the working combination
    for the session.

    Override with RAPIDAPI_SKYSCANNER_ENDPOINT in .env to skip endpoint probing
    (parameter scheme probing still runs).
    """

    source_id = "skyscanner_api"

    def __init__(self) -> None:
        settings = get_settings()
        self._key = settings.rapidapi_key
        self._host = settings.rapidapi_skyscanner_host
        self._configured_endpoint = settings.rapidapi_skyscanner_endpoint
        self._discovered_endpoint: Optional[str] = None
        self._discovered_scheme: Optional[Dict[str, str]] = None
        self._probed = False
        self.enabled = bool(self._key)
        self._logged_sample = False

    def _headers(self) -> dict:
        return {
            "X-RapidAPI-Key": self._key,
            "X-RapidAPI-Host": self._host,
        }

    async def _probe(self, client: httpx.AsyncClient) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
        """Try combinations of endpoints × param schemes until one returns 200.

        Returns (endpoint, param_scheme) or (None, None) if all fail.
        """
        user_overrode = self._configured_endpoint != _DEFAULT_ENDPOINT
        endpoints = [self._configured_endpoint] if user_overrode else _ENDPOINT_CANDIDATES

        for endpoint in endpoints:
            url = f"https://{self._host}{endpoint}"
            for scheme in _PARAM_SCHEMES:
                test_params = _build_params(
                    scheme, "MXP", "LHR", date(2026, 7, 1), None, 1
                )
                try:
                    resp = await client.get(url, params=test_params, headers=self._headers())
                    if resp.status_code == 404:
                        break  # endpoint doesn't exist, try next endpoint
                    if resp.status_code == 200:
                        log.info(
                            "skyscanner_api_discovered",
                            endpoint=endpoint,
                            param_scheme=scheme["origin_key"],
                            status=200,
                        )
                        return endpoint, scheme
                    # 422/400 = endpoint exists but wrong params, try next scheme
                    if resp.status_code in (422, 400):
                        continue
                    # 429 = rate limited, endpoint + scheme probably correct
                    if resp.status_code == 429:
                        log.info(
                            "skyscanner_api_discovered",
                            endpoint=endpoint,
                            param_scheme=scheme["origin_key"],
                            status=429,
                            note="rate limited but endpoint confirmed",
                        )
                        return endpoint, scheme
                except Exception:
                    continue

        return None, None

    @async_retry(
        max_attempts=3, min_wait=2.0, max_wait=16.0,
        retry_on=(httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
    )
    async def _search_one_pair(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        scheme: Dict[str, str],
        origin: str,
        destination: str,
        dep_date: date,
        return_date: Optional[date],
        adults: int,
    ) -> List[RawFlightResult]:
        params = _build_params(scheme, origin, destination, dep_date, return_date, adults)

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
            if not self._probed:
                self._probed = True
                self._discovered_endpoint, self._discovered_scheme = await self._probe(client)
                if not self._discovered_endpoint or not self._discovered_scheme:
                    log.error(
                        "skyscanner_no_working_api",
                        host=self._host,
                        hint="No endpoint+parameter combination returned 200. "
                             "Check your RapidAPI subscription and set RAPIDAPI_SKYSCANNER_ENDPOINT in .env",
                    )
                    self.enabled = False
                    return results

            if not self._discovered_endpoint or not self._discovered_scheme:
                return results

            base_url = f"https://{self._host}{self._discovered_endpoint}"
            tasks = []
            for origin in params.origins[:3]:
                for dest in params.destinations[:10]:
                    dep = params.departure_date_from
                    ret_date = dep + timedelta(days=params.nights_min + 1) if params.nights_min else None
                    tasks.append(
                        self._search_one_pair(
                            client, base_url, self._discovered_scheme,
                            origin, dest, dep, ret_date, params.adults,
                        )
                    )
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for r in gathered:
                if isinstance(r, list):
                    results.extend(r)
                elif isinstance(r, Exception):
                    log.warning("skyscanner_pair_failed", error=str(r))
        return results
