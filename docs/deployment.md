# Deployment Guide

## Local (development)

```bash
# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env   # fill in your tokens

# Verify setup with a single cycle
python main.py cycle

# Start engine
python main.py run
```

## Docker (recommended for production)

```bash
# Build and start
docker-compose up -d

# View logs
docker-compose logs -f engine

# Check health
docker-compose exec engine python main.py health

# Stop
docker-compose down
```

### Persistent data

The `docker-compose.yml` mounts three host directories:
- `./data/` — SQLite database
- `./logs/` — structured log files
- `./config/` — `user_preferences.yaml`

These survive container restarts and upgrades.

## VPS (systemd service)

```ini
# /etc/systemd/system/cheaptrip.service
[Unit]
Description=CheapTrip Deal Engine
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/cheaptrip
EnvironmentFile=/opt/cheaptrip/.env
ExecStart=/opt/cheaptrip/venv/bin/python main.py run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable cheaptrip
systemctl start cheaptrip
journalctl -u cheaptrip -f
```

## Monitoring

- `python main.py status` — DB deal/alert counts
- `python main.py health` — per-scraper success rates and last run times
- Logs are structured JSON (structlog) — pipe to any log aggregator

## Upgrading

```bash
git pull
pip install -r requirements.txt  # pick up new deps
# Database migrations are handled automatically by init_db() on startup
python main.py run
```
