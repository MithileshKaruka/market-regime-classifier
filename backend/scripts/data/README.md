# Historical Data Loading Scripts

## preload_historical.py

Downloads historical market data from Databento and loads it into the DuckDB database.

### Data Types

- **OHLCV-1M**: 1-minute candles aggregated to 5M, 15M, 1H, 4H, 1D timeframes
- **MBP-1**: Market-by-price (Level 1 order book) for DOM imbalance and delta calculations
- **Trades**: Tick-level trade data for institutional activity and trade flow metrics

### Local Usage

```bash
# Estimate cost before downloading
python scripts/data/preload_historical.py --estimate

# Download with default settings (5yr OHLCV + 60d MBP)
python scripts/data/preload_historical.py --load

# Custom ranges
python scripts/data/preload_historical.py --load --ohlcv-years 5 --mbp-days 14

# OHLCV only
python scripts/data/preload_historical.py --load --ohlcv-only

# MBP only
python scripts/data/preload_historical.py --load --mbp-only
```

### Docker / EC2 Usage

Run the weekly reload job in background (survives terminal close):

```bash
# Find container name
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"

# Run reload with auto-confirm (5yr OHLCV + 14d MBP + 14d trades)
yes | docker exec -i market-regime-classifier-backend-1 python /app/scripts/data/preload_historical.py --load --ohlcv-years 5 --mbp-days 14 --trades-days 14 > /home/ubuntu/reload_$(date +%Y%m%d).log 2>&1 &

# Monitor progress
tail -f /home/ubuntu/reload_$(date +%Y%m%d).log

# Check if still running
ps aux | grep preload_historical
```

### Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `--estimate` | Estimate Databento API cost without downloading | - |
| `--load` | Download and load data | - |
| `--ohlcv-only` | Only download OHLCV data | - |
| `--mbp-only` | Only download MBP-1 data | - |
| `--trades-only` | Only download trades data | - |
| `--ohlcv-years` | Years of OHLCV history | 5 |
| `--ohlcv-start` | Fixed start date (YYYY-MM-DD) | - |
| `--mbp-days` | Days of MBP-1 data | 60 |
| `--trades-days` | Days of trades data | 14 |
| `--keep-files` | Keep downloaded DBN files | false |
| `--swap-db` | Minimal downtime mode (see below) | false |
| `--download-only` | Download to new DB only (no copy/swap) | false |
| `--copy-and-swap` | Copy ingestion data and swap DBs | false |

### Split Workflow (`--download-only` + `--copy-and-swap`)

For maximum flexibility, split the process into two steps:

**Step 1: Download historical data (run anytime, even during market hours)**
```bash
# Takes 1-2 hours, doesn't affect live system
yes | docker exec -i market-regime-classifier-backend-1 python /app/scripts/data/preload_historical.py --load --download-only --ohlcv-years 5 --mbp-days 14 --trades-days 14 > /home/ubuntu/download_$(date +%Y%m%d).log 2>&1 &
```

**Step 2: Copy ingestion data and swap (run during CME close)**
```bash
# Takes ~30 seconds total
docker exec -i market-regime-classifier-backend-1 python /app/scripts/data/preload_historical.py --copy-and-swap
```

**Workflow:**
1. `--download-only`: Downloads historical data into `market_data_new.duckdb` and verifies
2. Wait for CME maintenance window (Sunday 5-6pm ET or daily close)
3. `--copy-and-swap`: Copies any live-ingested bars newer than historical, then swaps DBs

**Benefits:**
- Run the long download during market hours without affecting live ingestion
- The swap itself only takes ~30 seconds during CME close
- Perfect for weekday updates when you can't wait for weekend maintenance

### Minimal Downtime Mode (`--swap-db`)

The `--swap-db` flag enables a workflow that minimizes live ingestion downtime and can be run **any day of the week** (not just weekends):

1. **Load into new database**: All data is downloaded and loaded into `market_data_new.duckdb`
2. **Verify data**: Automatically checks row counts and date ranges
3. **Copy live-ingested bars**: Copies any bars from current DB that are newer than the historical data (preserves the gap between Databento's latest data and now)
4. **Swap databases**: Only pauses ingestion for ~20-30 seconds during the file swap:
   - `market_data.duckdb` → `market_data_backup.duckdb`
   - `market_data_new.duckdb` → `market_data.duckdb`
5. **Resume ingestion**: Live data continues flowing

```bash
# Recommended for production (minimal downtime, works any day)
yes | docker exec -i market-regime-classifier-backend-1 python /app/scripts/data/preload_historical.py --load --swap-db --ohlcv-years 5 --mbp-days 14 --trades-days 14 > /home/ubuntu/reload_$(date +%Y%m%d).log 2>&1 &
```

**Benefits:**
- Live ingestion only paused for ~20-30 seconds (vs hours without `--swap-db`)
- **Works on weekdays**: Copies live-ingested bars to preserve recent data
- Automatic verification before swap prevents bad data from going live
- Backup kept at `market_data_backup.duckdb` for rollback if needed

**How the gap is handled:**
- Databento historical data typically has ~1 day delay
- Live ingestion writes bars to the current database in real-time
- Before swapping, the script copies any bars from the current DB that are **newer** than the latest historical data
- This ensures you don't lose weekend/recent data when swapping on a Monday or mid-week

### Live Ingestion Integration

The preload script automatically **pauses live ingestion** before loading data and **resumes** after completion. This prevents database write conflicts.

- If the backend is running, ingestion is paused via `/api/admin/ingestion/pause`
- After data load completes (or on error), ingestion resumes via `/api/admin/ingestion/resume`
- If the backend is not running, the script proceeds without pausing

**Manual control** (if needed):
```bash
# Pause live ingestion
curl -X POST http://localhost:8000/api/admin/ingestion/pause

# Check status
curl http://localhost:8000/api/admin/ingestion/status

# Resume live ingestion
curl -X POST http://localhost:8000/api/admin/ingestion/resume
```

### Weekly Maintenance Schedule

Recommended weekly reload to refresh order flow data:

- **OHLCV**: 5 years (stable, rarely needs full reload)
- **MBP/Trades**: 14 days (rolling window for accurate DOM/delta)

Run every weekend when markets are closed (CME maintenance: Sunday 5-6pm ET).
