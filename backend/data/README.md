# Data Processing Guide

This folder contains market data files and the DuckDB database for the Market Regime Classifier.

## Data Schemas

The system supports two data configurations:

### Live Streaming (Personal Plan)
- **MBP-1**: Top-of-book quotes (best bid/ask) - for DOM imbalance
- **Trades**: Individual trade executions - for accurate CVD/delta

### Historical Analysis
- **MBP-10**: 10-level order book depth (if available)
- **Trades**: Individual trade executions

## Quick Start

Use the unified data loader to run the complete pipeline:

```bash
cd backend
python scripts/load_all_data.py
```

This opens an interactive menu to:
1. Check current data status
2. Load OHLCV candlestick data
3. Load MBP tick data (MBP-1 or MBP-10)
4. Load trades data
5. Update order flow metrics (DOM/CVD)
6. Run full pipeline (all steps)

### Command Line Options

```bash
python scripts/load_all_data.py --status   # Check data status
python scripts/load_all_data.py --ohlcv    # Load OHLCV only
python scripts/load_all_data.py --mbp      # Load MBP data only
python scripts/load_all_data.py --trades   # Load trades data only
python scripts/load_all_data.py --update   # Update orderflow metrics only
python scripts/load_all_data.py --all      # Run all steps non-interactively
```

## Data Files

### OHLCV Data
- `glbx-mdp3-*.ohlcv-1m.dbn.zst` - 1-minute OHLCV candlestick data from Databento
  - Compressed DBN format (zstd)
  - Used for chart display and technical analysis

### MBP (Market By Price) Data
- `glbx-mdp3-*.mbp-1.dbn` - MBP-1 (top of book) data
- `glbx-mdp3-*.mbp-10.dbn` - MBP-10 (10-level) order book data
  - Contains bid/ask prices, sizes, and counts
  - Used for DOM imbalance calculation

### Trades Data
- `glbx-mdp3-*.trades.dbn` - Individual trade executions
  - Contains trade price, size, and aggressor side
  - Used for accurate CVD/delta calculation

### Database
- `market_data.duckdb` - DuckDB database with processed data
  - `order_book` table: OHLCV bars with DOM imbalance and CVD
  - `mbp_ticks` table: Tick-level order book data
  - `trades` table: Individual trade executions

## Data Processing Pipeline

The pipeline consists of these steps. **All steps must be run** for order flow signals to appear on the chart.

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

### Step 2a: Load MBP Tick Data (for DOM)
Load order book tick data for DOM imbalance calculation.

```bash
python scripts/load_mbp.py
```

This script:
- Auto-detects MBP-1 or MBP-10 files
- Presents a menu to select files to process
- Offers streaming (faster) or iterative (lower RAM) processing modes
- Calculates DOM imbalance from bid/ask volumes
- Stores in `mbp_ticks` table

### Step 2b: Load Trades Data (for CVD) - Recommended
Load trade execution data for accurate CVD calculation.

```bash
python scripts/load_trades.py
```

This script:
- Loads individual trade executions
- Extracts trade aggressor side ('A' = buy, 'B' = sell)
- Calculates signed_size and cumulative delta
- Stores in `trades` table

**Why use trades for CVD?**
- MBP delta is approximated from order book changes
- Trades delta uses actual trade direction (more accurate)
- Personal plan has trades available for live streaming

### Step 3: Update Order Flow Metrics (REQUIRED!)
Aggregate tick/trade data and update OHLCV bars with real DOM/CVD values.

```bash
python scripts/update_orderflow_metrics.py
```

**This step is critical!** Without it, the order_book table will have default DOM (0.5) and CVD (0.0) values, and no order flow signals will appear.

This script:
- Uses `trades` table for CVD (if available, more accurate)
- Uses `mbp_ticks` table for DOM imbalance
- Falls back to `mbp_ticks` for CVD if no trades data
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
| total_bid_depth | BIGINT | Sum of all bid levels (MBP-1: same as bid_size) |
| total_ask_depth | BIGINT | Sum of all ask levels (MBP-1: same as ask_size) |
| dom_imbalance | DOUBLE | Order book imbalance |
| delta | DOUBLE | Instant delta (bid - ask volume) |
| cvd | DOUBLE | Cumulative delta |

### trades Table
| Column | Type | Description |
|--------|------|-------------|
| timestamp | TIMESTAMP | Trade timestamp |
| symbol | VARCHAR | Instrument symbol |
| price | DOUBLE | Trade price |
| size | INTEGER | Trade size |
| side | VARCHAR | Aggressor side ('A' = ask/buy, 'B' = bid/sell) |
| signed_size | INTEGER | Signed size (+ for buy, - for sell) |
| delta | BIGINT | Cumulative delta |

## Live Streaming

For live data streaming, the system subscribes to both MBP-1 and Trades schemas:

```python
# In live_ingestion.py
client.subscribe(dataset="GLBX.MDP3", schema="mbp-1", symbols=["MNQ"])
client.subscribe(dataset="GLBX.MDP3", schema="trades", symbols=["MNQ"])
```

This provides:
- Real-time DOM imbalance from top-of-book quotes
- Accurate CVD from trade aggressor side
- Lower data costs (MBP-1 vs MBP-10)

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

## Trade Side Encoding

Databento uses these codes for trade aggressor side:
- `'A'` (Ask): Buy aggressor - buyer lifted the ask (bullish)
- `'B'` (Bid): Sell aggressor - seller hit the bid (bearish)
- `'N'` (None): Unknown/indeterminate

## Notes

- Delta values from MBP data may be stored as uint32; negative values wrap around. The scripts handle this conversion.
- 4H and 1D timeframes use CME session boundaries (18:00 ET), not UTC midnight.
- Always run `update_orderflow_metrics.py` after loading new tick/trade data to populate DOM/CVD in the order_book table.
- For personal Databento plans, MBP-10 may not be available for live streaming - use MBP-1 + Trades instead.
