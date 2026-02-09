#!/usr/bin/env python3
"""
Edge Improvement Explorer

Tests various combinations and approaches to improve predictive edge:
1. Component weight variations (Trend/Intensity/Orderflow)
2. Multi-timeframe signal confirmation
3. Time-of-day filters
4. Signal combination strategies
5. Zone + Signal confirmation

Usage:
    python scripts/backtesting/explore_edge_improvements.py
    python scripts/backtesting/explore_edge_improvements.py --test weights
    python scripts/backtesting/explore_edge_improvements.py --test mtf
    python scripts/backtesting/explore_edge_improvements.py --test tod
"""
import sys
from pathlib import Path
import argparse
import logging
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import polars as pl
from app.data.storage import DuckDBStorage
from app.features.agent_bias import AgentBiasCalculator, AgentMode
from app.features.orderflow_signals import OrderflowSignalDetector
from app.features.zone_bias import ZoneBiasScorer, ZoneType

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class EdgeTestResult:
    """Result of an edge test"""
    name: str
    total_signals: int
    hit_rate_5: float
    hit_rate_10: float
    avg_return_5: float
    profit_factor: float
    details: str = ""


def load_data(timeframe: str, symbol: str = "MNQ", limit: int = 10000) -> pl.DataFrame:
    """Load historical data"""
    db = DuckDBStorage()
    query = f"""
        SELECT *
        FROM ohlcv_ticks
        WHERE symbol = '{symbol}' AND timeframe = '{timeframe}'
          AND volume > 100
        ORDER BY timestamp DESC
        LIMIT {limit}
    """
    with db as storage:
        df = storage.conn.execute(query).pl()
    return df.reverse() if len(df) > 0 else df


def calculate_forward_returns(rows: List[Dict], idx: int, direction: str) -> Dict:
    """Calculate forward returns at index (rows = pre-converted list of dicts)"""
    if idx + 10 >= len(rows):
        return None

    entry = rows[idx]["close"]
    ret_5 = (rows[idx + 5]["close"] - entry) / entry
    ret_10 = (rows[idx + 10]["close"] - entry) / entry

    if direction == "BEARISH":
        ret_5, ret_10 = -ret_5, -ret_10

    return {
        "ret_5": ret_5,
        "ret_10": ret_10,
        "hit_5": ret_5 > 0,
        "hit_10": ret_10 > 0,
    }


def summarize_results(results: List[Dict], name: str) -> EdgeTestResult:
    """Summarize test results"""
    if not results:
        return EdgeTestResult(name=name, total_signals=0, hit_rate_5=0, hit_rate_10=0,
                             avg_return_5=0, profit_factor=0)

    total = len(results)
    hit_5 = sum(1 for r in results if r["hit_5"]) / total
    hit_10 = sum(1 for r in results if r["hit_10"]) / total
    avg_ret = sum(r["ret_5"] for r in results) / total

    wins = [r["ret_5"] for r in results if r["ret_5"] > 0]
    losses = [abs(r["ret_5"]) for r in results if r["ret_5"] < 0]
    pf = sum(wins) / max(sum(losses), 0.0001)

    return EdgeTestResult(
        name=name,
        total_signals=total,
        hit_rate_5=hit_5,
        hit_rate_10=hit_10,
        avg_return_5=avg_ret,
        profit_factor=pf,
    )


# =============================================================================
# TEST 1: Component Weight Variations
# =============================================================================

def test_weight_combinations(df: pl.DataFrame, timeframe: str) -> List[EdgeTestResult]:
    """Test different component weight combinations"""
    results = []

    # Weight combinations to test: (trend, intensity, orderflow)
    weight_combos = [
        (20, 20, 60),   # Current default
        (20, 40, 40),   # Boost intensity
        (40, 20, 40),   # Boost trend
        (10, 30, 60),   # Lower trend, boost intensity
        (30, 30, 40),   # Balanced trend/intensity
        (10, 50, 40),   # High intensity focus
        (20, 60, 20),   # Intensity dominant
        (40, 40, 20),   # Trend+Intensity, minimal orderflow
    ]

    # Pre-compute all indicators ONCE using Polars (vectorized, fast)
    df_with_indicators = df.with_columns([
        pl.col("close").ewm_mean(span=12).alias("ema12"),
        pl.col("close").ewm_mean(span=25).alias("ema25"),
        pl.col("volume").rolling_mean(window_size=20).alias("vol_avg"),
    ])
    rows = df_with_indicators.to_dicts()

    for trend_w, intensity_w, of_w in weight_combos:
        name = f"T{trend_w}/I{intensity_w}/O{of_w}"
        test_results = []

        for i in range(50, len(rows) - 11):
            row = rows[i]

            # Trend score (EMA based)
            if row.get("ema12") and row.get("ema25"):
                trend_score = 70 if row["ema12"] > row["ema25"] else 30
            else:
                trend_score = 50

            # Intensity score (RVOL based) - using pre-computed rolling avg
            vol_avg = row.get("vol_avg", 1) or 1
            rvol = row["volume"] / max(vol_avg, 1)
            intensity_score = min(90, 50 + rvol * 20) if rvol > 1 else max(10, 50 - (1-rvol) * 20)

            # Orderflow score (DOM based)
            dom = row.get("dom_imbalance", 0.5)
            if dom and dom == dom:  # Not NaN
                of_score = 50 + (dom - 0.5) * 80
            else:
                of_score = 50

            # Combined score
            total = (trend_score * trend_w + intensity_score * intensity_w + of_score * of_w) / 100
            direction = "BULLISH" if total > 55 else "BEARISH" if total < 45 else "NEUTRAL"

            if direction != "NEUTRAL":
                fwd = calculate_forward_returns(rows, i, direction)
                if fwd:
                    test_results.append(fwd)

        results.append(summarize_results(test_results, name))

    return results


# =============================================================================
# TEST 2: Multi-Timeframe Confirmation
# =============================================================================

def test_mtf_confirmation() -> List[EdgeTestResult]:
    """Test multi-timeframe signal confirmation"""
    results = []

    # Load multiple timeframes
    df_5m = load_data("5M", limit=5000)
    df_15m = load_data("15M", limit=2000)
    df_1h = load_data("1H", limit=500)

    if len(df_5m) < 100 or len(df_15m) < 50:
        logger.warning("Insufficient data for MTF test")
        return results

    # Create timestamp lookup for alignment
    ts_to_15m = {row["timestamp"]: row for row in df_15m.to_dicts()}
    ts_to_1h = {row["timestamp"]: row for row in df_1h.to_dicts()}

    rows_5m = df_5m.to_dicts()

    # Test: 5M signal confirmed by 15M direction
    test_results_confirmed = []
    test_results_unconfirmed = []

    for i in range(50, len(rows_5m) - 11):
        row_5m = rows_5m[i]
        ts = row_5m["timestamp"]

        # Get 5M DOM direction
        dom_5m = row_5m.get("dom_imbalance", 0.5)
        if dom_5m is None or dom_5m != dom_5m:
            continue
        dir_5m = "BULLISH" if dom_5m > 0.55 else "BEARISH" if dom_5m < 0.45 else "NEUTRAL"
        if dir_5m == "NEUTRAL":
            continue

        # Find closest 15M bar
        closest_15m_ts = None
        min_diff = float('inf')
        for ts_15m in ts_to_15m:
            diff = abs((ts - ts_15m).total_seconds())
            if diff < min_diff:
                min_diff = diff
                closest_15m_ts = ts_15m

        if closest_15m_ts and min_diff < 900:  # Within 15 minutes
            row_15m = ts_to_15m[closest_15m_ts]
            dom_15m = row_15m.get("dom_imbalance", 0.5)
            if dom_15m and dom_15m == dom_15m:
                dir_15m = "BULLISH" if dom_15m > 0.55 else "BEARISH" if dom_15m < 0.45 else "NEUTRAL"

                fwd = calculate_forward_returns(rows_5m, i, dir_5m)
                if fwd:
                    if dir_5m == dir_15m:
                        test_results_confirmed.append(fwd)
                    else:
                        test_results_unconfirmed.append(fwd)

    results.append(summarize_results(test_results_confirmed, "5M+15M Aligned"))
    results.append(summarize_results(test_results_unconfirmed, "5M+15M Conflict"))

    return results


# =============================================================================
# TEST 3: Time-of-Day Analysis
# =============================================================================

def test_time_of_day(df: pl.DataFrame, timeframe: str) -> List[EdgeTestResult]:
    """Test performance by time of day"""
    results = []
    rows = df.to_dicts()

    # Define time buckets (ET hours)
    time_buckets = {
        "Pre-market (4-9:30)": (4, 9),
        "Morning (9:30-12)": (9, 12),
        "Midday (12-14)": (12, 14),
        "Afternoon (14-16)": (14, 16),
        "After-hours (16-20)": (16, 20),
    }

    for bucket_name, (start_hour, end_hour) in time_buckets.items():
        test_results = []

        for i in range(50, len(rows) - 11):
            row = rows[i]
            ts = row["timestamp"]

            # Extract hour (assuming ET timezone)
            hour = ts.hour if hasattr(ts, 'hour') else 12

            if start_hour <= hour < end_hour:
                dom = row.get("dom_imbalance", 0.5)
                if dom and dom == dom:
                    direction = "BULLISH" if dom > 0.55 else "BEARISH" if dom < 0.45 else "NEUTRAL"
                    if direction != "NEUTRAL":
                        fwd = calculate_forward_returns(rows, i, direction)
                        if fwd:
                            test_results.append(fwd)

        results.append(summarize_results(test_results, bucket_name))

    return results


# =============================================================================
# TEST 4: Signal Combinations
# =============================================================================

def test_signal_combinations(df: pl.DataFrame, timeframe: str) -> List[EdgeTestResult]:
    """Test different primary signal combinations"""
    results = []

    detector = OrderflowSignalDetector(timeframe=timeframe, lookback_bars=20)

    # Detect all signals
    try:
        abs_signals = detector.detect_absorption(df)
        exh_signals = detector.detect_exhaustion(df)
    except Exception as e:
        logger.warning(f"Signal detection failed: {e}")
        return results

    # Create timestamp to signal mapping
    abs_by_ts = defaultdict(list)
    exh_by_ts = defaultdict(list)

    for sig in abs_signals:
        abs_by_ts[sig.timestamp].append(sig)
    for sig in exh_signals:
        exh_by_ts[sig.timestamp].append(sig)

    rows = df.to_dicts()

    # Test: Absorption only
    test_abs = []
    for i, row in enumerate(rows[:-11]):
        ts = row["timestamp"]
        if ts in abs_by_ts:
            sig = abs_by_ts[ts][0]
            direction = sig.direction.value if hasattr(sig.direction, 'value') else sig.direction
            fwd = calculate_forward_returns(rows, i, direction)
            if fwd:
                test_abs.append(fwd)
    results.append(summarize_results(test_abs, "Absorption Only"))

    # Test: Exhaustion only
    test_exh = []
    for i, row in enumerate(rows[:-11]):
        ts = row["timestamp"]
        if ts in exh_by_ts:
            sig = exh_by_ts[ts][0]
            direction = sig.direction.value if hasattr(sig.direction, 'value') else sig.direction
            fwd = calculate_forward_returns(rows, i, direction)
            if fwd:
                test_exh.append(fwd)
    results.append(summarize_results(test_exh, "Exhaustion Only"))

    # Test: Absorption + Exhaustion aligned
    test_both = []
    for i, row in enumerate(rows[:-11]):
        ts = row["timestamp"]
        if ts in abs_by_ts and ts in exh_by_ts:
            abs_dir = abs_by_ts[ts][0].direction.value if hasattr(abs_by_ts[ts][0].direction, 'value') else abs_by_ts[ts][0].direction
            exh_dir = exh_by_ts[ts][0].direction.value if hasattr(exh_by_ts[ts][0].direction, 'value') else exh_by_ts[ts][0].direction
            if abs_dir == exh_dir:
                fwd = calculate_forward_returns(rows, i, abs_dir)
                if fwd:
                    test_both.append(fwd)
    results.append(summarize_results(test_both, "ABS+EXH Aligned"))

    # Test: High DOM + Signal
    test_dom_sig = []
    for i, row in enumerate(rows[:-11]):
        ts = row["timestamp"]
        dom = row.get("dom_imbalance", 0.5)
        if dom and dom == dom and (dom > 0.60 or dom < 0.40):
            dom_dir = "BULLISH" if dom > 0.60 else "BEARISH"
            # Check if we have a confirming signal
            has_confirm = False
            if ts in abs_by_ts:
                sig_dir = abs_by_ts[ts][0].direction.value if hasattr(abs_by_ts[ts][0].direction, 'value') else abs_by_ts[ts][0].direction
                if sig_dir == dom_dir:
                    has_confirm = True
            if ts in exh_by_ts:
                sig_dir = exh_by_ts[ts][0].direction.value if hasattr(exh_by_ts[ts][0].direction, 'value') else exh_by_ts[ts][0].direction
                if sig_dir == dom_dir:
                    has_confirm = True

            if has_confirm:
                fwd = calculate_forward_returns(rows, i, dom_dir)
                if fwd:
                    test_dom_sig.append(fwd)
    results.append(summarize_results(test_dom_sig, "DOM>60% + Signal"))

    return results


# =============================================================================
# TEST 5: Zone + Signal Confirmation
# =============================================================================

def test_zone_signal(df: pl.DataFrame, timeframe: str) -> List[EdgeTestResult]:
    """Test zone entries with signal confirmation"""
    results = []

    scorer = ZoneBiasScorer()
    detector = OrderflowSignalDetector(timeframe=timeframe, lookback_bars=20)

    # Detect zones
    zones = scorer.detect_active_zones(df, timeframe, len(df) - 1)
    logger.info(f"Detected {len(zones)} zones for zone+signal test")

    if len(zones) == 0:
        return results

    # Detect signals
    try:
        abs_signals = detector.detect_absorption(df)
        exh_signals = detector.detect_exhaustion(df)
    except Exception as e:
        logger.warning(f"Signal detection failed: {e}")
        return results

    abs_by_ts = defaultdict(list)
    exh_by_ts = defaultdict(list)
    for sig in abs_signals:
        abs_by_ts[sig.timestamp].append(sig)
    for sig in exh_signals:
        exh_by_ts[sig.timestamp].append(sig)

    rows = df.to_dicts()

    # Test: At zone + signal
    test_zone_sig = []
    test_zone_no_sig = []

    for i, row in enumerate(rows[:-11]):
        ts = row["timestamp"]
        price = row["close"]

        # Check if at a zone
        at_zone = None
        for zone in zones:
            if zone.price_low <= price <= zone.price_high:
                at_zone = zone
                break

        if at_zone:
            expected_dir = "BULLISH" if at_zone.zone_type == ZoneType.DEMAND else "BEARISH"

            # Check for confirming signal
            has_signal = False
            if ts in abs_by_ts:
                sig_dir = abs_by_ts[ts][0].direction.value if hasattr(abs_by_ts[ts][0].direction, 'value') else abs_by_ts[ts][0].direction
                if sig_dir == expected_dir:
                    has_signal = True
            if ts in exh_by_ts:
                sig_dir = exh_by_ts[ts][0].direction.value if hasattr(exh_by_ts[ts][0].direction, 'value') else exh_by_ts[ts][0].direction
                if sig_dir == expected_dir:
                    has_signal = True

            fwd = calculate_forward_returns(rows, i, expected_dir)
            if fwd:
                if has_signal:
                    test_zone_sig.append(fwd)
                else:
                    test_zone_no_sig.append(fwd)

    results.append(summarize_results(test_zone_sig, "Zone + Signal"))
    results.append(summarize_results(test_zone_no_sig, "Zone (no signal)"))

    return results


def print_results(results: List[EdgeTestResult], title: str):
    """Print formatted results"""
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")
    print(f"{'Name':<25} {'Signals':>8} {'Hit5%':>8} {'Hit10%':>8} {'AvgRet':>10} {'PF':>8}")
    print("-" * 80)

    for r in sorted(results, key=lambda x: x.profit_factor, reverse=True):
        if r.total_signals > 0:
            print(f"{r.name:<25} {r.total_signals:>8} {r.hit_rate_5:>7.1%} {r.hit_rate_10:>7.1%} "
                  f"{r.avg_return_5*100:>9.3f}% {r.profit_factor:>7.2f}")

    # Find best
    if results:
        valid = [r for r in results if r.total_signals >= 10]
        if valid:
            best_hit = max(valid, key=lambda x: x.hit_rate_5)
            best_pf = max(valid, key=lambda x: x.profit_factor)
            print("-" * 80)
            print(f"Best Hit Rate: {best_hit.name} ({best_hit.hit_rate_5:.1%})")
            print(f"Best Profit Factor: {best_pf.name} ({best_pf.profit_factor:.2f})")


def main():
    parser = argparse.ArgumentParser(description='Explore edge improvements')
    parser.add_argument('--test', choices=['weights', 'mtf', 'tod', 'signals', 'zones', 'all'],
                        default='all', help='Which test to run')
    parser.add_argument('--timeframe', '-t', default='15M', help='Timeframe to test')
    parser.add_argument('--limit', '-l', type=int, default=10000, help='Data limit')

    args = parser.parse_args()

    print(f"\n{'#'*80}")
    print(f"#  EDGE IMPROVEMENT EXPLORER - {args.timeframe}")
    print(f"{'#'*80}")

    df = load_data(args.timeframe, limit=args.limit)
    logger.info(f"Loaded {len(df)} bars for {args.timeframe}")

    if len(df) < 100:
        logger.error("Insufficient data")
        return

    if args.test in ['weights', 'all']:
        results = test_weight_combinations(df, args.timeframe)
        print_results(results, "COMPONENT WEIGHT VARIATIONS")

    if args.test in ['mtf', 'all']:
        results = test_mtf_confirmation()
        print_results(results, "MULTI-TIMEFRAME CONFIRMATION")

    if args.test in ['tod', 'all']:
        results = test_time_of_day(df, args.timeframe)
        print_results(results, "TIME-OF-DAY ANALYSIS")

    if args.test in ['signals', 'all']:
        results = test_signal_combinations(df, args.timeframe)
        print_results(results, "SIGNAL COMBINATIONS")

    if args.test in ['zones', 'all']:
        results = test_zone_signal(df, args.timeframe)
        print_results(results, "ZONE + SIGNAL CONFIRMATION")

    print(f"\n{'#'*80}")
    print("#  EXPLORATION COMPLETE")
    print(f"{'#'*80}\n")


if __name__ == '__main__':
    main()
