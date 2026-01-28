# Documentation

Detailed documentation for the Market Regime Classifier project.

## Contents

| Document | Description |
|----------|-------------|
| [Orderflow Signals](orderflow-signals.md) | Order flow analysis, bias scoring system, and signal detection |
| [Data Pipeline](data-pipeline.md) | Data loading, streaming, gap recovery, and maintenance scripts |
| [Database Schema](database-schema.md) | DuckDB schema, table structures, and data retention |
| [AWS Deployment](aws-deployment.md) | EC2 deployment guide with Docker Compose |

## Quick Links

### Getting Started
- [Main README](../README.md) - Project overview and setup instructions

### Configuration
- [Agent Config](../backend/config/agent_config.yaml) - Scoring weights, thresholds, CVD windows
- [Databento Config](../backend/config/databento_config.yaml) - Streaming and retention settings

### Key Scripts
- [preload_historical.py](../backend/scripts/data/preload_historical.py) - Initial data preload (5yr OHLCV + 60d MBP-1)
- [load_historical_data.py](../backend/scripts/data/load_historical_data.py) - Historical data loader
- [backfill_gaps.py](../backend/scripts/maintenance/backfill_gaps.py) - Gap detection & recovery
- [live_ingestion.py](../backend/app/streaming/live_ingestion.py) - Real-time streaming

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        DATA SOURCES                              │
├─────────────────────────────────────────────────────────────────┤
│  Databento                                                       │
│  ├── OHLCV-1M (price + volume)                                  │
│  └── MBP-1 (top-of-book quotes → orderflow)                     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      DATA PIPELINE                               │
├─────────────────────────────────────────────────────────────────┤
│  Historical: load_historical_data.py                            │
│  ├── Build continuous contract                                   │
│  ├── Resample to 5M, 15M, 1H, 4H, 1D                           │
│  └── Calculate orderflow (DOM, delta, rolling CVD)              │
│                                                                  │
│  Live: live_ingestion.py                                         │
│  ├── Subscribe to MBP-1 stream                                   │
│  ├── Aggregate to OHLCV bars                                     │
│  ├── Push via WebSocket                                          │
│  └── Archive to DBN files                                        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       DATABASE                                   │
├─────────────────────────────────────────────────────────────────┤
│  DuckDB: ohlcv_ticks (single source of truth)                   │
│  ├── OHLCV prices                                                │
│  ├── Volume (actual contracts)                                   │
│  └── Orderflow (DOM, delta, CVD)                                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    ANALYSIS LAYER                                │
├─────────────────────────────────────────────────────────────────┤
│  Features Module:                                                │
│  ├── Agent Bias Score (0-100)                                   │
│  │   ├── Trend & Structure (20%)                                │
│  │   ├── Market Intensity (20%)                                 │
│  │   └── Orderflow Alpha (60%)                                  │
│  │                                                               │
│  ├── Signal Detection                                            │
│  │   ├── Absorption                                              │
│  │   ├── Delta Unwind                                            │
│  │   ├── Exhaustion                                              │
│  │   ├── OBI (Order Book Imbalance)                             │
│  │   └── LSF (Liquidity Sweep Fade)                             │
│  │                                                               │
│  └── Support/Resistance                                          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      API / FRONTEND                              │
├─────────────────────────────────────────────────────────────────┤
│  FastAPI:                                                        │
│  ├── /api/v2/chart/{timeframe}                                  │
│  ├── /api/orderflow/agent-bias/{timeframe}                      │
│  ├── /api/regime/{timeframe}                                    │
│  └── /ws/live (WebSocket)                                       │
│                                                                  │
│  React Frontend:                                                 │
│  ├── TradingView Charts                                          │
│  └── Regime Panel                                                │
└─────────────────────────────────────────────────────────────────┘
```
