# Market Regime Classifier - MNQ Futures

A multi-timeframe market regime analysis platform for Micro NQ (MNQ) futures using OHLCV data and order flow analysis.

## 🚀 Quick Start

### Prerequisites
- Python 3.12+
- Node.js 18+
- Databento API key

### 1. Start Backend
```bash
cd backend
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Backend available at: **http://localhost:8000**

### 2. Start Frontend
```bash
cd frontend
npm run dev
```

Frontend available at: **http://localhost:5173**

## 📊 Current Status

### Data Loaded
- **Period**: January 2021 - January 2026 (5 years)
- **Source**: Databento OHLCV-1M schema
- **Total Bars**: 2.8M 1-minute bars
- **Clean Data**: 19.4K 1H bars (after filtering outliers)

### Timeframes Available
| Timeframe | Total Bars | Clean Bars | Coverage |
|-----------|------------|------------|----------|
| 1M        | 1,768,910  | TBD        | 5 years  |
| 5M        | 353,784    | TBD        | 5 years  |
| 15M       | 117,928    | TBD        | 5 years  |
| 1H        | 29,520     | 19,468     | 2.2 years|
| 4H        | 8,002      | 3,322      | 5 years  |
| 1D        | 1,557      | 203        | 5 years  |

## 🏗️ Architecture

### Tech Stack
- **Backend**: FastAPI + Python
- **Frontend**: React + TypeScript + Vite
- **Database**: DuckDB (embedded, serverless)
- **Data Processing**: Polars (high-performance DataFrames)
- **Charts**: TradingView Lightweight Charts
- **Data Source**: Databento (CME MDP 3.0)

### Directory Structure
```
market-regime-classifier/
├── backend/
│   ├── app/
│   │   ├── api/          # FastAPI endpoints
│   │   ├── data/         # Database storage layer
│   │   ├── features/     # Indicators & order flow
│   │   └── classifiers/  # Regime classification logic
│   ├── scripts/          # Data loading & maintenance
│   └── data/             # DuckDB database (295MB)
├── frontend/
│   └── src/
│       ├── components/   # React components
│       └── config.ts     # API configuration
└── README.md
```

## 📡 API Endpoints

### Chart Data
```
GET /api/v2/chart/{timeframe}?limit=5000&offset=0&indicators=ema_20,ema_50
```

**Parameters:**
- `timeframe`: 5M, 15M, 1H, 4H, 1D
- `limit`: Number of bars (default: 5000, max: 10000)
- `offset`: Pagination offset (default: 0)
- `indicators`: Comma-separated list (ema_20, ema_50, ema_100, ema_200, rvwap_7, etc.)

**Response:**
```json
{
  "bars": [...],
  "total_count": 19468,
  "returned_count": 5000,
  "offset": 0
}
```

### Health Check
```
GET /api/health
```

### Regime Data
```
GET /api/regime/latest/{timeframe}
GET /api/regime/history/{timeframe}?limit=100
```

## 🛠️ Development

### Backend Setup
```bash
cd backend
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt
```

### Frontend Setup
```bash
cd frontend
npm install
```

### Load Historical Data
```bash
cd backend
python scripts/load_ohlcv.py
```

This loads the 5-year OHLCV dataset from the DBN file located at:
`backend/data/glbx-mdp3-20210116-20260115.ohlcv-1m.dbn.zst`

## 📈 Features

### Implemented ✅
- ✅ OHLCV chart display (5 years of data)
- ✅ Multi-timeframe support (1M, 5M, 15M, 1H, 4H, 1D)
- ✅ Price filtering (removes settlement artifacts)
- ✅ Pagination support (up to 10k bars per request)
- ✅ Technical indicators (EMA, RVWAP)
- ✅ Clean data storage in DuckDB

### In Progress 🚧
- 🚧 CVD (Cumulative Volume Delta) from trades data
- 🚧 Regime classification (bullish/bearish/neutral)
- 🚧 Support/Resistance level detection
- 🚧 Lazy loading for chart (infinite scroll)

### Planned 📋
- 📋 Real-time MBP-10 order book data
- 📋 DOM (Depth of Market) imbalance
- 📋 Order flow-based signals
- 📋 Multi-timeframe regime alignment
- 📋 Automated trading signals

## 🗄️ Database Schema

### `order_book` Table
```sql
CREATE TABLE order_book (
    timestamp TIMESTAMP,
    symbol VARCHAR,
    timeframe VARCHAR,
    open DOUBLE,
    high DOUBLE,
    low DOUBLE,
    close DOUBLE,
    volume BIGINT,
    dom_imbalance DOUBLE,  -- Placeholder for real-time data
    cvd DOUBLE,            -- Will calculate from trades
    vwap DOUBLE,
    PRIMARY KEY (timestamp, symbol, timeframe)
)
```

### `regimes` Table
```sql
CREATE TABLE regimes (
    timestamp TIMESTAMP,
    symbol VARCHAR,
    timeframe VARCHAR,
    regime VARCHAR,         -- BULLISH, BEARISH, NEUTRAL
    confidence DOUBLE,
    key_signal VARCHAR,
    dom_imbalance DOUBLE,
    cvd DOUBLE,
    vwap DOUBLE,
    price DOUBLE,
    PRIMARY KEY (timestamp, symbol, timeframe)
)
```

## 🐛 Troubleshooting

### Backend Issues

**Backend won't start?**
```bash
cd backend
pip install -r requirements.txt
```

**Database errors?**
```bash
cd backend
python scripts/reset_database.py
python scripts/load_ohlcv.py
```

**Check database contents:**
```bash
cd backend
python scripts/verify_data.py
```

### Frontend Issues

**Frontend won't start?**
```bash
cd frontend
npm install
```

**Chart not showing data?**
1. Ensure backend is running at http://localhost:8000
2. Check browser console for API errors
3. Hard refresh browser (Ctrl+Shift+R)

**Charts show compressed/weird prices?**
- Backend filters outliers (prices < $1000 or > $30000)
- This is normal - settlement artifacts are removed
- Valid MNQ range over 5 years: ~$10k-$30k

## 📝 Notes

### Data Quality
- **Outliers removed**: Settlement/rollover artifacts create extreme low values (~$225)
- **Filter applied**: All OHLC values must be > $1000 and < $30000
- **Result**: Clean, tradeable price data

### Performance
- **API limit**: 5000 bars default, 10000 max per request
- **Database size**: ~295MB for 5 years of multi-timeframe data
- **Response time**: < 1s for 5000 bars with indicators

### Next Phase: CVD Calculation
To enable true order flow analysis:
1. Download `trades` DBN file (same date range)
2. Calculate CVD from trade sides (buyer vs seller initiated)
3. Update database with actual CVD values
4. Enable CVD-based regime classification

## 📄 License

MIT

## 🤝 Contributing

This is a personal project, but feedback and suggestions are welcome!
