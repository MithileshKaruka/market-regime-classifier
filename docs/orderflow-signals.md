# Features - Order Flow Analysis & Market Regime Classification

This module contains the core logic for analyzing order flow data and classifying market regimes.

---

## Table of Contents

1. [Market Regime Classifier (Agent Bias Score)](#market-regime-classifier-agent-bias-score)
2. [Order Flow Signals](#order-flow-signals)
3. [Data Sources](#data-sources)

---

## Market Regime Classifier (Agent Bias Score)

The Agent Bias Score is a unified 0-100 scoring system that combines multiple signal categories to determine market direction and trading mode.

### Formula

```
Total Score = (Trend & Structure × 20%) + (Market Intensity × 20%) + (Order Flow Alpha × 60%)
```

### Component 1: Trend & Structure (20% of total)

Measures price trend and market structure patterns.

| Sub-component | Weight | Description |
|---------------|--------|-------------|
| **EMA Trend** | 40% | EMA 12/25 spread and price position relative to EMAs |
| **Market Structure** | 40% | Swing high/low pattern analysis (HH/HL vs LH/LL) |
| **Price vs S/R** | 20% | Price position relative to support/resistance levels |

**EMA Trend Scoring:**
- STRONG_UP (85-100): EMA spread > 0.5% and price > EMA
- UP (65-85): EMA spread > 0.1% and price > EMA
- NEUTRAL (50): EMAs converged
- DOWN (15-35): EMA spread < -0.1% and price < EMA
- STRONG_DOWN (0-15): EMA spread < -0.5% and price < EMA

**Market Structure Scoring:**
- HH_HL (80): Higher highs + higher lows = bullish structure
- LH_LL (20): Lower highs + lower lows = bearish structure
- BREAKOUT_UP (90): Price breaks above prior swing high
- BREAKOUT_DOWN (10): Price breaks below prior swing low
- CONSOLIDATION (50): No clear pattern

### Component 2: Market Intensity (20% of total)

Measures conviction behind price moves using volume and flow toxicity.

| Sub-component | Weight | Description |
|---------------|--------|-------------|
| **RVOL** | 50% | Relative Volume vs 20-period moving average |
| **VPIN** | 50% | Volume-Synchronized Probability of Informed Trading |

**RVOL Scoring:**
- >= 2.0x: Very high conviction (base score 80)
- >= 1.5x: High conviction (base score 70)
- >= 1.0x: Normal volume (base score 50)
- >= 0.5x: Low conviction (base score 35)
- < 0.5x: Very low volume, likely fakeout (base score 20)

Score is then adjusted based on price direction (UP amplifies bullish, DOWN inverts to bearish).

**VPIN Scoring:**
- >= 0.7: High informed trading - institutional flow detected
- >= 0.5: Moderate informed trading
- < 0.5: Retail flow dominant

High VPIN amplifies the existing direction signal.

### Component 3: Order Flow Alpha (60% of total)

The most heavily weighted component - measures what institutional money is doing using **context-aware scoring**.

#### Context-Aware Scoring Logic

The scoring dynamically adjusts based on which primary signal is active:

**When PRIMARY SIGNAL is active (Absorption, Exhaustion, or Delta Unwind):**

| Component | Weight | Description |
|-----------|--------|-------------|
| **Primary Signal** | 50% | The active signal (highest conviction) |
| **LDR** | 20% | Liquidity Depth Ratio - wall detection |
| **OBI** | 15% | Order Book Imbalance - bid/ask depth |
| **CVD** | 15% | Cumulative Volume Delta |

**When NO PRIMARY SIGNAL is active (BASE mode):**

| Component | Weight | Description |
|-----------|--------|-------------|
| **LDR** | 33% | Liquidity Depth Ratio |
| **OBI** | 33% | Order Book Imbalance |
| **CVD** | 34% | Cumulative Volume Delta |

#### Primary Signal Priority (by conviction)

When multiple primary signals are active, the strongest signal is used:

1. **Delta Unwind** - 86.7% hit rate on 5M (highest conviction)
2. **Exhaustion** - 81.8% hit rate on 5M
3. **Absorption** - 66.7% hit rate on 15M

#### Supporting Signal Scoring

**OBI/LDR Scoring (0-100):**
- >= 2.5x: Strong support wall (90-95)
- >= 2.0x: Support present (80)
- >= 1.3x: Mild bid bias (55-60)
- <= 0.4x: Strong resistance wall (5-10)
- <= 0.5x: Resistance present (20)
- <= 0.77x: Mild ask bias (40-45)
- else: Neutral (50)

**CVD Scoring (0-100):**
Uses configured threshold (default 5000) to normalize:
- >= 2x threshold: Strong buying (90-100)
- >= threshold: Moderate buying (60-90)
- > 0: Weak buying (50-60)
- <= -2x threshold: Strong selling (0-10)
- <= -threshold: Moderate selling (10-40)
- < 0: Weak selling (40-50)

### Score to Trading Mode Mapping

| Score Range | Mode | Trading Action |
|-------------|------|----------------|
| **0-30** | HIGH_BEARISH | Short entries only, ignore support bounces, sell rallies |
| **30-45** | WEAK_BEARISH | Exit longs, wait for clarity, don't enter shorts yet |
| **45-55** | NEUTRAL | Wait mode, avoid trading (high chop risk) |
| **55-70** | WEAK_BULLISH | Cautious longs at proven S/R levels only |
| **70-100** | HIGH_BULLISH | Aggressive longs, buy breakouts, add to winners |

### Confidence Level

Determined by agreement between components:
- **HIGH**: Score variance < 15 AND high RVOL + VPIN
- **MEDIUM**: Score variance < 25
- **LOW**: Score variance >= 25 (conflicting signals)

### Component Alignment (Cross-Component Communication)

The scoring system calculates **Order Flow Alpha FIRST** to derive direction, then uses that direction to adjust other components. This ensures all components "talk" to each other rather than being calculated independently.

**Calculation Order:**
1. **Order Flow Alpha** (60%) - Calculated first
2. **Trend & Structure** (20%) - Adjusted by orderflow direction
3. **Market Intensity** (20%) - Amplified/dampened by orderflow agreement

**Orderflow Direction Derivation:**
- Score > 55 → BULLISH
- Score < 45 → BEARISH
- Score 45-55 → NEUTRAL

**Market Intensity Alignment Modifier (±20%):**

When price direction and orderflow direction are compared:
- **AGREE** (e.g., price UP + orderflow BULLISH): Amplify score by 20% (push away from 50)
- **CONFLICT** (e.g., price UP + orderflow BEARISH): Dampen score by 20% (pull toward 50)

Details output shows `[ALIGNED]` or `[CONFLICT]` tag.

**Trend & Structure Confidence Modifier (±15%):**

When trend direction and orderflow direction are compared:
- **CONFIRMS** (e.g., uptrend + BULLISH orderflow): Boost score by 15%
- **CONTRADICTS** (e.g., uptrend + BEARISH orderflow): Reduce score by 15%

Details output shows `[OF+]` (orderflow confirms) or `[OF-]` (orderflow contradicts) tag.

**Example:**
```
Order Flow Alpha: 72 (BULLISH direction)
Trend & Structure: 65 → 67.2 (+15% boost because trend confirms)
Market Intensity: 60 → 62 (+20% amplify because price agrees)
```

---

## Order Flow Signals

Three primary signals used in context-aware scoring for trade timing.

### 1. Delta Unwind (HIGHEST CONVICTION)

**Logic:** Cumulative delta reaches an extreme (z-score), then starts reversing - indicates exhaustion of trend and mean reversion.

**Detection Criteria:**
- CVD z-score > `delta_zscore_threshold` (default: 2.0)
- CVD reverses by > `delta_unwind_pct` (default: 10%) within `delta_unwind_bars` (default: 3)

**Direction:**
- BULLISH: Extreme negative delta unwinding (sellers exhausted)
- BEARISH: Extreme positive delta unwinding (buyers exhausted)

**Backtested Performance:**
- 5M timeframe: **86.7% hit rate**, 18.58 profit factor (15 signals)
- 15M timeframe: 66.7% hit rate, 2.20 profit factor (18 signals)

### 2. Exhaustion (HIGH CONVICTION)

**Logic:** High volume bar with minimal price movement - indicates the move is running out of steam.

**Detection Criteria:**
- Volume > `exhaustion_volume_mult` × average (default: 1.5x)
- Price range < `exhaustion_range_ratio_max` × average range (default: 0.5x)
- Trend context from lookback period determines direction

**Direction:**
- BULLISH: Exhaustion after downtrend (sellers exhausted)
- BEARISH: Exhaustion after uptrend (buyers exhausted)

**Backtested Performance:**
- 5M timeframe: **81.8% hit rate**, 12.19 profit factor (11 signals)
- 15M timeframe: 58.5% hit rate, 1.48 profit factor (94 signals)

### 3. Absorption

**Logic:** Large volume hitting a price level but price remains stable - indicates a large player absorbing aggressive flow.

**Detection Criteria:**
- Volume > `absorption_volume_mult` × 20-bar average (default: 2.0x)
- Price range < `absorption_price_tol` × average range (default: 0.5x)
- DOM imbalance > `absorption_dom_threshold` (default: 0.6) for direction

**Direction:**
- BULLISH: Bids absorbing aggressive sells (buyers defending)
- BEARISH: Asks absorbing aggressive buys (sellers defending)

**Backtested Performance:**
- 15M timeframe: **66.7% hit rate**, 2.62 profit factor

---

## Data Sources

All data flows through the `ohlcv_ticks` table, which is the single source of truth.

### Historical Data
- **Source**: Databento OHLCV-1M and MBP-1 DBN files
- **Loaded via**: `scripts/data/load_historical_data.py`
- **Contains**: OHLCV + orderflow metrics (from MBP-1)

### Live Data
- **Source**: Databento MBP-1 (top-of-book quotes)
- **Streamed via**: `app/streaming/live_ingestion.py`
- **Contains**: Full OHLCV + orderflow metrics

### Orderflow Metrics (Live Only)

| Metric | Calculation | Use |
|--------|-------------|-----|
| DOM Imbalance | bid_size / (bid_size + ask_size) | Order book pressure |
| Instant Delta | Inferred from quote size changes | Bar buying/selling |
| CVD | Cumulative sum of instant_delta | Trend confirmation |

### Database Schema

```sql
ohlcv_ticks (
    timestamp TIMESTAMP,
    symbol VARCHAR,
    timeframe VARCHAR,
    open, high, low, close DOUBLE,
    volume BIGINT,
    instant_delta BIGINT,      -- NULL for historical
    dom_imbalance DOUBLE,      -- NULL for historical
    cvd BIGINT,                -- NULL for historical
    PRIMARY KEY (symbol, timeframe, timestamp)
)
```

---

## Configuration

All parameters are configurable via `config/agent_config.yaml`:

```yaml
# Scoring weights
scoring:
  trend_structure_weight: 20
  market_intensity_weight: 20
  orderflow_alpha_weight: 60

# Signal thresholds
orderflow_alpha:
  absorption_volume_mult: 2.0
  absorption_price_tol: 0.5
  delta_zscore_threshold: 2.0
  delta_unwind_pct: 0.1
  exhaustion_volume_mult: 1.5
  exhaustion_range_ratio_max: 0.5
```

---

## File Structure

```
features/
├── README.md                 # This file
├── agent_bias.py            # Agent Bias Score calculator (main regime classifier)
├── orderflow_signals.py     # Signal detection (Absorption, Delta Unwind, Exhaustion, OBI)
├── orderflow_metrics.py     # RVOL, VPIN, LDR calculations
├── order_flow.py            # OrderFlowCalculator for MBP processing
├── indicators.py            # Technical indicators (EMA, ATR, Bollinger Bands, etc.)
└── support_resistance.py    # S/R level detection
```

---

## Usage Example

```python
from app.features.agent_bias import AgentBiasCalculator
from app.features.orderflow_signals import OrderflowSignalDetector

# Calculate regime bias score
calculator = AgentBiasCalculator()
result = calculator.calculate_total_bias(
    df=ohlcv_data,
    sr_levels=support_resistance,
    rvol=1.8,
    vpin=0.65,
    obi_ratio=1.4,
    ldr=1.6,
    absorption_signals=[{"direction": "BULLISH", "strength": 0.8}],
    delta_unwind_signals=[{"direction": "BULLISH", "strength": 0.95}],
    exhaustion_signals=[],
    cvd=8500,
)

print(f"Score: {result.total_score}")  # 0-100
print(f"Mode: {result.mode}")          # HIGH_BULLISH, WEAK_BULLISH, etc.
print(f"Order Flow Mode: {result.orderflow_alpha.active_mode}")  # DELTA_UNWIND, ABSORPTION, etc.
print(f"Action: {result.recommendation}")

# Detect signals on a DataFrame
detector = OrderflowSignalDetector(timeframe="5M")
signals = detector.detect_all_signals(df)
for sig in signals:
    print(f"{sig.signal_type}: {sig.direction} at {sig.price}")
```
