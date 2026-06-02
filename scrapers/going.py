from __future__ import annotations

import re
from datetime import date
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from scrapers.base import BaseFlightScraper, build_client, random_headers
from storage.models import RawFlightResult, ScraperParams
from utils.logging_config import get_logger
from utils.retry import async_retry

log = get_logger(__name__)

_BASE = "https://going.com"
_DEALS_URL = f"{_BASE}/deals"

_AIRPORT_RE = re.compile(r"\b([A-Z]{3})\b")
_PRICE_RE = re.compile(r"\$\s*(\d{1,4}(?:\.\d{2})?)")
_FROM_RE = re.compile(r"from\s+\$(\d{1,4})", re.I)


class GoingScraper(BaseFlightScraper):
    """Scrapes Going (formerly Scott's Cheap Flights) public deal feed."""

    source_id = "going"

    @async_retry(
        max_attempts=3, min_wait=2.0, max_wait=15.0,
        retry_on=(httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
    )
    async def _fetch(self, client: httpx.AsyncClient, url: str) -> Optional[str]:
        resp = await client.get(url, headers=random_headers())
        resp.raise_for_status()
        return resp.text

    def _parse_card(self, card: BeautifulSoup, dep_date: date) -> Optional[RawFlightResult]:
        text = card.get_text(separator=" ")
        airports = [a for a in _AIRPORT_RE.findall(text.upper()) if a.isalpha()]

        origin, dest = None, None
        if len(airports) >= 2:
            origin, dest = airports[0], airports[1]
        if not origin or not dest:
            return None

        price_m = _FROM_RE.search(text) or _PRICE_RE.search(text)
        if not price_m:
            return None
        try:
            price = float(price_m.group(1))
        except ValueError:
            return None
        if price < 5 or price > 3000:
            return None

        link = card.find("a", href=True)
        booking_url = None
        if link:
            href = link["href"]
            booking_url = href if href.startswith("http") else f"{_BASE}{href}"

        return RawFlightResult(
            origin=origin,
            destination=dest,
            price=price,
            currency="USD",
            departure_date=dep_date,
            booking_url=booking_url,
            source=self.source_id,
        )

    async def scrape(self, params: ScraperParams) -> List[RawFlightResult]:
        results: List[RawFlightResult] = []
        async with build_client(timeout=20.0) as client:
            try:
                html = await self._fetch(client, _DEALS_URL)
                if not html:
                    return results
                soup = BeautifulSoup(html, "lxml")
                cards = soup.find_all("article") or soup.find_all(
                    "div", class_=re.compile(r"deal|card|offer|flight", re.I)
                )
                for card in cards:
                    r = self._parse_card(card, params.departure_date_from)
                    if r:
                        results.append(r)
            except Exception as exc:
                log.warning("going_scrape_failed", error=str(exc))
        return results
