from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import LAYER_1_AIRPORTS, LAYER_2_AIRPORTS, LAYER_3_HUBS, POPULAR_DESTINATIONS, get_settings
from filters.deduplication import deduplicate_trips
from filters.hard_filters import apply_hard_filters
from filters.time_decay import apply_time_decay
from normalizers.flight import normalize_flights
from normalizers.hotel import normalize_hotels
from notifier.telegram import TelegramNotifier
from scrapers.aggregator import ScraperAggregator
from storage.database import get_digest_deals, get_pending_instant_alerts, init_db
from storage.models import ScraperParams
from trip_builder.builder import build_trips
from utils.logging_config import get_logger

log = get_logger(__name__)

_notifier = TelegramNotifier()
_aggregator = ScraperAggregator()


def _build_scraper_params(
    origins: list[str],
    destinations: list[str],
    departure_from: Optional[date] = None,
    nights_min: int = 2,
    nights_max: int = 7,
) -> ScraperParams:
    if departure_from is None:
        departure_from = datetime.utcnow().date() + timedelta(days=7)
    departure_to = departure_from + timedelta(days=30)
    return ScraperParams(
        origins=origins,
        destinations=destinations,
        departure_date_from=departure_from,
        departure_date_to=departure_to,
        nights_min=nights_min,
        nights_max=nights_max,
    )


async def run_pipeline_cycle() -> None:
    """
    Full pipeline execution:
    1. Scrape flights + hotels
    2. Normalize to EUR + confidence scores
    3. Build trips (direct, complete, repositioned)
    4. Apply hard filters
    5. Apply time decay
    6. Deduplicate
    7. Send instant alerts (up to quota)
    """
    cycle_start = datetime.utcnow()
    log.info("pipeline_cycle_start", ts=cycle_start.isoformat())

    try:
        # ── Layer 1 primary origins ───────────────────────────────────────────
        params_primary = _build_scraper_params(
            origins=LAYER_1_AIRPORTS,
            destinations=POPULAR_DESTINATIONS[:30],
        )

        # ── Layer 2+3: Italy + EU hubs for repositioning ──────────────────────
        params_expanded = _build_scraper_params(
            origins=LAYER_2_AIRPORTS + LAYER_3_HUBS[:5],
            destinations=POPULAR_DESTINATIONS[30:60],
        )

        # Collect from all scrapers concurrently
        (raw_flights_p, flight_stats_p), (raw_hotels_p, hotel_stats_p) = await asyncio.gather(
            _aggregator.collect_flights(params_primary),
            _aggregator.collect_hotels(params_primary),
        )
        (raw_flights_e, _), _ = await asyncio.gather(
            _aggregator.collect_flights(params_expanded),
            asyncio.sleep(0),
        )

        raw_flights = raw_flights_p + raw_flights_e
        raw_hotels = raw_hotels_p

        log.info(
            "raw_collected",
            flights=len(raw_flights),
            hotels=len(raw_hotels),
        )

        # ── Normalize ─────────────────────────────────────────────────────────
        flight_legs, hotel_deals = await asyncio.gather(
            normalize_flights(raw_flights),
            normalize_hotels(raw_hotels),
        )

        # ── Build trips ───────────────────────────────────────────────────────
        trips = await build_trips(flight_legs, hotel_deals)
        if not trips:
            log.info("no_trips_built_this_cycle")
            return

        # ── Hard filters ──────────────────────────────────────────────────────
        instant_candidates, digest_candidates = apply_hard_filters(trips)

        # ── Time decay ────────────────────────────────────────────────────────
        instant_fresh, digest_fresh, _ = apply_time_decay(instant_candidates)
        # Digest candidates stay as digest regardless of decay
        all_digest = digest_fresh + digest_candidates

        # ── Deduplication ─────────────────────────────────────────────────────
        new_instant, _ = await deduplicate_trips(instant_fresh)
        new_digest, _ = await deduplicate_trips(all_digest)

        log.info(
            "pipeline_filtered",
            new_instant=len(new_instant),
            new_digest=len(new_digest),
        )

        # ── Send instant alerts ───────────────────────────────────────────────
        if new_instant:
            sent = await _notifier.process_instant_queue(new_instant)
            log.info("instant_alerts_sent", count=sent)

        duration = (datetime.utcnow() - cycle_start).total_seconds()
        log.info("pipeline_cycle_complete", duration_s=round(duration, 1))

    except Exception as exc:
        log.error("pipeline_cycle_failed", error=str(exc), exc_info=True)
        # Do NOT crash the scheduler — just log


async def run_daily_digest() -> None:
    """Send the daily digest of best deals."""
    log.info("digest_run_start")
    try:
        trips = await get_digest_deals(limit=20)
        if trips:
            await _notifier.send_digest(trips)
        else:
            log.info("digest_no_deals_to_send")
    except Exception as exc:
        log.error("digest_failed", error=str(exc), exc_info=True)


def create_scheduler() -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Main scraping cycle
    scheduler.add_job(
        run_pipeline_cycle,
        trigger=IntervalTrigger(minutes=settings.scrape_interval_minutes),
        id="pipeline_cycle",
        name="Main scraping + alert pipeline",
        replace_existing=True,
        max_instances=1,  # Prevent overlapping runs
        misfire_grace_time=300,
    )

    # Daily digest
    scheduler.add_job(
        run_daily_digest,
        trigger=CronTrigger(
            hour=settings.digest_hour,
            minute=settings.digest_minute,
            timezone="Europe/Rome",
        ),
        id="daily_digest",
        name="Daily deal digest",
        replace_existing=True,
        max_instances=1,
    )

    return scheduler


async def run_forever() -> None:
    """
    Main entry point for continuous operation.
    Initializes DB, starts scheduler, runs pipeline immediately on boot.
    """
    await init_db()
    log.info("database_initialized")

    scheduler = create_scheduler()
    scheduler.start()
    log.info(
        "scheduler_started",
        interval_min=get_settings().scrape_interval_minutes,
    )

    # Notify Telegram that engine is running
    await _notifier.send_system_message(
        "🚀 Travel Deal Intelligence Engine started\n"
        f"Monitoring {len(LAYER_1_AIRPORTS + LAYER_2_AIRPORTS + LAYER_3_HUBS)} airports "
        f"× {len(POPULAR_DESTINATIONS)} destinations"
    )

    # Run immediately on startup (don't wait for first interval)
    await run_pipeline_cycle()

    # Keep running
    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stopping")
        scheduler.shutdown(wait=False)
