# Travel Deal Intelligence Engine

A production-grade system that continuously monitors global flight and hotel prices, detects anomalies, builds complete itineraries, and delivers high-value travel deals via Telegram.

## What it does

- Scrapes flights and hotels from multiple sources (Skyscanner, Google Flights, Booking.com, Secret Flying, Holiday Pirates, Going)
- Detects historically low prices using 30/90-day baselines and standard deviation analysis
- Builds three deal types: flight-only, complete trip (flight + hotel), and repositioned multi-leg routes
- Applies configurable quality filters: hotel ratings, trip feasibility, price thresholds
- Categorizes deals: Weekend Escape, Beach Holiday, Long-Haul Adventure, Error Fare, etc.
- Delivers instant Telegram alerts (rate-limited) and a daily digest

## Quick start

```bash
# 1. Copy and configure environment
cp .env.example .env
# Edit .env with your Telegram bot token, chat ID, and Anthropic API key

# 2. Configure preferences (optional — sensible defaults apply)
cp config/user_preferences.yaml.example config/user_preferences.yaml
# Edit to set your home airports, preferred trip lengths, hotel rating threshold

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run a single cycle to verify setup
python main.py cycle

# 5. Start the engine
python main.py run
```

## Docker

```bash
docker-compose up -d
docker-compose logs -f
```

## CLI commands

| Command | Description |
|---------|-------------|
| `python main.py run` | Start continuous 24/7 engine |
| `python main.py cycle` | Run one scraping cycle and exit |
| `python main.py digest` | Send today's digest and exit |
| `python main.py status` | Show DB statistics |
| `python main.py health` | Show scraper health status |
| `python main.py search --origin MXP --dest NRT` | Search a specific route |

## Configuration

See [docs/configuration.md](docs/configuration.md) for all settings.

Edit `config/user_preferences.yaml` to configure:
- Home airports (supports multi-airport clusters)
- Preferred trip lengths (weekend / short / medium / long)
- Minimum hotel rating (default 7.0/10)
- Search window (default 90 days ahead)
- Maximum budget

## Architecture

See [docs/architecture.md](docs/architecture.md) for system design.

## Telegram setup

See [docs/telegram_setup.md](docs/telegram_setup.md) to create a bot and get your chat ID.

## Tests

```bash
pytest tests/ -v
```

## Project structure

```
cheaptrip-by-claude/
├── main.py                    # CLI entry point
├── config.py                  # Settings, airport lists, constants
├── preferences.py             # User preferences (YAML-backed)
├── config/
│   └── user_preferences.yaml  # User-editable configuration
├── scrapers/                  # Data collection layer
│   ├── aggregator.py          # Runs all scrapers concurrently
│   ├── health_monitor.py      # Tracks scraper success/failure rates
│   └── *.py                   # Individual scraper implementations
├── normalizers/               # Data normalization (EUR, confidence)
├── storage/
│   ├── models.py              # Pydantic data models
│   ├── database.py            # SQLite async operations
│   └── price_analytics.py     # Historical anomaly detection
├── trip_builder/
│   ├── builder.py             # Core trip assembly
│   ├── categorizer.py         # Deal categorization rules
│   ├── date_discovery.py      # Flexible date window generation
│   └── feasibility.py         # Trip feasibility checks
├── filters/                   # Hard filters, deduplication, time decay
├── ai_layer/                  # Claude Haiku formatting (no price computation)
├── notifier/                  # Telegram delivery with rate limiting
├── scheduler/                 # APScheduler pipeline runner
├── utils/
│   ├── airport_clusters.py    # Multi-airport cluster expansion
│   └── confidence.py          # Confidence scoring
└── tests/                     # Full test suite
```
