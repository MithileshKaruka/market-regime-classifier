# Data Processing Guide

This folder contains market data files and the DuckDB database for the Market Regime Classifier.

## Quick Start

Use the unified data loader to run the complete pipeline:

```bash
cd backend
python scripts/load_all_data.py
```

This opens an interactive menu to:
1. Check current data status
2. Load OHLCV candlestick data
3. Load MBP-10 tick data
4. Update order flow metrics (DOM/CVD)
5. Run full pipeline (all steps)

### Command Line Options

```bash
python scripts/load_all_data.py --status   # Check data status
python scripts/load_all_data.py --ohlcv    # Load OHLCV only
python scripts/load_all_data.py --mbp      # Load MBP-10 only
python scripts/load_all_data.py --update   # Update orderflow metrics only
python scripts/load_all_data.py --all      # Run all steps non-interactively
```

## Data Files

### OHLCV Data
- `glbx-mdp3-20210116-20260115.ohlcv-1m.dbn.zst` - 1-minute OHLCV candlestick data from Databento
  - Compressed DBN format (zstd)
  - Contains MNQ futures data from Jan 2021 to Jan 2026
  - Used for chart display and technical analysis

### MBP-10 (Market By Price) Data
- `glbx-mdp3-YYYYMMDD.mbp-10.dbn.zst` - Daily MBP-10 tick data files
  - 10-level order book snapshots
  - Contains bid/ask prices, sizes, and counts at each level
  - Used for order flow analysis (DOM imbalance, CVD)

### Database
- `market_data.duckdb` - DuckDB database with processed data
  - `order_book` table: OHLCV bars with DOM imbalance and CVD
  - `mbp_ticks` table: Tick-level order book data

## Data Processing Pipeline

The pipeline consists of three steps. **All three steps must be run** for order flow signals to appear on the chart.

### Step 1: Load OHLCV Data
Load candlestick data into the database for all timeframes.

```bash
python scripts/load_ohlcv.py
```

This script:
- Reads the OHLCV DBN file
- Builds continuous contract from daily volume leader
- Filters out settlement artifacts and corrupted bars
- Resamples 1M data to 5M, 15M, 1H, 4H, 1D timeframes
- Stores in `order_book` table with placeholder DOM/CVD values (0.5/0.0)

### Step 2: Load MBP-10 Tick Data
Load order book tick data for order flow analysis.

```bash
python scripts/load_mbp10.py
```

This script:
- Presents a menu to select MBP-10 files to process
- Offers streaming (faster) or iterative (lower RAM) processing modes
- Calculates DOM imbalance and delta from bid/ask volumes
- Stores in `mbp_ticks` table

### Step 3: Update Order Flow Metrics (REQUIRED!)
Aggregate tick data and update OHLCV bars with real DOM/CVD values.

```bash
python scripts/update_orderflow_metrics.py
```

**This step is critical!** Without it, the order_book table will have default DOM (0.5) and CVD (0.0) values, and no order flow signals will appear.

This script:
- Aggregates `mbp_ticks` data into timeframe buckets
- Converts uint32 delta values to signed int32 (handles Databento's encoding)
- Calculates rolling CVD using configurable windows
- Updates `order_book` table with real DOM imbalance and CVD
- Handles CME session boundaries for 4H and 1D timeframes

## Data Schema

### order_book Table
| Column | Type | Description |
|--------|------|-------------|
| timestamp | TIMESTAMP | Bar start time |
| symbol | VARCHAR | Instrument symbol (MNQ) |
| timeframe | VARCHAR | Timeframe (1M, 5M, 15M, 1H, 4H, 1D) |
| open | DOUBLE | Open price |
| high | DOUBLE | High price |
| low | DOUBLE | Low price |
| close | DOUBLE | Close price |
| volume | BIGINT | Bar volume |
| dom_imbalance | DOUBLE | Order book imbalance (0-1, 0.5 = neutral) |
| cvd | DOUBLE | Cumulative Volume Delta (rolling window) |
| vwap | DOUBLE | Volume-Weighted Average Price |

### mbp_ticks Table
| Column | Type | Description |
|--------|------|-------------|
| timestamp | TIMESTAMP | Tick timestamp |
| symbol | VARCHAR | Instrument symbol |
| mid_price | DOUBLE | Mid price |
| bid_price | DOUBLE | Best bid price |
| ask_price | DOUBLE | Best ask price |
| spread | DOUBLE | Bid-ask spread |
| bid_size | INTEGER | Best bid size |
| ask_size | INTEGER | Best ask size |
| total_bid_depth | BIGINT | Sum of all bid levels |
| total_ask_depth | BIGINT | Sum of all ask levels |
| dom_imbalance | DOUBLE | Order book imbalance |
| delta | DOUBLE | Instant delta (bid - ask volume) |
| cvd | DOUBLE | Cumulative delta |

## Utility Scripts

### Verify Data
Check data integrity and display statistics.

```bash
python scripts/verify_data.py
```

### Reset Database
Clear all data and recreate tables.

```bash
python scripts/reset_database.py
```

## CVD Configuration

CVD (Cumulative Volume Delta) uses rolling windows defined in `config/agent_config.yaml`:

```yaml
regime:
  cvd_windows:
    5M: 288    # 24 hours (288 * 5min)
    15M: 96    # 24 hours (96 * 15min)
    1H: 24     # 24 hours
    4H: 30     # 5 days (30 * 4h)
    1D: 5      # 5 days
```

## Notes

- Delta values from Databento are stored as uint32; negative values wrap around (e.g., -1 becomes 4294967295). The `update_orderflow_metrics.py` script handles this conversion.
- 4H and 1D timeframes use CME session boundaries (18:00 ET), not UTC midnight.
- Always run `update_orderflow_metrics.py` after loading new MBP-10 data to populate DOM/CVD in the order_book table.
