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

# Add backend directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

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
        # Trade flow parameters
        use_trade_flow: bool = True,
        delta_z_threshold: float = 1.0,  # Require delta z-score above this for direction
        trade_flow_threshold: float = 0.55,  # Trade flow ratio threshold for confirmation
        require_trend_confirm: bool = True,  # Require trend to match delta direction
    ):
        """Initialize backtester with detection parameters

        Args:
            volume_mult: Volume must exceed rolling avg * this multiplier
            range_ratio_max: Price range must be less than this ratio of expected range
            trend_lookback: Bars to look back for determining prior trend direction
            lookback_bars: Bars for calculating rolling volume/range averages
            use_trade_flow: Use trade flow for direction confirmation
            delta_z_threshold: Delta z-score threshold for direction
            trade_flow_threshold: Trade flow ratio threshold for confirmation
            require_trend_confirm: Require trend direction to match delta
        """
        self.volume_mult = volume_mult
        self.range_ratio_max = range_ratio_max
        self.trend_lookback = trend_lookback
        self.lookback_bars = lookback_bars
        # Trade flow params
        self.use_trade_flow = use_trade_flow
        self.delta_z_threshold = delta_z_threshold
        self.trade_flow_threshold = trade_flow_threshold
        self.require_trend_confirm = require_trend_confirm

        self.db = DuckDBStorage()

    def get_parameters(self) -> dict:
        """Return current parameters as dict"""
        return {
            "volume_mult": self.volume_mult,
            "range_ratio_max": self.range_ratio_max,
            "trend_lookback": self.trend_lookback,
            "lookback_bars": self.lookback_bars,
            "use_trade_flow": self.use_trade_flow,
            "delta_z_threshold": self.delta_z_threshold,
            "trade_flow_threshold": self.trade_flow_threshold,
            "require_trend_confirm": self.require_trend_confirm,
        }

    def load_data(
        self,
        timeframe: str = "1M",
        symbol: str = "MNQ",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100000,
    ) -> pl.DataFrame:
        """Load historical OHLCV data with orderflow metrics"""
        where_clauses = [f"symbol = '{symbol}'", f"timeframe = '{timeframe}'"]
        # Only load bars with orderflow data
        where_clauses.append("instant_delta IS NOT NULL AND instant_delta != 0")
        if start_date:
            where_clauses.append(f"timestamp >= '{start_date}'")
        if end_date:
            where_clauses.append(f"timestamp <= '{end_date}'")

        where_str = " AND ".join(where_clauses)

        query = f"""
            SELECT
                timestamp,
                open, high, low, close, volume,
                instant_delta,
                dom_imbalance,
                cvd,
                trade_flow_ratio,
                large_trade_count
            FROM ohlcv_ticks
            WHERE {where_str}
            ORDER BY timestamp ASC
            LIMIT {limit}
        """

        df = self.db.conn.execute(query).pl()
        logger.info(f"Loaded {len(df)} bars with orderflow for {symbol} {timeframe}")
        return df

    def detect_signals(self, df: pl.DataFrame) -> List[ExhaustionSignal]:
        """Detect Exhaustion signals in the data

        Exhaustion Logic:
        1. High volume (spike above average)
        2. Small price range (price didn't move much despite volume)
        3. Determine direction from delta z-score (with trade flow confirmation)
        4. Signal reversal of the exhausted move

        Trade Flow Logic:
        - Strong buying (delta_z > threshold) + small range = buying exhausted = BEARISH
        - Strong selling (delta_z < -threshold) + small range = selling exhausted = BULLISH
        - Optionally confirm with trend direction and trade_flow_ratio
        """
        signals = []
        import math

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
        ])

        # Calculate delta z-score for trade flow mode
        if self.use_trade_flow:
            df = df.with_columns([
                pl.col("instant_delta").rolling_mean(window_size=self.lookback_bars).alias("avg_delta"),
                pl.col("instant_delta").rolling_std(window_size=self.lookback_bars).alias("std_delta"),
            ])
            df = df.with_columns([
                ((pl.col("instant_delta") - pl.col("avg_delta")) /
                 (pl.col("std_delta") + 1)).alias("delta_z")
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
            expected_range = row["avg_range"] * math.sqrt(volume_ratio)
            actual_range = row["bar_range"]

            range_ratio = actual_range / expected_range if expected_range > 0 else 1.0

            # Check for exhaustion (range is small relative to what volume suggests)
            if range_ratio > self.range_ratio_max:
                continue

            # Get delta and trend info
            instant_delta = row.get("instant_delta", 0) or 0
            trend_change = row.get("trend_change", 0) or 0
            delta_direction = "POSITIVE" if instant_delta > 0 else "NEGATIVE"
            prior_trend = "UP" if trend_change > 0 else "DOWN"

            direction = None

            if self.use_trade_flow:
                # Trade flow mode: use delta_z for direction
                delta_z = row.get("delta_z")
                trade_flow = row.get("trade_flow_ratio")

                if delta_z is None:
                    continue

                # Strong BUYING pressure (delta_z > threshold) + small range
                # = Buying exhausted = BEARISH (expect reversal down)
                if delta_z > self.delta_z_threshold:
                    # Optionally require trend to confirm
                    if self.require_trend_confirm and trend_change <= 0:
                        continue  # Trend doesn't confirm buying pressure

                    # Confirm with trade_flow_ratio if available
                    if trade_flow is not None and trade_flow < self.trade_flow_threshold:
                        continue  # Trade flow doesn't confirm buying pressure

                    direction = "BEARISH"

                # Strong SELLING pressure (delta_z < -threshold) + small range
                # = Selling exhausted = BULLISH (expect reversal up)
                elif delta_z < -self.delta_z_threshold:
                    # Optionally require trend to confirm
                    if self.require_trend_confirm and trend_change >= 0:
                        continue  # Trend doesn't confirm selling pressure

                    # Confirm with trade_flow_ratio if available
                    if trade_flow is not None and trade_flow > (1 - self.trade_flow_threshold):
                        continue  # Trade flow doesn't confirm selling pressure

                    direction = "BULLISH"

            else:
                # Legacy mode: use simple delta/trend logic (but with AND instead of OR)
                # If buying exhaustion (positive delta AND uptrend) -> BEARISH
                # If selling exhaustion (negative delta AND downtrend) -> BULLISH
                if instant_delta > 0 and trend_change > 0:
                    direction = "BEARISH"
                elif instant_delta < 0 and trend_change < 0:
                    direction = "BULLISH"

            if direction is None:
                continue

            # Calculate strength
            vol_strength = min(1.0, (volume_ratio - 1) / 2)
            range_strength = 1 - range_ratio  # Lower range = higher strength
            if self.use_trade_flow and row.get("delta_z") is not None:
                delta_strength = min(1.0, abs(row["delta_z"]) / 3)
                strength = (vol_strength + range_strength + delta_strength) / 3
            else:
                strength = (vol_strength + range_strength) / 2

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
        use_trade_flow: bool = True,
    ) -> List[BacktestSummary]:
        """Run parameter sweep to find optimal settings

        Args:
            timeframe: Bar timeframe
            symbol: Trading symbol
            limit: Max bars to load
            use_trade_flow: If True, sweep trade flow params; if False, use legacy mode
        """
        self.use_trade_flow = use_trade_flow
        df = self.load_data(timeframe=timeframe, symbol=symbol, limit=limit)

        if len(df) == 0:
            logger.error("No data loaded")
            return []

        results = []

        if use_trade_flow:
            # Trade flow parameter sweep
            from itertools import product

            volume_mults = [1.5, 1.8, 2.0, 2.5]
            range_ratios = [0.3, 0.4, 0.5, 0.6]
            delta_z_thresholds = [0.5, 1.0, 1.5, 2.0]
            lookbacks = [15, 20, 30]
            trade_flow_thresholds = [0.52, 0.55, 0.60]
            require_trend_confirms = [True, False]

            total_combos = (len(volume_mults) * len(range_ratios) * len(delta_z_thresholds) *
                          len(lookbacks) * len(trade_flow_thresholds) * len(require_trend_confirms))

            logger.info(f"Testing {total_combos} trade flow parameter combinations...")

            combo_count = 0
            for vol, rng, dz, lb, tf, rtc in product(
                volume_mults, range_ratios, delta_z_thresholds, lookbacks, trade_flow_thresholds, require_trend_confirms
            ):
                combo_count += 1
                self.volume_mult = vol
                self.range_ratio_max = rng
                self.delta_z_threshold = dz
                self.lookback_bars = lb
                self.trade_flow_threshold = tf
                self.require_trend_confirm = rtc
                self.trend_lookback = 5  # Fixed for trade flow mode

                signals = self.detect_signals(df.clone())
                if len(signals) < 10:
                    continue

                bt_results = self.calculate_forward_returns(df, signals)
                if len(bt_results) < 10:
                    continue

                summary = self.calculate_summary(bt_results)
                if summary.total_signals >= 10:
                    results.append(summary)

                if combo_count % 100 == 0:
                    logger.info(f"Progress: {combo_count}/{total_combos}")

        else:
            # Legacy parameter sweep
            volume_mults = [1.3, 1.5, 1.8, 2.0, 2.5]
            range_ratios = [0.3, 0.4, 0.5, 0.6, 0.7]
            trend_lookbacks = [3, 5, 10]
            lookbacks = [15, 20, 30]

            total_combos = len(volume_mults) * len(range_ratios) * len(trend_lookbacks) * len(lookbacks)

            logger.info(f"Testing {total_combos} legacy parameter combinations...")

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

        # Sort by profit factor (more useful than hit rate alone)
        results.sort(key=lambda x: (x.profit_factor, x.hit_rate_5), reverse=True)
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

    # Trade flow parameters
    parser.add_argument("--no-trade-flow", action="store_true", help="Use legacy mode instead of trade flow")
    parser.add_argument("--delta-z", type=float, default=1.0, help="Delta z-score threshold")
    parser.add_argument("--trade-flow-threshold", type=float, default=0.55, help="Trade flow ratio threshold")
    parser.add_argument("--no-trend-confirm", action="store_true", help="Don't require trend confirmation")

    args = parser.parse_args()

    use_trade_flow = not args.no_trade_flow

    if args.sweep:
        mode = "trade flow" if use_trade_flow else "legacy"
        print(f"\nRunning Exhaustion parameter sweep ({mode} mode)...")

        backtester = ExhaustionBacktester(use_trade_flow=use_trade_flow)
        results = backtester.run_parameter_sweep(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
            use_trade_flow=use_trade_flow,
        )

        if not results:
            print("No valid parameter combinations found")
        else:
            print(f"\nTop 10 parameter combinations (sorted by profit factor):\n")

            if use_trade_flow:
                print(f"{'VolMult':>8} {'RngMax':>8} {'DeltaZ':>8} {'TF_Thr':>8} {'TrConf':>7} {'LB':>4} {'Sigs':>6} {'Hit5%':>8} {'PF':>8}")
                print("-" * 85)
                for r in results[:10]:
                    p = r.parameters
                    tc = "Yes" if p.get('require_trend_confirm', True) else "No"
                    print(f"{p['volume_mult']:>8.1f} {p['range_ratio_max']:>8.2f} "
                          f"{p.get('delta_z_threshold', 1.0):>8.1f} {p.get('trade_flow_threshold', 0.55):>8.2f} "
                          f"{tc:>7} {p['lookback_bars']:>4} "
                          f"{r.total_signals:>6} {r.hit_rate_5:>8.1f} {r.profit_factor:>8.2f}")
            else:
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
            use_trade_flow=use_trade_flow,
            delta_z_threshold=args.delta_z,
            trade_flow_threshold=args.trade_flow_threshold,
            require_trend_confirm=not args.no_trend_confirm,
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
