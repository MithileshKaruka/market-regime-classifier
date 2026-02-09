#!/usr/bin/env python3
"""
DOM Imbalance Lookback Backtester

Tests different lookback values (1-20 bars) to find optimal setting
for predicting price reversals from DOM imbalance extremes.

Logic:
- Calculate DOM imbalance averaged over N bars
- When DOM shows extreme imbalance (bid/ask heavy beyond threshold)
- Check if price reversed in the following bars
- Find optimal lookback for highest hit rate / profit factor

Usage:
    python scripts/backtesting/backtest_dom_lookback.py
    python scripts/backtesting/backtest_dom_lookback.py --timeframe 15M
    python scripts/backtesting/backtest_dom_lookback.py --threshold 0.55
"""
import sys
from pathlib import Path
import argparse
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple
from datetime import datetime

# Add backend directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import polars as pl
from app.data.storage import DuckDBStorage

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class DOMSignal:
    """Represents a DOM imbalance signal"""
    timestamp: datetime
    direction: str  # BULLISH (bid heavy -> expect up) or BEARISH (ask heavy -> expect down)
    dom_value: float  # Averaged DOM imbalance
    price: float
    lookback: int


@dataclass
class BacktestResult:
    """Result for a single signal"""
    signal: DOMSignal
    forward_return_1: float
    forward_return_3: float
    forward_return_5: float
    forward_return_10: float
    hit_1: bool
    hit_3: bool
    hit_5: bool
    hit_10: bool


@dataclass
class LookbackSummary:
    """Summary for a specific lookback value"""
    lookback: int
    total_signals: int
    bullish_signals: int
    bearish_signals: int
    hit_rate_1: float
    hit_rate_3: float
    hit_rate_5: float
    hit_rate_10: float
    avg_return_5: float
    profit_factor: float


class DOMBacktester:
    """Backtester for DOM imbalance signals"""

    def __init__(
        self,
        dom_threshold: float = 0.55,  # DOM > threshold = bid heavy (bullish)
        min_bars_between: int = 5,  # Min bars between signals to avoid clustering
    ):
        """Initialize backtester

        Args:
            dom_threshold: DOM > this = bullish signal, DOM < (1-this) = bearish signal
            min_bars_between: Minimum bars between signals
        """
        self.dom_threshold = dom_threshold
        self.min_bars_between = min_bars_between
        self.db = DuckDBStorage()

    def load_data(
        self,
        timeframe: str = "5M",
        symbol: str = "MNQ",
        limit: int = 50000,
    ) -> pl.DataFrame:
        """Load historical data with DOM imbalance"""
        query = f"""
            SELECT timestamp, open, high, low, close, volume, dom_imbalance
            FROM ohlcv_ticks
            WHERE symbol = '{symbol}' AND timeframe = '{timeframe}'
              AND volume > 100
              AND dom_imbalance IS NOT NULL
            ORDER BY timestamp ASC
            LIMIT {limit}
        """

        with self.db as storage:
            df = storage.conn.execute(query).pl()

        logger.info(f"Loaded {len(df):,} bars for {symbol} {timeframe}")
        return df

    def calculate_dom_avg(self, df: pl.DataFrame, lookback: int) -> pl.DataFrame:
        """Calculate rolling average DOM imbalance"""
        return df.with_columns([
            pl.col("dom_imbalance").rolling_mean(window_size=lookback).alias("dom_avg")
        ])

    def detect_signals(
        self,
        df: pl.DataFrame,
        lookback: int,
    ) -> List[DOMSignal]:
        """Detect DOM imbalance signals for a given lookback"""
        signals = []

        # Calculate rolling average
        df = self.calculate_dom_avg(df, lookback)
        rows = df.to_dicts()

        last_signal_idx = -self.min_bars_between

        for i in range(lookback, len(rows) - 11):  # Leave room for forward returns
            row = rows[i]
            dom_avg = row.get("dom_avg")

            if dom_avg is None:
                continue

            # Check for extreme imbalance
            direction = None
            if dom_avg > self.dom_threshold:
                direction = "BULLISH"  # Bid heavy -> expect price to go up
            elif dom_avg < (1 - self.dom_threshold):
                direction = "BEARISH"  # Ask heavy -> expect price to go down

            if direction is None:
                continue

            # Enforce minimum bars between signals
            if i - last_signal_idx < self.min_bars_between:
                continue

            signals.append(DOMSignal(
                timestamp=row["timestamp"],
                direction=direction,
                dom_value=dom_avg,
                price=row["close"],
                lookback=lookback,
            ))
            last_signal_idx = i

        return signals

    def backtest_signals(
        self,
        df: pl.DataFrame,
        signals: List[DOMSignal],
    ) -> List[BacktestResult]:
        """Evaluate signals against forward returns"""
        results = []
        rows = df.to_dicts()

        # Create timestamp index for fast lookup
        ts_to_idx = {row["timestamp"]: i for i, row in enumerate(rows)}

        for signal in signals:
            idx = ts_to_idx.get(signal.timestamp)
            if idx is None or idx + 10 >= len(rows):
                continue

            entry_price = signal.price

            # Calculate forward returns
            ret_1 = (rows[idx + 1]["close"] - entry_price) / entry_price
            ret_3 = (rows[idx + 3]["close"] - entry_price) / entry_price
            ret_5 = (rows[idx + 5]["close"] - entry_price) / entry_price
            ret_10 = (rows[idx + 10]["close"] - entry_price) / entry_price

            # For bearish signals, flip the sign (we're looking for price drop)
            if signal.direction == "BEARISH":
                ret_1, ret_3, ret_5, ret_10 = -ret_1, -ret_3, -ret_5, -ret_10

            results.append(BacktestResult(
                signal=signal,
                forward_return_1=ret_1,
                forward_return_3=ret_3,
                forward_return_5=ret_5,
                forward_return_10=ret_10,
                hit_1=ret_1 > 0,
                hit_3=ret_3 > 0,
                hit_5=ret_5 > 0,
                hit_10=ret_10 > 0,
            ))

        return results

    def summarize_results(
        self,
        results: List[BacktestResult],
        lookback: int,
    ) -> Optional[LookbackSummary]:
        """Calculate summary statistics for a lookback value"""
        if not results:
            return None

        total = len(results)
        bullish = sum(1 for r in results if r.signal.direction == "BULLISH")
        bearish = total - bullish

        hit_1 = sum(1 for r in results if r.hit_1) / total
        hit_3 = sum(1 for r in results if r.hit_3) / total
        hit_5 = sum(1 for r in results if r.hit_5) / total
        hit_10 = sum(1 for r in results if r.hit_10) / total

        avg_ret_5 = sum(r.forward_return_5 for r in results) / total

        # Profit factor
        wins = [r.forward_return_5 for r in results if r.forward_return_5 > 0]
        losses = [abs(r.forward_return_5) for r in results if r.forward_return_5 < 0]

        total_wins = sum(wins) if wins else 0
        total_losses = sum(losses) if losses else 0.0001
        pf = total_wins / total_losses

        return LookbackSummary(
            lookback=lookback,
            total_signals=total,
            bullish_signals=bullish,
            bearish_signals=bearish,
            hit_rate_1=hit_1,
            hit_rate_3=hit_3,
            hit_rate_5=hit_5,
            hit_rate_10=hit_10,
            avg_return_5=avg_ret_5,
            profit_factor=pf,
        )

    def run_optimization(
        self,
        timeframe: str = "5M",
        lookback_range: Tuple[int, int] = (1, 20),
    ) -> List[LookbackSummary]:
        """Run backtest for all lookback values"""
        df = self.load_data(timeframe=timeframe)

        if len(df) < 1000:
            logger.warning(f"Not enough data for {timeframe}")
            return []

        summaries = []

        print(f"\n{'='*80}")
        print(f"DOM Imbalance Lookback Optimization - {timeframe}")
        print(f"Threshold: DOM > {self.dom_threshold:.0%} = Bullish, < {1-self.dom_threshold:.0%} = Bearish")
        print(f"{'='*80}\n")

        for lookback in range(lookback_range[0], lookback_range[1] + 1):
            signals = self.detect_signals(df, lookback)

            if len(signals) < 10:
                logger.debug(f"Lookback {lookback}: insufficient signals ({len(signals)})")
                continue

            results = self.backtest_signals(df, signals)
            summary = self.summarize_results(results, lookback)

            if summary:
                summaries.append(summary)

        return summaries


def print_results(summaries: List[LookbackSummary], timeframe: str):
    """Print formatted results table"""
    if not summaries:
        print("No results to display")
        return

    print(f"\n{'Lookback':>8} | {'Signals':>7} | {'Hit@1':>6} | {'Hit@3':>6} | {'Hit@5':>6} | {'Hit@10':>6} | {'Avg Ret':>8} | {'PF':>6}")
    print("-" * 80)

    best_pf = max(summaries, key=lambda s: s.profit_factor)
    best_hit5 = max(summaries, key=lambda s: s.hit_rate_5)

    for s in summaries:
        pf_marker = " *" if s.lookback == best_pf.lookback else ""
        hit_marker = " ^" if s.lookback == best_hit5.lookback else ""

        print(f"{s.lookback:>8} | {s.total_signals:>7} | {s.hit_rate_1:>5.1%} | {s.hit_rate_3:>5.1%} | "
              f"{s.hit_rate_5:>5.1%} | {s.hit_rate_10:>5.1%} | {s.avg_return_5*100:>7.3f}% | {s.profit_factor:>5.2f}{pf_marker}{hit_marker}")

    print("-" * 80)
    print(f"* = Best Profit Factor ({best_pf.lookback} bars, PF={best_pf.profit_factor:.2f})")
    print(f"^ = Best Hit Rate @5 ({best_hit5.lookback} bars, {best_hit5.hit_rate_5:.1%})")

    # Recommendations
    print(f"\n{'='*80}")
    print("RECOMMENDATIONS")
    print(f"{'='*80}")

    # Find sweet spot (balance of PF and hit rate)
    scored = [(s, s.profit_factor * s.hit_rate_5 * s.total_signals) for s in summaries if s.total_signals >= 20]
    if scored:
        best_overall = max(scored, key=lambda x: x[1])[0]
        print(f"\nOptimal Lookback for {timeframe}: {best_overall.lookback} bars")
        print(f"  - Hit Rate @5 bars: {best_overall.hit_rate_5:.1%}")
        print(f"  - Profit Factor: {best_overall.profit_factor:.2f}")
        print(f"  - Signal Count: {best_overall.total_signals}")


def main():
    parser = argparse.ArgumentParser(description='Backtest DOM imbalance lookback values')
    parser.add_argument('--timeframe', '-t', default='5M',
                        choices=['5M', '15M', '1H', '4H', '1D'],
                        help='Timeframe to test')
    parser.add_argument('--threshold', type=float, default=0.55,
                        help='DOM threshold (default 0.55 = 55%%)')
    parser.add_argument('--min-lookback', type=int, default=1,
                        help='Minimum lookback to test')
    parser.add_argument('--max-lookback', type=int, default=20,
                        help='Maximum lookback to test')
    parser.add_argument('--all-timeframes', '-a', action='store_true',
                        help='Test all timeframes')

    args = parser.parse_args()

    if args.all_timeframes:
        timeframes = ['5M', '15M', '1H', '4H']
    else:
        timeframes = [args.timeframe]

    for tf in timeframes:
        backtester = DOMBacktester(dom_threshold=args.threshold)
        summaries = backtester.run_optimization(
            timeframe=tf,
            lookback_range=(args.min_lookback, args.max_lookback),
        )
        print_results(summaries, tf)


if __name__ == '__main__':
    main()
