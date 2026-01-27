#!/usr/bin/env python3
"""
Delta Unwind Signal Backtester

Detects when cumulative delta reaches an extreme and starts to reverse (unwind).
The idea is that accumulated buying/selling pressure eventually unwinds as positions
are closed, creating mean-reversion opportunities.

Signal Logic:
1. Cumulative delta reaches extreme (vs rolling std dev)
2. Delta starts reversing direction (unwind begins)
3. Trade in direction of unwind (fade the prior move)

Usage:
    python scripts/backtest_delta_unwind.py --timeframe 5M
    python scripts/backtest_delta_unwind.py --timeframe 15M --sweep
    python scripts/backtest_delta_unwind.py --timeframe 1M --show-signals
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
class DeltaUnwindSignal:
    """Represents a detected Delta Unwind signal"""
    timestamp: datetime
    direction: str  # BULLISH (delta was negative, now unwinding up) or BEARISH (delta was positive, now unwinding down)
    price: float
    cumulative_delta: float  # The cumulative delta at signal time
    delta_zscore: float  # How extreme the delta was (z-score)
    unwind_pct: float  # Percentage of delta that has unwound
    strength: float


@dataclass
class BacktestResult:
    """Result for a single signal"""
    signal: DeltaUnwindSignal
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


class DeltaUnwindBacktester:
    """Backtester for Delta Unwind signals"""

    def __init__(
        self,
        zscore_threshold: float = 2.0,  # Z-score threshold for "extreme" delta
        unwind_pct: float = 0.1,  # Min % of delta that must unwind
        unwind_bars: int = 3,  # Bars to look for unwind confirmation
        lookback_bars: int = 50,  # Bars for rolling stats
    ):
        """Initialize backtester with detection parameters

        Args:
            zscore_threshold: Cumulative delta must exceed this z-score
            unwind_pct: Minimum % of peak delta that must unwind
            unwind_bars: Bars to confirm unwind is happening
            lookback_bars: Bars for calculating rolling mean/std
        """
        self.zscore_threshold = zscore_threshold
        self.unwind_pct = unwind_pct
        self.unwind_bars = unwind_bars
        self.lookback_bars = lookback_bars

        self.db = DuckDBStorage()

    def get_parameters(self) -> dict:
        """Return current parameters as dict"""
        return {
            "zscore_threshold": self.zscore_threshold,
            "unwind_pct": self.unwind_pct,
            "unwind_bars": self.unwind_bars,
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

    def detect_signals(self, df: pl.DataFrame) -> List[DeltaUnwindSignal]:
        """Detect Delta Unwind signals in the data

        Delta Unwind Logic:
        1. Calculate cumulative delta
        2. Find when cumulative delta reaches extreme (high z-score)
        3. Detect when delta starts unwinding (reversing)
        4. Signal in direction of unwind
        """
        signals = []

        if len(df) < self.lookback_bars + self.unwind_bars + 2:
            logger.warning(f"Not enough data: {len(df)} bars")
            return signals

        # Calculate cumulative delta and rolling stats
        df = df.with_columns([
            pl.col("bar_delta").cum_sum().alias("cum_delta"),
        ])

        df = df.with_columns([
            pl.col("cum_delta").rolling_mean(window_size=self.lookback_bars).alias("delta_mean"),
            pl.col("cum_delta").rolling_std(window_size=self.lookback_bars).alias("delta_std"),
        ])

        # Calculate z-score
        df = df.with_columns([
            ((pl.col("cum_delta") - pl.col("delta_mean")) / pl.col("delta_std")).alias("delta_zscore"),
        ])

        rows = df.to_dicts()

        for i in range(self.lookback_bars, len(rows) - self.unwind_bars - 1):
            row = rows[i]

            if row["delta_std"] is None or row["delta_std"] == 0:
                continue
            if row["delta_zscore"] is None:
                continue

            zscore = row["delta_zscore"]
            cum_delta = row["cum_delta"]

            # Check for extreme positive delta (potential bearish unwind)
            if zscore > self.zscore_threshold and cum_delta > 0:
                # Look for unwind (delta decreasing)
                peak_delta = cum_delta
                for j in range(1, self.unwind_bars + 1):
                    future_row = rows[i + j]
                    future_delta = future_row["cum_delta"]

                    if future_delta is None:
                        continue

                    unwind_amount = peak_delta - future_delta
                    unwind_pct = unwind_amount / abs(peak_delta) if peak_delta != 0 else 0

                    if unwind_pct > self.unwind_pct:
                        strength = min(1.0, abs(zscore) / (self.zscore_threshold * 2))
                        signals.append(DeltaUnwindSignal(
                            timestamp=future_row["timestamp"],
                            direction="BEARISH",  # Delta was positive, now unwinding down
                            price=future_row["close"],
                            cumulative_delta=future_delta,
                            delta_zscore=zscore,
                            unwind_pct=unwind_pct,
                            strength=strength,
                        ))
                        break

            # Check for extreme negative delta (potential bullish unwind)
            elif zscore < -self.zscore_threshold and cum_delta < 0:
                # Look for unwind (delta increasing / becoming less negative)
                trough_delta = cum_delta
                for j in range(1, self.unwind_bars + 1):
                    future_row = rows[i + j]
                    future_delta = future_row["cum_delta"]

                    if future_delta is None:
                        continue

                    unwind_amount = future_delta - trough_delta  # positive if unwinding
                    unwind_pct = unwind_amount / abs(trough_delta) if trough_delta != 0 else 0

                    if unwind_pct > self.unwind_pct:
                        strength = min(1.0, abs(zscore) / (self.zscore_threshold * 2))
                        signals.append(DeltaUnwindSignal(
                            timestamp=future_row["timestamp"],
                            direction="BULLISH",  # Delta was negative, now unwinding up
                            price=future_row["close"],
                            cumulative_delta=future_delta,
                            delta_zscore=zscore,
                            unwind_pct=unwind_pct,
                            strength=strength,
                        ))
                        break

        logger.info(f"Detected {len(signals)} Delta Unwind signals")
        return signals

    def calculate_forward_returns(
        self,
        df: pl.DataFrame,
        signals: List[DeltaUnwindSignal]
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
        zscore_thresholds = [1.5, 2.0, 2.5, 3.0]
        unwind_pcts = [0.05, 0.10, 0.15, 0.20, 0.30]
        unwind_bars_list = [2, 3, 5, 8]
        lookbacks = [30, 50, 100]

        results = []
        total_combos = len(zscore_thresholds) * len(unwind_pcts) * len(unwind_bars_list) * len(lookbacks)

        logger.info(f"Testing {total_combos} parameter combinations...")

        combo_count = 0
        for zscore in zscore_thresholds:
            for unwind in unwind_pcts:
                for bars in unwind_bars_list:
                    for lb in lookbacks:
                        combo_count += 1
                        self.zscore_threshold = zscore
                        self.unwind_pct = unwind
                        self.unwind_bars = bars
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
    print("DELTA UNWIND BACKTEST RESULTS")
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
    print("\n" + "=" * 100)
    print("SIGNAL DETAILS")
    print("=" * 100)
    print(f"\n{'Timestamp':<20} {'Dir':>8} {'Price':>10} {'CumDelta':>12} {'Z-Score':>8} {'Unwind%':>8} {'Ret 5bar':>10} {'Hit?':>6}")
    print("-" * 100)

    for result in results[:limit]:
        s = result.signal
        hit = "Yes" if result.hit_5 else "No"
        ts_str = s.timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(s.timestamp, "strftime") else str(s.timestamp)[:16]

        print(f"{ts_str:<20} {s.direction:>8} {s.price:>10.2f} {s.cumulative_delta:>12.0f} "
              f"{s.delta_zscore:>8.2f} {s.unwind_pct*100:>8.1f} {result.forward_return_5*100:>10.4f}% {hit:>6}")

    print("-" * 100)
    print(f"Showing {min(limit, len(results))} of {len(results)} signals")


def main():
    parser = argparse.ArgumentParser(description="Backtest Delta Unwind signal detection")
    parser.add_argument("--timeframe", "-t", default="5M", help="Bar timeframe")
    parser.add_argument("--symbol", "-s", default="MNQ", help="Trading symbol")
    parser.add_argument("--limit", "-l", type=int, default=50000, help="Max bars")
    parser.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    parser.add_argument("--show-signals", action="store_true", help="Show individual signals")

    # Detection parameters
    parser.add_argument("--zscore", type=float, default=2.0, help="Z-score threshold for extreme")
    parser.add_argument("--unwind-pct", type=float, default=0.1, help="Min % of delta that must unwind")
    parser.add_argument("--unwind-bars", type=int, default=3, help="Bars to confirm unwind")
    parser.add_argument("--lookback", type=int, default=50, help="Bars for rolling stats")

    args = parser.parse_args()

    if args.sweep:
        print("\nRunning Delta Unwind parameter sweep...")
        backtester = DeltaUnwindBacktester()
        results = backtester.run_parameter_sweep(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
        )

        if not results:
            print("No valid parameter combinations found (all hit rates <= 50%)")
        else:
            print(f"\nTop 10 parameter combinations (by 5-bar hit rate):\n")
            print(f"{'Z-Score':>8} {'Unwind%':>8} {'Bars':>6} {'LB':>6} {'Sigs':>6} {'Hit5%':>8} {'PF':>8}")
            print("-" * 60)
            for r in results[:10]:
                p = r.parameters
                print(f"{p['zscore_threshold']:>8.1f} {p['unwind_pct']*100:>8.1f} "
                      f"{p['unwind_bars']:>6} {p['lookback_bars']:>6} "
                      f"{r.total_signals:>6} {r.hit_rate_5:>8.1f} {r.profit_factor:>8.2f}")

            print("\n" + "=" * 60)
            print("Best parameters:")
            print_summary(results[0])

    else:
        backtester = DeltaUnwindBacktester(
            zscore_threshold=args.zscore,
            unwind_pct=args.unwind_pct,
            unwind_bars=args.unwind_bars,
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
