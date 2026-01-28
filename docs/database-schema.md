# Data Directory

This folder contains market data files and the DuckDB database for the Market Regime Classifier.

## Directory Contents

```
data/
├── market_data.duckdb      # Main DuckDB database
├── archive/                # DBN archive files from live streaming
│   └── mbp1_YYYY-MM-DD.dbn.zst  # Daily MBP-1 archives
└── *.dbn.zst              # Historical Databento files (gitignored)
```

## Database Schema

### `ohlcv_ticks` Table (Single Source of Truth)

All OHLCV and orderflow data in one consolidated table:

| Column | Type | Description |
|--------|------|-------------|
| timestamp | TIMESTAMP | Bar start time |
| symbol | VARCHAR | Instrument symbol (MNQ) |
| timeframe | VARCHAR | Timeframe (5M, 15M, 1H, 4H, 1D) |
| open | DOUBLE | Open price |
| high | DOUBLE | High price |
| low | DOUBLE | Low price |
| close | DOUBLE | Close price |
| volume | BIGINT | Bar volume |
| instant_delta | BIGINT | Bar delta (buy - sell) |
| dom_imbalance | DOUBLE | Order book imbalance (0-1) |
| total_bid_depth | DOUBLE | Average bid depth |
| total_ask_depth | DOUBLE | Average ask depth |
| cvd | BIGINT | Cumulative Volume Delta |

**Primary Key**: (symbol, timeframe, timestamp)

### `regimes` Table

Regime classifications:

| Column | Type | Description |
|--------|------|-------------|
| timestamp | TIMESTAMP | Classification time |
| symbol | VARCHAR | Instrument symbol |
| timeframe | VARCHAR | Timeframe |
| regime | VARCHAR | BULLISH, BEARISH, NEUTRAL |
| confidence | DOUBLE | Classification confidence |
| key_signal | VARCHAR | Primary signal driver |

## Data Pipeline

### Historical Data Loading

Use the unified data loader to load from Databento DBN files:

```bash
cd backend

# Load OHLCV data only (NULL orderflow columns)
python scripts/data/load_historical_data.py --ohlcv data/glbx-mdp3.ohlcv-1m.dbn.zst

# Load MBP-1 data (with orderflow metrics)
python scripts/data/load_historical_data.py --mbp data/glbx-mdp3.mbp-1.dbn.zst

# Load both OHLCV and MBP-1
python scripts/data/load_historical_data.py --ohlcv data/ohlcv.dbn.zst --mbp data/mbp1.dbn.zst

# Aggregate existing mbp_ticks to ohlcv_ticks
python scripts/data/load_historical_data.py --aggregate

# Check database status
python scripts/data/load_historical_data.py --status
```

The loader:
1. Reads OHLCV-1M DBN file → builds continuous contract → resamples to timeframes
2. Reads MBP-1 DBN file → calculates DOM/delta/CVD → aggregates to OHLCV bars
3. Overlays MBP data onto OHLCV bars (orderflow metrics)

### Live Streaming

The live ingestion service handles real-time data:

```bash
python -m app.streaming.live_ingestion
```

Features:
- Subscribes to MBP-1 schema from Databento
- Calculates orderflow metrics in real-time:
  - DOM imbalance from bid/ask sizes
  - Delta from quote size changes
  - CVD as cumulative delta
- Stores completed bars to `ohlcv_ticks`
- Pushes updates via WebSocket at `/ws/live`
- Archives raw data to `.dbn.zst` files

### DBN Archive Files

Live streaming archives raw MBP-1 data to compressed DBN files:

```
data/archive/
├── mbp1_2024-01-15.dbn.zst
├── mbp1_2024-01-16.dbn.zst
└── ...
```

These files can be used for backtesting:

```python
from scripts.backtesting.dbn_loader import DBNLoader

loader = DBNLoader()
df = loader.load_for_backtest(
    start_date="2024-01-15",
    end_date="2024-01-20",
    timeframe="15M"
)
```

### Weekly Maintenance

Run Friday at 4:30 PM CST (after CME close):

```bash
python scripts/maintenance/weekly_maintenance.py
```

Tasks:
- Delete DBN archives > 60 days
- Delete OHLCV data > 5 years
- Vacuum database

## Data Retention

| Data Type | Location | Retention |
|-----------|----------|-----------|
| OHLCV + Orderflow | `ohlcv_ticks` | 5 years |
| MBP-1 Archives | `data/archive/*.dbn.zst` | 60 days |

## Databento File Formats

### OHLCV-1M (Historical)
- File: `glbx-mdp3-*.ohlcv-1m.dbn.zst`
- Content: 1-minute candlesticks
- Use: Initial historical data load

### MBP-1 (Live + Archive)
- File: `mbp1_YYYY-MM-DD.dbn.zst`
- Content: Top-of-book quotes
- Use: DOM imbalance, delta calculation

## Configuration

Streaming settings in `config/databento_config.yaml`:

```yaml
streaming:
  dataset: "GLBX.MDP3"
  symbols: ["MNQ"]
  schemas: ["mbp-1"]
  timeframes: ["5M", "15M", "1H", "4H", "1D"]

retention:
  live_db:
    ohlcv_ticks_days: 1825  # 5 years
  archive:
    mbp_days: 60
```

## Notes

- Historical OHLCV data has NULL orderflow columns (delta, DOM, CVD)
- Live data has full orderflow metrics calculated from MBP-1
- DBN files are zstd compressed (~10:1 ratio)
- 4H and 1D bars use CME session boundaries (18:00 ET)
