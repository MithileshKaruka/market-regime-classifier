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

# Add backend directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

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
    """Backtester for LSF (Liquidity Sweep Fade) signals with orderflow confirmation"""

    def __init__(
        self,
        sweep_threshold_pct: float = 0.001,  # Min % beyond level to count as sweep
        snapback_pct: float = 0.002,  # Min % snapback into range
        snapback_bars: int = 3,  # Max bars to wait for snapback
        lookback_bars: int = 20,  # Bars for rolling high/low
        # Orderflow confirmation parameters
        use_orderflow: bool = False,  # Enable orderflow confirmation
        require_delta_divergence: bool = False,  # Delta must diverge from sweep direction
        require_volume_spike: bool = False,  # Sweep bar must have elevated volume
        volume_mult: float = 1.5,  # Volume must exceed avg by this mult
    ):
        """Initialize backtester with detection parameters

        Args:
            sweep_threshold_pct: Minimum % price must exceed prior high/low
            snapback_pct: Minimum snapback % into prior range
            snapback_bars: Maximum bars to wait for snapback confirmation
            lookback_bars: Bars for calculating rolling high/low levels
            use_orderflow: Enable orderflow confirmation (loads instant_delta, volume)
            require_delta_divergence: Delta must oppose sweep direction (stop hunt pattern)
            require_volume_spike: Sweep bar volume must exceed average
            volume_mult: Volume multiplier threshold for spike detection
        """
        self.sweep_threshold_pct = sweep_threshold_pct
        self.snapback_pct = snapback_pct
        self.snapback_bars = snapback_bars
        self.lookback_bars = lookback_bars
        # Orderflow params
        self.use_orderflow = use_orderflow
        self.require_delta_divergence = require_delta_divergence
        self.require_volume_spike = require_volume_spike
        self.volume_mult = volume_mult

        self.db = DuckDBStorage()

    def get_parameters(self) -> dict:
        """Return current parameters as dict"""
        return {
            "sweep_threshold_pct": self.sweep_threshold_pct,
            "snapback_pct": self.snapback_pct,
            "snapback_bars": self.snapback_bars,
            "lookback_bars": self.lookback_bars,
            "use_orderflow": self.use_orderflow,
            "require_delta_divergence": self.require_delta_divergence,
            "require_volume_spike": self.require_volume_spike,
            "volume_mult": self.volume_mult,
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

        if start_date:
            where_clauses.append(f"timestamp >= '{start_date}'")
        if end_date:
            where_clauses.append(f"timestamp <= '{end_date}'")

        # If using orderflow, require instant_delta to be present
        if self.use_orderflow:
            where_clauses.append("instant_delta IS NOT NULL")

        where_str = " AND ".join(where_clauses)

        # Include orderflow columns if enabled
        if self.use_orderflow:
            query = f"""
                SELECT
                    timestamp,
                    open, high, low, close, volume,
                    instant_delta,
                    trade_flow_ratio,
                    large_trade_count
                FROM ohlcv_ticks
                WHERE {where_str}
                ORDER BY timestamp ASC
                LIMIT {limit}
            """
        else:
            query = f"""
                SELECT
                    timestamp,
                    open, high, low, close, volume
                FROM ohlcv_ticks
                WHERE {where_str}
                ORDER BY timestamp ASC
                LIMIT {limit}
            """

        df = self.db.conn.execute(query).pl()

        logger.info(f"Loaded {len(df)} bars for {symbol} {timeframe}")
        return df

    def detect_signals(self, df: pl.DataFrame) -> List[LSFSignal]:
        """Detect LSF signals in the data

        LSF Logic:
        1. Price sweeps beyond prior high/low (liquidity grab)
        2. Price snaps back into prior range within N bars (fade)

        Orderflow Confirmations (optional):
        - Delta divergence: Delta opposes sweep direction (stop hunt pattern)
          - Sweep high + negative delta = strong bearish (sellers already in control)
          - Sweep low + positive delta = strong bullish (buyers already in control)
        - Volume spike: Elevated volume on sweep bar confirms liquidity grab
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

        # Calculate rolling volume average if using orderflow
        if self.use_orderflow and self.require_volume_spike:
            df = df.with_columns([
                pl.col("volume").rolling_mean(window_size=self.lookback_bars).alias("avg_volume"),
            ])

        rows = df.to_dicts()
        has_delta = "instant_delta" in df.columns
        has_volume = "avg_volume" in df.columns

        for i in range(self.lookback_bars, len(rows) - self.snapback_bars - 1):
            row = rows[i]

            if row["prior_high"] is None or row["prior_low"] is None:
                continue
            if row["prior_high"] == 0 or row["prior_low"] == 0:
                continue

            # Get orderflow data for sweep bar
            instant_delta = row.get("instant_delta", 0) or 0
            avg_volume = row.get("avg_volume")
            volume = row.get("volume", 0) or 0

            # Check volume spike if required
            if self.require_volume_spike and has_volume:
                if avg_volume is None or avg_volume == 0:
                    continue
                if volume < avg_volume * self.volume_mult:
                    continue  # No volume spike, skip

            # Check for bearish LSF (sweep high then reverse down)
            sweep_depth_high = (row["high"] - row["prior_high"]) / row["prior_high"]
            if sweep_depth_high > self.sweep_threshold_pct:
                # Check delta divergence if required
                # For bearish LSF: delta should be negative (sellers already winning despite price sweep up)
                if self.require_delta_divergence and has_delta:
                    if instant_delta >= 0:
                        continue  # Delta doesn't diverge, skip

                # Look for snapback within N bars
                for j in range(1, self.snapback_bars + 1):
                    future_row = rows[i + j]
                    snapback_pct = (row["high"] - future_row["close"]) / row["high"]

                    if snapback_pct > self.snapback_pct:
                        strength = min(1.0, snapback_pct / (self.snapback_pct * 3))
                        # Boost strength if delta diverged
                        if has_delta and instant_delta < 0:
                            strength = min(1.0, strength * 1.2)
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
                # Check delta divergence if required
                # For bullish LSF: delta should be positive (buyers already winning despite price sweep down)
                if self.require_delta_divergence and has_delta:
                    if instant_delta <= 0:
                        continue  # Delta doesn't diverge, skip

                # Look for snapback within N bars
                for j in range(1, self.snapback_bars + 1):
                    future_row = rows[i + j]
                    snapback_pct = (future_row["close"] - row["low"]) / row["low"]

                    if snapback_pct > self.snapback_pct:
                        strength = min(1.0, snapback_pct / (self.snapback_pct * 3))
                        # Boost strength if delta diverged
                        if has_delta and instant_delta > 0:
                            strength = min(1.0, strength * 1.2)
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
        mode: str = "price",  # "price", "delta_div", "volume", "both"
    ) -> List[BacktestSummary]:
        """Run parameter sweep to find optimal settings

        Args:
            timeframe: Bar timeframe
            symbol: Trading symbol
            limit: Max bars to load
            mode: Detection mode
                - "price": Pure price-based LSF
                - "delta_div": Require delta divergence at sweep
                - "volume": Require volume spike at sweep
                - "both": Require both delta divergence and volume spike
        """
        # Set orderflow mode based on parameter
        self.use_orderflow = mode != "price"
        self.require_delta_divergence = mode in ("delta_div", "both")
        self.require_volume_spike = mode in ("volume", "both")

        df = self.load_data(timeframe=timeframe, symbol=symbol, limit=limit)

        if len(df) == 0:
            logger.error("No data loaded")
            return []

        # Parameter ranges to test
        sweep_thresholds = [0.0005, 0.001, 0.0015, 0.002, 0.003]
        snapback_pcts = [0.001, 0.0015, 0.002, 0.003, 0.005]
        snapback_bars_list = [1, 2, 3, 5]
        lookbacks = [10, 15, 20, 30]

        # Volume multipliers if using volume mode
        if self.require_volume_spike:
            volume_mults = [1.2, 1.5, 1.8, 2.0]
        else:
            volume_mults = [1.5]  # Default, not used

        results = []
        from itertools import product

        total_combos = len(sweep_thresholds) * len(snapback_pcts) * len(snapback_bars_list) * len(lookbacks)
        if self.require_volume_spike:
            total_combos *= len(volume_mults)

        logger.info(f"Testing {total_combos} parameter combinations ({mode} mode)...")

        combo_count = 0
        for sweep_thresh, snap_pct, snap_bars, lb in product(
            sweep_thresholds, snapback_pcts, snapback_bars_list, lookbacks
        ):
            for vol_mult in volume_mults:
                combo_count += 1
                self.sweep_threshold_pct = sweep_thresh
                self.snapback_pct = snap_pct
                self.snapback_bars = snap_bars
                self.lookback_bars = lb
                self.volume_mult = vol_mult

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

        # Sort by profit factor (more useful than hit rate alone)
        results.sort(key=lambda x: (x.profit_factor, x.hit_rate_5), reverse=True)
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

    # Orderflow confirmation modes
    parser.add_argument("--mode", choices=["price", "delta_div", "volume", "both"], default="price",
                        help="Detection mode: price (pure price), delta_div (require delta divergence), "
                             "volume (require volume spike), both (require both)")
    parser.add_argument("--volume-mult", type=float, default=1.5, help="Volume multiplier for spike detection")

    args = parser.parse_args()

    if args.sweep:
        mode_desc = {
            "price": "Pure Price",
            "delta_div": "Delta Divergence",
            "volume": "Volume Spike",
            "both": "Delta Div + Volume"
        }
        print(f"\nRunning LSF parameter sweep ({mode_desc[args.mode]} mode)...")

        backtester = LSFBacktester()
        results = backtester.run_parameter_sweep(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
            mode=args.mode,
        )

        if not results:
            print("No valid parameter combinations found")
        else:
            print(f"\nTop 10 parameter combinations (sorted by profit factor):\n")

            if args.mode in ("volume", "both"):
                print(f"{'Sweep%':>8} {'Snap%':>8} {'SnapB':>6} {'LB':>6} {'VolM':>6} {'Sigs':>6} {'Hit5%':>8} {'PF':>8}")
                print("-" * 70)
                for r in results[:10]:
                    p = r.parameters
                    print(f"{p['sweep_threshold_pct']*100:>8.2f} {p['snapback_pct']*100:>8.2f} "
                          f"{p['snapback_bars']:>6} {p['lookback_bars']:>6} {p['volume_mult']:>6.1f} "
                          f"{r.total_signals:>6} {r.hit_rate_5:>8.1f} {r.profit_factor:>8.2f}")
            else:
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
        use_orderflow = args.mode != "price"
        backtester = LSFBacktester(
            sweep_threshold_pct=args.sweep_threshold,
            snapback_pct=args.snapback_pct,
            snapback_bars=args.snapback_bars,
            lookback_bars=args.lookback,
            use_orderflow=use_orderflow,
            require_delta_divergence=args.mode in ("delta_div", "both"),
            require_volume_spike=args.mode in ("volume", "both"),
            volume_mult=args.volume_mult,
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
