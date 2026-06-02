from __future__ import annotations

import asyncio
from datetime import datetime
from typing import List

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
from preferences import get_preferences
from scrapers.aggregator import ScraperAggregator
from scrapers.health_monitor import get_health_monitor
from storage.database import get_digest_deals, get_pending_instant_alerts, init_db
from storage.models import ScraperParams
from trip_builder.builder import build_trips
from trip_builder.date_discovery import generate_search_windows
from utils.airport_clusters import expand_list_to_clusters
from utils.logging_config import get_logger

log = get_logger(__name__)

# Notifier is initialized async in run_forever() to restore rate-limiter state from DB.
# Fallback sync init is used for cycle/search commands that don't go through run_forever().
_notifier = TelegramNotifier()
_aggregator = ScraperAggregator()


async def run_pipeline_cycle() -> None:
    """
    Full pipeline execution:
    1. Load user preferences + generate flexible date windows
    2. Scrape flights + hotels (cluster-expanded airports)
    3. Normalize to EUR + confidence scores
    4. Build trips (direct, complete, repositioned)
    5. Apply hard filters (quality gate + feasibility gate)
    6. Apply time decay
    7. Deduplicate
    8. Send instant alerts (up to quota)
    """
    cycle_start = datetime.utcnow()
    log.info("pipeline_cycle_start", ts=cycle_start.isoformat())

    try:
        prefs = get_preferences()

        # Cluster-expand home airports from preferences
        origins = expand_list_to_clusters(prefs.home_airports)
        excluded = set(prefs.excluded_destinations)
        destinations = [d for d in POPULAR_DESTINATIONS if d not in excluded]

        # Generate flexible date windows based on preferred trip lengths
        param_batches = generate_search_windows(
            prefs=prefs,
            origins=origins,
            destinations=destinations,
            max_price_eur=prefs.max_trip_budget or 2000.0,
        )

        log.info(
            "search_windows_generated",
            batches=len(param_batches),
            origins=len(origins),
            destinations=len(destinations),
        )

        # Collect from all scrapers across all date windows
        all_raw_flights = []
        all_raw_hotels = []

        for params in param_batches:
            (raw_flights, _), (raw_hotels, _) = await asyncio.gather(
                _aggregator.collect_flights(params),
                _aggregator.collect_hotels(params),
            )
            all_raw_flights.extend(raw_flights)
            all_raw_hotels.extend(raw_hotels)

        log.info(
            "raw_collected",
            flights=len(all_raw_flights),
            hotels=len(all_raw_hotels),
        )

        # ── Normalize ─────────────────────────────────────────────────────────
        flight_legs, hotel_deals = await asyncio.gather(
            normalize_flights(all_raw_flights),
            normalize_hotels(all_raw_hotels),
        )

        # ── Build trips ───────────────────────────────────────────────────────
        trips = await build_trips(flight_legs, hotel_deals)
        if not trips:
            log.info("no_trips_built_this_cycle")
            await _log_health_summary()
            return

        # ── Hard filters ──────────────────────────────────────────────────────
        instant_candidates, digest_candidates = apply_hard_filters(trips)

        # ── Time decay ────────────────────────────────────────────────────────
        instant_fresh, digest_fresh, _ = apply_time_decay(instant_candidates)
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

        await _log_health_summary()

        duration = (datetime.utcnow() - cycle_start).total_seconds()
        log.info("pipeline_cycle_complete", duration_s=round(duration, 1))

    except Exception as exc:
        log.error("pipeline_cycle_failed", error=str(exc), exc_info=True)
        # Do NOT crash the scheduler — just log


async def _log_health_summary() -> None:
    """Log a brief scraper health summary after each cycle."""
    try:
        monitor = get_health_monitor()
        report = await monitor.get_health_report()
        failing = [h.source_id for h in report if h.status in ("FAILING", "STALE")]
        if failing:
            log.warning("scrapers_degraded", sources=failing)
        else:
            ok = sum(1 for h in report if h.status == "OK")
            log.info("scraper_health_ok", ok_count=ok, total=len(report))
    except Exception:
        pass


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

    # Re-initialize notifier now that DB is ready — restores rate-limiter state
    global _notifier
    _notifier = await TelegramNotifier.create()

    prefs = get_preferences()
    scheduler = create_scheduler()
    scheduler.start()
    log.info(
        "scheduler_started",
        interval_min=get_settings().scrape_interval_minutes,
        home_airports=prefs.home_airports,
        trip_lengths=prefs.preferred_trip_lengths,
        search_window_days=prefs.search_window_days,
    )

    await _notifier.send_system_message(
        "🚀 Travel Deal Intelligence Engine started\n"
        f"Origins: {', '.join(prefs.home_airports)}\n"
        f"Profiles: {', '.join(prefs.preferred_trip_lengths)}\n"
        f"Monitoring {len(POPULAR_DESTINATIONS)} destinations"
    )

    # Run immediately on startup
    await run_pipeline_cycle()

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stopping")
        scheduler.shutdown(wait=False)
