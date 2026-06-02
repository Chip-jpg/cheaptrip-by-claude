from __future__ import annotations

import asyncio
import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import httpx

from config import get_settings
from scrapers.base import BaseFlightScraper, build_client
from storage.models import RawFlightResult, ScraperParams
from utils.logging_config import get_logger
from utils.retry import async_retry

log = get_logger(__name__)

_DEFAULT_ENDPOINT = "/api/v1/searchFlights"
_CACHE_PATH = Path("data/skyscanner_cache.json")
_PROBE_TTL_SECONDS = 3600
_ENTITY_TTL_SECONDS = 86400 * 7

_AIRPORT_SEARCH_PATHS = [
    "/flights/searchAirport",
    "/api/v1/flights/searchAirport",
    "/api/v2/flights/searchAirport",
]

_FLIGHT_SEARCH_PATHS = [
    "/flights/searchFlights",
    "/api/v1/searchFlights",
    "/api/v1/flights/searchFlights",
    "/api/v2/flights/searchFlights",
    "/flights/search-one-way",
    "/search",
]

# Confirmed entity IDs from API responses and Skyscanner documentation.
# City-level IDs (2753/2754 range) work for all airports in that city.
# Airport-level IDs (9556 range) are for specific airports.
_KNOWN_ENTITIES: Dict[str, str] = {
    # Cities (confirmed from Skyscanner docs / API)
    "LOND": "27544008",   # London
    "PARI": "27539733",   # Paris
    "NYCA": "27537542",   # New York
    # Airports (confirmed from API response)
    "MXP": "95565070",    # Milan Malpensa
}

# IATA airport codes → Skyscanner city skyId codes.
# The API prefers city-level skyIds (LOND) over airport codes (LHR).
_IATA_TO_SKYID: Dict[str, str] = {
    "LHR": "LOND", "LGW": "LOND", "STN": "LOND", "LTN": "LOND",
    "CDG": "PARI", "ORY": "PARI",
    "FCO": "ROME", "CIA": "ROME",
    "JFK": "NYCA", "EWR": "NYCA",
    "NRT": "TYOA", "HND": "TYOA",
    "MXP": "MILA", "LIN": "MILA", "BGY": "MILA",
}

# IATA → city name for airport resolution fallback
_IATA_TO_CITY: Dict[str, str] = {
    "LHR": "London", "LGW": "London", "STN": "London", "LTN": "London",
    "CDG": "Paris", "ORY": "Paris",
    "FCO": "Rome", "CIA": "Rome",
    "JFK": "New York", "EWR": "Newark",
    "NRT": "Tokyo", "HND": "Tokyo",
    "AMS": "Amsterdam", "FRA": "Frankfurt",
    "MAD": "Madrid", "BCN": "Barcelona",
    "DUB": "Dublin", "CPH": "Copenhagen",
    "ARN": "Stockholm", "HEL": "Helsinki",
    "OSL": "Oslo", "VIE": "Vienna",
    "ZRH": "Zurich", "BRU": "Brussels",
    "EDI": "Edinburgh", "GVA": "Geneva",
    "NCE": "Nice", "MRS": "Marseille",
    "OPO": "Porto", "LIS": "Lisbon",
    "ATH": "Athens", "BUD": "Budapest",
    "PRG": "Prague", "WAW": "Warsaw",
    "KRK": "Krakow", "MXP": "Milan",
    "LIN": "Milan", "BGY": "Milan",
    "VCE": "Venice", "VRN": "Verona",
    "BLQ": "Bologna", "PMI": "Palma",
    "IBZ": "Ibiza", "TFS": "Tenerife",
    "ACE": "Lanzarote", "LPA": "Gran Canaria",
    "LAX": "Los Angeles", "MIA": "Miami",
    "ORD": "Chicago", "BOS": "Boston",
    "YYZ": "Toronto", "YVR": "Vancouver",
    "ICN": "Seoul", "HKG": "Hong Kong",
    "BKK": "Bangkok", "SIN": "Singapore",
    "KUL": "Kuala Lumpur", "DXB": "Dubai",
    "DOH": "Doha", "TLV": "Tel Aviv",
    "GRU": "Sao Paulo", "EZE": "Buenos Aires",
    "SYD": "Sydney", "MEL": "Melbourne",
    "AKL": "Auckland", "NBO": "Nairobi",
    "JNB": "Johannesburg", "CPT": "Cape Town",
    "MAH": "Menorca", "GRO": "Girona",
    "SEV": "Seville", "CAI": "Cairo",
    "BOG": "Bogota", "LIM": "Lima",
    "SCL": "Santiago", "CGK": "Jakarta",
    "AUH": "Abu Dhabi",
}


def _deep_find_entity_id(obj: object) -> Optional[str]:
    if not isinstance(obj, dict):
        return None
    for key in ("entityId", "entity_id"):
        val = obj.get(key)
        if val is not None and str(val).strip():
            return str(val)
    for val in obj.values():
        if isinstance(val, dict):
            found = _deep_find_entity_id(val)
            if found:
                return found
    return None


# ── Disk cache ──────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    try:
        if _CACHE_PATH.exists():
            return json.loads(_CACHE_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_cache(data: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        log.debug("skyscanner_cache_write_failed", error=str(exc))


class SkyscannerScraper(BaseFlightScraper):
    """Skyscanner via RapidAPI with persistent cache.

    Caches discovered endpoints (1h TTL) and entity IDs (7-day TTL).
    Falls back to hardcoded entity IDs when the API is rate-limited.
    """

    source_id = "skyscanner_api"

    def __init__(self) -> None:
        settings = get_settings()
        self._key = settings.rapidapi_key
        self._host = settings.rapidapi_skyscanner_host
        self._configured_endpoint = settings.rapidapi_skyscanner_endpoint
        self._search_endpoint: Optional[str] = None
        self._airport_endpoint: Optional[str] = None
        self._entity_cache: Dict[str, str] = {}
        self._probed = False
        self.enabled = bool(self._key)
        self._logged_sample = False
        self._logged_airport_sample = False

        self._restore_from_cache()

    def _restore_from_cache(self) -> None:
        cache = _load_cache()
        if not cache:
            return

        now = time.time()

        entities = cache.get("entities", {})
        entities_ts = cache.get("entities_cached_at", 0)
        if entities and (now - entities_ts) < _ENTITY_TTL_SECONDS:
            self._entity_cache = entities
            log.info("skyscanner_cache_entities_loaded", count=len(entities))

        probe_ts = cache.get("probe_cached_at", 0)
        if (now - probe_ts) < _PROBE_TTL_SECONDS:
            airport_ep = cache.get("airport_endpoint")
            search_ep = cache.get("search_endpoint")
            if search_ep:
                self._airport_endpoint = airport_ep
                self._search_endpoint = search_ep
                self._probed = True
                log.info(
                    "skyscanner_cache_endpoints_loaded",
                    airport=airport_ep,
                    search=search_ep,
                    age_min=round((now - probe_ts) / 60, 1),
                )

    def _persist_cache(self) -> None:
        now = time.time()
        existing = _load_cache()

        merged_entities = existing.get("entities", {})
        merged_entities.update(self._entity_cache)

        _save_cache({
            "airport_endpoint": self._airport_endpoint,
            "search_endpoint": self._search_endpoint,
            "probe_cached_at": now,
            "entities": merged_entities,
            "entities_cached_at": now,
            "host": self._host,
        })

    def _headers(self) -> dict:
        return {
            "X-RapidAPI-Key": self._key,
            "X-RapidAPI-Host": self._host,
        }

    def _get_sky_id(self, iata: str) -> str:
        return _IATA_TO_SKYID.get(iata, iata)

    def _get_entity_id(self, iata: str) -> Optional[str]:
        """Look up entity ID: cache → known hardcoded → skyId fallback."""
        if iata in self._entity_cache:
            return self._entity_cache[iata]
        sky_id = self._get_sky_id(iata)
        if sky_id in self._entity_cache:
            return self._entity_cache[sky_id]
        if iata in _KNOWN_ENTITIES:
            return _KNOWN_ENTITIES[iata]
        if sky_id in _KNOWN_ENTITIES:
            return _KNOWN_ENTITIES[sky_id]
        return None

    # ── Airport resolution ──────────────────────────────────────────────────

    async def _find_airport_endpoint(self, client: httpx.AsyncClient) -> Optional[str]:
        for path in _AIRPORT_SEARCH_PATHS:
            url = f"https://{self._host}{path}"
            try:
                resp = await client.get(
                    url,
                    params={"query": "London", "locale": "en-US"},
                    headers=self._headers(),
                )
                if resp.status_code in (200, 429):
                    log.info("skyscanner_airport_endpoint", path=path, status=resp.status_code)
                    return path
            except Exception:
                continue
        return None

    async def _resolve_entity_id(self, client: httpx.AsyncClient, iata: str) -> Optional[str]:
        cached = self._get_entity_id(iata)
        if cached:
            return cached
        if not self._airport_endpoint:
            return None

        entity_id = await self._try_resolve(client, iata)
        if entity_id:
            self._entity_cache[iata] = entity_id
            self._persist_cache()
            return entity_id

        city = _IATA_TO_CITY.get(iata)
        if city:
            entity_id = await self._try_resolve(client, city)
            if entity_id:
                self._entity_cache[iata] = entity_id
                self._persist_cache()
                return entity_id

        return None

    async def _try_resolve(self, client: httpx.AsyncClient, query: str) -> Optional[str]:
        url = f"https://{self._host}{self._airport_endpoint}"
        try:
            resp = await client.get(
                url,
                params={"query": query, "locale": "en-US"},
                headers=self._headers(),
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            places = data.get("data") or data.get("results") or data.get("places") or []

            if not self._logged_airport_sample:
                self._logged_airport_sample = True
                preview = str(places[0])[:400] if places else "empty"
                log.info("skyscanner_airport_sample", query=query, count=len(places), preview=preview)

            for place in places:
                sky_id = (
                    place.get("skyId")
                    or place.get("iata")
                    or place.get("id", "")
                )
                if sky_id.upper() == query.upper():
                    entity_id = _deep_find_entity_id(place)
                    if entity_id:
                        return entity_id

                nav = place.get("navigation", {})
                nav_sky = nav.get("relevantFlightParams", {}).get("skyId", "")
                if nav_sky.upper() == query.upper():
                    entity_id = _deep_find_entity_id(place)
                    if entity_id:
                        return entity_id

            if places:
                entity_id = _deep_find_entity_id(places[0])
                if entity_id:
                    return entity_id

        except Exception as exc:
            log.debug("skyscanner_resolve_failed", query=query, error=str(exc))
        return None

    # ── Batch resolution (called from CLI) ──────────────────────────────────

    async def resolve_all_airports(self, iata_codes: List[str]) -> Dict[str, str]:
        """Resolve a list of IATA codes and cache entity IDs. For CLI use."""
        resolved: Dict[str, str] = {}
        async with build_client(timeout=30.0) as client:
            if not self._airport_endpoint:
                self._airport_endpoint = await self._find_airport_endpoint(client)
                if not self._airport_endpoint:
                    log.error("skyscanner_no_airport_endpoint")
                    return resolved

            for iata in iata_codes:
                existing = self._get_entity_id(iata)
                if existing:
                    resolved[iata] = existing
                    continue

                entity_id = await self._resolve_entity_id(client, iata)
                if entity_id:
                    resolved[iata] = entity_id
                    log.info("resolved", iata=iata, entity_id=entity_id)
                else:
                    log.warning("unresolved", iata=iata)
                await asyncio.sleep(0.3)

        self._persist_cache()
        return resolved

    # ── Endpoint probing ────────────────────────────────────────────────────

    def _build_search_params(
        self,
        origin: str,
        destination: str,
        dep_date: date,
        return_date: Optional[date],
        adults: int,
        origin_entity: Optional[str] = None,
        dest_entity: Optional[str] = None,
    ) -> dict:
        params: dict = {
            "originSkyId": self._get_sky_id(origin),
            "destinationSkyId": self._get_sky_id(destination),
            "date": dep_date.strftime("%Y-%m-%d"),
            "adults": str(adults),
            "currency": "EUR",
            "market": "IT",
            "locale": "en-US",
            "cabinClass": "economy",
        }
        if origin_entity:
            params["originEntityId"] = origin_entity
        if dest_entity:
            params["destinationEntityId"] = dest_entity
        if return_date:
            params["returnDate"] = return_date.strftime("%Y-%m-%d")
        return params

    async def _probe_search_endpoint(
        self,
        client: httpx.AsyncClient,
        origin_entity: Optional[str],
        dest_entity: Optional[str],
    ) -> Optional[str]:
        user_overrode = self._configured_endpoint != _DEFAULT_ENDPOINT
        paths = [self._configured_endpoint] if user_overrode else _FLIGHT_SEARCH_PATHS

        best_422_path: Optional[str] = None

        for path in paths:
            url = f"https://{self._host}{path}"
            params = self._build_search_params(
                "MXP", "LHR", date(2026, 7, 1), None, 1,
                origin_entity, dest_entity,
            )
            try:
                resp = await client.get(url, params=params, headers=self._headers())
                if resp.status_code == 404:
                    continue
                if resp.status_code == 200:
                    log.info("skyscanner_search_endpoint", path=path, status=200)
                    return path
                if resp.status_code == 429:
                    log.info("skyscanner_search_endpoint", path=path, status=429)
                    return path
                if resp.status_code in (400, 422):
                    try:
                        body = resp.json()
                    except Exception:
                        body = resp.text[:300]
                    log.info(
                        "skyscanner_probe_rejected",
                        path=path,
                        status=resp.status_code,
                        body=str(body)[:300],
                    )
                    if best_422_path is None:
                        best_422_path = path
            except Exception:
                continue

        if best_422_path:
            log.warning(
                "skyscanner_using_422_endpoint",
                path=best_422_path,
                note="Best available — returned 422, may need different params",
            )
            return best_422_path
        return None

    async def _probe(self, client: httpx.AsyncClient) -> bool:
        self._airport_endpoint = await self._find_airport_endpoint(client)

        origin_entity = self._get_entity_id("MXP")
        dest_entity = self._get_entity_id("LHR")
        if self._airport_endpoint:
            if not origin_entity:
                origin_entity = await self._resolve_entity_id(client, "MXP")
            if not dest_entity:
                dest_entity = await self._resolve_entity_id(client, "LHR")
        log.info("skyscanner_entities", mxp=origin_entity, lhr=dest_entity)

        self._search_endpoint = await self._probe_search_endpoint(
            client, origin_entity, dest_entity,
        )

        if self._search_endpoint:
            self._persist_cache()

        return self._search_endpoint is not None

    # ── Actual search ───────────────────────────────────────────────────────

    @async_retry(
        max_attempts=3, min_wait=2.0, max_wait=16.0,
        retry_on=(httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
    )
    async def _search_one_pair(
        self,
        client: httpx.AsyncClient,
        origin: str,
        destination: str,
        dep_date: date,
        return_date: Optional[date],
        adults: int,
    ) -> List[RawFlightResult]:
        origin_entity = self._get_entity_id(origin)
        dest_entity = self._get_entity_id(destination)

        if not origin_entity:
            origin_entity = await self._resolve_entity_id(client, origin)
        if not dest_entity:
            dest_entity = await self._resolve_entity_id(client, destination)

        if not origin_entity or not dest_entity:
            log.debug(
                "skyscanner_skip_pair",
                origin=origin,
                destination=destination,
                origin_entity=origin_entity,
                dest_entity=dest_entity,
            )
            return []

        url = f"https://{self._host}{self._search_endpoint}"
        params = self._build_search_params(
            origin, destination, dep_date, return_date, adults,
            origin_entity, dest_entity,
        )

        resp = await client.get(url, params=params, headers=self._headers())
        resp.raise_for_status()
        data = resp.json()

        if not self._logged_sample:
            self._logged_sample = True
            top_keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            log.info(
                "skyscanner_response_sample",
                url=url,
                top_level_keys=top_keys,
                preview=str(data)[:300],
            )

        return self._parse_results(data, origin, destination, dep_date, return_date)

    @staticmethod
    def _parse_results(
        data: dict,
        origin: str,
        destination: str,
        dep_date: date,
        return_date: Optional[date],
    ) -> List[RawFlightResult]:
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
                        booking_url=(
                            item.get("deeplink")
                            or item.get("url")
                            or item.get("bookingUrl")
                        ),
                        source="skyscanner_api",
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
                found = await self._probe(client)
                if not found:
                    log.error(
                        "skyscanner_no_working_api",
                        host=self._host,
                        hint="No working endpoint found. Check your RapidAPI subscription "
                             "and set RAPIDAPI_SKYSCANNER_ENDPOINT in .env",
                    )
                    self.enabled = False
                    return results

            if not self._search_endpoint:
                return results

            tasks = []
            for origin in params.origins[:3]:
                for dest in params.destinations[:10]:
                    dep = params.departure_date_from
                    ret_date = (
                        dep + timedelta(days=params.nights_min + 1)
                        if params.nights_min
                        else None
                    )
                    tasks.append(
                        self._search_one_pair(
                            client, origin, dest, dep, ret_date, params.adults,
                        )
                    )
            gathered = await asyncio.gather(*tasks, return_exceptions=True)
            for r in gathered:
                if isinstance(r, list):
                    results.extend(r)
                elif isinstance(r, Exception):
                    log.warning("skyscanner_pair_failed", error=str(r))
        return results
