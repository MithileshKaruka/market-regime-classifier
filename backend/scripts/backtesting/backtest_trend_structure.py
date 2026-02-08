#!/usr/bin/env python3
"""
Trend & Structure Signal Backtester

Tests trend-following and market structure signals using OHLCV data only.
Does not require orderflow metrics (MBP ticks).

Signals:
1. EMA Crossovers (12/25 fast trend, 50/200 major trend)
2. Price vs Key EMAs (bounce/rejection at EMA levels)
3. Market Structure (HH/HL for bullish, LH/LL for bearish)
4. RVOL confirmation (volume > average = stronger signal)

Usage:
    python scripts/backtesting/backtest_trend_structure.py --timeframe 15M
    python scripts/backtesting/backtest_trend_structure.py --timeframe 1H --sweep
    python scripts/backtesting/backtest_trend_structure.py --timeframe 15M --show-signals
"""
import os
import sys
from pathlib import Path
import argparse
import logging
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import polars as pl
import numpy as np
from app.data.storage import DuckDBStorage

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class TrendSignal:
    """Detected trend signal"""
    timestamp: datetime
    signal_type: str  # EMA_CROSS, EMA_BOUNCE, STRUCTURE_BREAK, TREND_CONTINUATION
    direction: str  # BULLISH or BEARISH
    price: float
    strength: float  # 0-1 confidence
    details: str


@dataclass
class BacktestResult:
    """Result for a single signal"""
    signal: TrendSignal
    forward_return_1: float
    forward_return_5: float
    forward_return_10: float
    forward_return_20: float
    hit_1: bool
    hit_5: bool
    hit_10: bool
    hit_20: bool


@dataclass
class BacktestSummary:
    """Summary statistics"""
    parameters: dict
    total_signals: int
    by_type: Dict[str, int]
    bullish_signals: int
    bearish_signals: int
    hit_rate_1: float
    hit_rate_5: float
    hit_rate_10: float
    hit_rate_20: float
    avg_return_1: float
    avg_return_5: float
    avg_return_10: float
    avg_return_20: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    by_type_hit_rate: Dict[str, float]


class TrendStructureBacktester:
    """Backtester for trend and structure signals using OHLCV data"""

    def __init__(
        self,
        ema_fast: int = 12,
        ema_slow: int = 25,
        ema_major: int = 50,
        ema_long: int = 200,
        structure_lookback: int = 20,
        rvol_threshold: float = 1.2,
        bounce_tolerance: float = 0.002,  # 0.2% tolerance for EMA bounce
    ):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.ema_major = ema_major
        self.ema_long = ema_long
        self.structure_lookback = structure_lookback
        self.rvol_threshold = rvol_threshold
        self.bounce_tolerance = bounce_tolerance
        self.db = DuckDBStorage()

    def get_parameters(self) -> dict:
        return {
            "ema_fast": self.ema_fast,
            "ema_slow": self.ema_slow,
            "ema_major": self.ema_major,
            "ema_long": self.ema_long,
            "structure_lookback": self.structure_lookback,
            "rvol_threshold": self.rvol_threshold,
            "bounce_tolerance": self.bounce_tolerance,
        }

    def load_data(
        self,
        timeframe: str = "15M",
        symbol: str = "MNQ",
        limit: int = 50000,
    ) -> pl.DataFrame:
        """Load OHLCV data directly from ohlcv_ticks table"""
        query = f"""
            SELECT timestamp, open, high, low, close, volume
            FROM ohlcv_ticks
            WHERE symbol = '{symbol}' AND timeframe = '{timeframe}'
            ORDER BY timestamp ASC
            LIMIT {limit}
        """
        df = self.db.conn.execute(query).pl()
        logger.info(f"Loaded {len(df)} bars for {symbol} {timeframe}")
        return df

    def calculate_emas(self, df: pl.DataFrame) -> pl.DataFrame:
        """Add EMA columns to dataframe"""
        df = df.with_columns([
            pl.col("close").ewm_mean(span=self.ema_fast).alias("ema_fast"),
            pl.col("close").ewm_mean(span=self.ema_slow).alias("ema_slow"),
            pl.col("close").ewm_mean(span=self.ema_major).alias("ema_major"),
            pl.col("close").ewm_mean(span=self.ema_long).alias("ema_long"),
        ])

        # Calculate RVOL
        df = df.with_columns([
            (pl.col("volume") / pl.col("volume").rolling_mean(window_size=20)).alias("rvol"),
        ])

        return df

    def detect_ema_crossovers(self, df: pl.DataFrame) -> List[TrendSignal]:
        """Detect EMA crossover signals"""
        signals = []
        rows = df.to_dicts()

        for i in range(1, len(rows)):
            prev = rows[i - 1]
            curr = rows[i]

            # Skip if EMAs not calculated yet
            if any(v is None for v in [curr["ema_fast"], curr["ema_slow"], prev["ema_fast"], prev["ema_slow"]]):
                continue

            rvol = curr.get("rvol", 1.0) or 1.0
            strength = min(1.0, rvol / 2)  # Higher volume = higher strength

            # Fast/Slow crossover (12/25)
            if prev["ema_fast"] <= prev["ema_slow"] and curr["ema_fast"] > curr["ema_slow"]:
                signals.append(TrendSignal(
                    timestamp=curr["timestamp"],
                    signal_type="EMA_CROSS_12_25",
                    direction="BULLISH",
                    price=curr["close"],
                    strength=strength,
                    details=f"EMA12 crossed above EMA25, RVOL={rvol:.1f}x"
                ))

            elif prev["ema_fast"] >= prev["ema_slow"] and curr["ema_fast"] < curr["ema_slow"]:
                signals.append(TrendSignal(
                    timestamp=curr["timestamp"],
                    signal_type="EMA_CROSS_12_25",
                    direction="BEARISH",
                    price=curr["close"],
                    strength=strength,
                    details=f"EMA12 crossed below EMA25, RVOL={rvol:.1f}x"
                ))

            # Major crossover (50/200) - Golden/Death cross
            if curr["ema_major"] and curr["ema_long"] and prev["ema_major"] and prev["ema_long"]:
                if prev["ema_major"] <= prev["ema_long"] and curr["ema_major"] > curr["ema_long"]:
                    signals.append(TrendSignal(
                        timestamp=curr["timestamp"],
                        signal_type="GOLDEN_CROSS",
                        direction="BULLISH",
                        price=curr["close"],
                        strength=0.9,
                        details="EMA50 crossed above EMA200 (Golden Cross)"
                    ))

                elif prev["ema_major"] >= prev["ema_long"] and curr["ema_major"] < curr["ema_long"]:
                    signals.append(TrendSignal(
                        timestamp=curr["timestamp"],
                        signal_type="DEATH_CROSS",
                        direction="BEARISH",
                        price=curr["close"],
                        strength=0.9,
                        details="EMA50 crossed below EMA200 (Death Cross)"
                    ))

        return signals

    def detect_ema_bounces(self, df: pl.DataFrame) -> List[TrendSignal]:
        """Detect price bouncing off key EMAs"""
        signals = []
        rows = df.to_dicts()

        for i in range(2, len(rows)):
            prev2 = rows[i - 2]
            prev = rows[i - 1]
            curr = rows[i]

            if any(v is None for v in [curr["ema_major"], curr["ema_long"]]):
                continue

            close = curr["close"]
            low = curr["low"]
            high = curr["high"]
            rvol = curr.get("rvol", 1.0) or 1.0

            # Check bounce off EMA50
            ema50 = curr["ema_major"]
            tol = ema50 * self.bounce_tolerance

            # Bullish bounce: price touched EMA50 from above and reversed up
            if prev["low"] <= ema50 + tol and prev["low"] >= ema50 - tol and curr["close"] > prev["close"]:
                if prev2["close"] > ema50:  # Was above EMA before
                    signals.append(TrendSignal(
                        timestamp=curr["timestamp"],
                        signal_type="EMA50_BOUNCE",
                        direction="BULLISH",
                        price=close,
                        strength=min(1.0, rvol / 1.5),
                        details=f"Bullish bounce off EMA50 ({ema50:.2f})"
                    ))

            # Bearish rejection: price touched EMA50 from below and reversed down
            if prev["high"] >= ema50 - tol and prev["high"] <= ema50 + tol and curr["close"] < prev["close"]:
                if prev2["close"] < ema50:  # Was below EMA before
                    signals.append(TrendSignal(
                        timestamp=curr["timestamp"],
                        signal_type="EMA50_REJECT",
                        direction="BEARISH",
                        price=close,
                        strength=min(1.0, rvol / 1.5),
                        details=f"Bearish rejection at EMA50 ({ema50:.2f})"
                    ))

            # Check EMA200 bounce/rejection (stronger signals)
            ema200 = curr["ema_long"]
            if ema200:
                tol200 = ema200 * self.bounce_tolerance

                if prev["low"] <= ema200 + tol200 and prev["low"] >= ema200 - tol200 and curr["close"] > prev["close"]:
                    if prev2["close"] > ema200:
                        signals.append(TrendSignal(
                            timestamp=curr["timestamp"],
                            signal_type="EMA200_BOUNCE",
                            direction="BULLISH",
                            price=close,
                            strength=0.85,
                            details=f"Bullish bounce off EMA200 ({ema200:.2f})"
                        ))

                if prev["high"] >= ema200 - tol200 and prev["high"] <= ema200 + tol200 and curr["close"] < prev["close"]:
                    if prev2["close"] < ema200:
                        signals.append(TrendSignal(
                            timestamp=curr["timestamp"],
                            signal_type="EMA200_REJECT",
                            direction="BEARISH",
                            price=close,
                            strength=0.85,
                            details=f"Bearish rejection at EMA200 ({ema200:.2f})"
                        ))

        return signals

    def detect_structure_breaks(self, df: pl.DataFrame) -> List[TrendSignal]:
        """Detect market structure breaks (HH/HL or LH/LL patterns)"""
        signals = []
        rows = df.to_dicts()
        lookback = self.structure_lookback

        for i in range(lookback * 2, len(rows)):
            curr = rows[i]
            window = rows[i - lookback:i]

            # Find recent swing highs and lows
            highs = [r["high"] for r in window]
            lows = [r["low"] for r in window]

            recent_high = max(highs)
            recent_low = min(lows)

            prev_window = rows[i - lookback * 2:i - lookback]
            prev_highs = [r["high"] for r in prev_window]
            prev_lows = [r["low"] for r in prev_window]

            prev_high = max(prev_highs)
            prev_low = min(prev_lows)

            rvol = curr.get("rvol", 1.0) or 1.0
            strength = min(1.0, rvol / 1.5)

            # Higher High + Higher Low = Bullish structure
            if recent_high > prev_high and recent_low > prev_low:
                # Only signal on breakout candle
                if curr["close"] >= recent_high * 0.999:
                    signals.append(TrendSignal(
                        timestamp=curr["timestamp"],
                        signal_type="HH_HL_BREAK",
                        direction="BULLISH",
                        price=curr["close"],
                        strength=strength,
                        details=f"Higher High ({recent_high:.2f}) + Higher Low ({recent_low:.2f})"
                    ))

            # Lower High + Lower Low = Bearish structure
            if recent_high < prev_high and recent_low < prev_low:
                if curr["close"] <= recent_low * 1.001:
                    signals.append(TrendSignal(
                        timestamp=curr["timestamp"],
                        signal_type="LH_LL_BREAK",
                        direction="BEARISH",
                        price=curr["close"],
                        strength=strength,
                        details=f"Lower High ({recent_high:.2f}) + Lower Low ({recent_low:.2f})"
                    ))

        return signals

    def detect_trend_continuation(self, df: pl.DataFrame) -> List[TrendSignal]:
        """Detect trend continuation pullbacks"""
        signals = []
        rows = df.to_dicts()

        for i in range(25, len(rows)):
            curr = rows[i]
            prev = rows[i - 1]

            if any(v is None for v in [curr["ema_fast"], curr["ema_slow"], curr["ema_major"]]):
                continue

            rvol = curr.get("rvol", 1.0) or 1.0

            # Bullish continuation: EMAs aligned bullish, pullback to fast EMA, bounce
            emas_bullish = curr["ema_fast"] > curr["ema_slow"] > curr["ema_major"]
            if emas_bullish:
                tol = curr["ema_fast"] * self.bounce_tolerance * 2
                if prev["low"] <= curr["ema_fast"] + tol and curr["close"] > prev["close"]:
                    if rvol >= self.rvol_threshold:
                        signals.append(TrendSignal(
                            timestamp=curr["timestamp"],
                            signal_type="TREND_PULLBACK",
                            direction="BULLISH",
                            price=curr["close"],
                            strength=min(1.0, rvol / 2),
                            details=f"Bullish pullback to EMA12, RVOL={rvol:.1f}x"
                        ))

            # Bearish continuation
            emas_bearish = curr["ema_fast"] < curr["ema_slow"] < curr["ema_major"]
            if emas_bearish:
                tol = curr["ema_fast"] * self.bounce_tolerance * 2
                if prev["high"] >= curr["ema_fast"] - tol and curr["close"] < prev["close"]:
                    if rvol >= self.rvol_threshold:
                        signals.append(TrendSignal(
                            timestamp=curr["timestamp"],
                            signal_type="TREND_PULLBACK",
                            direction="BEARISH",
                            price=curr["close"],
                            strength=min(1.0, rvol / 2),
                            details=f"Bearish pullback to EMA12, RVOL={rvol:.1f}x"
                        ))

        return signals

    def detect_signals(self, df: pl.DataFrame) -> List[TrendSignal]:
        """Detect all trend signals"""
        df = self.calculate_emas(df)

        all_signals = []
        all_signals.extend(self.detect_ema_crossovers(df))
        all_signals.extend(self.detect_ema_bounces(df))
        all_signals.extend(self.detect_structure_breaks(df))
        all_signals.extend(self.detect_trend_continuation(df))

        # Sort by timestamp
        all_signals.sort(key=lambda s: s.timestamp)
        return all_signals

    def backtest_signals(
        self,
        df: pl.DataFrame,
        signals: List[TrendSignal],
    ) -> List[BacktestResult]:
        """Calculate forward returns for each signal"""
        results = []

        # Build timestamp -> index map
        ts_to_idx = {}
        rows = df.to_dicts()
        for i, row in enumerate(rows):
            ts_to_idx[row["timestamp"]] = i

        for signal in signals:
            idx = ts_to_idx.get(signal.timestamp)
            if idx is None:
                continue

            # Get forward prices
            price_1 = rows[idx + 1]["close"] if idx + 1 < len(rows) else None
            price_5 = rows[idx + 5]["close"] if idx + 5 < len(rows) else None
            price_10 = rows[idx + 10]["close"] if idx + 10 < len(rows) else None
            price_20 = rows[idx + 20]["close"] if idx + 20 < len(rows) else None

            if price_5 is None:
                continue

            # Calculate returns
            ret_1 = (price_1 - signal.price) / signal.price if price_1 else 0
            ret_5 = (price_5 - signal.price) / signal.price if price_5 else 0
            ret_10 = (price_10 - signal.price) / signal.price if price_10 else 0
            ret_20 = (price_20 - signal.price) / signal.price if price_20 else 0

            # Flip for bearish signals
            if signal.direction == "BEARISH":
                ret_1, ret_5, ret_10, ret_20 = -ret_1, -ret_5, -ret_10, -ret_20

            results.append(BacktestResult(
                signal=signal,
                forward_return_1=ret_1,
                forward_return_5=ret_5,
                forward_return_10=ret_10,
                forward_return_20=ret_20,
                hit_1=ret_1 > 0,
                hit_5=ret_5 > 0,
                hit_10=ret_10 > 0,
                hit_20=ret_20 > 0,
            ))

        return results

    def calculate_summary(
        self,
        results: List[BacktestResult],
    ) -> BacktestSummary:
        """Calculate summary statistics"""
        if not results:
            return None

        total = len(results)
        bullish = sum(1 for r in results if r.signal.direction == "BULLISH")
        bearish = total - bullish

        # By type counts
        by_type = {}
        for r in results:
            by_type[r.signal.signal_type] = by_type.get(r.signal.signal_type, 0) + 1

        # Hit rates
        hit_rate_1 = sum(1 for r in results if r.hit_1) / total * 100
        hit_rate_5 = sum(1 for r in results if r.hit_5) / total * 100
        hit_rate_10 = sum(1 for r in results if r.hit_10) / total * 100
        hit_rate_20 = sum(1 for r in results if r.hit_20) / total * 100

        # Avg returns
        avg_ret_1 = sum(r.forward_return_1 for r in results) / total * 100
        avg_ret_5 = sum(r.forward_return_5 for r in results) / total * 100
        avg_ret_10 = sum(r.forward_return_10 for r in results) / total * 100
        avg_ret_20 = sum(r.forward_return_20 for r in results) / total * 100

        # Win/loss analysis
        wins = [r.forward_return_5 for r in results if r.forward_return_5 > 0]
        losses = [r.forward_return_5 for r in results if r.forward_return_5 < 0]

        avg_win = sum(wins) / len(wins) * 100 if wins else 0
        avg_loss = sum(losses) / len(losses) * 100 if losses else 0

        total_wins = sum(wins)
        total_losses = abs(sum(losses))
        profit_factor = total_wins / total_losses if total_losses > 0 else 0

        # Hit rate by type
        by_type_hit = {}
        for signal_type in by_type.keys():
            type_results = [r for r in results if r.signal.signal_type == signal_type]
            if type_results:
                by_type_hit[signal_type] = sum(1 for r in type_results if r.hit_5) / len(type_results) * 100

        return BacktestSummary(
            parameters=self.get_parameters(),
            total_signals=total,
            by_type=by_type,
            bullish_signals=bullish,
            bearish_signals=bearish,
            hit_rate_1=hit_rate_1,
            hit_rate_5=hit_rate_5,
            hit_rate_10=hit_rate_10,
            hit_rate_20=hit_rate_20,
            avg_return_1=avg_ret_1,
            avg_return_5=avg_ret_5,
            avg_return_10=avg_ret_10,
            avg_return_20=avg_ret_20,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=profit_factor,
            by_type_hit_rate=by_type_hit,
        )

    def run_backtest(
        self,
        timeframe: str = "15M",
        symbol: str = "MNQ",
        limit: int = 50000,
    ) -> Tuple[BacktestSummary, List[TrendSignal], List[BacktestResult]]:
        """Run complete backtest"""
        df = self.load_data(timeframe=timeframe, symbol=symbol, limit=limit)

        if len(df) < 200:
            logger.error("Not enough data")
            return None, [], []

        signals = self.detect_signals(df)
        logger.info(f"Detected {len(signals)} signals")

        results = self.backtest_signals(df, signals)
        summary = self.calculate_summary(results)

        return summary, signals, results


def print_summary(summary: BacktestSummary):
    """Pretty print results"""
    print("\n" + "=" * 70)
    print("TREND & STRUCTURE BACKTEST RESULTS")
    print("=" * 70)

    print(f"\nParameters:")
    for k, v in summary.parameters.items():
        print(f"  {k}: {v}")

    print(f"\n{'-' * 70}")
    print("SIGNAL COUNTS")
    print(f"{'-' * 70}")
    print(f"  Total Signals: {summary.total_signals}")
    print(f"  Bullish: {summary.bullish_signals}")
    print(f"  Bearish: {summary.bearish_signals}")

    print(f"\n  By Type:")
    for t, count in sorted(summary.by_type.items(), key=lambda x: -x[1]):
        hit_rate = summary.by_type_hit_rate.get(t, 0)
        print(f"    {t:<20} {count:>5} signals ({hit_rate:>5.1f}% hit rate)")

    print(f"\n{'-' * 70}")
    print("HIT RATES")
    print(f"{'-' * 70}")
    print(f"  1-bar:  {summary.hit_rate_1:>6.1f}%")
    print(f"  5-bar:  {summary.hit_rate_5:>6.1f}%")
    print(f"  10-bar: {summary.hit_rate_10:>6.1f}%")
    print(f"  20-bar: {summary.hit_rate_20:>6.1f}%")

    print(f"\n{'-' * 70}")
    print("RETURNS")
    print(f"{'-' * 70}")
    print(f"  Avg 1-bar:  {summary.avg_return_1:>8.4f}%")
    print(f"  Avg 5-bar:  {summary.avg_return_5:>8.4f}%")
    print(f"  Avg 10-bar: {summary.avg_return_10:>8.4f}%")
    print(f"  Avg 20-bar: {summary.avg_return_20:>8.4f}%")

    print(f"\n  Avg Win:  {summary.avg_win:>8.4f}%")
    print(f"  Avg Loss: {summary.avg_loss:>8.4f}%")
    print(f"  Profit Factor: {summary.profit_factor:.2f}")

    print(f"\n{'-' * 70}")
    print("INTERPRETATION")
    print(f"{'-' * 70}")

    if summary.hit_rate_5 > 55:
        print(f"  [+] Signals have predictive value (hit rate > 55%)")
    elif summary.hit_rate_5 > 50:
        print(f"  [~] Marginal edge (hit rate 50-55%)")
    else:
        print(f"  [-] No clear edge (hit rate <= 50%)")

    if summary.profit_factor > 1.5:
        print(f"  [+] Strong profit factor ({summary.profit_factor:.2f})")
    elif summary.profit_factor > 1.0:
        print(f"  [~] Positive expectancy ({summary.profit_factor:.2f})")
    else:
        print(f"  [-] Negative expectancy ({summary.profit_factor:.2f})")

    # Best performing signal type
    if summary.by_type_hit_rate:
        best = max(summary.by_type_hit_rate.items(), key=lambda x: x[1])
        if best[1] > 55:
            print(f"  [+] Best signal: {best[0]} ({best[1]:.1f}% hit rate)")

    print("=" * 70)


def print_signals(signals: List[TrendSignal], limit: int = 30):
    """Print individual signals"""
    print("\n" + "=" * 100)
    print("RECENT SIGNALS")
    print("=" * 100)

    print(f"\n{'Timestamp':<20} {'Type':<20} {'Dir':<8} {'Price':>12} {'Str':>6}")
    print("-" * 100)

    for signal in signals[-limit:]:
        ts = signal.timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(signal.timestamp, "strftime") else str(signal.timestamp)[:16]
        print(f"{ts:<20} {signal.signal_type:<20} {signal.direction:<8} {signal.price:>12.2f} {signal.strength:>6.2f}")

    print("-" * 100)
    print(f"Showing {min(limit, len(signals))} of {len(signals)} signals")


def main():
    parser = argparse.ArgumentParser(description="Backtest Trend & Structure Signals")
    parser.add_argument("--timeframe", "-t", default="15M", help="Bar timeframe")
    parser.add_argument("--symbol", "-s", default="MNQ", help="Trading symbol")
    parser.add_argument("--limit", "-l", type=int, default=50000, help="Max bars")
    parser.add_argument("--show-signals", action="store_true", help="Show individual signals")

    args = parser.parse_args()

    backtester = TrendStructureBacktester()
    summary, signals, results = backtester.run_backtest(
        timeframe=args.timeframe,
        symbol=args.symbol,
        limit=args.limit,
    )

    if summary:
        print_summary(summary)
        if args.show_signals:
            print_signals(signals)


if __name__ == "__main__":
    main()
