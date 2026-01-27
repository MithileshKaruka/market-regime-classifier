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

### 2. Load Data
```bash
cd backend
python scripts/load_all_data.py --all
```

This runs the complete data pipeline:
- Load OHLCV candlestick data
- Load MBP tick data (DOM imbalance)
- Load trades data (CVD/delta)
- Update order flow metrics

See [backend/data/README.md](backend/data/README.md) for detailed data ingestion steps.

### 3. Start Backend
```bash
cd backend
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Backend available at: **http://localhost:8000**

### 4. Start Frontend
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
│   │   ├── services/      # Storage services
│   │   └── streaming/     # Live data ingestion
│   ├── config/            # Centralized configuration
│   │   ├── config.py      # Dataclass definitions
│   │   └── agent_config.yaml  # All tunable parameters
│   ├── scripts/           # Data loading & maintenance
│   └── data/              # DuckDB database & raw data files
├── frontend/
│   └── src/
│       ├── components/    # React components
│       └── config.ts      # API configuration
└── README.md
```

## Features

### Order Flow Analysis
- **DOM Imbalance**: Real-time order book imbalance from MBP-1/MBP-10 data
- **CVD (Cumulative Volume Delta)**: Rolling window CVD from actual trade executions
- **RVOL**: Relative Volume with Point of Control (POC)
- **VPIN**: Volume-Synchronized Probability of Informed Trading
- **LDR**: Liquidity Depth Ratio for wall detection

### Signal Detection
- **Absorption**: Large volume absorbed at stable price levels
- **LSF (Liquidity Sweep Fade)**: Stop run followed by snap-back reversals
- **OBI (Order Book Imbalance)**: Weighted imbalance across order book levels

### Bias Scoring System (0-100)
Combines three signal categories into a unified trading bias:

| Category | Weight | Components |
|----------|--------|------------|
| Trend & Structure | 20% | EMA crossovers, market structure (HH/HL vs LH/LL), S/R levels |
| Market Intensity | 30% | RVOL (volume conviction) + VPIN (informed trading) |
| Order Flow Alpha | 50% | OBI, LDR, Absorption, LSF signals |

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
  market_intensity_weight: 30
  orderflow_alpha_weight: 50
```

### Order Flow Thresholds
```yaml
orderflow_alpha:
  obi_strong_imbalance: 1.5
  obi_moderate_imbalance: 1.2
  cvd_threshold: 5000
  absorption_volume_mult: 1.3
  lsf_spike_mult: 1.5
```

### Instrument Settings
```yaml
instrument:
  symbol: MNQ
  tick_size: 0.25
  min_price: 18000
  max_price: 32000
```

See [backend/config/agent_config.yaml](backend/config/agent_config.yaml) for all available parameters.

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

### Health Check
```
GET /api/health
```

## Data Pipeline

### Historical Data
1. **OHLCV**: Candlestick data from Databento OHLCV-1M schema
2. **MBP**: Order book data (MBP-1 for live, MBP-10 for historical)
3. **Trades**: Individual trade executions with aggressor side

### Live Streaming
```python
# Subscribes to both schemas for real-time data
client.subscribe(dataset="GLBX.MDP3", schema="mbp-1", symbols=["MNQ"])
client.subscribe(dataset="GLBX.MDP3", schema="trades", symbols=["MNQ"])
```

See [backend/data/README.md](backend/data/README.md) for complete data ingestion documentation.

## Database Schema

### `order_book` Table
| Column | Type | Description |
|--------|------|-------------|
| timestamp | TIMESTAMP | Bar start time |
| symbol | VARCHAR | Instrument symbol (MNQ) |
| timeframe | VARCHAR | Timeframe (5M, 15M, 1H, 4H, 1D) |
| open/high/low/close | DOUBLE | OHLC prices |
| volume | BIGINT | Bar volume |
| dom_imbalance | DOUBLE | Order book imbalance (0-1) |
| cvd | DOUBLE | Rolling Cumulative Volume Delta |
| vwap | DOUBLE | Volume-Weighted Average Price |

### `regimes` Table
| Column | Type | Description |
|--------|------|-------------|
| timestamp | TIMESTAMP | Classification time |
| regime | VARCHAR | BULLISH, BEARISH, NEUTRAL |
| confidence | DOUBLE | Classification confidence |
| key_signal | VARCHAR | Primary signal driver |

### `trades` Table
| Column | Type | Description |
|--------|------|-------------|
| timestamp | TIMESTAMP | Trade timestamp |
| price | DOUBLE | Trade price |
| size | INTEGER | Trade size |
| side | VARCHAR | Aggressor ('A' = buy, 'B' = sell) |

## Development

### Running Tests
```bash
cd backend
pytest
```

### Verifying Data
```bash
cd backend
python scripts/verify_data.py
```

### Database Status
```bash
cd backend
python scripts/load_all_data.py --status
```

## Troubleshooting

### No Order Flow Signals?
Make sure to run the orderflow metrics update after loading data:
```bash
python scripts/update_orderflow_metrics.py
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
