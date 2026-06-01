from __future__ import annotations

from typing import List

from normalizers.currency import get_converter
from storage.models import HotelDeal, RawHotelResult
from utils.confidence import compute_hotel_confidence
from utils.logging_config import get_logger

log = get_logger(__name__)


async def normalize_hotels(raw: List[RawHotelResult]) -> List[HotelDeal]:
    """
    Convert raw hotel results to normalized HotelDeal objects with EUR prices.
    """
    converter = get_converter()
    await converter.ensure_fresh()

    # Group by name+location for dedup / confidence
    groups: dict[str, List[RawHotelResult]] = {}
    for r in raw:
        key = f"{r.name.lower().strip()}_{r.location.lower().strip()}"
        groups.setdefault(key, []).append(r)

    deals: List[HotelDeal] = []
    for key, group in groups.items():
        best = min(group, key=lambda x: converter.to_eur(x.price_per_night, x.currency))
        confidence = compute_hotel_confidence(group)
        price_per_night_eur = converter.to_eur(best.price_per_night, best.currency)

        if price_per_night_eur <= 0:
            continue

        try:
            deal = HotelDeal(
                name=best.name,
                location=best.location,
                price_per_night_eur=price_per_night_eur,
                nights=best.nights,
                total_price_eur=round(price_per_night_eur * best.nights, 2),
                rating=best.rating,
                stars=best.stars,
                booking_url=best.booking_url,
                source=best.source,
                data_confidence_score=confidence,
                scraped_at=best.scraped_at,
                raw_currency=best.currency,
                raw_price_per_night=best.price_per_night,
                check_in=best.check_in,
                check_out=best.check_out,
            )
            deals.append(deal)
        except Exception as exc:
            log.warning("hotel_normalize_error", key=key, error=str(exc))

    log.info("hotels_normalized", raw_count=len(raw), normalized_count=len(deals))
    return deals
