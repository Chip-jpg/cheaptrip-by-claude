from __future__ import annotations

from typing import List, Tuple

from config import get_settings
from storage.models import AlertTier, DealType, Trip
from utils.logging_config import get_logger

log = get_logger(__name__)

_EUROPE_AIRPORTS = {
    "KRK", "WAW", "PRG", "BUD", "LIS", "ATH", "DUB", "CPH", "ARN", "HEL", "OSL",
    "VIE", "ZRH", "BRU", "EDI", "GVA", "NCE", "MRS", "OPO", "SEV", "MAH", "IBZ",
    "PMI", "TFS", "ACE", "LPA", "LHR", "LGW", "AMS", "CDG", "FRA", "MAD", "BCN",
    "FCO", "CIA", "MXP", "LIN", "BGY", "VCE", "VRN", "BLQ",
}


def _is_europe_trip(trip: Trip) -> bool:
    dest = ""
    if trip.outbound_flight:
        dest = trip.outbound_flight.destination
    elif trip.hotel:
        # Infer from hotel location — crude but functional
        dest = trip.hotel.location[:3].upper()
    return dest in _EUROPE_AIRPORTS


def apply_hard_filters(trips: List[Trip]) -> Tuple[List[Trip], List[Trip]]:
    """
    Split trips into (instant_eligible, digest_eligible).
    Trips that pass NO threshold are discarded.

    Returns (instant, digest) — caller decides final send.
    """
    settings = get_settings()
    instant: List[Trip] = []
    digest: List[Trip] = []
    discarded = 0

    for trip in trips:
        if trip.total_cost_eur <= 0:
            discarded += 1
            continue

        is_europe = _is_europe_trip(trip)
        cost = trip.total_cost_eur
        discount = trip.discount_pct or 0.0

        # Hard instant conditions
        passes_instant = (
            (is_europe and cost < settings.europe_trip_max_eur)
            or (not is_europe and cost < settings.longhaul_trip_max_eur)
            or (trip.deal_type == DealType.HOTEL_ONLY and discount >= settings.hotel_discount_min_pct)
            or (trip.deal_type == DealType.FLIGHT_ONLY and discount >= settings.flight_discount_min_pct)
            or trip.is_error_fare
        )

        if passes_instant:
            trip.alert_tier = AlertTier.INSTANT
            instant.append(trip)
            continue

        # Digest condition: reasonable deal worth including in daily summary
        passes_digest = (
            (is_europe and cost < settings.europe_trip_max_eur * 2.5)
            or (not is_europe and cost < settings.longhaul_trip_max_eur * 1.5)
            or (discount >= 35.0)
        )

        if passes_digest:
            trip.alert_tier = AlertTier.DIGEST
            digest.append(trip)
        else:
            discarded += 1

    log.info(
        "hard_filter_results",
        instant=len(instant),
        digest=len(digest),
        discarded=discarded,
    )
    return instant, digest
