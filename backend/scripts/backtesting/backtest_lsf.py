#!/usr/bin/env python3
"""
LSF (Liquidity Sweep Fade) Signal Backtester

Pure price-based LSF detection:
1. Price makes new high/low beyond recent range (liquidity sweep)
2. Price snaps back into prior range within N bars (fade)

No delta requirement - focuses purely on price action pattern.

Usage:
    python scripts/backtest_lsf.py --timeframe 5M
    python scripts/backtest_lsf.py --timeframe 15M --sweep
    python scripts/backtest_lsf.py --timeframe 1M --show-signals
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
class LSFSignal:
    """Represents a detected LSF signal"""
    timestamp: datetime
    direction: str  # BULLISH or BEARISH
    price: float
    sweep_price: float  # The extreme price of the sweep
    prior_level: float  # The level that was swept (prior high/low)
    snapback_pct: float  # Percentage snapback into range
    sweep_depth_pct: float  # How far beyond the level price swept
    strength: float


@dataclass
class BacktestResult:
    """Result for a single signal"""
    signal: LSFSignal
    forward_return_1: float  # Return after 1 bar
    forward_return_5: float  # Return after 5 bars
    forward_return_10: float  # Return after 10 bars
    hit_1: bool  # Did price move in predicted direction?
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


class LSFBacktester:
    """Backtester for LSF (Liquidity Sweep Fade) signals - Pure Price Based"""

    def __init__(
        self,
        sweep_threshold_pct: float = 0.001,  # Min % beyond level to count as sweep
        snapback_pct: float = 0.002,  # Min % snapback into range
        snapback_bars: int = 3,  # Max bars to wait for snapback
        lookback_bars: int = 20,  # Bars for rolling high/low
    ):
        """Initialize backtester with detection parameters

        Args:
            sweep_threshold_pct: Minimum % price must exceed prior high/low
            snapback_pct: Minimum snapback % into prior range
            snapback_bars: Maximum bars to wait for snapback confirmation
            lookback_bars: Bars for calculating rolling high/low levels
        """
        self.sweep_threshold_pct = sweep_threshold_pct
        self.snapback_pct = snapback_pct
        self.snapback_bars = snapback_bars
        self.lookback_bars = lookback_bars

        self.db = DuckDBStorage()

    def get_parameters(self) -> dict:
        """Return current parameters as dict"""
        return {
            "sweep_threshold_pct": self.sweep_threshold_pct,
            "snapback_pct": self.snapback_pct,
            "snapback_bars": self.snapback_bars,
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
        # Note: delta values may have unsigned int overflow (e.g. 4294967294 = -2)
        # Convert to signed by treating values > 2^31 as negative
        query = f"""
            WITH bars AS (
                SELECT
                    time_bucket(INTERVAL '{interval}', timestamp) as bar_time,
                    FIRST(mid_price) as open,
                    MAX(mid_price) as high,
                    MIN(mid_price) as low,
                    LAST(mid_price) as close,
                    COUNT(*) as volume,
                    -- Fix unsigned overflow: convert values > 2^31 to signed
                    SUM(CASE WHEN delta > 2147483647 THEN CAST(delta AS BIGINT) - 4294967296 ELSE delta END) as instant_delta,
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

    def detect_signals(self, df: pl.DataFrame) -> List[LSFSignal]:
        """Detect LSF signals in the data

        Pure Price-Based LSF Logic:
        1. Price sweeps beyond prior high/low (liquidity grab)
        2. Price snaps back into prior range within N bars (fade)
        """
        signals = []

        if len(df) < self.lookback_bars + self.snapback_bars + 2:
            logger.warning(f"Not enough data: {len(df)} bars, need {self.lookback_bars + self.snapback_bars + 2}")
            return signals

        # Calculate rolling high/low (shifted to exclude current bar)
        df = df.with_columns([
            pl.col("high").rolling_max(window_size=self.lookback_bars).shift(1).alias("prior_high"),
            pl.col("low").rolling_min(window_size=self.lookback_bars).shift(1).alias("prior_low"),
        ])

        rows = df.to_dicts()

        for i in range(self.lookback_bars, len(rows) - self.snapback_bars - 1):
            row = rows[i]

            if row["prior_high"] is None or row["prior_low"] is None:
                continue
            if row["prior_high"] == 0 or row["prior_low"] == 0:
                continue

            # Check for bearish LSF (sweep high then reverse down)
            sweep_depth_high = (row["high"] - row["prior_high"]) / row["prior_high"]
            if sweep_depth_high > self.sweep_threshold_pct:
                # Look for snapback within N bars
                for j in range(1, self.snapback_bars + 1):
                    future_row = rows[i + j]
                    snapback_pct = (row["high"] - future_row["close"]) / row["high"]

                    if snapback_pct > self.snapback_pct:
                        strength = min(1.0, snapback_pct / (self.snapback_pct * 3))
                        signals.append(LSFSignal(
                            timestamp=future_row["timestamp"],
                            direction="BEARISH",
                            price=future_row["close"],
                            sweep_price=row["high"],
                            prior_level=row["prior_high"],
                            snapback_pct=snapback_pct,
                            sweep_depth_pct=sweep_depth_high,
                            strength=strength,
                        ))
                        break  # Only one signal per sweep

            # Check for bullish LSF (sweep low then reverse up)
            sweep_depth_low = (row["prior_low"] - row["low"]) / row["prior_low"]
            if sweep_depth_low > self.sweep_threshold_pct:
                # Look for snapback within N bars
                for j in range(1, self.snapback_bars + 1):
                    future_row = rows[i + j]
                    snapback_pct = (future_row["close"] - row["low"]) / row["low"]

                    if snapback_pct > self.snapback_pct:
                        strength = min(1.0, snapback_pct / (self.snapback_pct * 3))
                        signals.append(LSFSignal(
                            timestamp=future_row["timestamp"],
                            direction="BULLISH",
                            price=future_row["close"],
                            sweep_price=row["low"],
                            prior_level=row["prior_low"],
                            snapback_pct=snapback_pct,
                            sweep_depth_pct=sweep_depth_low,
                            strength=strength,
                        ))
                        break  # Only one signal per sweep

        logger.info(f"Detected {len(signals)} LSF signals")
        return signals

    def calculate_forward_returns(
        self,
        df: pl.DataFrame,
        signals: List[LSFSignal]
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
        sweep_thresholds = [0.0005, 0.001, 0.0015, 0.002, 0.003]
        snapback_pcts = [0.001, 0.0015, 0.002, 0.003, 0.005]
        snapback_bars_list = [1, 2, 3, 5]
        lookbacks = [10, 15, 20, 30]

        results = []
        total_combos = len(sweep_thresholds) * len(snapback_pcts) * len(snapback_bars_list) * len(lookbacks)

        logger.info(f"Testing {total_combos} parameter combinations...")

        combo_count = 0
        for sweep_thresh in sweep_thresholds:
            for snap_pct in snapback_pcts:
                for snap_bars in snapback_bars_list:
                    for lb in lookbacks:
                        combo_count += 1
                        self.sweep_threshold_pct = sweep_thresh
                        self.snapback_pct = snap_pct
                        self.snapback_bars = snap_bars
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

                        if combo_count % 100 == 0:
                            logger.info(f"Progress: {combo_count}/{total_combos}")

        results.sort(key=lambda x: (x.hit_rate_5, x.profit_factor), reverse=True)
        return results


def print_summary(summary: BacktestSummary):
    """Pretty print backtest summary"""
    print("\n" + "=" * 60)
    print("LSF BACKTEST RESULTS")
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
        print(f"  No edge (profit factor < 1.0)")

    print("=" * 60)


def print_signals(results: List[BacktestResult], limit: int = 20):
    """Print individual signals for review"""
    print("\n" + "=" * 110)
    print("SIGNAL DETAILS")
    print("=" * 110)
    print(f"\n{'Timestamp':<20} {'Dir':>8} {'Price':>10} {'Sweep':>10} {'Prior':>10} {'Snap%':>8} {'Ret 5bar':>10} {'Hit?':>6}")
    print("-" * 110)

    for result in results[:limit]:
        s = result.signal
        hit = "Yes" if result.hit_5 else "No"
        ts_str = s.timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(s.timestamp, "strftime") else str(s.timestamp)[:16]

        print(f"{ts_str:<20} {s.direction:>8} {s.price:>10.2f} {s.sweep_price:>10.2f} {s.prior_level:>10.2f} "
              f"{s.snapback_pct*100:>8.3f} {result.forward_return_5*100:>10.4f}% {hit:>6}")

    print("-" * 110)
    print(f"Showing {min(limit, len(results))} of {len(results)} signals")


def main():
    parser = argparse.ArgumentParser(description="Backtest LSF (Liquidity Sweep Fade) signal detection")
    parser.add_argument("--timeframe", "-t", default="5M", help="Bar timeframe")
    parser.add_argument("--symbol", "-s", default="MNQ", help="Trading symbol")
    parser.add_argument("--limit", "-l", type=int, default=50000, help="Max bars")
    parser.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    parser.add_argument("--show-signals", action="store_true", help="Show individual signals")

    # Detection parameters
    parser.add_argument("--sweep-threshold", type=float, default=0.001, help="Min % beyond level for sweep")
    parser.add_argument("--snapback-pct", type=float, default=0.002, help="Min % snapback into range")
    parser.add_argument("--snapback-bars", type=int, default=3, help="Max bars to wait for snapback")
    parser.add_argument("--lookback", type=int, default=20, help="Bars for rolling high/low")

    args = parser.parse_args()

    if args.sweep:
        print("\nRunning LSF (Pure Price) parameter sweep...")
        backtester = LSFBacktester()
        results = backtester.run_parameter_sweep(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
        )

        if not results:
            print("No valid parameter combinations found (all hit rates <= 50%)")
        else:
            print(f"\nTop 10 parameter combinations (by 5-bar hit rate):\n")
            print(f"{'Sweep%':>8} {'Snap%':>8} {'SnapB':>6} {'LB':>6} {'Sigs':>6} {'Hit5%':>8} {'PF':>8}")
            print("-" * 60)
            for r in results[:10]:
                p = r.parameters
                print(f"{p['sweep_threshold_pct']*100:>8.2f} {p['snapback_pct']*100:>8.2f} "
                      f"{p['snapback_bars']:>6} {p['lookback_bars']:>6} "
                      f"{r.total_signals:>6} {r.hit_rate_5:>8.1f} {r.profit_factor:>8.2f}")

            print("\n" + "=" * 60)
            print("Best parameters:")
            print_summary(results[0])

    else:
        backtester = LSFBacktester(
            sweep_threshold_pct=args.sweep_threshold,
            snapback_pct=args.snapback_pct,
            snapback_bars=args.snapback_bars,
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
