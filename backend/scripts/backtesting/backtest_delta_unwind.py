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

Modes:
- delta_only: Pure delta-based detection (baseline)
- trade_flow: Require trade_flow_ratio to confirm unwind direction
- volume: Require elevated volume during unwind
- dom: Require DOM imbalance to support unwind direction
- all: Combine all confirmations

Usage:
    python scripts/backtest_delta_unwind.py --timeframe 15M --sweep --mode delta_only
    python scripts/backtest_delta_unwind.py --timeframe 15M --sweep --mode trade_flow
    python scripts/backtest_delta_unwind.py --timeframe 15M --sweep --mode all
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

    # Valid modes for signal detection
    MODES = ["delta_only", "trade_flow", "volume", "dom", "both", "all"]

    def __init__(
        self,
        zscore_threshold: float = 2.0,  # Z-score threshold for "extreme" delta
        unwind_pct: float = 0.1,  # Min % of delta that must unwind
        unwind_bars: int = 3,  # Bars to look for unwind confirmation
        lookback_bars: int = 50,  # Bars for rolling stats
        mode: str = "delta_only",  # Detection mode
        # Mode-specific parameters
        tf_threshold: float = 0.55,  # Trade flow ratio threshold (>0.5 = more buys)
        vol_mult: float = 1.5,  # Volume multiplier for unwind bar
        dom_threshold: float = 0.1,  # DOM imbalance threshold (positive = bid heavy)
        large_trade_min: int = 1,  # Min large trades during unwind
    ):
        """Initialize backtester with detection parameters

        Args:
            zscore_threshold: Cumulative delta must exceed this z-score
            unwind_pct: Minimum % of peak delta that must unwind
            unwind_bars: Bars to confirm unwind is happening
            lookback_bars: Bars for calculating rolling mean/std
            mode: Detection mode (delta_only, trade_flow, volume, dom, all)
            tf_threshold: Trade flow ratio threshold for confirmation
            vol_mult: Volume multiplier threshold
            dom_threshold: DOM imbalance threshold for confirmation
            large_trade_min: Minimum large trades during unwind
        """
        self.zscore_threshold = zscore_threshold
        self.unwind_pct = unwind_pct
        self.unwind_bars = unwind_bars
        self.lookback_bars = lookback_bars
        self.mode = mode if mode in self.MODES else "delta_only"
        self.tf_threshold = tf_threshold
        self.vol_mult = vol_mult
        self.dom_threshold = dom_threshold
        self.large_trade_min = large_trade_min

        self.db = DuckDBStorage()

    def get_parameters(self) -> dict:
        """Return current parameters as dict"""
        params = {
            "mode": self.mode,
            "zscore_threshold": self.zscore_threshold,
            "unwind_pct": self.unwind_pct,
            "unwind_bars": self.unwind_bars,
            "lookback_bars": self.lookback_bars,
        }
        # Add mode-specific params
        if self.mode in ["trade_flow", "both", "all"]:
            params["tf_threshold"] = self.tf_threshold
        if self.mode in ["volume", "both", "all"]:
            params["vol_mult"] = self.vol_mult
        if self.mode in ["dom", "all"]:
            params["dom_threshold"] = self.dom_threshold
        return params

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
        # Only load bars with orderflow data (instant_delta is not null)
        where_clauses.append("instant_delta IS NOT NULL")
        if start_date:
            where_clauses.append(f"timestamp >= '{start_date}'")
        if end_date:
            where_clauses.append(f"timestamp <= '{end_date}'")

        where_str = " AND ".join(where_clauses)

        query = f"""
            SELECT
                timestamp,
                open, high, low, close, volume,
                instant_delta as bar_delta,
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

        # Log data availability for mode columns
        if self.mode != "delta_only":
            tf_count = df.filter(pl.col("trade_flow_ratio").is_not_null()).height
            dom_count = df.filter(pl.col("dom_imbalance").is_not_null()).height
            lt_count = df.filter(pl.col("large_trade_count").is_not_null()).height
            logger.info(f"Mode data availability - trade_flow: {tf_count}, dom: {dom_count}, large_trades: {lt_count}")

        return df

    def _check_mode_confirmations(
        self,
        future_row: dict,
        direction: str,
        avg_volume: float,
    ) -> bool:
        """Check mode-specific confirmations for unwind signal

        Args:
            future_row: The bar data at signal time
            direction: BULLISH or BEARISH
            avg_volume: Rolling average volume for comparison

        Returns:
            True if all mode confirmations pass
        """
        if self.mode == "delta_only":
            return True

        # Trade flow confirmation: ratio should support unwind direction
        if self.mode in ["trade_flow", "both", "all"]:
            tf_ratio = future_row.get("trade_flow_ratio")
            if tf_ratio is not None:
                # BULLISH unwind: need more buying (tf_ratio > threshold)
                # BEARISH unwind: need more selling (tf_ratio < 1 - threshold)
                if direction == "BULLISH" and tf_ratio < self.tf_threshold:
                    return False
                if direction == "BEARISH" and tf_ratio > (1 - self.tf_threshold):
                    return False

        # Volume confirmation: elevated volume during unwind
        if self.mode in ["volume", "both", "all"]:
            volume = future_row.get("volume")
            if volume is not None and avg_volume > 0:
                if volume < avg_volume * self.vol_mult:
                    return False

        # DOM confirmation: order book should support unwind direction
        if self.mode in ["dom", "all"]:
            dom = future_row.get("dom_imbalance")
            if dom is not None:
                # BULLISH unwind: need positive DOM (more bids)
                # BEARISH unwind: need negative DOM (more asks)
                if direction == "BULLISH" and dom < self.dom_threshold:
                    return False
                if direction == "BEARISH" and dom > -self.dom_threshold:
                    return False

        return True

    def detect_signals(self, df: pl.DataFrame) -> List[DeltaUnwindSignal]:
        """Detect Delta Unwind signals in the data

        Delta Unwind Logic:
        1. Calculate cumulative delta
        2. Find when cumulative delta reaches extreme (high z-score)
        3. Detect when delta starts unwinding (reversing)
        4. Apply mode-specific confirmations
        5. Signal in direction of unwind
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
            pl.col("volume").rolling_mean(window_size=self.lookback_bars).alias("avg_volume"),
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
            avg_volume = row.get("avg_volume", 0) or 0

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
                        # Check mode-specific confirmations
                        if not self._check_mode_confirmations(future_row, "BEARISH", avg_volume):
                            continue

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
                        # Check mode-specific confirmations
                        if not self._check_mode_confirmations(future_row, "BULLISH", avg_volume):
                            continue

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

        logger.info(f"Detected {len(signals)} Delta Unwind signals (mode: {self.mode})")
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

        # Core parameter ranges
        zscore_thresholds = [1.5, 2.0, 2.5, 3.0]
        unwind_pcts = [0.05, 0.10, 0.15, 0.20, 0.30]
        unwind_bars_list = [2, 3, 5, 8]
        lookbacks = [30, 50, 100]

        # Mode-specific parameter ranges
        tf_thresholds = [0.55] if self.mode not in ["trade_flow", "both", "all"] else [0.52, 0.55, 0.58, 0.60]
        vol_mults = [1.5] if self.mode not in ["volume", "both", "all"] else [1.2, 1.5, 1.8, 2.0]
        dom_thresholds = [0.1] if self.mode not in ["dom", "all"] else [0.05, 0.10, 0.15, 0.20]

        results = []
        total_combos = (
            len(zscore_thresholds) * len(unwind_pcts) * len(unwind_bars_list) *
            len(lookbacks) * len(tf_thresholds) * len(vol_mults) * len(dom_thresholds)
        )

        logger.info(f"Testing {total_combos} parameter combinations for mode '{self.mode}'...")

        combo_count = 0
        for zscore in zscore_thresholds:
            for unwind in unwind_pcts:
                for bars in unwind_bars_list:
                    for lb in lookbacks:
                        for tf_thr in tf_thresholds:
                            for vol_m in vol_mults:
                                for dom_thr in dom_thresholds:
                                    combo_count += 1
                                    self.zscore_threshold = zscore
                                    self.unwind_pct = unwind
                                    self.unwind_bars = bars
                                    self.lookback_bars = lb
                                    self.tf_threshold = tf_thr
                                    self.vol_mult = vol_m
                                    self.dom_threshold = dom_thr

                                    signals = self.detect_signals(df.clone())
                                    if len(signals) < 5:  # Lower threshold for filtered modes
                                        continue

                                    bt_results = self.calculate_forward_returns(df, signals)
                                    if len(bt_results) < 5:
                                        continue

                                    summary = self.calculate_summary(bt_results)
                                    if summary.hit_rate_5 > 50:
                                        results.append(summary)

                                    if combo_count % 100 == 0:
                                        logger.info(f"Progress: {combo_count}/{total_combos}")

        results.sort(key=lambda x: (x.profit_factor, x.hit_rate_5), reverse=True)
        return results


def print_summary(summary: BacktestSummary):
    """Pretty print backtest summary"""
    mode = summary.parameters.get("mode", "delta_only")
    print("\n" + "=" * 60)
    print(f"DELTA UNWIND BACKTEST RESULTS (mode: {mode})")
    print("=" * 60)
    print(f"\nParameters:")
    for k, v in summary.parameters.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.3f}")
        else:
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

    # Mode selection
    parser.add_argument("--mode", "-m", default="delta_only",
                       choices=DeltaUnwindBacktester.MODES,
                       help="Detection mode: delta_only, trade_flow, volume, dom, all")

    # Core detection parameters
    parser.add_argument("--zscore", type=float, default=2.0, help="Z-score threshold for extreme")
    parser.add_argument("--unwind-pct", type=float, default=0.1, help="Min %% of delta that must unwind")
    parser.add_argument("--unwind-bars", type=int, default=3, help="Bars to confirm unwind")
    parser.add_argument("--lookback", type=int, default=50, help="Bars for rolling stats")

    # Mode-specific parameters
    parser.add_argument("--tf-threshold", type=float, default=0.55, help="Trade flow ratio threshold")
    parser.add_argument("--vol-mult", type=float, default=1.5, help="Volume multiplier for unwind bar")
    parser.add_argument("--dom-threshold", type=float, default=0.1, help="DOM imbalance threshold")

    args = parser.parse_args()

    if args.sweep:
        print(f"\nRunning Delta Unwind parameter sweep (mode: {args.mode})...")
        backtester = DeltaUnwindBacktester(mode=args.mode)
        results = backtester.run_parameter_sweep(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
        )

        if not results:
            print(f"No valid parameter combinations found for mode '{args.mode}' (all hit rates <= 50%)")
        else:
            print(f"\nTop 10 parameter combinations (mode: {args.mode}, sorted by PF):\n")

            # Build header based on mode
            header = f"{'Z-Score':>8} {'Unwind%':>8} {'Bars':>6} {'LB':>6}"
            if args.mode in ["trade_flow", "both", "all"]:
                header += f" {'TF_Thr':>7}"
            if args.mode in ["volume", "both", "all"]:
                header += f" {'VolM':>6}"
            if args.mode in ["dom", "all"]:
                header += f" {'DOM':>6}"
            header += f" {'Sigs':>6} {'Hit5%':>8} {'PF':>8}"
            print(header)
            print("-" * len(header))

            for r in results[:10]:
                p = r.parameters
                row = f"{p['zscore_threshold']:>8.1f} {p['unwind_pct']*100:>8.1f} "
                row += f"{p['unwind_bars']:>6} {p['lookback_bars']:>6}"
                if args.mode in ["trade_flow", "both", "all"]:
                    row += f" {p.get('tf_threshold', 0.55):>7.2f}"
                if args.mode in ["volume", "both", "all"]:
                    row += f" {p.get('vol_mult', 1.5):>6.1f}"
                if args.mode in ["dom", "all"]:
                    row += f" {p.get('dom_threshold', 0.1):>6.2f}"
                row += f" {r.total_signals:>6} {r.hit_rate_5:>8.1f} {r.profit_factor:>8.2f}"
                print(row)

            print("\n" + "=" * 60)
            print("Best parameters:")
            print_summary(results[0])

    else:
        backtester = DeltaUnwindBacktester(
            zscore_threshold=args.zscore,
            unwind_pct=args.unwind_pct,
            unwind_bars=args.unwind_bars,
            lookback_bars=args.lookback,
            mode=args.mode,
            tf_threshold=args.tf_threshold,
            vol_mult=args.vol_mult,
            dom_threshold=args.dom_threshold,
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
