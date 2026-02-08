# Orderflow Signal Backtesting Results

This document summarizes backtesting results for orderflow signals on MNQ futures data.

## Signal Types

| Signal | Description | Best Timeframes |
|--------|-------------|-----------------|
| **LSF** | Liquidity Sweep Fade - price sweeps beyond range then snaps back | 4H, 5M |
| **Absorption** | High volume with flat price - one side absorbing the other | 5M, 15M |
| **Exhaustion** | High volume with small range - move running out of steam | 5M |
| **Delta Unwind** | Cumulative delta reaches extreme and starts reversing | 15M |
| **Institutional** | Large trades with directional flow - smart money activity | 15M |
| **TF Divergence** | Trade flow diverges from price - contrarian accumulation/distribution | 15M |

---

## LSF (Liquidity Sweep Fade) Results

LSF detects price sweeps beyond prior range that then snap back. Orderflow confirmation significantly improves signal quality.

### Mode Comparison

| TF | Mode | PF | Hit Rate | Signals | Best Parameters |
|----|------|-----|----------|---------|-----------------|
| **5M** | delta_div | 3.22 | 60.0% | 10 | sweep 0.30%, snap 0.30%, snapB 2, lb 10 |
| **5M** | volume | 5.32 | 71.4% | 14 | sweep 0.30%, snap 0.30%, snapB 3, lb 10, volM 2.0 |
| **5M** | both | **6.67** | 72.7% | 11 | sweep 0.30%, snap 0.30%, snapB 5, lb 10, volM 1.5 |
| | | | | | |
| **15M** | delta_div | **1.77** | 59.1% | 22 | sweep 0.20%, snap 0.50%, snapB 2, lb 10 |
| **15M** | volume | 1.71 | 60.3% | 68 | sweep 0.15%, snap 0.50%, snapB 1, lb 20, volM 1.2 |
| **15M** | both | 1.46 | 54.5% | 22 | - |
| **15M** | price | 1.69 | 51.1% | 92 | sweep 0.15%, snap 0.30%, snapB 5, lb 10 |
| | | | | | |
| **1H** | delta_div | **5.29** | 58.8% | 17 | sweep 0.30%, snap 0.30%, snapB 1, lb 30 |
| **1H** | volume | 1.44 | 54.8% | 290 | sweep 0.10%, snap 0.30%, snapB 5, lb 20, volM 1.2 |
| **1H** | both | 3.27 | 58.1% | 31 | - |
| | | | | | |
| **4H** | delta_div | **18.26** | 91.7% | 12 | sweep 0.15%, snap 0.50%, snapB 1, lb 30 |
| **4H** | volume | 1.58 | 71.0% | 31 | sweep 0.30%, snap 0.50%, snapB 1, lb 20, volM 1.2 |
| **4H** | both | 5.28 | 69.2% | 13 | - |

### Production Defaults

Based on backtesting, production uses:

| TF | Mode | Parameters |
|----|------|------------|
| 5M | both | sweep 0.30%, snap 0.30%, snapB 5, lb 10, volM 1.5 |
| 15M | pure price | sweep 0.15%, snap 0.30%, snapB 5, lb 10 |
| 1H | delta_div | sweep 0.30%, snap 0.30%, snapB 1, lb 30 |
| 4H | delta_div | sweep 0.15%, snap 0.50%, snapB 1, lb 30 |

### Key Insights

1. **4H delta_div is exceptional**: PF 18.26 with 91.7% hit rate
2. **Volume spike hurts 1H/4H**: Adding volume requirement reduces edge
3. **5M benefits from both confirmations**: Both mode has best PF (6.67)
4. **15M uses pure price**: Orderflow modes have weak edge (~1.7 PF), pure price is simpler

---

## Absorption Results

Absorption detects high volume with flat price - indicates one side absorbing aggressive flow.

### Trade Flow Mode (Recommended)

| TF | PF | Hit Rate | Signals | Best Parameters |
|----|-----|----------|---------|-----------------|
| **5M** | 2.89 | 64.3% | 42 | vol 2.0, priceTol 0.20%, deltaZ 1.5, tfThr 0.60, lb 20 |
| **15M** | 2.45 | 61.8% | 55 | vol 1.8, priceTol 0.30%, deltaZ 1.0, tfThr 0.55, lb 20 |

### Production Defaults

| TF | Parameters |
|----|------------|
| 5M | vol_mult 2.0, price_tol 0.002, delta_z 1.5, tf_thr 0.60, lb 20 |
| 15M | vol_mult 1.8, price_tol 0.003, delta_z 1.0, tf_thr 0.55, lb 20 |

### Key Insights

1. **Only enable on 5M and 15M**: Higher timeframes smooth trade_flow_ratio too much
2. **Trade flow mode outperforms DOM**: Use instant_delta + trade_flow_ratio instead of DOM imbalance
3. **Requires trades data**: Must have trade_flow_ratio column populated

---

## Exhaustion Results

Exhaustion detects high volume with small price range - indicates move is running out of steam.

### Trade Flow Mode (Recommended)

| TF | PF | Hit Rate | Signals | Best Parameters |
|----|-----|----------|---------|-----------------|
| **5M** | 6.42 | 70.6% | 34 | vol 2.5, rng 0.70, deltaZ 1.0, tfThr 0.55, trendConf Yes |
| **15M** | 2.72 | 58.8% | 51 | vol 1.8, rng 0.60, deltaZ 1.0, tfThr 0.55, trendConf Yes |
| **1H** | 2.31 | 60.4% | 48 | vol 1.3, rng 0.70, deltaZ 0.5, tfThr 0.52, trendConf Yes |

### Production Defaults

| TF | Parameters |
|----|------------|
| 5M | vol_mult 2.5, range_ratio 0.70, trend_lookback 3, lb 15 |
| 15M | vol_mult 1.8, range_ratio 0.60, trend_lookback 10, lb 20 |
| 1H | vol_mult 1.3, range_ratio 0.70, trend_lookback 5, lb 30 |

### Key Insights

1. **5M has strong edge**: PF 6.42 with 70.6% hit rate
2. **Trend confirmation required**: Improves signal quality significantly
3. **Direction from delta z-score**: Strong buying + flat = bearish, strong selling + flat = bullish

---

## Delta Unwind Results

Delta Unwind detects when cumulative delta reaches an extreme and starts reversing. The accumulated buying/selling pressure unwinds, causing price to follow.

### Mode Comparison (15M only)

| Mode | PF | Hit Rate | Signals | Best Parameters |
|------|-----|----------|---------|-----------------|
| **trade_flow** | **8.59** | **87.5%** | 8 | zscore 1.5, unwind 15%, bars 8, lb 100, tfThr 0.52 |
| volume | 8.76 | 77.8% | 9 | zscore 1.5, unwind 30%, bars 8, lb 100, volM 1.2 |
| delta_only | 7.18 | 77.8% | 9 | zscore 1.5, unwind 30%, bars 8, lb 100 |
| both | ∞ | 100% | 5 | zscore 1.5, unwind 5%, bars 5, lb 100, tfThr 0.52, volM 1.2 |
| dom | - | - | 0 | DOM data too sparse for unwind timing |
| all | - | - | 0 | Combined filters too restrictive |

### Results by Timeframe

| TF | PF | Hit Rate | Signals | Best Parameters |
|----|-----|----------|---------|--------------------|
| **5M** | - | - | 0-3 | Too rare (no valid combos) |
| **15M** | **8.59** | **87.5%** | 8 | trade_flow mode (see above) |
| **1H** | - | ≤50% | 5-9 | No predictive edge |
| **4H** | - | - | 2 | Too few signals |

### Production Defaults

| TF | Mode | Parameters |
|----|------|------------|
| 15M | trade_flow | zscore 1.5, unwind_pct 0.15, unwind_bars 8, lookback 100, tf_threshold 0.52 |

### Key Insights

1. **Only enable on 15M**: Other timeframes have no predictive edge
2. **Trade flow confirmation recommended**: 87.5% hit rate vs 77.8% baseline
3. **Long lookback required**: 100 bars captures full delta cycle
4. **Lower unwind threshold with trade_flow**: 15% unwind (vs 30% for delta_only) due to confirmation
5. **DOM mode doesn't work**: DOM data doesn't align well with unwind timing
6. **"both" mode too restrictive**: 100% hit rate but only 5 signals - consider trade_flow alone

---

## Institutional Activity Results

Institutional Activity detects "smart money" accumulation/distribution by looking for multiple large trades (≥50 contracts) with directional trade flow.

### Results by Timeframe

| TF | PF | Hit Rate | Signals | Best Parameters |
|----|-----|----------|---------|-----------------|
| **15M** | **5.61** | **72.2%** | 18 | large_trade_min 2, flow_threshold 0.55 |
| **5M** | 1.94 | 70.0% | 10 | large_trade_min 4, flow_threshold 0.55 |
| **1H** | - | - | 0-4 | Too few signals / no edge |

### Production Defaults

| TF | Parameters |
|----|------------|
| 15M | large_trade_min 2, flow_threshold 0.55 |

### Key Insights

1. **15M is optimal**: Best balance of signal frequency and edge (PF 5.61, 72.2%)
2. **Requires large trades**: Signal needs at least 2+ large trades per bar
3. **Only works on lower timeframes**: 1H+ aggregates trades too much, losing signal
4. **Flow threshold 0.55**: Lower threshold captures more signals while maintaining edge

---

## Trade Flow Divergence Results

Trade Flow Divergence detects when trade flow (buy/sell ratio) diverges from price direction - a contrarian signal indicating hidden accumulation or distribution.

### Results by Timeframe

| TF | PF | Hit Rate | Signals | Best Parameters |
|----|-----|----------|---------|-----------------|
| **15M** | **29.65** | **80.0%** | 10 | flow_threshold 0.60, price_change 0.20%, lookback 3 |
| **5M** | 6.09 | 70.0% | 10 | flow_threshold 0.60, price_change 0.30%, lookback 10 |
| **1H** | - | - | ≤10 | No edge (hit rates ≤50%) |

### Production Defaults

| TF | Parameters |
|----|------------|
| 15M | flow_threshold 0.60, price_change_pct 0.002, lookback_bars 3 |

### Key Insights

1. **15M has exceptional edge**: PF 29.65 with 80% hit rate (though limited signals)
2. **Price change threshold matters**: 0.20% provides cleaner divergence signals
3. **Short lookback works better**: 3-5 bars captures immediate divergence
4. **Contrarian interpretation**: "Price down but buying" = accumulation → bullish

---

## Running Backtests

### LSF Backtest

```bash
# Parameter sweep with orderflow mode
python scripts/backtesting/backtest_lsf.py --timeframe 5M --sweep --mode both

# Modes: price, delta_div, volume, both
python scripts/backtesting/backtest_lsf.py --timeframe 4H --sweep --mode delta_div
```

### Absorption Backtest

```bash
# Trade flow mode (recommended)
python scripts/backtesting/backtest_absorption.py --timeframe 5M --sweep

# Legacy DOM mode
python scripts/backtesting/backtest_absorption.py --timeframe 5M --sweep --no-trade-flow
```

### Exhaustion Backtest

```bash
# Trade flow mode (recommended)
python scripts/backtesting/backtest_exhaustion.py --timeframe 5M --sweep

# Legacy mode
python scripts/backtesting/backtest_exhaustion.py --timeframe 5M --sweep --no-trade-flow
```

### Delta Unwind Backtest

```bash
# Parameter sweep with modes (only 15M has edge)
python scripts/backtesting/backtest_delta_unwind.py --timeframe 15M --sweep --mode trade_flow

# Modes: delta_only, trade_flow, volume, both, dom, all
python scripts/backtesting/backtest_delta_unwind.py --timeframe 15M --sweep --mode delta_only

# Single run with custom params
python scripts/backtesting/backtest_delta_unwind.py --timeframe 15M --mode trade_flow --zscore 1.5 --unwind-pct 0.15 --unwind-bars 8 --lookback 100 --tf-threshold 0.52
```

### Trades Signals Backtest

```bash
# Institutional Activity sweep
python scripts/backtesting/backtest_trades_signals.py --timeframe 15M --sweep --signal-type institutional

# Trade Flow Divergence sweep
python scripts/backtesting/backtest_trades_signals.py --timeframe 15M --sweep --signal-type tfd

# Both signals
python scripts/backtesting/backtest_trades_signals.py --timeframe 15M --sweep --signal-type both

# Single run with custom params
python scripts/backtesting/backtest_trades_signals.py --timeframe 15M --signal-type institutional --inst-large-min 2 --inst-flow 0.55
python scripts/backtesting/backtest_trades_signals.py --timeframe 15M --signal-type tfd --tfd-flow 0.60 --tfd-price-pct 0.002 --tfd-lookback 3
```

---

## Metrics Explanation

| Metric | Description |
|--------|-------------|
| **PF (Profit Factor)** | Total wins / Total losses. >1.5 = strong edge |
| **Hit Rate** | % of signals where price moved in predicted direction (5-bar horizon) |
| **Signals** | Total signals detected in backtest period |
| **sweep_pct** | Min % price must exceed prior high/low to count as sweep |
| **snap_pct** | Min % snapback into prior range to confirm fade |
| **snapB** | Max bars to wait for snapback confirmation |
| **lb** | Lookback bars for rolling high/low calculation |
| **volM** | Volume multiplier - sweep bar volume must exceed avg * volM |
| **deltaZ** | Delta z-score threshold for direction confirmation |
| **tfThr** | Trade flow ratio threshold (>0.5 = more buys) |

---

## Data Requirements

Backtests require the following columns in `ohlcv_ticks`:

| Signal | Required Columns |
|--------|------------------|
| LSF | timestamp, high, low, close, volume, instant_delta (optional) |
| Absorption | timestamp, open, close, volume, instant_delta, trade_flow_ratio |
| Exhaustion | timestamp, open, high, low, close, volume, instant_delta |
| Delta Unwind | timestamp, close, volume, instant_delta, trade_flow_ratio (for trade_flow mode) |
| Institutional | timestamp, close, trade_flow_ratio, large_trade_count |
| TF Divergence | timestamp, close, trade_flow_ratio |

Data can be downloaded using:
```bash
python scripts/data/download_data_pipeline.py --start 2025-08-01 --end 2025-12-01
```

---

*Last updated: 2026-02-04*
