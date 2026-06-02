from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup

from scrapers.base import BaseFlightScraper, build_client, random_headers
from storage.models import RawFlightResult, ScraperParams
from utils.logging_config import get_logger
from utils.retry import async_retry

log = get_logger(__name__)

_BASE_URL = "https://www.secretflying.com"
_DEALS_PATHS = [
    "/posts/category/europe",
    "/posts/category/us-deals",
    "/posts/category/error-fares",
    "/posts/",
]

# IATA codes we care about
_MILAN_AIRPORTS = {"MXP", "LIN", "BGY"}
_ITALY_AIRPORTS = {"MXP", "LIN", "BGY", "VCE", "VRN", "BLQ", "FCO", "CIA"}

_AIRPORT_PATTERN = re.compile(r"\b([A-Z]{3})\b")
_PRICE_PATTERN = re.compile(r"[€\$£]?\s*(\d{1,4}(?:[.,]\d{2})?)\s*(?:€|USD|GBP|EUR)?")


def _extract_airports(text: str) -> Tuple[Optional[str], Optional[str]]:
    codes = _AIRPORT_PATTERN.findall(text.upper())
    valid = [c for c in codes if len(c) == 3 and c.isalpha()]
    if len(valid) >= 2:
        return valid[0], valid[1]
    return None, None


def _extract_price(text: str) -> Optional[float]:
    m = _PRICE_PATTERN.search(text)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None
    return None


def _guess_currency(text: str) -> str:
    if "£" in text or "GBP" in text:
        return "GBP"
    if "$" in text or "USD" in text:
        return "USD"
    return "EUR"


class SecretFlyingScraper(BaseFlightScraper):
    """Scrapes Secret Flying deal posts via HTML parsing."""

    source_id = "secret_flying"

    @async_retry(
        max_attempts=3, min_wait=2.0, max_wait=15.0,
        retry_on=(httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError),
    )
    async def _fetch_page(self, client: httpx.AsyncClient, url: str) -> Optional[str]:
        resp = await client.get(url, headers=random_headers())
        resp.raise_for_status()
        return resp.text

    def _parse_deal_card(
        self, card: BeautifulSoup, dep_date: date
    ) -> Optional[RawFlightResult]:
        # Title typically: "Milan to New York from €249!"
        title_tag = card.find(["h2", "h3", "h4", "a"])
        if not title_tag:
            return None
        title = title_tag.get_text(strip=True)

        origin, dest = _extract_airports(title)
        price = _extract_price(title)
        currency = _guess_currency(title)

        # If no airports in title, try the card body
        if not origin or not dest:
            body = card.get_text(separator=" ")
            origin, dest = _extract_airports(body)
            if not price:
                price = _extract_price(body)

        if not origin or not dest or not price:
            return None
        if price < 5 or price > 3000:
            return None

        link_tag = card.find("a", href=True)
        booking_url = None
        if link_tag and link_tag["href"].startswith("http"):
            booking_url = link_tag["href"]
        elif link_tag:
            booking_url = f"{_BASE_URL}{link_tag['href']}"

        return RawFlightResult(
            origin=origin,
            destination=dest,
            price=price,
            currency=currency,
            departure_date=dep_date,
            booking_url=booking_url,
            source=self.source_id,
        )

    def _parse_page(self, html: str, dep_date: date) -> List[RawFlightResult]:
        soup = BeautifulSoup(html, "lxml")
        results: List[RawFlightResult] = []

        # Secret Flying uses article cards
        cards = soup.find_all("article") or soup.find_all("div", class_=re.compile(r"post|deal|card", re.I))
        for card in cards:
            try:
                r = self._parse_deal_card(card, dep_date)
                if r:
                    results.append(r)
            except Exception as exc:
                log.debug("sf_card_parse_error", error=str(exc))
        return results

    async def scrape(self, params: ScraperParams) -> List[RawFlightResult]:
        results: List[RawFlightResult] = []
        dep_date = params.departure_date_from

        async with build_client(timeout=20.0) as client:
            for path in _DEALS_PATHS:
                try:
                    html = await self._fetch_page(client, f"{_BASE_URL}{path}")
                    if html:
                        found = self._parse_page(html, dep_date)
                        results.extend(found)
                except Exception as exc:
                    log.warning("sf_page_failed", path=path, error=str(exc))

        # Filter to deals with relevant origins; return empty list if none match
        # (returning all results on fallback would flood the pipeline with irrelevant deals)
        italy_relevant = [
            r for r in results
            if r.origin in _ITALY_AIRPORTS or r.destination in _ITALY_AIRPORTS
        ]
        return italy_relevant
