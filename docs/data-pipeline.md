# Data Pipeline & Maintenance Scripts

This directory contains scripts for managing the market data pipeline, including historical data loading, gap detection, backfilling, and database maintenance.

## Overview

```
scripts/
├── data/
│   ├── preload_historical.py      # Initial data preload from Databento
│   └── load_historical_data.py    # Historical data loader (from DBN files)
├── maintenance/
│   ├── backfill_gaps.py           # Gap detection & backfill utility
│   └── weekly_maintenance.py      # Database cleanup job
├── backtesting/
│   └── dbn_loader.py              # DBN archive loader for backtests
└── utils/
    ├── verify_data.py             # Data verification utility
    └── reset_database.py          # Schema reset utility
```

## Data Sources

| Schema | Data Type | Use Case |
|--------|-----------|----------|
| `ohlcv-1m` | 1-minute candlesticks | Price (OHLC) + actual contract volume |
| `mbp-1` | Top-of-book quotes | Orderflow metrics (DOM, delta, CVD) |

**Both are needed for complete data:**
- OHLCV provides accurate price bars with real trading volume
- MBP-1 provides orderflow metrics (DOM imbalance, delta, CVD)

## Database Schema

Single source of truth: `ohlcv_ticks`

```sql
ohlcv_ticks (
    timestamp TIMESTAMP,
    symbol VARCHAR,              -- MNQ
    timeframe VARCHAR,           -- 5M, 15M, 1H, 4H, 1D
    open, high, low, close DOUBLE,
    volume BIGINT,               -- From OHLCV (actual contracts)
    instant_delta BIGINT,        -- From MBP-1 (buy - sell)
    dom_imbalance DOUBLE,        -- From MBP-1 (0-1)
    total_bid_depth DOUBLE,      -- From MBP-1
    total_ask_depth DOUBLE,      -- From MBP-1
    cvd BIGINT,                  -- Rolling CVD (windowed)
    PRIMARY KEY (symbol, timeframe, timestamp)
)
```

## Initial Data Preload

For new installations, use the preload utility to download historical data directly from Databento:

```bash
cd backend

# Step 1: Estimate costs before downloading
python scripts/data/preload_historical.py --estimate

# Step 2: Download and load all data
python scripts/data/preload_historical.py --load
```

### Default Data Ranges

| Schema | Duration | Purpose |
|--------|----------|---------|
| OHLCV-1M | 5 years | Price history, backtesting, regime analysis |
| MBP-1 | 60 days | Orderflow metrics (DOM, delta, CVD) |

### Custom Ranges

```bash
# Download 3 years OHLCV instead of 5
python scripts/data/preload_historical.py --load --ohlcv-years 3

# Download 90 days MBP-1 instead of 60
python scripts/data/preload_historical.py --load --mbp-days 90

# Download only OHLCV
python scripts/data/preload_historical.py --load --ohlcv-only

# Download only MBP-1
python scripts/data/preload_historical.py --load --mbp-only

# Keep downloaded DBN files after loading
python scripts/data/preload_historical.py --load --keep-files
```

### Preload Process

1. **Cost Estimation**: Queries Databento API for data cost
2. **Download**: Fetches compressed DBN files to `data/`
3. **Load OHLCV**: Creates base bars with price and volume
4. **Load MBP-1**: Overlays orderflow metrics
5. **Cleanup**: Deletes DBN files (unless `--keep-files`)

---

## Historical Data Loading (From Local Files)

### Initial Load

```bash
cd backend

# Step 1: Load OHLCV data (price + volume)
python scripts/data/load_historical_data.py --ohlcv data/glbx-mdp3.ohlcv-1m.dbn.zst

# Step 2: Load MBP-1 data (orderflow metrics)
python scripts/data/load_historical_data.py --mbp data/glbx-mdp3.mbp-1.dbn.zst

# Or load both at once
python scripts/data/load_historical_data.py \
    --ohlcv data/ohlcv.dbn.zst \
    --mbp data/mbp1.dbn.zst

# Check database status
python scripts/data/load_historical_data.py --status
```

### What the Loader Does

1. **OHLCV Loading** (`--ohlcv`):
   - Reads OHLCV-1M DBN file
   - Builds continuous contract (uses daily volume leader)
   - Filters settlement artifacts and invalid data
   - Resamples to timeframes: 5M, 15M, 1H, 4H, 1D
   - Inserts with NULL orderflow columns

2. **MBP-1 Loading** (`--mbp`):
   - Reads MBP-1 DBN file
   - Calculates orderflow metrics:
     - DOM imbalance: `bid_size / (bid_size + ask_size)`
     - Delta: inferred from quote size changes
     - CVD: rolling sum of delta (windowed per timeframe)
   - Inserts raw ticks to `mbp_ticks` table
   - Aggregates to `ohlcv_ticks` (overlays on existing OHLCV)

### CVD Rolling Windows

CVD uses rolling windows instead of cumulative sums:

| Timeframe | Window Size | Period |
|-----------|-------------|--------|
| 5M | 288 bars | 24 hours |
| 15M | 96 bars | 24 hours |
| 1H | 24 bars | 24 hours |
| 4H | 30 bars | 5 days |
| 1D | 5 bars | 5 days |

Configured in `config/agent_config.yaml` under `regime.cvd_windows`.

### CME Session Boundaries

4H and 1D bars align to CME session start (18:00 ET):
- Timestamps shifted by 6 hours before bucketing
- Ensures daily bars match CME trading sessions

## Live Streaming

```bash
python -m app.streaming.live_ingestion
```

The live ingestion service:
- Subscribes to MBP-1 schema from Databento
- Calculates orderflow metrics in real-time
- Aggregates to OHLCV bars for all timeframes
- Stores to `ohlcv_ticks` with rolling CVD
- Pushes updates via WebSocket at `/ws/live`
- Archives raw data to `data/archive/mbp1_YYYY-MM-DD.dbn.zst`

## Gap Detection & Backfill

If live streaming is interrupted, data gaps can be detected and backfilled.

### Check for Gaps

```bash
python scripts/maintenance/backfill_gaps.py --check
```

Output shows unexpected gaps (ignores weekends and CME maintenance windows):
```
Detecting gaps in 1H data...
  Data range: 2024-01-01 00:00:00 to 2024-01-31 23:00:00
  Total bars: 720

  Found 1 gap(s):
    2024-01-15 16:00:00 -> 2024-01-16 09:00:00 (17:00:00)
```

### Backfill Missing Data

```bash
# Backfill all detected gaps (downloads OHLCV + MBP-1)
python scripts/maintenance/backfill_gaps.py --backfill

# Backfill specific date range
python scripts/maintenance/backfill_gaps.py --backfill \
    --start 2024-01-15 \
    --end 2024-01-16

# Only download OHLCV (no orderflow)
python scripts/maintenance/backfill_gaps.py --backfill --ohlcv-only

# Only download MBP-1 (orderflow only, if OHLCV exists)
python scripts/maintenance/backfill_gaps.py --backfill --mbp-only
```

### Backfill Process

1. **Downloads from Databento**:
   - OHLCV-1M: `ohlcv1m_YYYY-MM-DD.dbn.zst`
   - MBP-1: `mbp1_YYYY-MM-DD.dbn.zst`
   - Saved to `data/backfill/`

2. **Loads OHLCV** (if not `--mbp-only`):
   - Creates base bars with real volume
   - Resamples to all timeframes

3. **Loads MBP-1** (if not `--ohlcv-only`):
   - Overlays orderflow metrics
   - Recalculates rolling CVD

### When to Use Each Option

| Scenario | Command |
|----------|---------|
| Live feed down for a day | `--backfill` (full) |
| OHLCV exists, need orderflow | `--backfill --mbp-only` |
| Just need price/volume | `--backfill --ohlcv-only` |
| Specific dates to fix | `--backfill --start X --end Y` |

## Weekly Maintenance

Run every Friday at 4:30 PM CST (after CME close):

```bash
python scripts/maintenance/weekly_maintenance.py
```

Tasks:
- Delete DBN archives older than 60 days
- Delete OHLCV data older than 5 years
- Vacuum database for performance

### Scheduling (Linux/macOS)

Add to crontab (`crontab -e`):
```bash
# Weekly maintenance - Friday 4:30 PM CST (22:30 UTC)
30 22 * * 5 cd /path/to/backend && python scripts/maintenance/weekly_maintenance.py
```

### Scheduling (Windows)

Use Task Scheduler to run at 4:30 PM CST every Friday.

## Data Verification

```bash
python scripts/utils/verify_data.py
```

Output:
```
======================================================================
  Data Verification - ohlcv_ticks
======================================================================

5M  :    123,456 bars (50,000 with orderflow)
      Date range: 2024-01-01 to 2024-01-31
      Price range: $18,500.00 - $21,200.00

15M :     41,152 bars (16,667 with orderflow)
      ...
```

## Database Reset

**Warning**: This deletes all data!

```bash
python scripts/utils/reset_database.py
```

Creates fresh schema:
- `ohlcv_ticks` - main data table
- `mbp_ticks` - raw tick data for aggregation
- `regimes` - regime classifications

## Backtesting

Load archived DBN files for backtesting:

```python
from scripts.backtesting.dbn_loader import DBNLoader

loader = DBNLoader()

# Load from archive
df = loader.load_for_backtest(
    start_date="2024-01-01",
    end_date="2024-01-31",
    timeframe="15M"
)

# Load historical DBN file
df = loader.load_historical_dbn(
    "data/glbx-mdp3.mbp-1.dbn.zst",
    timeframe="15M"
)
```

The loader applies:
- Rolling CVD windows (same as live/historical)
- CME session boundaries for 4H/1D
- DOM imbalance and delta calculations

## Troubleshooting

### No Orderflow Data?

Check if bars have orderflow metrics:
```bash
python scripts/data/load_historical_data.py --status
```

If `orderflow_bars = 0`, load MBP-1 data:
```bash
python scripts/data/load_historical_data.py --mbp data/mbp1.dbn.zst
```

### Incorrect CVD Values?

CVD is rolling, not cumulative. Window sizes are in `config/agent_config.yaml`:
```yaml
regime:
  cvd_windows:
    5M: 288    # 24 hours
    15M: 96    # 24 hours
    1H: 24     # 24 hours
    4H: 30     # 5 days
    1D: 5      # 5 days
```

### Data Gaps After Outage?

1. Check for gaps: `python scripts/maintenance/backfill_gaps.py --check`
2. Backfill: `python scripts/maintenance/backfill_gaps.py --backfill`

### Database Corrupted?

Reset and reload:
```bash
python scripts/utils/reset_database.py
python scripts/data/load_historical_data.py --ohlcv data/ohlcv.dbn.zst --mbp data/mbp1.dbn.zst
```

## Configuration

### Databento API Key

Required for backfilling. Add to `config/secrets.yaml`:
```yaml
api_key: "your-databento-api-key"
```

### Data Retention

Configured in `config/databento_config.yaml`:
```yaml
retention:
  live_db:
    ohlcv_ticks_days: 1825  # 5 years
  archive:
    mbp_days: 60            # 60 days
```

### CVD Windows

Configured in `config/agent_config.yaml`:
```yaml
regime:
  cvd_windows:
    5M: 288
    15M: 96
    1H: 24
    4H: 30
    1D: 5
```
