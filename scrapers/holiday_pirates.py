from __future__ import annotations

import re
from datetime import date
from typing import List, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from scrapers.base import BaseFlightScraper, BaseHotelScraper, build_client, random_headers
from storage.models import RawFlightResult, RawHotelResult, ScraperParams
from utils.logging_config import get_logger
from utils.retry import async_retry

log = get_logger(__name__)

_BASE = "https://www.holidaypirates.com"
# Try root paths first; /en/ locale prefix often redirects or 404s
_FLIGHT_CANDIDATES = [
    f"{_BASE}/deals?category=flight",
    f"{_BASE}/flights",
    f"{_BASE}/en/deals?category=flight",
    f"{_BASE}/deals",
]
_HOTEL_CANDIDATES = [
    f"{_BASE}/deals?category=hotel",
    f"{_BASE}/hotels",
    f"{_BASE}/en/deals?category=hotel",
    f"{_BASE}/deals",
]

_AIRPORT_RE = re.compile(r"\b([A-Z]{3})\b")
_PRICE_RE = re.compile(r"(?:from\s+)?[€\$£]?\s*(\d{1,4}(?:[.,]\d{2})?)\s*(?:€|EUR|USD|GBP)?", re.I)
_DISCOUNT_RE = re.compile(r"-\s*(\d{1,3})\s*%")


def _first_price(text: str) -> Optional[float]:
    for m in _PRICE_RE.finditer(text):
        try:
            val = float(m.group(1).replace(",", "."))
            if 5 < val < 5000:
                return val
        except ValueError:
            pass
    return None


def _first_discount(text: str) -> Optional[float]:
    m = _DISCOUNT_RE.search(text)
    return float(m.group(1)) if m else None


class HolidayPiratesFlightScraper(BaseFlightScraper):
    """Scrapes HolidayPirates flight deals page."""

    source_id = "holiday_pirates"

    @async_retry(
        max_attempts=3, min_wait=2.0, max_wait=15.0,
        retry_on=(httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
    )
    async def _fetch(self, client: httpx.AsyncClient, url: str) -> Optional[str]:
        resp = await client.get(url, headers=random_headers())
        resp.raise_for_status()
        return resp.text

    def _parse_flight_card(
        self, card: BeautifulSoup, dep_date: date
    ) -> Optional[RawFlightResult]:
        text = card.get_text(separator=" ")
        airports = _AIRPORT_RE.findall(text.upper())
        valid = [a for a in airports if a.isalpha()]

        origin, dest = None, None
        if len(valid) >= 2:
            origin, dest = valid[0], valid[1]
        if not origin or not dest:
            return None

        price = _first_price(text)
        if not price:
            return None

        link = card.find("a", href=True)
        booking_url = None
        if link:
            href = link["href"]
            booking_url = href if href.startswith("http") else urljoin(_BASE, href)

        currency = "GBP" if "£" in text else "USD" if "$" in text else "EUR"

        return RawFlightResult(
            origin=origin,
            destination=dest,
            price=price,
            currency=currency,
            departure_date=dep_date,
            booking_url=booking_url,
            source=self.source_id,
        )

    async def scrape(self, params: ScraperParams) -> List[RawFlightResult]:
        results: List[RawFlightResult] = []
        async with build_client(timeout=20.0) as client:
            for url in _FLIGHT_CANDIDATES:
                try:
                    html = await self._fetch(client, url)
                    if not html:
                        continue
                    soup = BeautifulSoup(html, "lxml")
                    cards = soup.find_all("article") or soup.find_all(
                        "div", class_=re.compile(r"deal|card|offer|item", re.I)
                    )
                    for card in cards:
                        r = self._parse_flight_card(card, params.departure_date_from)
                        if r:
                            results.append(r)
                    if results:
                        break  # found results, no need to try more URLs
                except Exception as exc:
                    log.warning("hp_flight_failed", url=url, error=str(exc))
        return results


class HolidayPiratesHotelScraper(BaseHotelScraper):
    """Scrapes HolidayPirates hotel deal pages."""

    source_id = "holiday_pirates"

    @async_retry(
        max_attempts=3, min_wait=2.0, max_wait=15.0,
        retry_on=(httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
    )
    async def _fetch(self, client: httpx.AsyncClient, url: str) -> Optional[str]:
        resp = await client.get(url, headers=random_headers())
        resp.raise_for_status()
        return resp.text

    def _parse_hotel_card(
        self, card: BeautifulSoup, params: ScraperParams
    ) -> Optional[RawHotelResult]:
        text = card.get_text(separator=" ")
        price = _first_price(text)
        if not price:
            return None

        discount = _first_discount(text)
        name_tag = card.find(["h2", "h3", "h4"])
        name = name_tag.get_text(strip=True) if name_tag else "Unknown Hotel"

        # Best-effort location extraction
        location = ""
        loc_pat = re.search(r"in\s+([A-Za-z\s,]+?)(?:\s*\||,|\n|$)", text)
        if loc_pat:
            location = loc_pat.group(1).strip()[:50]

        link = card.find("a", href=True)
        booking_url = None
        if link:
            href = link["href"]
            booking_url = href if href.startswith("http") else urljoin(_BASE, href)

        nights = params.nights_min or 3

        return RawHotelResult(
            name=name,
            location=location or "Unknown",
            price_per_night=price,
            currency="EUR",
            nights=nights,
            booking_url=booking_url,
            source=self.source_id,
            extra={"discount_pct": discount},
        )

    async def scrape(self, params: ScraperParams) -> List[RawHotelResult]:
        results: List[RawHotelResult] = []
        async with build_client(timeout=20.0) as client:
            for url in _HOTEL_CANDIDATES:
                try:
                    html = await self._fetch(client, url)
                    if not html:
                        continue
                    soup = BeautifulSoup(html, "lxml")
                    cards = soup.find_all("article") or soup.find_all(
                        "div", class_=re.compile(r"deal|card|offer|hotel", re.I)
                    )
                    for card in cards:
                        r = self._parse_hotel_card(card, params)
                        if r:
                            results.append(r)
                    if results:
                        break
                except Exception as exc:
                    log.warning("hp_hotel_failed", url=url, error=str(exc))
        return results
