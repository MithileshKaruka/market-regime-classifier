"""Absorption Signal Backtester

Tests the absorption detection logic against historical data to validate
signal accuracy and optimize parameters.

Usage:
    python scripts/backtest_absorption.py --timeframe 1M --lookback 20
    python scripts/backtest_absorption.py --sweep  # Run parameter sweep
"""
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple
import polars as pl
import numpy as np
from datetime import datetime, timedelta
from itertools import product

from app.data.storage import DuckDBStorage
from config import get_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class AbsorptionSignal:
    """Detected absorption signal with metadata"""
    timestamp: datetime
    direction: str  # BULLISH or BEARISH
    price: float
    volume: float
    avg_volume: float
    dom_imbalance: float
    strength: float


@dataclass
class BacktestResult:
    """Result of a single signal's forward performance"""
    signal: AbsorptionSignal
    forward_return_1: float  # Return after 1 bar
    forward_return_5: float  # Return after 5 bars
    forward_return_10: float  # Return after 10 bars
    hit_1: bool  # Did price move in predicted direction after 1 bar
    hit_5: bool
    hit_10: bool


@dataclass
class BacktestSummary:
    """Summary statistics for a backtest run"""
    total_signals: int
    bullish_signals: int
    bearish_signals: int
    hit_rate_1: float
    hit_rate_5: float
    hit_rate_10: float
    avg_return_1: float
    avg_return_5: float
    avg_return_10: float
    avg_win_return: float
    avg_loss_return: float
    profit_factor: float  # Sum of wins / Sum of losses
    parameters: dict


class AbsorptionBacktester:
    """Backtester for absorption signal detection"""

    def __init__(
        self,
        volume_mult: float = 1.3,
        price_tol: float = 0.001,
        dom_threshold: float = 0.52,
        lookback_bars: int = 20,
        depth_ratio_min: float = 0.7,
        depth_ratio_max: float = 1.5,
        require_cvd_confirm: bool = False,
    ):
        """Initialize backtester with detection parameters

        Args:
            volume_mult: Volume must exceed avg * this multiplier
            price_tol: Max price change % to be considered "stable"
            dom_threshold: DOM imbalance threshold for direction
            lookback_bars: Bars for rolling averages
            depth_ratio_min: Min depth ratio for stability
            depth_ratio_max: Max depth ratio for stability
            require_cvd_confirm: Require CVD to confirm DOM direction
        """
        self.volume_mult = volume_mult
        self.price_tol = price_tol
        self.dom_threshold = dom_threshold
        self.lookback_bars = lookback_bars
        self.depth_ratio_min = depth_ratio_min
        self.depth_ratio_max = depth_ratio_max
        self.require_cvd_confirm = require_cvd_confirm

        self.db = DuckDBStorage()

    def get_parameters(self) -> dict:
        """Return current parameters as dict"""
        return {
            "volume_mult": self.volume_mult,
            "price_tol": self.price_tol,
            "dom_threshold": self.dom_threshold,
            "lookback_bars": self.lookback_bars,
            "depth_ratio_min": self.depth_ratio_min,
            "depth_ratio_max": self.depth_ratio_max,
            "require_cvd_confirm": self.require_cvd_confirm,
        }

    def load_data(
        self,
        timeframe: str = "1M",
        symbol: str = "MNQ",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100000,
    ) -> pl.DataFrame:
        """Load historical data by aggregating MBP ticks into bars

        Uses MBP tick data which has actual DOM imbalance values,
        and aggregates into OHLCV bars at the specified timeframe.

        Args:
            timeframe: Bar timeframe (1M, 5M, 15M, 1H, etc.)
            symbol: Trading symbol
            start_date: Start date (ISO format)
            end_date: End date (ISO format)
            limit: Max bars to return

        Returns:
            DataFrame with OHLCV and orderflow metrics
        """
        # Parse timeframe to interval
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

        # Aggregate MBP ticks into bars with proper OHLCV and DOM metrics
        query = f"""
            WITH bars AS (
                SELECT
                    time_bucket(INTERVAL '{interval}', timestamp) as bar_time,
                    FIRST(mid_price) as open,
                    MAX(mid_price) as high,
                    MIN(mid_price) as low,
                    LAST(mid_price) as close,
                    COUNT(*) as volume,
                    AVG(dom_imbalance) as dom_imbalance,
                    SUM(delta) as cvd,
                    AVG(total_bid_depth) as total_bid_depth,
                    AVG(total_ask_depth) as total_ask_depth
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

        # Rename bar_time to timestamp for consistency
        if "bar_time" in df.columns:
            df = df.rename({"bar_time": "timestamp"})

        logger.info(f"Loaded {len(df)} bars from MBP ticks for {symbol} {timeframe}")
        return df

    def detect_signals(self, df: pl.DataFrame) -> List[AbsorptionSignal]:
        """Detect absorption signals in the data

        Uses the current parameter settings to identify absorption patterns.

        Absorption Logic:
        1. Volume exceeds average by multiplier (high activity)
        2. Price change is minimal (absorption holding price)
        3. DOM shows directional bias (who is absorbing)
        4. (Optional) Depth stability - resting orders not depleting

        Args:
            df: DataFrame with OHLCV and orderflow data

        Returns:
            List of detected absorption signals
        """
        signals = []

        if len(df) < self.lookback_bars + 1:
            logger.warning(f"Not enough data: {len(df)} bars, need {self.lookback_bars + 1}")
            return signals

        # Calculate rolling averages
        df = df.with_columns([
            pl.col("volume").rolling_mean(window_size=self.lookback_bars).alias("avg_volume"),
        ])

        # Add depth ratio calculations if we have depth data
        has_depth = "total_bid_depth" in df.columns and "total_ask_depth" in df.columns
        if has_depth:
            df = df.with_columns([
                pl.col("total_bid_depth").rolling_mean(window_size=self.lookback_bars).alias("avg_bid_depth"),
                pl.col("total_ask_depth").rolling_mean(window_size=self.lookback_bars).alias("avg_ask_depth"),
            ])
            df = df.with_columns([
                (pl.col("total_bid_depth") / pl.col("avg_bid_depth")).alias("bid_depth_ratio"),
                (pl.col("total_ask_depth") / pl.col("avg_ask_depth")).alias("ask_depth_ratio"),
            ])

        # Calculate price change percentage
        df = df.with_columns([
            ((pl.col("close") - pl.col("open")).abs() / pl.col("open")).alias("price_change_pct"),
        ])

        # Iterate and detect
        rows = df.to_dicts()

        for i, row in enumerate(rows):
            if row["avg_volume"] is None or row["avg_volume"] == 0:
                continue

            # Condition 1: High volume (activity spike)
            volume_high = row["volume"] > row["avg_volume"] * self.volume_mult

            # Condition 2: Price stability (absorption holding level)
            price_stable = row["price_change_pct"] < self.price_tol

            # Condition 3: DOM directional bias
            dom = row["dom_imbalance"]
            if dom is None:
                continue

            # Condition 4 (optional): Depth stability
            depth_stable = True
            if has_depth:
                bid_ratio = row.get("bid_depth_ratio")
                ask_ratio = row.get("ask_depth_ratio")
                if bid_ratio is not None and ask_ratio is not None:
                    bid_stable = self.depth_ratio_min < bid_ratio < self.depth_ratio_max
                    ask_stable = self.depth_ratio_min < ask_ratio < self.depth_ratio_max
                    depth_stable = bid_stable or ask_stable

            # All conditions must pass
            if volume_high and price_stable and depth_stable:
                # Determine direction from DOM
                if dom > self.dom_threshold:
                    direction = "BULLISH"
                elif dom < (1 - self.dom_threshold):
                    direction = "BEARISH"
                else:
                    continue  # Neutral DOM, skip

                # Optional: CVD confirmation
                # If enabled, CVD should align with DOM direction
                if self.require_cvd_confirm:
                    cvd = row.get("cvd", 0)
                    if cvd is None:
                        cvd = 0
                    if direction == "BULLISH" and cvd <= 0:
                        continue  # CVD doesn't confirm bullish
                    if direction == "BEARISH" and cvd >= 0:
                        continue  # CVD doesn't confirm bearish

                # Calculate strength based on volume excess
                strength = min(1.0, (row["volume"] / row["avg_volume"] - 1) / 2)

                signals.append(AbsorptionSignal(
                    timestamp=row["timestamp"],
                    direction=direction,
                    price=row["close"],
                    volume=row["volume"],
                    avg_volume=row["avg_volume"],
                    dom_imbalance=dom,
                    strength=strength,
                ))

        logger.info(f"Detected {len(signals)} absorption signals")
        return signals

    def calculate_forward_returns(
        self,
        df: pl.DataFrame,
        signals: List[AbsorptionSignal],
    ) -> List[BacktestResult]:
        """Calculate forward returns for each signal

        Args:
            df: Price data
            signals: Detected signals

        Returns:
            List of backtest results with forward returns
        """
        results = []
        rows = df.to_dicts()

        # Create timestamp index map
        ts_to_idx = {row["timestamp"]: i for i, row in enumerate(rows)}

        for signal in signals:
            if signal.timestamp not in ts_to_idx:
                continue

            idx = ts_to_idx[signal.timestamp]
            entry_price = signal.price

            # Calculate forward returns at different horizons
            def get_return(bars_ahead: int) -> Tuple[float, bool]:
                if idx + bars_ahead >= len(rows):
                    return 0.0, False

                exit_price = rows[idx + bars_ahead]["close"]
                ret = (exit_price - entry_price) / entry_price

                # Adjust sign based on direction
                if signal.direction == "BEARISH":
                    ret = -ret

                hit = ret > 0
                return ret, hit

            ret_1, hit_1 = get_return(1)
            ret_5, hit_5 = get_return(5)
            ret_10, hit_10 = get_return(10)

            results.append(BacktestResult(
                signal=signal,
                forward_return_1=ret_1,
                forward_return_5=ret_5,
                forward_return_10=ret_10,
                hit_1=hit_1,
                hit_5=hit_5,
                hit_10=hit_10,
            ))

        return results

    def calculate_summary(self, results: List[BacktestResult]) -> BacktestSummary:
        """Calculate summary statistics from backtest results

        Args:
            results: List of backtest results

        Returns:
            Summary statistics
        """
        if not results:
            return BacktestSummary(
                total_signals=0,
                bullish_signals=0,
                bearish_signals=0,
                hit_rate_1=0.0,
                hit_rate_5=0.0,
                hit_rate_10=0.0,
                avg_return_1=0.0,
                avg_return_5=0.0,
                avg_return_10=0.0,
                avg_win_return=0.0,
                avg_loss_return=0.0,
                profit_factor=0.0,
                parameters=self.get_parameters(),
            )

        total = len(results)
        bullish = sum(1 for r in results if r.signal.direction == "BULLISH")
        bearish = total - bullish

        # Hit rates
        hit_1 = sum(1 for r in results if r.hit_1) / total
        hit_5 = sum(1 for r in results if r.hit_5) / total
        hit_10 = sum(1 for r in results if r.hit_10) / total

        # Average returns
        avg_1 = np.mean([r.forward_return_1 for r in results])
        avg_5 = np.mean([r.forward_return_5 for r in results])
        avg_10 = np.mean([r.forward_return_10 for r in results])

        # Win/loss analysis (using 5-bar horizon as main metric)
        wins = [r.forward_return_5 for r in results if r.forward_return_5 > 0]
        losses = [r.forward_return_5 for r in results if r.forward_return_5 < 0]

        avg_win = np.mean(wins) if wins else 0.0
        avg_loss = np.mean(losses) if losses else 0.0

        sum_wins = sum(wins) if wins else 0.0
        sum_losses = abs(sum(losses)) if losses else 0.0001  # Avoid div by zero

        profit_factor = sum_wins / sum_losses

        return BacktestSummary(
            total_signals=total,
            bullish_signals=bullish,
            bearish_signals=bearish,
            hit_rate_1=hit_1,
            hit_rate_5=hit_5,
            hit_rate_10=hit_10,
            avg_return_1=avg_1,
            avg_return_5=avg_5,
            avg_return_10=avg_10,
            avg_win_return=avg_win,
            avg_loss_return=avg_loss,
            profit_factor=profit_factor,
            parameters=self.get_parameters(),
        )

    def run_backtest(
        self,
        timeframe: str = "1M",
        symbol: str = "MNQ",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100000,
    ) -> BacktestSummary:
        """Run complete backtest with current parameters

        Args:
            timeframe: Bar timeframe
            symbol: Trading symbol
            start_date: Start date (ISO format)
            end_date: End date (ISO format)
            limit: Max records

        Returns:
            BacktestSummary with results
        """
        # Load data
        df = self.load_data(timeframe, symbol, start_date, end_date, limit)

        if len(df) == 0:
            logger.error("No data loaded")
            return self.calculate_summary([])

        # Detect signals
        signals = self.detect_signals(df)

        if not signals:
            logger.warning("No signals detected with current parameters")
            return self.calculate_summary([])

        # Calculate forward returns
        results = self.calculate_forward_returns(df, signals)

        # Calculate summary
        summary = self.calculate_summary(results)

        return summary

    def run_parameter_sweep(
        self,
        timeframe: str = "1M",
        symbol: str = "MNQ",
        limit: int = 50000,
    ) -> List[BacktestSummary]:
        """Run parameter sweep to find optimal settings

        Tests combinations of parameters and returns results sorted by hit rate.

        Returns:
            List of BacktestSummary sorted by hit_rate_5 descending
        """
        # Parameter ranges to test
        volume_mults = [1.2, 1.3, 1.5, 1.8, 2.0]
        price_tols = [0.0005, 0.001, 0.002, 0.003]
        dom_thresholds = [0.51, 0.52, 0.55, 0.58]
        lookbacks = [10, 20, 30, 50]

        # Load data once
        df = self.load_data(timeframe, symbol, limit=limit)
        if len(df) == 0:
            logger.error("No data for parameter sweep")
            return []

        results = []
        total_combos = len(volume_mults) * len(price_tols) * len(dom_thresholds) * len(lookbacks)
        logger.info(f"Testing {total_combos} parameter combinations...")

        for i, (vm, pt, dt, lb) in enumerate(product(volume_mults, price_tols, dom_thresholds, lookbacks)):
            self.volume_mult = vm
            self.price_tol = pt
            self.dom_threshold = dt
            self.lookback_bars = lb

            signals = self.detect_signals(df.clone())
            if not signals:
                continue

            backtest_results = self.calculate_forward_returns(df, signals)
            summary = self.calculate_summary(backtest_results)

            # Only include if we have enough signals
            if summary.total_signals >= 20:
                results.append(summary)

            if (i + 1) % 50 == 0:
                logger.info(f"Progress: {i + 1}/{total_combos}")

        # Sort by hit rate at 5-bar horizon
        results.sort(key=lambda x: x.hit_rate_5, reverse=True)

        return results


def print_signals(results: List[BacktestResult], limit: int = 20):
    """Print individual signals for manual review

    Args:
        results: Backtest results
        limit: Max signals to show
    """
    print("\n" + "=" * 100)
    print("SIGNAL DETAILS (for manual validation)")
    print("=" * 100)
    print(f"\n{'Timestamp':<20} {'Dir':>8} {'Price':>10} {'DOM':>8} {'Vol/Avg':>8} {'Ret 5bar':>10} {'Hit?':>6}")
    print("-" * 100)

    for result in results[:limit]:
        s = result.signal
        vol_ratio = s.volume / s.avg_volume
        hit = "Yes" if result.hit_5 else "No"
        ts_str = s.timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(s.timestamp, "strftime") else str(s.timestamp)[:16]

        print(f"{ts_str:<20} {s.direction:>8} {s.price:>10.2f} {s.dom_imbalance:>8.3f} "
              f"{vol_ratio:>8.2f} {result.forward_return_5*100:>10.4f}% {hit:>6}")

    print("-" * 100)
    print(f"Showing {min(limit, len(results))} of {len(results)} signals")


def export_signals_csv(results: List[BacktestResult], filepath: str = "absorption_signals.csv"):
    """Export signals to CSV for analysis

    Args:
        results: Backtest results
        filepath: Output file path
    """
    import csv

    with open(filepath, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'timestamp', 'direction', 'price', 'volume', 'avg_volume', 'vol_ratio',
            'dom_imbalance', 'strength', 'return_1bar', 'return_5bar', 'return_10bar',
            'hit_1bar', 'hit_5bar', 'hit_10bar'
        ])

        for result in results:
            s = result.signal
            ts_str = s.timestamp.strftime("%Y-%m-%d %H:%M:%S") if hasattr(s.timestamp, "strftime") else str(s.timestamp)
            writer.writerow([
                ts_str, s.direction, s.price, s.volume, s.avg_volume,
                s.volume / s.avg_volume, s.dom_imbalance, s.strength,
                result.forward_return_1, result.forward_return_5, result.forward_return_10,
                result.hit_1, result.hit_5, result.hit_10
            ])

    print(f"\nSignals exported to: {filepath}")


def print_summary(summary: BacktestSummary):
    """Pretty print backtest summary"""
    print("\n" + "=" * 60)
    print("ABSORPTION BACKTEST RESULTS")
    print("=" * 60)
    print(f"\nParameters:")
    for k, v in summary.parameters.items():
        print(f"  {k}: {v}")

    print(f"\nSignals Detected: {summary.total_signals}")
    print(f"  Bullish: {summary.bullish_signals}")
    print(f"  Bearish: {summary.bearish_signals}")

    print(f"\nHit Rates (% signals where price moved in predicted direction):")
    print(f"  1-bar:  {summary.hit_rate_1:.1%}")
    print(f"  5-bar:  {summary.hit_rate_5:.1%}")
    print(f"  10-bar: {summary.hit_rate_10:.1%}")

    print(f"\nAverage Returns (direction-adjusted):")
    print(f"  1-bar:  {summary.avg_return_1:.4%}")
    print(f"  5-bar:  {summary.avg_return_5:.4%}")
    print(f"  10-bar: {summary.avg_return_10:.4%}")

    print(f"\nWin/Loss Analysis (5-bar horizon):")
    print(f"  Avg Win:  {summary.avg_win_return:.4%}")
    print(f"  Avg Loss: {summary.avg_loss_return:.4%}")
    print(f"  Profit Factor: {summary.profit_factor:.2f}")

    # Interpretation
    print(f"\nInterpretation:")
    if summary.hit_rate_5 > 0.55:
        print("  Signal has predictive value (hit rate > 55%)")
    elif summary.hit_rate_5 > 0.50:
        print("  Signal is marginally predictive (hit rate 50-55%)")
    else:
        print("  Signal appears random (hit rate <= 50%)")

    if summary.profit_factor > 1.5:
        print("  Strong edge (profit factor > 1.5)")
    elif summary.profit_factor > 1.0:
        print("  Slight edge (profit factor > 1.0)")
    else:
        print("  No edge (profit factor < 1.0)")

    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Backtest absorption signal detection")
    parser.add_argument("--timeframe", "-t", default="1M", help="Bar timeframe (1M, 5M, 15M, etc.)")
    parser.add_argument("--symbol", "-s", default="MNQ", help="Trading symbol")
    parser.add_argument("--limit", "-l", type=int, default=50000, help="Max bars to load")
    parser.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    parser.add_argument("--show-signals", action="store_true", help="Show individual signals for review")
    parser.add_argument("--export-csv", type=str, help="Export signals to CSV file")

    # Detection parameters (for single run)
    parser.add_argument("--volume-mult", type=float, default=1.3, help="Volume multiplier threshold")
    parser.add_argument("--price-tol", type=float, default=0.001, help="Price tolerance (0.001 = 0.1%)")
    parser.add_argument("--dom-threshold", type=float, default=0.52, help="DOM imbalance threshold")
    parser.add_argument("--lookback", type=int, default=20, help="Lookback period for averages")
    parser.add_argument("--cvd-confirm", action="store_true", help="Require CVD to confirm signal direction")

    args = parser.parse_args()

    if args.sweep:
        # Parameter sweep mode
        print("\nRunning parameter sweep...")
        backtester = AbsorptionBacktester()
        results = backtester.run_parameter_sweep(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
        )

        if not results:
            print("No valid parameter combinations found")
            return

        print(f"\nTop 10 parameter combinations (sorted by 5-bar hit rate):")
        print("-" * 100)
        print(f"{'Vol Mult':>10} {'Price Tol':>10} {'DOM Thr':>10} {'Lookback':>10} {'Signals':>10} {'Hit 5bar':>10} {'PF':>10}")
        print("-" * 100)

        for summary in results[:10]:
            p = summary.parameters
            print(f"{p['volume_mult']:>10.2f} {p['price_tol']:>10.4f} {p['dom_threshold']:>10.2f} "
                  f"{p['lookback_bars']:>10} {summary.total_signals:>10} {summary.hit_rate_5:>10.1%} "
                  f"{summary.profit_factor:>10.2f}")

        # Print detailed summary of best result
        print("\n\nBest Parameters:")
        print_summary(results[0])

    else:
        # Single run mode
        backtester = AbsorptionBacktester(
            volume_mult=args.volume_mult,
            price_tol=args.price_tol,
            dom_threshold=args.dom_threshold,
            lookback_bars=args.lookback,
            require_cvd_confirm=args.cvd_confirm,
        )

        # Load data and detect signals
        df = backtester.load_data(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
        )

        signals = backtester.detect_signals(df)
        results = backtester.calculate_forward_returns(df, signals)
        summary = backtester.calculate_summary(results)

        print_summary(summary)

        # Show individual signals if requested
        if args.show_signals and results:
            print_signals(results)

        # Export to CSV if requested
        if args.export_csv and results:
            export_signals_csv(results, args.export_csv)


if __name__ == "__main__":
    main()
