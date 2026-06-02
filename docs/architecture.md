# Architecture

## Pipeline overview

```
User Preferences (YAML)
       │
       ▼
Date Discovery ──► ScraperParams[] (one per profile × date chunk)
       │
       ▼
ScraperAggregator (concurrent)
  ├── Flight scrapers × 5  ──► RawFlightResult[]
  └── Hotel scrapers × 2   ──► RawHotelResult[]
       │
       ▼
Normalizers
  ├── normalize_flights() → FlightLeg[] (EUR prices + confidence)
  └── normalize_hotels()  → HotelDeal[] (EUR prices + quality flag)
       │
       ▼
TripBuilder
  ├── Flight-only deals
  ├── Complete trips (flight + hotel, cluster-matched)
  └── Repositioned multi-leg trips
       │ (each trip gets: feasibility check, category, anomaly detection, booking confidence)
       ▼
HardFilters
  ├── Hotel quality gate (rating threshold, except big discounts)
  ├── Feasibility gate (infeasible → max DIGEST)
  ├── Price thresholds (Europe <€120, long-haul <€450)
  └── Discount thresholds (flights >60%, hotels >60%)
       │
       ▼
TimeDecay → Deduplication (SHA-256 hash vs DB)
       │
       ▼
TelegramNotifier
  ├── Instant queue (sorted: booking_confidence → data_confidence → cost)
  └── Daily digest (top 15 deals)
```

## Key design decisions

### No AI for price computation
Claude Haiku is used exclusively for formatting alert messages. All prices, discounts, and verdicts are computed deterministically in Python. A post-format safety check verifies that AI output preserves all `€NNN` values.

### Confidence scoring
Each piece of data carries a `data_confidence_score` (0.0–1.0) built from:
- **Freshness** (30%): How recently was this scraped?
- **Source count** (20%): How many independent sources agree?
- **Field completeness** (25%): Are all expected fields present?
- **Source reliability** (25%): Known reliability weight per source

The separate `BookingConfidence` enum (HIGH/MEDIUM/LOW) is a human-readable tier for alert priority.

### Airport clusters
Airports serving the same city are grouped into clusters (Milan = MXP+LIN+BGY). The engine automatically expands any airport to its full cluster when scraping, so MXP origin searches LIN and BGY too. Route strings use the city name (e.g., "Milan → London") rather than individual airport codes.

### Historical price analytics
Each recorded price is stored in `price_history`. Anomaly detection computes:
- 30-day and 90-day averages
- 30-day standard deviation
- All-time low for the route
A price is flagged as anomalous if it is >2σ below the 30-day average, or an all-time low, or >30% below the recent median.

### Repositioning rules
A repositioning deal is only valid if:
- The repositioning leg saves ≥€80 OR ≥25% vs direct flight
- Layover at the hub is ≥2 hours
- The route is not domestic-only

### Database schema

```sql
deals        — all discovered trips (hash-unique, JSON payload)
alerts_sent  — alert delivery history (for rate limiting)
price_history — raw price observations per route
price_stats  — computed aggregates per route (avg_30d, std_dev, all_time_low)
```
