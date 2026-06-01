"""
Travel Deal Intelligence Engine — Main Entry Point

Usage:
  python main.py run           # Start continuous 24/7 engine
  python main.py cycle         # Run a single pipeline cycle and exit
  python main.py digest        # Send today's digest and exit
  python main.py status        # Print DB stats
"""
from __future__ import annotations

import asyncio
import sys

import click

from utils.logging_config import configure_logging, get_logger

log = get_logger(__name__)


@click.group()
def cli() -> None:
    """Travel Deal Intelligence Engine"""
    configure_logging()


@cli.command()
def run() -> None:
    """Start the continuous 24/7 deal monitoring engine."""
    from scheduler.runner import run_forever
    log.info("engine_starting")
    asyncio.run(run_forever())


@cli.command()
def cycle() -> None:
    """Run a single scraping + alert cycle and exit."""
    from storage.database import init_db
    from scheduler.runner import run_pipeline_cycle

    async def _run():
        await init_db()
        await run_pipeline_cycle()

    asyncio.run(_run())


@cli.command()
def digest() -> None:
    """Send today's deal digest and exit."""
    from storage.database import init_db
    from scheduler.runner import run_daily_digest

    async def _run():
        await init_db()
        await run_daily_digest()

    asyncio.run(_run())


@cli.command()
def status() -> None:
    """Print current engine status and DB statistics."""
    import aiosqlite
    from storage.database import get_db_path

    async def _run():
        path = await get_db_path()
        try:
            async with aiosqlite.connect(path) as db:
                cur = await db.execute("SELECT COUNT(*) FROM deals")
                total = (await cur.fetchone())[0]
                cur = await db.execute("SELECT COUNT(*) FROM deals WHERE is_alerted=1")
                alerted = (await cur.fetchone())[0]
                cur = await db.execute("SELECT COUNT(*) FROM deals WHERE alert_tier='instant'")
                instant = (await cur.fetchone())[0]
                cur = await db.execute("SELECT COUNT(*) FROM price_history")
                prices = (await cur.fetchone())[0]
                cur = await db.execute("SELECT COUNT(*) FROM alerts_sent")
                alerts_sent = (await cur.fetchone())[0]

            click.echo(f"""
Travel Deal Intelligence Engine — Status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Database:       {path}
Total deals:    {total}
Alerted:        {alerted}
Instant-tier:   {instant}
Price records:  {prices}
Alerts sent:    {alerts_sent}
""")
        except Exception as exc:
            click.echo(f"DB not found or not initialized: {exc}")
            click.echo("Run 'python main.py cycle' to initialize.")

    asyncio.run(_run())


@cli.command()
@click.option("--origin", default="MXP", help="IATA origin airport")
@click.option("--dest", default="KRK", help="IATA destination airport")
@click.option("--days", default=14, help="Days from now to search")
@click.option("--nights", default=3, help="Number of nights")
def search(origin: str, dest: str, days: int, nights: int) -> None:
    """Run a targeted search for a specific route."""
    from datetime import date, timedelta
    from storage.database import init_db
    from scrapers.aggregator import ScraperAggregator
    from normalizers.flight import normalize_flights
    from normalizers.hotel import normalize_hotels
    from trip_builder.builder import build_trips
    from storage.models import ScraperParams

    async def _run():
        await init_db()
        dep = date.today() + timedelta(days=days)
        params = ScraperParams(
            origins=[origin.upper()],
            destinations=[dest.upper()],
            departure_date_from=dep,
            departure_date_to=dep + timedelta(days=30),
            nights_min=nights,
            nights_max=nights + 3,
        )
        aggregator = ScraperAggregator()
        raw_flights, _ = await aggregator.collect_flights(params)
        raw_hotels, _ = await aggregator.collect_hotels(params)
        flights = await normalize_flights(raw_flights)
        hotels = await normalize_hotels(raw_hotels)
        trips = await build_trips(flights, hotels)
        trips.sort(key=lambda t: t.total_cost_eur)
        click.echo(f"\nFound {len(trips)} trips for {origin} → {dest}\n")
        for t in trips[:10]:
            click.echo(
                f"  {t.route} | €{t.total_cost_eur:.0f} | "
                f"{t.deal_type} | conf={t.data_confidence_score:.2f} | {t.verdict}"
            )

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
