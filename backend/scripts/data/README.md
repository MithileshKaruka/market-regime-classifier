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
