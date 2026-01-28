# Market Regime Classifier - MNQ Futures

A multi-timeframe market regime analysis platform for Micro NQ (MNQ) futures using OHLCV data, order book analysis, and trade flow signals.

## Quick Start

### Prerequisites
- Python 3.12+
- Node.js 18+
- Databento API key (for data ingestion)

### 1. Backend Setup
```bash
cd backend
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt
```

### 2. Configure Databento API
```bash
# Copy the example secrets file
cp config/secrets.yaml.example config/secrets.yaml

# Edit and add your Databento API key
```

### 3. Load Historical Data
```bash
cd backend

# Load OHLCV data (price history)
python scripts/data/load_historical_data.py --ohlcv data/glbx-mdp3.ohlcv-1m.dbn.zst

# Load MBP-1 data (orderflow metrics)
python scripts/data/load_historical_data.py --mbp data/glbx-mdp3.mbp-1.dbn.zst
```

This loads historical data from Databento DBN files (supports `.dbn` and `.dbn.zst`).

### 4. Start Backend
```bash
cd backend
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Backend available at: **http://localhost:8000**

### 5. Start Frontend
```bash
cd frontend
npm install
npm run dev
```

Frontend available at: **http://localhost:5173**

## Architecture

### Tech Stack
- **Backend**: FastAPI + Python
- **Frontend**: React + TypeScript + Vite
- **Database**: DuckDB (embedded, serverless)
- **Data Processing**: Polars (high-performance DataFrames)
- **Charts**: TradingView Lightweight Charts
- **Data Source**: Databento (CME MDP 3.0)
- **Agent Framework**: LangGraph (for trading decisions)
- **Real-time**: WebSocket for live chart updates

### Directory Structure
```
market-regime-classifier/
├── backend/
│   ├── app/
│   │   ├── api/           # FastAPI endpoints
│   │   ├── agent/         # LangGraph trading agent
│   │   ├── classifiers/   # Regime classification
│   │   ├── data/          # Database storage layer
│   │   ├── features/      # Indicators & order flow
│   │   └── streaming/     # Live data ingestion
│   ├── config/            # Centralized configuration
│   │   ├── config.py      # Dataclass definitions
│   │   ├── agent_config.yaml  # Tunable parameters
│   │   ├── databento_config.yaml  # Streaming settings
│   │   └── secrets.yaml   # API keys (gitignored)
│   └── scripts/           # Data loading & maintenance
│       ├── backtesting/   # Backtest scripts & DBN loader
│       ├── data/          # Historical data loader
│       └── maintenance/   # Gap recovery & cleanup jobs
├── docs/                  # Documentation
│   ├── orderflow-signals.md   # Bias scoring & signals
│   ├── data-pipeline.md       # Data loading & recovery
│   └── database-schema.md     # DuckDB schema
├── frontend/
│   └── src/
│       ├── components/    # React components
│       └── config.ts      # API configuration
└── README.md
```

### Documentation
See the [docs/](docs/) folder for detailed documentation:
- [Orderflow Signals](docs/orderflow-signals.md) - Bias scoring system and signal detection
- [Data Pipeline](docs/data-pipeline.md) - Data loading, streaming, and gap recovery
- [Database Schema](docs/database-schema.md) - DuckDB schema and data retention

## Features

### Order Flow Analysis
- **DOM Imbalance**: Real-time order book imbalance from MBP-1 data
- **CVD (Cumulative Volume Delta)**: Rolling window CVD from quote changes
- **RVOL**: Relative Volume with Point of Control (POC)
- **VPIN**: Volume-Synchronized Probability of Informed Trading
- **LDR**: Liquidity Depth Ratio for wall detection

### Signal Detection
- **Absorption**: Large volume absorbed at stable price levels
- **LSF (Liquidity Sweep Fade)**: Stop run followed by snap-back reversals
- **OBI (Order Book Imbalance)**: Weighted imbalance across order book levels
- **Delta Unwind**: CVD extreme reversal signals
- **Exhaustion**: High volume with minimal price movement

### Bias Scoring System (0-100)
Combines three signal categories into a unified trading bias:

| Category | Weight | Components |
|----------|--------|------------|
| Trend & Structure | 20% | EMA crossovers, market structure (HH/HL vs LH/LL), S/R levels |
| Market Intensity | 20% | RVOL (volume conviction) + VPIN (informed trading) |
| Order Flow Alpha | 60% | OBI, LDR, Absorption, Delta Unwind, Exhaustion signals |

### Score Interpretation
| Score Range | Mode | Action |
|-------------|------|--------|
| 0-30 | HIGH_BEARISH | Short entries only, ignore support bounces |
| 30-45 | WEAK_BEARISH | Exit longs, don't enter shorts yet |
| 45-55 | NEUTRAL | Wait mode, avoid trading |
| 55-70 | WEAK_BULLISH | Cautious longs at proven S/R only |
| 70-100 | HIGH_BULLISH | Aggressive longs, buy breakouts |

### Technical Indicators
- VWAP and Rolling VWAP (7, 30, 90, 200 periods)
- EMA (12, 25, 20, 50, 100, 200 periods)
- Bollinger Bands
- ATR (Average True Range)
- Support/Resistance levels with touch counts

### Multi-Timeframe Support
| Timeframe | CVD Window | Use Case |
|-----------|------------|----------|
| 5M | 288 bars (24h) | Intraday scalping |
| 15M | 96 bars (24h) | Intraday swing |
| 1H | 24 bars (24h) | Day trading |
| 4H | 30 bars (5d) | Swing trading |
| 1D | 5 bars (5d) | Position trading |

## Configuration

All parameters are centralized in `backend/config/agent_config.yaml`. Key sections:

### Scoring Weights
```yaml
scoring:
  trend_structure_weight: 20
  market_intensity_weight: 20
  orderflow_alpha_weight: 60
```

### Order Flow Thresholds
```yaml
orderflow_alpha:
  obi_strong_imbalance: 1.5
  obi_moderate_imbalance: 1.2
  cvd_threshold: 5000
  absorption_volume_mult: 2.0
  delta_zscore_threshold: 2.0
```

### Instrument Settings
```yaml
instrument:
  symbol: MNQ
  tick_size: 0.25
  min_price: 18000
  max_price: 32000
```

## API Endpoints

### Chart Data
```
GET /api/v2/chart/{timeframe}?limit=5000&offset=0&indicators=ema_20,ema_50
```

### Order Flow Features
```
GET /api/features/{timeframe}
```

### Orderflow Signals
```
GET /api/orderflow/signals/{timeframe}
GET /api/orderflow/advanced/{timeframe}
GET /api/orderflow/agent-bias/{timeframe}
```

### Regime Classification
```
GET /api/regime/current
GET /api/regime/{timeframe}
GET /api/regime/history/{timeframe}?limit=100
```

### Support/Resistance
```
GET /api/regime/support-resistance/{timeframe}
GET /api/regime/signals/{timeframe}
```

### Trading Agent
```
GET /api/orderflow/agent/{timeframe}?position=FLAT
```

### WebSocket (Live Updates)
```
WS /ws/live
```

Events: `bar_update`, `bar_close`, `signal`, `regime_change`

### Health Check
```
GET /api/health
```

## Data Pipeline

### Database Schema

The system uses a single `ohlcv_ticks` table as the source of truth:

| Column | Type | Description |
|--------|------|-------------|
| timestamp | TIMESTAMP | Bar start time |
| symbol | VARCHAR | Instrument symbol (MNQ) |
| timeframe | VARCHAR | Timeframe (5M, 15M, 1H, 4H, 1D) |
| open/high/low/close | DOUBLE | OHLC prices |
| volume | BIGINT | Bar volume |
| instant_delta | BIGINT | Bar delta (buy - sell volume) |
| dom_imbalance | DOUBLE | Order book imbalance (0-1) |
| total_bid_depth | DOUBLE | Average bid depth |
| total_ask_depth | DOUBLE | Average ask depth |
| cvd | BIGINT | Cumulative Volume Delta |

### Historical Data
- **Source**: Databento `.dbn.zst` files (OHLCV-1M and MBP-1)
- **Loader**: `scripts/data/load_historical_data.py`
- **Storage**: DuckDB `ohlcv_ticks` table

```bash
# Load OHLCV (price data, NULL orderflow)
python scripts/data/load_historical_data.py --ohlcv data/ohlcv.dbn.zst

# Load MBP-1 (adds orderflow metrics: delta, DOM, CVD)
python scripts/data/load_historical_data.py --mbp data/mbp1.dbn.zst
```

### Live Streaming
```bash
cd backend
python -m app.streaming.live_ingestion
```

The live ingestion service:
- Subscribes to MBP-1 schema from Databento
- Aggregates ticks into OHLCV bars with orderflow metrics
- Stores completed bars to `ohlcv_ticks`
- Pushes real-time updates via WebSocket
- Archives raw data to `.dbn.zst` files for backtesting

### Backtesting
```python
from scripts.backtesting.dbn_loader import DBNLoader

loader = DBNLoader()
df = loader.load_for_backtest(
    start_date="2024-01-01",
    end_date="2024-01-31",
    timeframe="15M"
)
```

### Weekly Maintenance
Run every Friday at 4:30 PM CST (after CME close):
```bash
python scripts/maintenance/weekly_maintenance.py
```

Tasks:
- Clean up DBN archives older than 60 days
- Clean up OHLCV data older than 5 years
- Vacuum database

### Gap Detection & Backfill
If live streaming was interrupted, detect and backfill missing data:
```bash
# Check for gaps
python scripts/maintenance/backfill_gaps.py --check

# Backfill gaps from Databento (downloads OHLCV + MBP-1)
python scripts/maintenance/backfill_gaps.py --backfill

# Backfill specific date range
python scripts/maintenance/backfill_gaps.py --backfill --start 2024-01-15 --end 2024-01-16

# Backfill only orderflow (MBP-1) if OHLCV already exists
python scripts/maintenance/backfill_gaps.py --backfill --mbp-only
```

The backfill utility:
- Detects unexpected gaps in `ohlcv_ticks` (ignores weekends/maintenance)
- Downloads OHLCV-1M (price + volume) and MBP-1 (orderflow) from Databento
- Loads and aggregates with correct rolling CVD windows

## Development

### Running Tests
```bash
cd backend
pytest
```

### Verifying Data
```bash
cd backend
python scripts/utils/verify_data.py
```

## Troubleshooting

### No Order Flow Signals?
Check that `ohlcv_ticks` has orderflow data:
```sql
SELECT COUNT(*) FROM ohlcv_ticks WHERE instant_delta IS NOT NULL;
```

### Price Data Looks Wrong?
The system filters settlement artifacts. Valid MNQ range is configured in `config/agent_config.yaml`:
```yaml
instrument:
  min_price: 18000
  max_price: 32000
```

### Backend Won't Start?
```bash
cd backend
pip install -r requirements.txt
```

### Check Configuration
```bash
cd backend
python -c "from config import get_config; c = get_config(); print(f'Symbol: {c.instrument.symbol}')"
```

## License

MIT

## Contributing

This is a personal project, but feedback and suggestions are welcome!
