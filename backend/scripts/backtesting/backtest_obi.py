#!/usr/bin/env python3
"""
OBI (Order Book Imbalance) Signal Backtester

Validates OBI detection logic by testing against historical data.
OBI: Imbalance between bid and ask depth indicating directional pressure.

Usage:
    python scripts/backtest_obi.py --timeframe 5M
    python scripts/backtest_obi.py --timeframe 15M --sweep
    python scripts/backtest_obi.py --timeframe 1M --show-signals
"""
import os
import sys
from pathlib import Path
import argparse
import logging
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime

# Add backend directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import polars as pl
from app.data.storage import DuckDBStorage

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class OBISignal:
    """Represents a detected OBI signal"""
    timestamp: datetime
    direction: str  # BULLISH or BEARISH
    price: float
    imbalance_ratio: float  # bid/ask ratio
    dom_imbalance: float  # DOM imbalance (0-1)
    strength: float


@dataclass
class BacktestResult:
    """Result for a single signal"""
    signal: OBISignal
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


class OBIBacktester:
    """Backtester for OBI (Order Book Imbalance) signals"""

    def __init__(
        self,
        obi_threshold: float = 1.2,
        dom_threshold: float = 0.55,
        min_depth: int = 100,
        lookback_bars: int = 20,
    ):
        """Initialize backtester with detection parameters

        Args:
            obi_threshold: Bid/Ask ratio threshold for imbalance signal
            dom_threshold: DOM imbalance threshold (>threshold=bullish, <1-threshold=bearish)
            min_depth: Minimum total depth to consider valid signal
            lookback_bars: Bars for rolling averages
        """
        self.obi_threshold = obi_threshold
        self.dom_threshold = dom_threshold
        self.min_depth = min_depth
        self.lookback_bars = lookback_bars

        self.db = DuckDBStorage()

    def get_parameters(self) -> dict:
        """Return current parameters as dict"""
        return {
            "obi_threshold": self.obi_threshold,
            "dom_threshold": self.dom_threshold,
            "min_depth": self.min_depth,
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
        """Load historical data from ohlcv_ticks table"""
        where_clauses = [f"symbol = '{symbol}'", f"timeframe = '{timeframe}'"]
        # Require dom_imbalance for OBI signals
        where_clauses.append("dom_imbalance IS NOT NULL")

        if start_date:
            where_clauses.append(f"timestamp >= '{start_date}'")
        if end_date:
            where_clauses.append(f"timestamp <= '{end_date}'")

        where_str = " AND ".join(where_clauses)

        query = f"""
            SELECT
                timestamp,
                open,
                high,
                low,
                close,
                volume,
                dom_imbalance,
                instant_delta,
                cvd
            FROM ohlcv_ticks
            WHERE {where_str}
            ORDER BY timestamp ASC
            LIMIT {limit}
        """

        df = self.db.conn.execute(query).pl()

        logger.info(f"Loaded {len(df)} bars with orderflow for {symbol} {timeframe}")
        return df

    def detect_signals(self, df: pl.DataFrame) -> List[OBISignal]:
        """Detect OBI signals in the data

        OBI Logic:
        1. Calculate bid/ask depth ratio
        2. Strong imbalance indicates directional pressure
        3. Confirm with DOM imbalance direction
        """
        signals = []

        if len(df) < self.lookback_bars + 1:
            logger.warning(f"Not enough data: {len(df)} bars, need {self.lookback_bars + 1}")
            return signals

        # Use bar-close depth values for more responsive signals
        # (average DOM tends to converge to 0.5)
        use_last_values = "last_bid_depth" in df.columns

        rows = df.to_dicts()

        for i, row in enumerate(rows):
            # Get depth values
            if use_last_values:
                bid_depth = row.get("last_bid_depth", row.get("total_bid_depth", 0))
                ask_depth = row.get("last_ask_depth", row.get("total_ask_depth", 0))
                dom = row.get("last_dom", row.get("dom_imbalance", 0.5))
            else:
                bid_depth = row.get("total_bid_depth", 0)
                ask_depth = row.get("total_ask_depth", 0)
                dom = row.get("dom_imbalance", 0.5)

            if bid_depth is None or ask_depth is None or dom is None:
                continue

            # Check minimum depth requirement
            total_depth = bid_depth + ask_depth
            if total_depth < self.min_depth:
                continue

            # Calculate imbalance ratio
            if ask_depth > 0:
                imbalance_ratio = bid_depth / ask_depth
            else:
                continue

            # Check for bullish OBI (bid heavy)
            if imbalance_ratio > self.obi_threshold and dom > self.dom_threshold:
                strength = min(1.0, (imbalance_ratio - self.obi_threshold) / self.obi_threshold)
                signals.append(OBISignal(
                    timestamp=row["timestamp"],
                    direction="BULLISH",
                    price=row["close"],
                    imbalance_ratio=imbalance_ratio,
                    dom_imbalance=dom,
                    strength=strength,
                ))

            # Check for bearish OBI (ask heavy)
            elif imbalance_ratio < (1 / self.obi_threshold) and dom < (1 - self.dom_threshold):
                inv_ratio = 1 / imbalance_ratio
                strength = min(1.0, (inv_ratio - self.obi_threshold) / self.obi_threshold)
                signals.append(OBISignal(
                    timestamp=row["timestamp"],
                    direction="BEARISH",
                    price=row["close"],
                    imbalance_ratio=imbalance_ratio,
                    dom_imbalance=dom,
                    strength=strength,
                ))

        logger.info(f"Detected {len(signals)} OBI signals")
        return signals

    def calculate_forward_returns(
        self,
        df: pl.DataFrame,
        signals: List[OBISignal]
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
        obi_thresholds = [1.1, 1.2, 1.3, 1.5, 1.8, 2.0]
        dom_thresholds = [0.51, 0.52, 0.53, 0.55, 0.57, 0.60]
        min_depths = [0, 50, 100, 200, 500]
        lookbacks = [10, 20, 30]

        results = []
        total_combos = len(obi_thresholds) * len(dom_thresholds) * len(min_depths) * len(lookbacks)

        logger.info(f"Testing {total_combos} parameter combinations...")

        combo_count = 0
        for obi in obi_thresholds:
            for dom in dom_thresholds:
                for depth in min_depths:
                    for lb in lookbacks:
                        combo_count += 1
                        self.obi_threshold = obi
                        self.dom_threshold = dom
                        self.min_depth = depth
                        self.lookback_bars = lb

                        signals = self.detect_signals(df.clone())
                        if len(signals) < 5:
                            continue

                        bt_results = self.calculate_forward_returns(df, signals)
                        if len(bt_results) < 5:
                            continue

                        summary = self.calculate_summary(bt_results)
                        if summary.hit_rate_5 > 50:
                            results.append(summary)

                        if combo_count % 100 == 0:
                            logger.info(f"Progress: {combo_count}/{total_combos}")

        results.sort(key=lambda x: x.hit_rate_5, reverse=True)
        return results


def print_summary(summary: BacktestSummary):
    """Pretty print backtest summary"""
    print("\n" + "=" * 60)
    print("OBI BACKTEST RESULTS")
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
    print("\n" + "=" * 100)
    print("SIGNAL DETAILS")
    print("=" * 100)
    print(f"\n{'Timestamp':<20} {'Dir':>8} {'Price':>10} {'Imb Ratio':>10} {'DOM':>8} {'Ret 5bar':>10} {'Hit?':>6}")
    print("-" * 100)

    for result in results[:limit]:
        s = result.signal
        hit = "Yes" if result.hit_5 else "No"
        ts_str = s.timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(s.timestamp, "strftime") else str(s.timestamp)[:16]

        print(f"{ts_str:<20} {s.direction:>8} {s.price:>10.2f} {s.imbalance_ratio:>10.2f} "
              f"{s.dom_imbalance:>8.3f} {result.forward_return_5*100:>10.4f}% {hit:>6}")

    print("-" * 100)
    print(f"Showing {min(limit, len(results))} of {len(results)} signals")


def main():
    parser = argparse.ArgumentParser(description="Backtest OBI signal detection")
    parser.add_argument("--timeframe", "-t", default="5M", help="Bar timeframe")
    parser.add_argument("--symbol", "-s", default="MNQ", help="Trading symbol")
    parser.add_argument("--limit", "-l", type=int, default=50000, help="Max bars")
    parser.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    parser.add_argument("--show-signals", action="store_true", help="Show individual signals")

    # Detection parameters
    parser.add_argument("--obi-threshold", type=float, default=1.2)
    parser.add_argument("--dom-threshold", type=float, default=0.55)
    parser.add_argument("--min-depth", type=int, default=100)
    parser.add_argument("--lookback", type=int, default=20)

    args = parser.parse_args()

    if args.sweep:
        print("\nRunning OBI parameter sweep...")
        backtester = OBIBacktester()
        results = backtester.run_parameter_sweep(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
        )

        if not results:
            print("No valid parameter combinations found")
        else:
            print(f"\nTop 10 parameter combinations (by 5-bar hit rate):\n")
            print(f"{'OBI':>6} {'DOM':>6} {'Depth':>6} {'LB':>4} {'Sigs':>6} {'Hit5%':>8} {'PF':>8}")
            print("-" * 50)
            for r in results[:10]:
                p = r.parameters
                print(f"{p['obi_threshold']:>6.2f} {p['dom_threshold']:>6.2f} {p['min_depth']:>6} "
                      f"{p['lookback_bars']:>4} {r.total_signals:>6} {r.hit_rate_5:>8.1f} {r.profit_factor:>8.2f}")

            print("\n" + "=" * 50)
            print("Best parameters:")
            print_summary(results[0])

    else:
        backtester = OBIBacktester(
            obi_threshold=args.obi_threshold,
            dom_threshold=args.dom_threshold,
            min_depth=args.min_depth,
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
