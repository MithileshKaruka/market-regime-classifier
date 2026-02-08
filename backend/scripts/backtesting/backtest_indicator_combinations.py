#!/usr/bin/env python3
"""
Backtest Agent Bias with Different Orderflow Indicator Combinations

Tests various weightings and combinations of:
1. Orderflow components (LDR, OBI, CVD, Primary Signals)
2. Component weights (Orderflow vs Trend vs Intensity)
3. Primary signal inclusion/exclusion

Measures predictive accuracy by comparing bias score to future price movement.
"""
import argparse
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
from datetime import datetime
import itertools

import polars as pl
import numpy as np

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.data.storage import DuckDBStorage
from app.features.orderflow_signals import OrderflowSignalDetector, SignalType, SignalDirection


@dataclass
class BacktestConfig:
    """Configuration for a single backtest run"""
    name: str
    # Component weights (must sum to 100)
    orderflow_weight: float = 60.0
    trend_weight: float = 20.0
    intensity_weight: float = 20.0
    # Orderflow sub-weights when primary signal active
    primary_signal_weight: float = 50.0
    ldr_weight_primary: float = 20.0
    obi_weight_primary: float = 15.0
    cvd_weight_primary: float = 15.0
    # Orderflow sub-weights when NO primary signal (base mode)
    ldr_weight_base: float = 33.0
    obi_weight_base: float = 33.0
    cvd_weight_base: float = 34.0
    # Which primary signals to consider
    use_absorption: bool = True
    use_exhaustion: bool = True
    use_delta_unwind: bool = True
    use_lsf: bool = False  # Often disabled
    use_obi_signal: bool = False  # Often disabled
    # Thresholds
    bullish_threshold: float = 55.0
    bearish_threshold: float = 45.0


@dataclass
class BacktestResult:
    """Results from a single backtest configuration"""
    config_name: str
    timeframe: str
    total_signals: int
    bullish_signals: int
    bearish_signals: int
    neutral_signals: int
    bullish_hit_rate: float
    bearish_hit_rate: float
    overall_hit_rate: float
    profit_factor: float
    avg_return_bullish: float
    avg_return_bearish: float
    score_correlation: float  # Correlation between score and future return
    high_conviction_hit_rate: float  # Hit rate when score > 70 or < 30


def load_data(timeframe: str, symbol: str = "MNQ", limit: int = 5000) -> pl.DataFrame:
    """Load OHLCV data with orderflow columns"""
    with DuckDBStorage() as storage:
        df = storage.conn.execute(f"""
            SELECT * FROM (
                SELECT
                    timestamp, open, high, low, close, volume,
                    dom_imbalance, cvd, instant_delta, trade_flow_ratio,
                    large_trade_count
                FROM ohlcv_ticks
                WHERE symbol = '{symbol}' AND timeframe = '{timeframe}'
                ORDER BY timestamp DESC
                LIMIT {limit}
            ) ORDER BY timestamp ASC
        """).pl()
    return df


def calculate_ldr_score(dom_imbalance: float) -> float:
    """Calculate LDR (Liquidity Depth Ratio) score from DOM imbalance

    DOM imbalance ranges from ~0.3 to ~0.7 typically
    0.5 = balanced, >0.5 = bid heavy (bullish), <0.5 = ask heavy (bearish)
    """
    if dom_imbalance is None:
        return 50.0
    # Scale from 0.3-0.7 range to 0-100
    # 0.5 -> 50, 0.7 -> 100, 0.3 -> 0
    score = (dom_imbalance - 0.3) / 0.4 * 100
    return max(0, min(100, score))


def calculate_cvd_score(cvd: float, cvd_mean: float, cvd_std: float) -> float:
    """Calculate CVD score based on z-score

    Positive CVD = net buying = bullish
    Negative CVD = net selling = bearish
    """
    if cvd is None or cvd_std == 0:
        return 50.0
    z_score = (cvd - cvd_mean) / cvd_std
    # Map z-score to 0-100 (z=0 -> 50, z=2 -> ~85, z=-2 -> ~15)
    score = 50 + z_score * 17.5
    return max(0, min(100, score))


def calculate_obi_score(dom_imbalance: float) -> float:
    """Calculate OBI (Order Book Imbalance) score

    Similar to LDR but with different scaling for imbalance emphasis
    """
    if dom_imbalance is None:
        return 50.0
    # OBI is more aggressive - emphasize extremes
    if dom_imbalance > 0.55:
        score = 60 + (dom_imbalance - 0.55) / 0.15 * 40  # 55-70 -> 60-100
    elif dom_imbalance < 0.45:
        score = 40 - (0.45 - dom_imbalance) / 0.15 * 40  # 30-45 -> 0-40
    else:
        score = 50  # Neutral zone
    return max(0, min(100, score))


def calculate_trend_score(df: pl.DataFrame, idx: int, lookback: int = 20) -> float:
    """Calculate trend score using EMA crossover and price action"""
    if idx < lookback:
        return 50.0

    window = df[max(0, idx-lookback):idx+1]
    closes = window["close"].to_list()

    if len(closes) < 12:
        return 50.0

    # Simple EMA approximation
    ema_fast = np.mean(closes[-5:])  # 5-period
    ema_slow = np.mean(closes[-12:])  # 12-period

    current_price = closes[-1]

    # Trend direction
    if ema_fast > ema_slow:
        trend_bias = 65  # Bullish
        if current_price > ema_fast:
            trend_bias = 75  # Strong bullish
    elif ema_fast < ema_slow:
        trend_bias = 35  # Bearish
        if current_price < ema_fast:
            trend_bias = 25  # Strong bearish
    else:
        trend_bias = 50

    return trend_bias


def calculate_intensity_score(
    volume: float,
    volume_ma: float,
    price_change_pct: float
) -> float:
    """Calculate market intensity score from RVOL and price action"""
    if volume_ma == 0:
        return 50.0

    rvol = volume / volume_ma

    # Base score from RVOL
    if rvol >= 2.0:
        base = 80
    elif rvol >= 1.5:
        base = 70
    elif rvol >= 1.0:
        base = 55
    elif rvol >= 0.5:
        base = 40
    else:
        base = 30

    # Adjust based on price direction
    if price_change_pct > 0.001:  # Up move
        score = base + 10
    elif price_change_pct < -0.001:  # Down move
        score = 100 - base  # Invert for bearish intensity
    else:
        score = 50

    return max(0, min(100, score))


def detect_primary_signals(
    df: pl.DataFrame,
    idx: int,
    config: BacktestConfig,
    detector: OrderflowSignalDetector
) -> Tuple[Optional[str], float, str]:
    """Detect primary orderflow signals at given index

    Returns: (signal_type, signal_score, direction)
    """
    if idx < 30:  # Need history
        return None, 50.0, "NEUTRAL"

    window_df = df[:idx+1]

    signals_found = []

    # Check each enabled signal type
    if config.use_absorption:
        try:
            signals = detector.detect_absorption(window_df)
            if signals and signals[-1].timestamp == df[idx, "timestamp"]:
                sig = signals[-1]
                score = 70 + sig.strength * 30 if sig.direction == SignalDirection.BULLISH else 30 - sig.strength * 30
                signals_found.append(("ABSORPTION", score, sig.direction.value, sig.strength))
        except:
            pass

    if config.use_exhaustion:
        try:
            signals = detector.detect_exhaustion(window_df)
            if signals and signals[-1].timestamp == df[idx, "timestamp"]:
                sig = signals[-1]
                score = 70 + sig.strength * 30 if sig.direction == SignalDirection.BULLISH else 30 - sig.strength * 30
                signals_found.append(("EXHAUSTION", score, sig.direction.value, sig.strength))
        except:
            pass

    if config.use_delta_unwind:
        try:
            signals = detector.detect_delta_unwind(window_df)
            if signals and signals[-1].timestamp == df[idx, "timestamp"]:
                sig = signals[-1]
                score = 70 + sig.strength * 30 if sig.direction == SignalDirection.BULLISH else 30 - sig.strength * 30
                signals_found.append(("DELTA_UNWIND", score, sig.direction.value, sig.strength))
        except:
            pass

    if config.use_lsf:
        try:
            signals = detector.detect_lsf(window_df)
            if signals and signals[-1].timestamp == df[idx, "timestamp"]:
                sig = signals[-1]
                score = 70 + sig.strength * 30 if sig.direction == SignalDirection.BULLISH else 30 - sig.strength * 30
                signals_found.append(("LSF", score, sig.direction.value, sig.strength))
        except:
            pass

    # Return highest strength signal
    if signals_found:
        best = max(signals_found, key=lambda x: x[3])
        return best[0], best[1], best[2]

    return None, 50.0, "NEUTRAL"


def calculate_bias_score(
    df: pl.DataFrame,
    idx: int,
    config: BacktestConfig,
    detector: OrderflowSignalDetector,
    cvd_stats: Tuple[float, float],
    volume_ma: float
) -> Tuple[float, str, bool]:
    """Calculate unified bias score using given configuration

    Returns: (score, direction, has_primary_signal)
    """
    row = df.row(idx, named=True)

    # Calculate sub-scores
    ldr_score = calculate_ldr_score(row.get("dom_imbalance"))
    obi_score = calculate_obi_score(row.get("dom_imbalance"))
    cvd_score = calculate_cvd_score(row.get("cvd"), cvd_stats[0], cvd_stats[1])
    trend_score = calculate_trend_score(df, idx)

    # Price change for intensity
    if idx > 0:
        prev_close = df[idx-1, "close"]
        price_change_pct = (row["close"] - prev_close) / prev_close
    else:
        price_change_pct = 0

    intensity_score = calculate_intensity_score(row["volume"], volume_ma, price_change_pct)

    # Check for primary signals
    primary_type, primary_score, primary_dir = detect_primary_signals(df, idx, config, detector)
    has_primary = primary_type is not None

    # Calculate orderflow score
    if has_primary:
        # Primary signal active - use primary weights
        orderflow_score = (
            primary_score * (config.primary_signal_weight / 100) +
            ldr_score * (config.ldr_weight_primary / 100) +
            obi_score * (config.obi_weight_primary / 100) +
            cvd_score * (config.cvd_weight_primary / 100)
        )
    else:
        # Base mode - no primary signal
        orderflow_score = (
            ldr_score * (config.ldr_weight_base / 100) +
            obi_score * (config.obi_weight_base / 100) +
            cvd_score * (config.cvd_weight_base / 100)
        )

    # Determine orderflow direction
    if orderflow_score > config.bullish_threshold:
        of_direction = "BULLISH"
    elif orderflow_score < config.bearish_threshold:
        of_direction = "BEARISH"
    else:
        of_direction = "NEUTRAL"

    # Apply alignment modifiers to trend and intensity
    # Trend alignment
    if of_direction != "NEUTRAL":
        trend_dir = "BULLISH" if trend_score > 55 else "BEARISH" if trend_score < 45 else "NEUTRAL"
        if trend_dir == of_direction:
            # Amplify
            trend_score = 50 + (trend_score - 50) * 1.15
        elif trend_dir != "NEUTRAL" and of_direction != trend_dir:
            # Dampen
            trend_score = 50 + (trend_score - 50) * 0.85

    # Intensity alignment
    if of_direction != "NEUTRAL":
        intensity_dir = "BULLISH" if intensity_score > 55 else "BEARISH" if intensity_score < 45 else "NEUTRAL"
        if intensity_dir == of_direction:
            intensity_score = 50 + (intensity_score - 50) * 1.2
        elif intensity_dir != "NEUTRAL" and of_direction != intensity_dir:
            intensity_score = 50 + (intensity_score - 50) * 0.8

    # Clamp scores
    trend_score = max(0, min(100, trend_score))
    intensity_score = max(0, min(100, intensity_score))

    # Final weighted score
    final_score = (
        orderflow_score * (config.orderflow_weight / 100) +
        trend_score * (config.trend_weight / 100) +
        intensity_score * (config.intensity_weight / 100)
    )

    # Determine final direction
    if final_score > config.bullish_threshold:
        direction = "BULLISH"
    elif final_score < config.bearish_threshold:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    return final_score, direction, has_primary


def run_backtest(
    df: pl.DataFrame,
    config: BacktestConfig,
    timeframe: str,
    forward_bars: int = 5
) -> BacktestResult:
    """Run backtest for a single configuration"""

    detector = OrderflowSignalDetector(timeframe=timeframe)

    # Calculate CVD stats for scoring
    cvd_values = df["cvd"].drop_nulls().to_list()
    cvd_mean = np.mean(cvd_values) if cvd_values else 0
    cvd_std = np.std(cvd_values) if cvd_values else 1

    # Calculate volume MA
    volume_ma = df["volume"].mean()

    results = []

    # Start after warmup period
    start_idx = 50
    end_idx = len(df) - forward_bars - 1

    for idx in range(start_idx, end_idx):
        score, direction, has_primary = calculate_bias_score(
            df, idx, config, detector, (cvd_mean, cvd_std), volume_ma
        )

        # Calculate forward return
        current_close = df[idx, "close"]
        future_close = df[idx + forward_bars, "close"]
        forward_return = (future_close - current_close) / current_close

        # Determine if prediction was correct
        if direction == "BULLISH":
            correct = forward_return > 0
        elif direction == "BEARISH":
            correct = forward_return < 0
        else:
            correct = None  # Neutral doesn't predict

        results.append({
            "idx": idx,
            "score": score,
            "direction": direction,
            "forward_return": forward_return,
            "correct": correct,
            "has_primary": has_primary,
        })

    # Calculate metrics
    bullish_results = [r for r in results if r["direction"] == "BULLISH"]
    bearish_results = [r for r in results if r["direction"] == "BEARISH"]
    neutral_results = [r for r in results if r["direction"] == "NEUTRAL"]

    bullish_correct = sum(1 for r in bullish_results if r["correct"]) if bullish_results else 0
    bearish_correct = sum(1 for r in bearish_results if r["correct"]) if bearish_results else 0

    bullish_hit = bullish_correct / len(bullish_results) * 100 if bullish_results else 0
    bearish_hit = bearish_correct / len(bearish_results) * 100 if bearish_results else 0

    total_directional = len(bullish_results) + len(bearish_results)
    total_correct = bullish_correct + bearish_correct
    overall_hit = total_correct / total_directional * 100 if total_directional > 0 else 0

    # Calculate profit factor
    bullish_returns = [r["forward_return"] for r in bullish_results]
    bearish_returns = [-r["forward_return"] for r in bearish_results]  # Invert for shorts
    all_returns = bullish_returns + bearish_returns

    wins = sum(r for r in all_returns if r > 0)
    losses = abs(sum(r for r in all_returns if r < 0))
    profit_factor = wins / losses if losses > 0 else float('inf') if wins > 0 else 0

    # Average returns
    avg_bullish = np.mean([r["forward_return"] for r in bullish_results]) * 100 if bullish_results else 0
    avg_bearish = np.mean([r["forward_return"] for r in bearish_results]) * 100 if bearish_results else 0

    # Score correlation with forward return
    scores = [r["score"] for r in results]
    returns = [r["forward_return"] for r in results]
    correlation = np.corrcoef(scores, returns)[0, 1] if len(scores) > 10 else 0

    # High conviction hit rate (score > 70 or < 30)
    high_conviction = [r for r in results if r["score"] > 70 or r["score"] < 30]
    hc_correct = sum(1 for r in high_conviction if r["correct"]) if high_conviction else 0
    hc_hit = hc_correct / len(high_conviction) * 100 if high_conviction else 0

    return BacktestResult(
        config_name=config.name,
        timeframe=timeframe,
        total_signals=len(results),
        bullish_signals=len(bullish_results),
        bearish_signals=len(bearish_results),
        neutral_signals=len(neutral_results),
        bullish_hit_rate=bullish_hit,
        bearish_hit_rate=bearish_hit,
        overall_hit_rate=overall_hit,
        profit_factor=profit_factor,
        avg_return_bullish=avg_bullish,
        avg_return_bearish=avg_bearish,
        score_correlation=correlation,
        high_conviction_hit_rate=hc_hit,
    )


def generate_test_configs() -> List[BacktestConfig]:
    """Generate various configurations to test"""
    configs = []

    # 1. Current production config
    configs.append(BacktestConfig(
        name="PROD_CURRENT",
        orderflow_weight=60, trend_weight=20, intensity_weight=20,
        primary_signal_weight=50, ldr_weight_primary=20, obi_weight_primary=15, cvd_weight_primary=15,
        ldr_weight_base=33, obi_weight_base=33, cvd_weight_base=34,
        use_absorption=True, use_exhaustion=True, use_delta_unwind=True,
    ))

    # 2. Orderflow dominant (80%)
    configs.append(BacktestConfig(
        name="OF_80",
        orderflow_weight=80, trend_weight=10, intensity_weight=10,
        primary_signal_weight=50, ldr_weight_primary=20, obi_weight_primary=15, cvd_weight_primary=15,
        ldr_weight_base=33, obi_weight_base=33, cvd_weight_base=34,
        use_absorption=True, use_exhaustion=True, use_delta_unwind=True,
    ))

    # 3. Equal weights
    configs.append(BacktestConfig(
        name="EQUAL_33",
        orderflow_weight=34, trend_weight=33, intensity_weight=33,
        primary_signal_weight=50, ldr_weight_primary=20, obi_weight_primary=15, cvd_weight_primary=15,
        ldr_weight_base=33, obi_weight_base=33, cvd_weight_base=34,
        use_absorption=True, use_exhaustion=True, use_delta_unwind=True,
    ))

    # 4. CVD focused (no LDR/OBI)
    configs.append(BacktestConfig(
        name="CVD_ONLY",
        orderflow_weight=60, trend_weight=20, intensity_weight=20,
        primary_signal_weight=50, ldr_weight_primary=0, obi_weight_primary=0, cvd_weight_primary=50,
        ldr_weight_base=0, obi_weight_base=0, cvd_weight_base=100,
        use_absorption=True, use_exhaustion=True, use_delta_unwind=True,
    ))

    # 5. LDR focused
    configs.append(BacktestConfig(
        name="LDR_FOCUSED",
        orderflow_weight=60, trend_weight=20, intensity_weight=20,
        primary_signal_weight=50, ldr_weight_primary=40, obi_weight_primary=5, cvd_weight_primary=5,
        ldr_weight_base=60, obi_weight_base=20, cvd_weight_base=20,
        use_absorption=True, use_exhaustion=True, use_delta_unwind=True,
    ))

    # 6. No primary signals (base mode only)
    configs.append(BacktestConfig(
        name="NO_PRIMARY",
        orderflow_weight=60, trend_weight=20, intensity_weight=20,
        ldr_weight_base=33, obi_weight_base=33, cvd_weight_base=34,
        use_absorption=False, use_exhaustion=False, use_delta_unwind=False,
    ))

    # 7. Only Delta Unwind (highest backtest edge)
    configs.append(BacktestConfig(
        name="DELTA_UNWIND_ONLY",
        orderflow_weight=60, trend_weight=20, intensity_weight=20,
        primary_signal_weight=60, ldr_weight_primary=15, obi_weight_primary=10, cvd_weight_primary=15,
        ldr_weight_base=33, obi_weight_base=33, cvd_weight_base=34,
        use_absorption=False, use_exhaustion=False, use_delta_unwind=True,
    ))

    # 8. Only Exhaustion
    configs.append(BacktestConfig(
        name="EXHAUSTION_ONLY",
        orderflow_weight=60, trend_weight=20, intensity_weight=20,
        primary_signal_weight=60, ldr_weight_primary=15, obi_weight_primary=10, cvd_weight_primary=15,
        ldr_weight_base=33, obi_weight_base=33, cvd_weight_base=34,
        use_absorption=False, use_exhaustion=True, use_delta_unwind=False,
    ))

    # 9. High primary signal weight
    configs.append(BacktestConfig(
        name="HIGH_PRIMARY_70",
        orderflow_weight=60, trend_weight=20, intensity_weight=20,
        primary_signal_weight=70, ldr_weight_primary=10, obi_weight_primary=10, cvd_weight_primary=10,
        ldr_weight_base=33, obi_weight_base=33, cvd_weight_base=34,
        use_absorption=True, use_exhaustion=True, use_delta_unwind=True,
    ))

    # 10. Trend dominant
    configs.append(BacktestConfig(
        name="TREND_DOMINANT",
        orderflow_weight=30, trend_weight=50, intensity_weight=20,
        primary_signal_weight=50, ldr_weight_primary=20, obi_weight_primary=15, cvd_weight_primary=15,
        ldr_weight_base=33, obi_weight_base=33, cvd_weight_base=34,
        use_absorption=True, use_exhaustion=True, use_delta_unwind=True,
    ))

    # 11. Wider thresholds (60/40)
    configs.append(BacktestConfig(
        name="WIDE_THRESH_60_40",
        orderflow_weight=60, trend_weight=20, intensity_weight=20,
        bullish_threshold=60, bearish_threshold=40,
        primary_signal_weight=50, ldr_weight_primary=20, obi_weight_primary=15, cvd_weight_primary=15,
        ldr_weight_base=33, obi_weight_base=33, cvd_weight_base=34,
        use_absorption=True, use_exhaustion=True, use_delta_unwind=True,
    ))

    # 12. Narrow thresholds (52/48)
    configs.append(BacktestConfig(
        name="NARROW_THRESH_52_48",
        orderflow_weight=60, trend_weight=20, intensity_weight=20,
        bullish_threshold=52, bearish_threshold=48,
        primary_signal_weight=50, ldr_weight_primary=20, obi_weight_primary=15, cvd_weight_primary=15,
        ldr_weight_base=33, obi_weight_base=33, cvd_weight_base=34,
        use_absorption=True, use_exhaustion=True, use_delta_unwind=True,
    ))

    # 13. All signals including LSF
    configs.append(BacktestConfig(
        name="ALL_SIGNALS",
        orderflow_weight=60, trend_weight=20, intensity_weight=20,
        primary_signal_weight=50, ldr_weight_primary=20, obi_weight_primary=15, cvd_weight_primary=15,
        ldr_weight_base=33, obi_weight_base=33, cvd_weight_base=34,
        use_absorption=True, use_exhaustion=True, use_delta_unwind=True, use_lsf=True,
    ))

    # 14. OBI heavy (DOM imbalance focus)
    configs.append(BacktestConfig(
        name="OBI_HEAVY",
        orderflow_weight=60, trend_weight=20, intensity_weight=20,
        primary_signal_weight=40, ldr_weight_primary=10, obi_weight_primary=40, cvd_weight_primary=10,
        ldr_weight_base=20, obi_weight_base=60, cvd_weight_base=20,
        use_absorption=True, use_exhaustion=True, use_delta_unwind=True,
    ))

    # 15. Intensity dominant (RVOL/VPIN focus)
    configs.append(BacktestConfig(
        name="INTENSITY_DOMINANT",
        orderflow_weight=40, trend_weight=20, intensity_weight=40,
        primary_signal_weight=50, ldr_weight_primary=20, obi_weight_primary=15, cvd_weight_primary=15,
        ldr_weight_base=33, obi_weight_base=33, cvd_weight_base=34,
        use_absorption=True, use_exhaustion=True, use_delta_unwind=True,
    ))

    return configs


def print_results(results: List[BacktestResult], timeframe: str):
    """Print results in a formatted table"""
    print(f"\n{'='*120}")
    print(f"BACKTEST RESULTS - {timeframe}")
    print(f"{'='*120}")

    # Sort by overall hit rate
    results_sorted = sorted(results, key=lambda x: x.overall_hit_rate, reverse=True)

    print(f"\n{'Config':<25} {'Bull%':>7} {'Bear%':>7} {'Overall%':>9} {'PF':>7} {'HC Hit%':>8} {'Corr':>7} {'Bull#':>6} {'Bear#':>6} {'Neut#':>6}")
    print("-" * 120)

    for r in results_sorted:
        print(f"{r.config_name:<25} {r.bullish_hit_rate:>6.1f}% {r.bearish_hit_rate:>6.1f}% {r.overall_hit_rate:>8.1f}% {r.profit_factor:>7.2f} {r.high_conviction_hit_rate:>7.1f}% {r.score_correlation:>7.3f} {r.bullish_signals:>6} {r.bearish_signals:>6} {r.neutral_signals:>6}")

    # Find best configs
    print(f"\n{'='*120}")
    print("TOP PERFORMERS")
    print(f"{'='*120}")

    best_overall = max(results, key=lambda x: x.overall_hit_rate)
    best_pf = max(results, key=lambda x: x.profit_factor if x.profit_factor < 100 else 0)
    best_hc = max(results, key=lambda x: x.high_conviction_hit_rate)
    best_corr = max(results, key=lambda x: x.score_correlation)

    print(f"Best Overall Hit Rate: {best_overall.config_name} ({best_overall.overall_hit_rate:.1f}%)")
    print(f"Best Profit Factor:    {best_pf.config_name} (PF {best_pf.profit_factor:.2f})")
    print(f"Best High Conviction:  {best_hc.config_name} ({best_hc.high_conviction_hit_rate:.1f}%)")
    print(f"Best Score Correlation: {best_corr.config_name} ({best_corr.score_correlation:.3f})")


def main():
    parser = argparse.ArgumentParser(description="Backtest orderflow indicator combinations")
    parser.add_argument("--timeframe", "-t", default="15M",
                        choices=["5M", "15M", "1H", "4H", "1D"],
                        help="Timeframe to test")
    parser.add_argument("--limit", "-l", type=int, default=5000,
                        help="Number of bars to load")
    parser.add_argument("--forward", "-f", type=int, default=5,
                        help="Forward bars for return calculation")
    parser.add_argument("--all-timeframes", "-a", action="store_true",
                        help="Test all timeframes")

    args = parser.parse_args()

    timeframes = ["5M", "15M", "1H", "4H"] if args.all_timeframes else [args.timeframe]
    configs = generate_test_configs()

    print(f"Testing {len(configs)} configurations...")
    print(f"Timeframes: {timeframes}")
    print(f"Bars per timeframe: {args.limit}")
    print(f"Forward return period: {args.forward} bars")

    for tf in timeframes:
        print(f"\n\nLoading {tf} data...")
        df = load_data(tf, limit=args.limit)
        print(f"Loaded {len(df)} bars from {df[0, 'timestamp']} to {df[-1, 'timestamp']}")

        results = []
        for i, config in enumerate(configs):
            print(f"  Testing {config.name} ({i+1}/{len(configs)})...", end=" ", flush=True)
            try:
                result = run_backtest(df, config, tf, forward_bars=args.forward)
                results.append(result)
                print(f"Hit: {result.overall_hit_rate:.1f}%")
            except Exception as e:
                print(f"ERROR: {e}")

        print_results(results, tf)


if __name__ == "__main__":
    main()
