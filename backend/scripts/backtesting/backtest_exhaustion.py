#!/usr/bin/env python3
"""
Exhaustion Signal Backtester

Detects exhaustion patterns where high volume/activity fails to produce
proportional price movement, suggesting the current move is running out of steam.

Signal Logic:
1. High volume (vs rolling average)
2. Small price range relative to volume (exhaustion)
3. Directional bias from delta or prior move direction
4. Trade for reversal (fade the exhausted move)

Usage:
    python scripts/backtest_exhaustion.py --timeframe 5M
    python scripts/backtest_exhaustion.py --timeframe 15M --sweep
    python scripts/backtest_exhaustion.py --timeframe 1M --show-signals
"""
import os
import sys
from pathlib import Path
import argparse
import logging
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import polars as pl
from app.data.storage import DuckDBStorage

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class ExhaustionSignal:
    """Represents a detected Exhaustion signal"""
    timestamp: datetime
    direction: str  # BULLISH (exhausted selling, expect bounce) or BEARISH (exhausted buying, expect drop)
    price: float
    volume_ratio: float  # Volume vs average
    range_ratio: float  # Price range vs expected (based on volume)
    delta_direction: str  # Was the delta positive or negative
    prior_trend: str  # Was price trending up or down before exhaustion
    strength: float


@dataclass
class BacktestResult:
    """Result for a single signal"""
    signal: ExhaustionSignal
    forward_return_1: float
    forward_return_5: float
    forward_return_10: float
    hit_1: bool
    hit_5: bool
    hit_10: bool


@dataclass
class BacktestSummary:
    """Summary statistics for backtest"""
    parameters: dict
    total_signals: int
    bullish_signals: int
    bearish_signals: int
    hit_rate_1: float
    hit_rate_5: float
    hit_rate_10: float
    avg_return_1: float
    avg_return_5: float
    avg_return_10: float
    avg_win: float
    avg_loss: float
    profit_factor: float


class ExhaustionBacktester:
    """Backtester for Exhaustion signals"""

    def __init__(
        self,
        volume_mult: float = 1.5,  # Volume must exceed avg by this mult
        range_ratio_max: float = 0.5,  # Price range must be < expected * this ratio
        trend_lookback: int = 5,  # Bars to determine prior trend
        lookback_bars: int = 20,  # Bars for rolling averages
    ):
        """Initialize backtester with detection parameters

        Args:
            volume_mult: Volume must exceed rolling avg * this multiplier
            range_ratio_max: Price range must be less than this ratio of expected range
            trend_lookback: Bars to look back for determining prior trend direction
            lookback_bars: Bars for calculating rolling volume/range averages
        """
        self.volume_mult = volume_mult
        self.range_ratio_max = range_ratio_max
        self.trend_lookback = trend_lookback
        self.lookback_bars = lookback_bars

        self.db = DuckDBStorage()

    def get_parameters(self) -> dict:
        """Return current parameters as dict"""
        return {
            "volume_mult": self.volume_mult,
            "range_ratio_max": self.range_ratio_max,
            "trend_lookback": self.trend_lookback,
            "lookback_bars": self.lookback_bars,
        }

    def load_data(
        self,
        timeframe: str = "1M",
        symbol: str = "MNQ",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100000,
    ) -> pl.DataFrame:
        """Load historical data by aggregating MBP ticks into bars"""
        tf_map = {
            "1M": "1 minute",
            "5M": "5 minutes",
            "15M": "15 minutes",
            "30M": "30 minutes",
            "1H": "1 hour",
            "4H": "4 hours",
            "1D": "1 day",
        }
        interval = tf_map.get(timeframe, "1 minute")

        where_clauses = [f"symbol = '{symbol}'"]
        if start_date:
            where_clauses.append(f"timestamp >= '{start_date}'")
        if end_date:
            where_clauses.append(f"timestamp <= '{end_date}'")

        where_str = " AND ".join(where_clauses)

        # Aggregate MBP ticks into bars
        # Fix unsigned overflow: convert values > 2^31 to signed
        query = f"""
            WITH bars AS (
                SELECT
                    time_bucket(INTERVAL '{interval}', timestamp) as bar_time,
                    FIRST(mid_price) as open,
                    MAX(mid_price) as high,
                    MIN(mid_price) as low,
                    LAST(mid_price) as close,
                    COUNT(*) as volume,
                    SUM(CASE WHEN delta > 2147483647 THEN CAST(delta AS BIGINT) - 4294967296 ELSE delta END) as bar_delta,
                    AVG(dom_imbalance) as dom_imbalance
                FROM mbp_ticks
                WHERE {where_str}
                GROUP BY bar_time
                ORDER BY bar_time ASC
            )
            SELECT * FROM bars
            WHERE open IS NOT NULL
            LIMIT {limit}
        """

        df = self.db.conn.execute(query).pl()

        if "bar_time" in df.columns:
            df = df.rename({"bar_time": "timestamp"})

        logger.info(f"Loaded {len(df)} bars from MBP ticks for {symbol} {timeframe}")
        return df

    def detect_signals(self, df: pl.DataFrame) -> List[ExhaustionSignal]:
        """Detect Exhaustion signals in the data

        Exhaustion Logic:
        1. High volume (spike above average)
        2. Small price range (price didn't move much despite volume)
        3. Determine direction from delta and prior trend
        4. Signal reversal of the exhausted move
        """
        signals = []

        if len(df) < self.lookback_bars + self.trend_lookback + 2:
            logger.warning(f"Not enough data: {len(df)} bars")
            return signals

        # Calculate bar range and rolling stats
        df = df.with_columns([
            (pl.col("high") - pl.col("low")).alias("bar_range"),
            (pl.col("close") - pl.col("open")).alias("bar_body"),
        ])

        df = df.with_columns([
            pl.col("volume").rolling_mean(window_size=self.lookback_bars).alias("avg_volume"),
            pl.col("bar_range").rolling_mean(window_size=self.lookback_bars).alias("avg_range"),
            # Calculate expected range based on volume ratio
            # (if volume is 2x average, we'd expect ~2x range)
        ])

        # Calculate price change over trend_lookback for trend direction
        df = df.with_columns([
            (pl.col("close") - pl.col("close").shift(self.trend_lookback)).alias("trend_change"),
        ])

        rows = df.to_dicts()

        for i in range(self.lookback_bars + self.trend_lookback, len(rows) - 1):
            row = rows[i]

            if row["avg_volume"] is None or row["avg_volume"] == 0:
                continue
            if row["avg_range"] is None or row["avg_range"] == 0:
                continue

            # Check for volume spike
            volume_ratio = row["volume"] / row["avg_volume"]
            if volume_ratio < self.volume_mult:
                continue

            # Calculate expected range based on volume
            # Simple model: if volume is Nx average, expect ~sqrt(N)x range (volatility scales with sqrt)
            import math
            expected_range = row["avg_range"] * math.sqrt(volume_ratio)
            actual_range = row["bar_range"]

            range_ratio = actual_range / expected_range if expected_range > 0 else 1.0

            # Check for exhaustion (range is small relative to what volume suggests)
            if range_ratio > self.range_ratio_max:
                continue

            # Determine direction based on delta and prior trend
            delta = row["bar_delta"] if row["bar_delta"] is not None else 0
            trend_change = row["trend_change"] if row["trend_change"] is not None else 0

            # Delta direction
            delta_direction = "POSITIVE" if delta > 0 else "NEGATIVE"

            # Prior trend direction
            prior_trend = "UP" if trend_change > 0 else "DOWN"

            # Signal direction: fade the exhausted move
            # If buying exhaustion (high volume, positive delta, but range small) -> BEARISH
            # If selling exhaustion (high volume, negative delta, but range small) -> BULLISH
            if delta > 0 or prior_trend == "UP":
                direction = "BEARISH"  # Exhausted buying, expect reversal down
            else:
                direction = "BULLISH"  # Exhausted selling, expect reversal up

            strength = min(1.0, volume_ratio / (self.volume_mult * 2) * (1 - range_ratio))

            signals.append(ExhaustionSignal(
                timestamp=row["timestamp"],
                direction=direction,
                price=row["close"],
                volume_ratio=volume_ratio,
                range_ratio=range_ratio,
                delta_direction=delta_direction,
                prior_trend=prior_trend,
                strength=strength,
            ))

        logger.info(f"Detected {len(signals)} Exhaustion signals")
        return signals

    def calculate_forward_returns(
        self,
        df: pl.DataFrame,
        signals: List[ExhaustionSignal]
    ) -> List[BacktestResult]:
        """Calculate forward returns for each signal"""
        results = []

        rows = df.to_dicts()
        timestamps = [r["timestamp"] for r in rows]

        for signal in signals:
            try:
                idx = timestamps.index(signal.timestamp)
            except ValueError:
                continue

            # Get forward prices
            price_1 = rows[idx + 1]["close"] if idx + 1 < len(rows) else None
            price_5 = rows[idx + 5]["close"] if idx + 5 < len(rows) else None
            price_10 = rows[idx + 10]["close"] if idx + 10 < len(rows) else None

            if price_1 is None:
                continue

            # Calculate returns
            ret_1 = (price_1 - signal.price) / signal.price if price_1 else 0
            ret_5 = (price_5 - signal.price) / signal.price if price_5 else 0
            ret_10 = (price_10 - signal.price) / signal.price if price_10 else 0

            # Adjust for direction
            if signal.direction == "BEARISH":
                ret_1, ret_5, ret_10 = -ret_1, -ret_5, -ret_10

            results.append(BacktestResult(
                signal=signal,
                forward_return_1=ret_1,
                forward_return_5=ret_5,
                forward_return_10=ret_10,
                hit_1=ret_1 > 0,
                hit_5=ret_5 > 0,
                hit_10=ret_10 > 0,
            ))

        return results

    def calculate_summary(self, results: List[BacktestResult]) -> BacktestSummary:
        """Calculate summary statistics"""
        if not results:
            return BacktestSummary(
                parameters=self.get_parameters(),
                total_signals=0,
                bullish_signals=0,
                bearish_signals=0,
                hit_rate_1=0, hit_rate_5=0, hit_rate_10=0,
                avg_return_1=0, avg_return_5=0, avg_return_10=0,
                avg_win=0, avg_loss=0, profit_factor=0,
            )

        bullish = sum(1 for r in results if r.signal.direction == "BULLISH")
        bearish = sum(1 for r in results if r.signal.direction == "BEARISH")

        hit_1 = sum(1 for r in results if r.hit_1) / len(results) * 100
        hit_5 = sum(1 for r in results if r.hit_5) / len(results) * 100
        hit_10 = sum(1 for r in results if r.hit_10) / len(results) * 100

        avg_ret_1 = sum(r.forward_return_1 for r in results) / len(results) * 100
        avg_ret_5 = sum(r.forward_return_5 for r in results) / len(results) * 100
        avg_ret_10 = sum(r.forward_return_10 for r in results) / len(results) * 100

        wins = [r.forward_return_5 for r in results if r.forward_return_5 > 0]
        losses = [r.forward_return_5 for r in results if r.forward_return_5 < 0]

        avg_win = sum(wins) / len(wins) * 100 if wins else 0
        avg_loss = sum(losses) / len(losses) * 100 if losses else 0

        total_wins = sum(wins) if wins else 0
        total_losses = abs(sum(losses)) if losses else 0
        profit_factor = total_wins / total_losses if total_losses > 0 else 0

        return BacktestSummary(
            parameters=self.get_parameters(),
            total_signals=len(results),
            bullish_signals=bullish,
            bearish_signals=bearish,
            hit_rate_1=hit_1,
            hit_rate_5=hit_5,
            hit_rate_10=hit_10,
            avg_return_1=avg_ret_1,
            avg_return_5=avg_ret_5,
            avg_return_10=avg_ret_10,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
        )

    def run_parameter_sweep(
        self,
        timeframe: str = "1M",
        symbol: str = "MNQ",
        limit: int = 50000,
    ) -> List[BacktestSummary]:
        """Run parameter sweep to find optimal settings"""
        df = self.load_data(timeframe=timeframe, symbol=symbol, limit=limit)

        if len(df) == 0:
            logger.error("No data loaded")
            return []

        # Parameter ranges to test
        volume_mults = [1.3, 1.5, 1.8, 2.0, 2.5]
        range_ratios = [0.3, 0.4, 0.5, 0.6, 0.7]
        trend_lookbacks = [3, 5, 10]
        lookbacks = [15, 20, 30]

        results = []
        total_combos = len(volume_mults) * len(range_ratios) * len(trend_lookbacks) * len(lookbacks)

        logger.info(f"Testing {total_combos} parameter combinations...")

        combo_count = 0
        for vol in volume_mults:
            for rng in range_ratios:
                for trend in trend_lookbacks:
                    for lb in lookbacks:
                        combo_count += 1
                        self.volume_mult = vol
                        self.range_ratio_max = rng
                        self.trend_lookback = trend
                        self.lookback_bars = lb

                        signals = self.detect_signals(df.clone())
                        if len(signals) < 10:
                            continue

                        bt_results = self.calculate_forward_returns(df, signals)
                        if len(bt_results) < 10:
                            continue

                        summary = self.calculate_summary(bt_results)
                        if summary.hit_rate_5 > 50:
                            results.append(summary)

                        if combo_count % 50 == 0:
                            logger.info(f"Progress: {combo_count}/{total_combos}")

        results.sort(key=lambda x: (x.hit_rate_5, x.profit_factor), reverse=True)
        return results


def print_summary(summary: BacktestSummary):
    """Pretty print backtest summary"""
    print("\n" + "=" * 60)
    print("EXHAUSTION BACKTEST RESULTS")
    print("=" * 60)
    print(f"\nParameters:")
    for k, v in summary.parameters.items():
        print(f"  {k}: {v}")

    print(f"\nSignals Detected: {summary.total_signals}")
    print(f"  Bullish: {summary.bullish_signals}")
    print(f"  Bearish: {summary.bearish_signals}")

    print(f"\nHit Rates (% signals where price moved in predicted direction):")
    print(f"  1-bar:  {summary.hit_rate_1:.1f}%")
    print(f"  5-bar:  {summary.hit_rate_5:.1f}%")
    print(f"  10-bar: {summary.hit_rate_10:.1f}%")

    print(f"\nAverage Returns (direction-adjusted):")
    print(f"  1-bar:  {summary.avg_return_1:.4f}%")
    print(f"  5-bar:  {summary.avg_return_5:.4f}%")
    print(f"  10-bar: {summary.avg_return_10:.4f}%")

    print(f"\nWin/Loss Analysis (5-bar horizon):")
    print(f"  Avg Win:  {summary.avg_win:.4f}%")
    print(f"  Avg Loss: {summary.avg_loss:.4f}%")
    print(f"  Profit Factor: {summary.profit_factor:.2f}")

    if summary.hit_rate_5 > 55:
        print(f"\nInterpretation:")
        print(f"  Signal has predictive value (hit rate > 55%)")
        if summary.profit_factor > 1.5:
            print(f"  Strong edge (profit factor > 1.5)")
        elif summary.profit_factor > 1.0:
            print(f"  Modest edge (profit factor > 1.0)")
    elif summary.hit_rate_5 > 50:
        print(f"\nInterpretation:")
        print(f"  Signal is marginally predictive (hit rate 50-55%)")
    else:
        print(f"\nInterpretation:")
        print(f"  Signal appears random (hit rate <= 50%)")

    print("=" * 60)


def print_signals(results: List[BacktestResult], limit: int = 20):
    """Print individual signals for review"""
    print("\n" + "=" * 110)
    print("SIGNAL DETAILS")
    print("=" * 110)
    print(f"\n{'Timestamp':<20} {'Dir':>8} {'Price':>10} {'VolRatio':>10} {'RngRatio':>10} {'Trend':>6} {'Ret 5bar':>10} {'Hit?':>6}")
    print("-" * 110)

    for result in results[:limit]:
        s = result.signal
        hit = "Yes" if result.hit_5 else "No"
        ts_str = s.timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(s.timestamp, "strftime") else str(s.timestamp)[:16]

        print(f"{ts_str:<20} {s.direction:>8} {s.price:>10.2f} {s.volume_ratio:>10.2f} "
              f"{s.range_ratio:>10.2f} {s.prior_trend:>6} {result.forward_return_5*100:>10.4f}% {hit:>6}")

    print("-" * 110)
    print(f"Showing {min(limit, len(results))} of {len(results)} signals")


def main():
    parser = argparse.ArgumentParser(description="Backtest Exhaustion signal detection")
    parser.add_argument("--timeframe", "-t", default="5M", help="Bar timeframe")
    parser.add_argument("--symbol", "-s", default="MNQ", help="Trading symbol")
    parser.add_argument("--limit", "-l", type=int, default=50000, help="Max bars")
    parser.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    parser.add_argument("--show-signals", action="store_true", help="Show individual signals")

    # Detection parameters
    parser.add_argument("--volume-mult", type=float, default=1.5, help="Volume spike multiplier")
    parser.add_argument("--range-ratio", type=float, default=0.5, help="Max range ratio for exhaustion")
    parser.add_argument("--trend-lookback", type=int, default=5, help="Bars for trend direction")
    parser.add_argument("--lookback", type=int, default=20, help="Bars for rolling averages")

    args = parser.parse_args()

    if args.sweep:
        print("\nRunning Exhaustion parameter sweep...")
        backtester = ExhaustionBacktester()
        results = backtester.run_parameter_sweep(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
        )

        if not results:
            print("No valid parameter combinations found (all hit rates <= 50%)")
        else:
            print(f"\nTop 10 parameter combinations (by 5-bar hit rate):\n")
            print(f"{'VolMult':>8} {'RngMax':>8} {'Trend':>6} {'LB':>6} {'Sigs':>6} {'Hit5%':>8} {'PF':>8}")
            print("-" * 60)
            for r in results[:10]:
                p = r.parameters
                print(f"{p['volume_mult']:>8.1f} {p['range_ratio_max']:>8.2f} "
                      f"{p['trend_lookback']:>6} {p['lookback_bars']:>6} "
                      f"{r.total_signals:>6} {r.hit_rate_5:>8.1f} {r.profit_factor:>8.2f}")

            print("\n" + "=" * 60)
            print("Best parameters:")
            print_summary(results[0])

    else:
        backtester = ExhaustionBacktester(
            volume_mult=args.volume_mult,
            range_ratio_max=args.range_ratio,
            trend_lookback=args.trend_lookback,
            lookback_bars=args.lookback,
        )

        df = backtester.load_data(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
        )

        signals = backtester.detect_signals(df)
        results = backtester.calculate_forward_returns(df, signals)
        summary = backtester.calculate_summary(results)

        print_summary(summary)

        if args.show_signals and results:
            print_signals(results)


if __name__ == "__main__":
    main()
