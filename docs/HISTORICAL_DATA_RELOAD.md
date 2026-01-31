# Historical Data Reload Guide

This guide explains how to clean up and reload historical data for the Market Regime Classifier.

**Best time to run:** Saturday night (after 6 PM ET) when CME futures markets are closed.

## Quick Start (Weekly Maintenance)

The recommended approach uses the weekly reload script which only downloads data if it's already cached in Databento (cost = $0).

```bash
# SSH into EC2
ssh -i your-key.pem ubuntu@your-ec2-ip
cd ~/market-regime-classifier

# Stop services
docker-compose down

# Check if reload will be free
docker-compose run --rm backend python scripts/maintenance/weekly_reload.py --check

# If cost is $0, run the reload
docker-compose run --rm backend python scripts/maintenance/weekly_reload.py --reload

# Restart services
docker-compose up -d
```

**What the weekly reload does:**
- Downloads 5 years of OHLCV data (price history)
- Downloads 7 days of MBP-1 data (orderflow metrics)
- Only proceeds if Databento cost is $0 (data already cached)
- Cleans up archive files automatically
- Resets database for a clean slate

---

## Alternative: Manual Full Reload

If you need to reload with fresh data (may incur Databento charges):

```bash
# SSH into EC2
ssh -i your-key.pem ubuntu@your-ec2-ip
cd ~/market-regime-classifier

# Stop the running containers
docker-compose down

# Reset database and reload data (inside backend container)
docker-compose run --rm backend bash -c "
  python scripts/utils/reset_database.py &&
  python scripts/data/preload_historical.py --load
"

# Restart services
docker-compose up -d
```

---

## Step-by-Step Instructions

### 1. Stop Services

```bash
# SSH into EC2
ssh -i your-key.pem ubuntu@your-ec2-ip
cd ~/market-regime-classifier

# Stop all containers
docker-compose down
```

### 2. Clean Up Old Data Files (Optional)

Free up disk space by removing archived MBP files:

```bash
# Remove archive folder
sudo rm -rf /var/lib/docker/volumes/market-regime-classifier_backend-data/_data/archive/

# Remove any large historical DBN files
sudo rm -f /var/lib/docker/volumes/market-regime-classifier_backend-data/_data/mbp1_*.dbn.zst

# Check disk space
df -h
```

### 3. Reset Database

This creates a fresh database with empty tables:

```bash
docker-compose run --rm backend python scripts/utils/reset_database.py
```

**What it does:**
- Drops existing tables (`ohlcv_ticks`, `mbp_ticks`, `regimes`)
- Creates fresh tables with proper schema
- Resets all data to empty state

### 4. Estimate Data Costs (Optional)

Check how much the Databento download will cost before loading:

```bash
docker-compose run --rm backend python scripts/data/preload_historical.py --estimate
```

**Default data ranges:**
- OHLCV-1M: 5 years of candlestick data
- MBP-1: 60 days of order book data

### 5. Load Historical Data

#### Option A: Full Load (Recommended)

Loads both OHLCV and MBP-1 data:

```bash
docker-compose run --rm backend python scripts/data/preload_historical.py --load
```

**Estimated time:** 15-30 minutes depending on network speed

#### Option B: Custom Date Ranges

```bash
# 3 years OHLCV + 90 days MBP-1
docker-compose run --rm backend python scripts/data/preload_historical.py --load --ohlcv-years 3 --mbp-days 90

# 1 year OHLCV + 30 days MBP-1 (smaller/faster)
docker-compose run --rm backend python scripts/data/preload_historical.py --load --ohlcv-years 1 --mbp-days 30
```

#### Option C: Load Only One Data Type

```bash
# OHLCV only (price data, no orderflow metrics)
docker-compose run --rm backend python scripts/data/preload_historical.py --load --ohlcv-only

# MBP-1 only (orderflow metrics, requires OHLCV already loaded)
docker-compose run --rm backend python scripts/data/preload_historical.py --load --mbp-only
```

### 6. Verify Data Loaded

```bash
docker-compose run --rm backend python -c "
from app.data.storage import DuckDBStorage
with DuckDBStorage() as db:
    result = db.conn.execute('''
        SELECT timeframe, COUNT(*) as bars, MIN(timestamp) as start, MAX(timestamp) as end
        FROM ohlcv_ticks
        WHERE symbol = 'MNQ'
        GROUP BY timeframe
        ORDER BY timeframe
    ''').fetchall()
    for row in result:
        print(f'{row[0]}: {row[1]:,} bars | {row[2]} to {row[3]}')
"
```

### 7. Restart Services

```bash
docker-compose up -d

# Check logs
docker-compose logs -f backend
```

---

## Data Loading Details

### What Gets Downloaded

| Data Type | Schema | Duration | Purpose |
|-----------|--------|----------|---------|
| OHLCV-1M | `ohlcv-1m` | 5 years | Price history, indicators |
| MBP-1 | `mbp-1` | 60 days | DOM imbalance, delta, CVD |

### Processing Steps

1. **OHLCV Loading:**
   - Downloads 1-minute candlesticks
   - Builds continuous contract (handles contract rollovers)
   - Filters invalid prices (range: 10,000-50,000)
   - Resamples to timeframes: 5M, 15M, 1H, 4H, 1D

2. **MBP-1 Loading:**
   - Downloads top-of-book quote data
   - Calculates orderflow metrics:
     - DOM imbalance: `bid_size / (bid_size + ask_size)`
     - Instant delta: price direction * volume
     - CVD: Rolling cumulative volume delta
   - Overlays metrics onto OHLCV bars

### Timeframes Created

| Timeframe | CVD Window | Use Case |
|-----------|------------|----------|
| 5M | 288 bars (24h) | Scalping signals |
| 15M | 96 bars (24h) | Intraday trading |
| 1H | 24 bars (24h) | Swing trades |
| 4H | 30 bars (5d) | Position bias |
| 1D | 5 bars (5d) | Trend direction |

---

## Troubleshooting

### "No API key found"

Ensure your Databento API key is configured:

```bash
# Check if secrets file exists
cat backend/config/secrets.yaml

# Should contain:
# api_key: "db-xxxxx..."
```

### "Rate limit exceeded"

Wait a few minutes and retry. Databento has API rate limits.

### "Insufficient funds"

Add credits to your Databento account at https://databento.com/account

### Database locked

Stop all containers before resetting:

```bash
docker-compose down
docker ps  # Verify nothing running
```

### Data gaps after reload

Run gap detection:

```bash
docker-compose run --rm backend python scripts/maintenance/backfill_gaps.py --check
```

If gaps found:

```bash
docker-compose run --rm backend python scripts/maintenance/backfill_gaps.py --backfill
```

---

## Weekend Maintenance Schedule

**Saturday Night (6 PM ET onwards):**

```bash
# 1. SSH into EC2
ssh -i your-key.pem ubuntu@your-ec2-ip
cd ~/market-regime-classifier

# 2. Stop services
docker-compose down

# 3. Check if reload is free
docker-compose run --rm backend python scripts/maintenance/weekly_reload.py --check

# 4. If cost is $0, run reload
docker-compose run --rm backend python scripts/maintenance/weekly_reload.py --reload

# 5. Restart services
docker-compose up -d

# 6. Verify services are running
docker-compose ps
docker-compose logs -f backend --tail=50
```

**Sunday Evening (before 6 PM ET):**

- Markets reopen at 6 PM ET Sunday
- Live data ingestion resumes automatically
- Verify WebSocket connection is active
- Check the UI shows live updates

---

## Databento Cost Reference

Approximate costs (as of 2024):

| Data Type | Duration | Estimated Cost |
|-----------|----------|----------------|
| OHLCV-1M | 1 year | ~$5-10 |
| OHLCV-1M | 5 years | ~$20-30 |
| MBP-1 | 30 days | ~$10-15 |
| MBP-1 | 60 days | ~$20-30 |

Use `--estimate` flag to get exact costs before downloading.

---

## Weekly Maintenance Script

The `weekly_reload.py` script is designed for Saturday night maintenance:

### Configuration

| Setting | Value | Purpose |
|---------|-------|---------|
| OHLCV_YEARS | 5 | Years of price history |
| MBP_DAYS | 7 | Days of orderflow data |
| MAX_ALLOWED_COST | $0.00 | Only proceed if free |

### Commands

```bash
# Check cost (dry run)
docker-compose run --rm backend python scripts/maintenance/weekly_reload.py --check

# Run reload (only if cost is $0)
docker-compose run --rm backend python scripts/maintenance/weekly_reload.py --reload

# Force reload even if cost > $0 (will charge your account)
docker-compose run --rm backend python scripts/maintenance/weekly_reload.py --reload --force

# Skip archive cleanup
docker-compose run --rm backend python scripts/maintenance/weekly_reload.py --reload --skip-archive-cleanup

# Keep backup even after successful reload
docker-compose run --rm backend python scripts/maintenance/weekly_reload.py --reload --keep-backup
```

### Safety Features

| Feature | Description |
|---------|-------------|
| **Auto-Backup** | Creates timestamped backup before any changes |
| **Auto-Restore** | Restores from backup if reload fails |
| **Verification** | Checks minimum bar counts before cleanup |
| **Backup Retention** | Keeps last 2 backups by default |

### Backup Location

Backups are stored in: `data/backups/market_data_backup_YYYYMMDD_HHMMSS.duckdb`

Manual restore if needed:
```bash
# List backups
ls -la data/backups/

# Manual restore
cp data/backups/market_data_backup_20260130_180000.duckdb data/market_data.duckdb
```

### How It Works

1. **Cost Check**: Queries Databento API for download cost
2. **Cost Validation**: Only proceeds if cost ≤ $0.00
3. **Database Backup**: Creates timestamped backup before changes
4. **Archive Cleanup**: Removes old MBP archive files
5. **Database Reset**: Drops and recreates tables
6. **OHLCV Download**: 5 years of 1-minute candles
7. **MBP Download**: 7 days of orderflow data (chunked for memory)
8. **Data Verification**: Checks minimum bar counts per timeframe
9. **Backup Cleanup**: Deletes backup only if reload successful
10. **Auto-Restore**: If reload fails, automatically restores from backup

### Why Cost = $0?

Databento caches previously downloaded data. If you've downloaded the same date range before, re-downloading is free. This ensures:
- No unexpected charges
- Data consistency (same source)
- Safe automation

---

## Files Reference

| Script | Purpose |
|--------|---------|
| `scripts/maintenance/weekly_reload.py` | **Weekly maintenance (recommended)** |
| `scripts/utils/reset_database.py` | Reset database schema |
| `scripts/data/preload_historical.py` | Download and load data |
| `scripts/data/load_historical_data.py` | Load from local DBN files |
| `scripts/maintenance/backfill_gaps.py` | Detect and fill gaps |

## Configuration Files

| File | Purpose |
|------|---------|
| `config/secrets.yaml` | Databento API key |
| `config/databento_config.yaml` | Data retention settings |
| `config/agent_config.yaml` | CVD windows, thresholds |
