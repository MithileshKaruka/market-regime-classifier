#!/usr/bin/env python3
"""
Trades-Based Signals Backtester

Backtests two signals that require actual trade data (not just order book):
1. Institutional Activity: Large trades (>=50 contracts) with directional flow
2. Trade Flow Divergence: Trade flow diverging from price direction (contrarian)

These signals use trade_flow_ratio and large_trade_count columns from ohlcv_ticks,
which are computed from actual trade data (trades table).

Usage:
    python scripts/backtesting/backtest_trades_signals.py --timeframe 5M
    python scripts/backtesting/backtest_trades_signals.py --timeframe 15M --sweep
    python scripts/backtesting/backtest_trades_signals.py --timeframe 1H --show-signals
    python scripts/backtesting/backtest_trades_signals.py --timeframe 5M --signal-type institutional
    python scripts/backtesting/backtest_trades_signals.py --timeframe 5M --signal-type tfd
"""
import os
import sys
from pathlib import Path
import argparse
import logging
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime
from enum import Enum

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import polars as pl
from app.data.storage import DuckDBStorage

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class TradesSignalType(str, Enum):
    INSTITUTIONAL = "Institutional"
    TRADE_FLOW_DIV = "TF Div"


@dataclass
class TradesSignal:
    """Represents a detected trades-based signal"""
    timestamp: datetime
    signal_type: TradesSignalType
    direction: str  # BULLISH or BEARISH
    price: float
    trade_flow_ratio: float  # 0.0=all sells, 1.0=all buys
    large_trade_count: int  # Number of large trades (>=50 contracts)
    price_change_pct: Optional[float]  # For TFD signals
    strength: float


@dataclass
class BacktestResult:
    """Result for a single signal"""
    signal: TradesSignal
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
    signal_type: str
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


class TradesSignalBacktester:
    """Backtester for trades-based signals (Institutional and Trade Flow Divergence)"""

    def __init__(
        self,
        # Institutional Activity params
        inst_large_trade_min: int = 3,       # Min large trades per bar
        inst_flow_threshold: float = 0.65,   # Flow ratio threshold (>0.65 bullish)
        inst_persistence_bars: int = 1,      # Require N consecutive bars with activity
        inst_volume_mult: float = 0.0,       # Require volume > avg * mult (0=disabled)
        # Trade Flow Divergence params
        tfd_flow_threshold: float = 0.60,    # Flow ratio threshold for divergence
        tfd_price_change_pct: float = 0.002, # Min price change (0.2%)
        tfd_lookback_bars: int = 5,          # Bars to measure price change
        tfd_persistence_bars: int = 1,       # Require N consecutive bars with divergence
        tfd_volume_mult: float = 0.0,        # Require volume > avg * mult (0=disabled)
        tfd_flow_avg_bars: int = 1,          # Rolling avg bars for flow (1=no avg)
        # General
        volume_lookback: int = 20,           # Lookback for volume average
    ):
        """Initialize backtester with detection parameters

        Args:
            inst_large_trade_min: Minimum large trades per bar for Institutional signal
            inst_flow_threshold: Trade flow ratio threshold (>threshold=bullish, <1-threshold=bearish)
            inst_persistence_bars: Require N consecutive bars with institutional activity
            inst_volume_mult: Require volume spike (0=disabled)
            tfd_flow_threshold: Flow threshold for divergence detection
            tfd_price_change_pct: Minimum price change to consider directional
            tfd_lookback_bars: Bars to look back for price change calculation
            tfd_persistence_bars: Require N consecutive bars with flow divergence
            tfd_volume_mult: Require volume spike (0=disabled)
            tfd_flow_avg_bars: Rolling average bars for flow ratio smoothing
            volume_lookback: Lookback bars for volume average calculation
        """
        # Institutional params
        self.inst_large_trade_min = inst_large_trade_min
        self.inst_flow_threshold = inst_flow_threshold
        self.inst_persistence_bars = inst_persistence_bars
        self.inst_volume_mult = inst_volume_mult

        # TFD params
        self.tfd_flow_threshold = tfd_flow_threshold
        self.tfd_price_change_pct = tfd_price_change_pct
        self.tfd_lookback_bars = tfd_lookback_bars
        self.tfd_persistence_bars = tfd_persistence_bars
        self.tfd_volume_mult = tfd_volume_mult
        self.tfd_flow_avg_bars = tfd_flow_avg_bars

        # General
        self.volume_lookback = volume_lookback

        self.db = DuckDBStorage()

    def get_parameters(self, signal_type: TradesSignalType) -> dict:
        """Return current parameters as dict for given signal type"""
        if signal_type == TradesSignalType.INSTITUTIONAL:
            params = {
                "inst_large_trade_min": self.inst_large_trade_min,
                "inst_flow_threshold": self.inst_flow_threshold,
            }
            if self.inst_persistence_bars > 1:
                params["inst_persistence_bars"] = self.inst_persistence_bars
            if self.inst_volume_mult > 0:
                params["inst_volume_mult"] = self.inst_volume_mult
            return params
        else:  # TFD
            params = {
                "tfd_flow_threshold": self.tfd_flow_threshold,
                "tfd_price_change_pct": self.tfd_price_change_pct,
                "tfd_lookback_bars": self.tfd_lookback_bars,
            }
            if self.tfd_persistence_bars > 1:
                params["tfd_persistence_bars"] = self.tfd_persistence_bars
            if self.tfd_volume_mult > 0:
                params["tfd_volume_mult"] = self.tfd_volume_mult
            if self.tfd_flow_avg_bars > 1:
                params["tfd_flow_avg_bars"] = self.tfd_flow_avg_bars
            return params

    def load_data(
        self,
        timeframe: str = "5M",
        symbol: str = "MNQ",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100000,
    ) -> pl.DataFrame:
        """Load OHLCV data with trade flow metrics from ohlcv_ticks table"""
        where_clauses = [
            f"symbol = '{symbol}'",
            f"timeframe = '{timeframe}'",
        ]
        if start_date:
            where_clauses.append(f"timestamp >= '{start_date}'")
        if end_date:
            where_clauses.append(f"timestamp <= '{end_date}'")

        where_str = " AND ".join(where_clauses)

        # Query ohlcv_ticks which has trade flow metrics
        query = f"""
            SELECT
                timestamp,
                open,
                high,
                low,
                close,
                volume,
                instant_delta,
                dom_imbalance,
                cvd,
                trade_flow_ratio,
                buy_trades,
                sell_trades,
                large_trade_count
            FROM ohlcv_ticks
            WHERE {where_str}
              AND trade_flow_ratio IS NOT NULL
            ORDER BY timestamp ASC
            LIMIT {limit}
        """

        df = self.db.conn.execute(query).pl()
        logger.info(f"Loaded {len(df)} bars with trade flow data for {symbol} {timeframe}")

        # Log data quality info
        if len(df) > 0:
            non_null = df.filter(pl.col("large_trade_count").is_not_null()).height
            logger.info(f"  Bars with large_trade_count: {non_null}/{len(df)}")

        return df

    def detect_institutional(self, df: pl.DataFrame) -> List[TradesSignal]:
        """Detect Institutional Activity signals

        Institutional Activity: Multiple large trades (>=50 contracts) with
        directional trade flow indicates institutional accumulation/distribution.

        Signal Logic:
        - Large trade count >= threshold (default 3)
        - Trade flow ratio strongly directional (>0.65 bullish, <0.35 bearish)
        - Optional: Require N consecutive bars with activity (persistence filter)
        - Optional: Require volume spike confirmation
        """
        signals = []

        # Add volume average if using volume confirmation
        if self.inst_volume_mult > 0:
            df = df.with_columns([
                pl.col("volume").rolling_mean(window_size=self.volume_lookback).alias("avg_volume"),
            ])

        rows = df.to_dicts()
        persistence_count = 0
        persistence_direction = None

        for i, row in enumerate(rows):
            large_count = row.get("large_trade_count")
            flow_ratio = row.get("trade_flow_ratio")

            # Skip if no trade data
            if large_count is None or flow_ratio is None:
                persistence_count = 0
                persistence_direction = None
                continue

            # Check for significant institutional activity
            if large_count < self.inst_large_trade_min:
                persistence_count = 0
                persistence_direction = None
                continue

            # Determine direction from trade flow
            direction = None
            if flow_ratio > self.inst_flow_threshold:
                direction = "BULLISH"
            elif flow_ratio < (1 - self.inst_flow_threshold):
                direction = "BEARISH"

            if direction is None:
                persistence_count = 0
                persistence_direction = None
                continue

            # Volume confirmation check
            if self.inst_volume_mult > 0:
                avg_vol = row.get("avg_volume")
                vol = row.get("volume", 0) or 0
                if avg_vol is None or avg_vol == 0 or vol < avg_vol * self.inst_volume_mult:
                    persistence_count = 0
                    persistence_direction = None
                    continue

            # Persistence filter - track consecutive bars
            if direction == persistence_direction:
                persistence_count += 1
            else:
                persistence_count = 1
                persistence_direction = direction

            # Only signal if persistence requirement met
            if persistence_count < self.inst_persistence_bars:
                continue

            strength = min(1.0, (abs(flow_ratio - 0.5) * 2) * (large_count / self.inst_large_trade_min) / 2)

            signals.append(TradesSignal(
                timestamp=row["timestamp"],
                signal_type=TradesSignalType.INSTITUTIONAL,
                direction=direction,
                price=row["close"],
                trade_flow_ratio=flow_ratio,
                large_trade_count=large_count,
                price_change_pct=None,
                strength=strength,
            ))

            # Reset persistence to avoid duplicate signals
            persistence_count = 0

        logger.info(f"Detected {len(signals)} Institutional signals")
        return signals

    def detect_trade_flow_divergence(self, df: pl.DataFrame) -> List[TradesSignal]:
        """Detect Trade Flow Divergence signals

        Trade Flow Divergence: When trade flow (buy/sell ratio) diverges from
        price direction, it's a contrarian signal indicating hidden accumulation
        or distribution.

        Signal Logic:
        - Price falling but trade_flow_ratio > threshold = Bullish divergence
        - Price rising but trade_flow_ratio < 1-threshold = Bearish divergence
        - Optional: Require N consecutive bars with divergence (persistence filter)
        - Optional: Require volume spike confirmation
        - Optional: Use rolling average of flow ratio (smoothing)
        """
        signals = []

        if len(df) < self.tfd_lookback_bars + 1:
            return signals

        # Calculate price change over lookback period
        df = df.with_columns([
            ((pl.col("close") - pl.col("close").shift(self.tfd_lookback_bars)) /
             pl.col("close").shift(self.tfd_lookback_bars)).alias("price_change_pct"),
        ])

        # Add rolling average of flow ratio if using smoothing
        if self.tfd_flow_avg_bars > 1:
            df = df.with_columns([
                pl.col("trade_flow_ratio").rolling_mean(window_size=self.tfd_flow_avg_bars).alias("avg_flow_ratio"),
            ])

        # Add volume average if using volume confirmation
        if self.tfd_volume_mult > 0:
            df = df.with_columns([
                pl.col("volume").rolling_mean(window_size=self.volume_lookback).alias("avg_volume"),
            ])

        rows = df.to_dicts()
        persistence_count = 0
        persistence_direction = None

        for i, row in enumerate(rows):
            # Use smoothed flow ratio if available
            if self.tfd_flow_avg_bars > 1:
                flow_ratio = row.get("avg_flow_ratio")
            else:
                flow_ratio = row.get("trade_flow_ratio")
            price_change = row.get("price_change_pct")

            # Skip if no data
            if flow_ratio is None or price_change is None:
                persistence_count = 0
                persistence_direction = None
                continue

            # Volume confirmation check
            if self.tfd_volume_mult > 0:
                avg_vol = row.get("avg_volume")
                vol = row.get("volume", 0) or 0
                if avg_vol is None or avg_vol == 0 or vol < avg_vol * self.tfd_volume_mult:
                    persistence_count = 0
                    persistence_direction = None
                    continue

            # Detect divergence direction
            direction = None
            # Bullish divergence: price falling but buyers dominating
            if price_change < -self.tfd_price_change_pct and flow_ratio > self.tfd_flow_threshold:
                direction = "BULLISH"
            # Bearish divergence: price rising but sellers dominating
            elif price_change > self.tfd_price_change_pct and flow_ratio < (1 - self.tfd_flow_threshold):
                direction = "BEARISH"

            if direction is None:
                persistence_count = 0
                persistence_direction = None
                continue

            # Persistence filter - track consecutive bars
            if direction == persistence_direction:
                persistence_count += 1
            else:
                persistence_count = 1
                persistence_direction = direction

            # Only signal if persistence requirement met
            if persistence_count < self.tfd_persistence_bars:
                continue

            strength = min(1.0, abs(flow_ratio - 0.5) * 2 * abs(price_change) / self.tfd_price_change_pct / 2)

            # Use raw flow ratio for signal data
            raw_flow = row.get("trade_flow_ratio", flow_ratio)

            signals.append(TradesSignal(
                timestamp=row["timestamp"],
                signal_type=TradesSignalType.TRADE_FLOW_DIV,
                direction=direction,
                price=row["close"],
                trade_flow_ratio=raw_flow,
                large_trade_count=row.get("large_trade_count", 0) or 0,
                price_change_pct=price_change,
                strength=strength,
            ))

            # Reset persistence to avoid duplicate signals
            persistence_count = 0

        logger.info(f"Detected {len(signals)} Trade Flow Divergence signals")
        return signals

    def detect_signals(
        self,
        df: pl.DataFrame,
        signal_type: Optional[TradesSignalType] = None
    ) -> List[TradesSignal]:
        """Detect signals of specified type (or both)"""
        signals = []

        if signal_type is None or signal_type == TradesSignalType.INSTITUTIONAL:
            signals.extend(self.detect_institutional(df))

        if signal_type is None or signal_type == TradesSignalType.TRADE_FLOW_DIV:
            signals.extend(self.detect_trade_flow_divergence(df.clone()))

        return signals

    def calculate_forward_returns(
        self,
        df: pl.DataFrame,
        signals: List[TradesSignal]
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

    def calculate_summary(
        self,
        results: List[BacktestResult],
        signal_type: TradesSignalType
    ) -> BacktestSummary:
        """Calculate summary statistics"""
        if not results:
            return BacktestSummary(
                parameters=self.get_parameters(signal_type),
                signal_type=signal_type.value,
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
            parameters=self.get_parameters(signal_type),
            signal_type=signal_type.value,
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

    def run_parameter_sweep_institutional(
        self,
        timeframe: str = "5M",
        symbol: str = "MNQ",
        limit: int = 50000,
        test_improvements: bool = False,
    ) -> List[BacktestSummary]:
        """Run parameter sweep for Institutional signal

        Args:
            test_improvements: If True, also test persistence and volume params
        """
        df = self.load_data(timeframe=timeframe, symbol=symbol, limit=limit)

        if len(df) == 0:
            logger.error("No data loaded")
            return []

        # Parameter ranges to test
        large_trade_mins = [1, 2, 3, 4, 5]
        flow_thresholds = [0.55, 0.60, 0.65, 0.70, 0.75]

        # Additional params for improvements
        if test_improvements:
            persistence_bars_list = [1, 2]
            volume_mults = [0.0, 1.2, 1.5]
        else:
            persistence_bars_list = [1]
            volume_mults = [0.0]

        results = []
        total_combos = len(large_trade_mins) * len(flow_thresholds) * len(persistence_bars_list) * len(volume_mults)

        logger.info(f"Testing {total_combos} Institutional parameter combinations...")

        combo_count = 0
        for ltm in large_trade_mins:
            for ft in flow_thresholds:
                for pb in persistence_bars_list:
                    for vm in volume_mults:
                        combo_count += 1
                        self.inst_large_trade_min = ltm
                        self.inst_flow_threshold = ft
                        self.inst_persistence_bars = pb
                        self.inst_volume_mult = vm

                        signals = self.detect_institutional(df.clone())
                        if len(signals) < 5:  # Lower threshold when testing improvements
                            continue

                        bt_results = self.calculate_forward_returns(df, signals)
                        if len(bt_results) < 5:
                            continue

                        summary = self.calculate_summary(bt_results, TradesSignalType.INSTITUTIONAL)
                        if summary.hit_rate_5 > 50:
                            results.append(summary)

                        if combo_count % 20 == 0:
                            logger.info(f"Progress: {combo_count}/{total_combos}")

        results.sort(key=lambda x: (x.hit_rate_5, x.profit_factor), reverse=True)
        return results

    def run_parameter_sweep_tfd(
        self,
        timeframe: str = "5M",
        symbol: str = "MNQ",
        limit: int = 50000,
        test_improvements: bool = False,
    ) -> List[BacktestSummary]:
        """Run parameter sweep for Trade Flow Divergence signal

        Args:
            test_improvements: If True, also test persistence, volume, and flow avg params
        """
        df = self.load_data(timeframe=timeframe, symbol=symbol, limit=limit)

        if len(df) == 0:
            logger.error("No data loaded")
            return []

        # Parameter ranges to test
        flow_thresholds = [0.55, 0.58, 0.60, 0.62, 0.65, 0.70]
        price_changes = [0.001, 0.0015, 0.002, 0.003, 0.004]
        lookbacks = [3, 5, 7, 10]

        # Additional params for improvements
        if test_improvements:
            persistence_bars_list = [1, 2]
            volume_mults = [0.0, 1.3]
            flow_avg_bars_list = [1, 2]
        else:
            persistence_bars_list = [1]
            volume_mults = [0.0]
            flow_avg_bars_list = [1]

        results = []
        total_combos = (len(flow_thresholds) * len(price_changes) * len(lookbacks) *
                        len(persistence_bars_list) * len(volume_mults) * len(flow_avg_bars_list))

        logger.info(f"Testing {total_combos} TFD parameter combinations...")

        combo_count = 0
        for ft in flow_thresholds:
            for pc in price_changes:
                for lb in lookbacks:
                    for pb in persistence_bars_list:
                        for vm in volume_mults:
                            for fab in flow_avg_bars_list:
                                combo_count += 1
                                self.tfd_flow_threshold = ft
                                self.tfd_price_change_pct = pc
                                self.tfd_lookback_bars = lb
                                self.tfd_persistence_bars = pb
                                self.tfd_volume_mult = vm
                                self.tfd_flow_avg_bars = fab

                                signals = self.detect_trade_flow_divergence(df.clone())
                                if len(signals) < 5:  # Lower threshold when testing improvements
                                    continue

                                bt_results = self.calculate_forward_returns(df, signals)
                                if len(bt_results) < 5:
                                    continue

                                summary = self.calculate_summary(bt_results, TradesSignalType.TRADE_FLOW_DIV)
                                if summary.hit_rate_5 > 50:
                                    results.append(summary)

                                if combo_count % 50 == 0:
                                    logger.info(f"Progress: {combo_count}/{total_combos}")

        results.sort(key=lambda x: (x.hit_rate_5, x.profit_factor), reverse=True)
        return results


def print_summary(summary: BacktestSummary):
    """Pretty print backtest summary"""
    print("\n" + "=" * 60)
    print(f"{summary.signal_type.upper()} BACKTEST RESULTS")
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
    print("\n" + "=" * 120)
    print("SIGNAL DETAILS")
    print("=" * 120)
    print(f"\n{'Timestamp':<20} {'Type':>12} {'Dir':>8} {'Price':>10} {'Flow':>8} {'LargeTr':>8} {'PriceChg':>10} {'Ret 5bar':>10} {'Hit?':>6}")
    print("-" * 120)

    for result in results[:limit]:
        s = result.signal
        hit = "Yes" if result.hit_5 else "No"
        ts_str = s.timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(s.timestamp, "strftime") else str(s.timestamp)[:16]
        price_chg = f"{s.price_change_pct*100:+.2f}%" if s.price_change_pct is not None else "N/A"
        sig_type = "INST" if s.signal_type == TradesSignalType.INSTITUTIONAL else "TFD"

        print(f"{ts_str:<20} {sig_type:>12} {s.direction:>8} {s.price:>10.2f} "
              f"{s.trade_flow_ratio:>8.2f} {s.large_trade_count:>8} {price_chg:>10} "
              f"{result.forward_return_5*100:>10.4f}% {hit:>6}")

    print("-" * 120)
    print(f"Showing {min(limit, len(results))} of {len(results)} signals")


def main():
    parser = argparse.ArgumentParser(description="Backtest trades-based signals (Institutional & TFD)")
    parser.add_argument("--timeframe", "-t", default="5M", help="Bar timeframe")
    parser.add_argument("--symbol", "-s", default="MNQ", help="Trading symbol")
    parser.add_argument("--limit", "-l", type=int, default=50000, help="Max bars")
    parser.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    parser.add_argument("--show-signals", action="store_true", help="Show individual signals")
    parser.add_argument("--signal-type", choices=["institutional", "tfd", "both"], default="both",
                        help="Which signal type to backtest")
    parser.add_argument("--test-improvements", action="store_true",
                        help="Test improvements: persistence filter, volume confirmation, flow smoothing")

    # Institutional params
    parser.add_argument("--inst-large-min", type=int, default=3, help="Min large trades")
    parser.add_argument("--inst-flow", type=float, default=0.65, help="Flow threshold")

    # TFD params
    parser.add_argument("--tfd-flow", type=float, default=0.60, help="TFD flow threshold")
    parser.add_argument("--tfd-price-pct", type=float, default=0.002, help="TFD price change pct")
    parser.add_argument("--tfd-lookback", type=int, default=5, help="TFD lookback bars")

    args = parser.parse_args()

    backtester = TradesSignalBacktester(
        inst_large_trade_min=args.inst_large_min,
        inst_flow_threshold=args.inst_flow,
        tfd_flow_threshold=args.tfd_flow,
        tfd_price_change_pct=args.tfd_price_pct,
        tfd_lookback_bars=args.tfd_lookback,
    )

    if args.sweep:
        # Run parameter sweeps
        if args.signal_type in ["institutional", "both"]:
            print("\n" + "=" * 60)
            mode = " (with improvements)" if args.test_improvements else ""
            print(f"INSTITUTIONAL SIGNAL PARAMETER SWEEP{mode}")
            print("=" * 60)
            results = backtester.run_parameter_sweep_institutional(
                timeframe=args.timeframe,
                symbol=args.symbol,
                limit=args.limit,
                test_improvements=args.test_improvements,
            )

            if not results:
                print("No valid parameter combinations found (all hit rates <= 50%)")
            else:
                print(f"\nTop 10 parameter combinations (by 5-bar hit rate):\n")
                print(f"{'LargeTrMin':>12} {'FlowThresh':>12} {'Signals':>10} {'Hit5%':>10} {'PF':>10}")
                print("-" * 60)
                for r in results[:10]:
                    p = r.parameters
                    print(f"{p['inst_large_trade_min']:>12} {p['inst_flow_threshold']:>12.2f} "
                          f"{r.total_signals:>10} {r.hit_rate_5:>10.1f} {r.profit_factor:>10.2f}")

                print("\nBest parameters:")
                print_summary(results[0])

        if args.signal_type in ["tfd", "both"]:
            print("\n" + "=" * 60)
            mode = " (with improvements)" if args.test_improvements else ""
            print(f"TRADE FLOW DIVERGENCE PARAMETER SWEEP{mode}")
            print("=" * 60)
            results = backtester.run_parameter_sweep_tfd(
                timeframe=args.timeframe,
                symbol=args.symbol,
                limit=args.limit,
                test_improvements=args.test_improvements,
            )

            if not results:
                print("No valid parameter combinations found (all hit rates <= 50%)")
            else:
                print(f"\nTop 10 parameter combinations (by 5-bar hit rate):\n")
                print(f"{'FlowThresh':>12} {'PriceChg%':>12} {'Lookback':>10} {'Signals':>10} {'Hit5%':>10} {'PF':>10}")
                print("-" * 70)
                for r in results[:10]:
                    p = r.parameters
                    print(f"{p['tfd_flow_threshold']:>12.2f} {p['tfd_price_change_pct']*100:>12.3f} "
                          f"{p['tfd_lookback_bars']:>10} {r.total_signals:>10} "
                          f"{r.hit_rate_5:>10.1f} {r.profit_factor:>10.2f}")

                print("\nBest parameters:")
                print_summary(results[0])

    else:
        # Run single backtest
        df = backtester.load_data(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
        )

        if len(df) == 0:
            print("No data loaded. Make sure you have trade flow data in ohlcv_ticks.")
            return

        # Run selected signal types
        if args.signal_type in ["institutional", "both"]:
            signals = backtester.detect_institutional(df.clone())
            results = backtester.calculate_forward_returns(df, signals)
            summary = backtester.calculate_summary(results, TradesSignalType.INSTITUTIONAL)
            print_summary(summary)

            if args.show_signals and results:
                print_signals(results)

        if args.signal_type in ["tfd", "both"]:
            signals = backtester.detect_trade_flow_divergence(df.clone())
            results = backtester.calculate_forward_returns(df, signals)
            summary = backtester.calculate_summary(results, TradesSignalType.TRADE_FLOW_DIV)
            print_summary(summary)

            if args.show_signals and results:
                print_signals(results)


if __name__ == "__main__":
    main()
