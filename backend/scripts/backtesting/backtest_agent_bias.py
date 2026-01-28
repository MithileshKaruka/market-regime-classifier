#!/usr/bin/env python3
"""
Agent Bias Score Backtester

Tests the predictive accuracy of the Agent Bias Score (0-100) regime classifier.
Unlike signal backtesters, this tests whether the bias score correctly predicts
market direction at each bar.

Key Metrics:
- Score Direction Accuracy: Does score > 55 predict up moves? Does score < 45 predict down?
- Mode Transition Performance: Forward returns when entering each mode
- Component Agreement: Performance when all components align vs conflict

Usage:
    python scripts/backtesting/backtest_agent_bias.py --timeframe 15M
    python scripts/backtesting/backtest_agent_bias.py --timeframe 5M --sweep
    python scripts/backtesting/backtest_agent_bias.py --timeframe 1H --show-transitions
"""
import os
import sys
from pathlib import Path
import argparse
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple
from datetime import datetime

# Add parent directories to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import polars as pl
from app.data.storage import DuckDBStorage
from app.features.agent_bias import AgentBiasCalculator, AgentMode
from app.features.orderflow_signals import OrderflowSignalDetector
from app.features.orderflow_metrics import OrderflowMetricsCalculator

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class BiasSnapshot:
    """Agent Bias state at a single bar"""
    timestamp: datetime
    price: float
    total_score: float
    mode: str
    trend_score: float
    intensity_score: float
    orderflow_score: float
    orderflow_mode: str
    confidence: str
    active_signals: List[str]


@dataclass
class ModeTransition:
    """Represents a mode change"""
    timestamp: datetime
    price: float
    from_mode: str
    to_mode: str
    score: float
    confidence: str


@dataclass
class TransitionResult:
    """Forward return result for a mode transition"""
    transition: ModeTransition
    forward_return_1: float
    forward_return_5: float
    forward_return_10: float
    forward_return_20: float
    hit_5: bool  # Did price move in predicted direction?
    hit_10: bool
    hit_20: bool


@dataclass
class ScoreAccuracyResult:
    """Result for score direction accuracy at each bar"""
    timestamp: datetime
    score: float
    predicted_direction: str  # BULLISH, BEARISH, or NEUTRAL
    actual_direction_5: str  # What price actually did over 5 bars
    actual_direction_10: str
    correct_5: bool
    correct_10: bool


@dataclass
class BacktestSummary:
    """Complete backtest summary"""
    parameters: dict
    total_bars: int

    # Score Direction Accuracy
    bullish_predictions: int
    bearish_predictions: int
    neutral_predictions: int
    bullish_accuracy_5: float
    bullish_accuracy_10: float
    bearish_accuracy_5: float
    bearish_accuracy_10: float

    # Mode Transition Performance
    mode_transitions: int
    transitions_by_mode: Dict[str, int]
    hit_rate_by_mode_5: Dict[str, float]
    hit_rate_by_mode_10: Dict[str, float]
    avg_return_by_mode_5: Dict[str, float]

    # Overall Performance
    overall_hit_rate_5: float
    overall_hit_rate_10: float
    avg_return_5: float
    avg_return_10: float
    profit_factor: float

    # High Conviction Performance (score > 70 or < 30)
    high_conviction_signals: int
    high_conviction_hit_rate: float
    high_conviction_avg_return: float


class AgentBiasBacktester:
    """Backtester for Agent Bias Score"""

    def __init__(
        self,
        bullish_threshold: float = 55.0,  # Score above this = bullish prediction
        bearish_threshold: float = 45.0,  # Score below this = bearish prediction
        high_conviction_threshold: float = 70.0,  # Score > this or < 30 = high conviction
        min_mode_duration: int = 3,  # Min bars before mode transition counts
    ):
        """Initialize backtester

        Args:
            bullish_threshold: Score threshold for bullish prediction (default 55)
            bearish_threshold: Score threshold for bearish prediction (default 45)
            high_conviction_threshold: Threshold for high conviction (score > this or < 100-this)
            min_mode_duration: Minimum bars a mode must last before transition counts
        """
        self.bullish_threshold = bullish_threshold
        self.bearish_threshold = bearish_threshold
        self.high_conviction_threshold = high_conviction_threshold
        self.min_mode_duration = min_mode_duration

        self.db = DuckDBStorage()
        self.bias_calculator = AgentBiasCalculator()

    def get_parameters(self) -> dict:
        """Return current parameters"""
        return {
            "bullish_threshold": self.bullish_threshold,
            "bearish_threshold": self.bearish_threshold,
            "high_conviction_threshold": self.high_conviction_threshold,
            "min_mode_duration": self.min_mode_duration,
        }

    def load_data(
        self,
        timeframe: str = "15M",
        symbol: str = "MNQ",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 10000,
    ) -> pl.DataFrame:
        """Load historical data with all metrics needed for bias calculation"""
        tf_map = {
            "1M": "1 minute",
            "5M": "5 minutes",
            "15M": "15 minutes",
            "30M": "30 minutes",
            "1H": "1 hour",
            "4H": "4 hours",
            "1D": "1 day",
        }
        interval = tf_map.get(timeframe, "15 minutes")

        where_clauses = [f"symbol = '{symbol}'"]
        if start_date:
            where_clauses.append(f"timestamp >= '{start_date}'")
        if end_date:
            where_clauses.append(f"timestamp <= '{end_date}'")

        where_str = " AND ".join(where_clauses)

        # Aggregate MBP ticks into bars with all needed metrics
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
                    AVG(dom_imbalance) as dom_imbalance,
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

        if "bar_time" in df.columns:
            df = df.rename({"bar_time": "timestamp"})

        # Calculate cumulative delta
        df = df.with_columns([
            pl.col("bar_delta").cum_sum().alias("cum_delta"),
        ])

        # Calculate total bid/ask depth for LDR
        if "total_bid_depth" in df.columns and "total_ask_depth" in df.columns:
            df = df.with_columns([
                (pl.col("total_bid_depth") / pl.col("total_ask_depth").replace(0, 1)).alias("ldr"),
            ])
        else:
            # Fallback: use dom_imbalance to estimate
            df = df.with_columns([
                (pl.col("dom_imbalance") / (1 - pl.col("dom_imbalance")).replace(0, 0.5)).alias("ldr"),
            ])

        logger.info(f"Loaded {len(df)} bars for {symbol} {timeframe}")
        return df

    def calculate_bias_series(
        self,
        df: pl.DataFrame,
        timeframe: str = "15M",
    ) -> List[BiasSnapshot]:
        """Calculate Agent Bias Score at each bar"""
        snapshots = []

        if len(df) < 50:  # Need minimum bars for calculations
            logger.warning("Not enough data for bias calculation")
            return snapshots

        signal_detector = OrderflowSignalDetector(timeframe=timeframe)

        rows = df.to_dicts()

        # Process each bar (need lookback for calculations)
        for i in range(50, len(rows)):
            try:
                # Get lookback window
                lookback_df = df.slice(max(0, i - 100), 100)
                recent_df = df.slice(max(0, i - 20), 20)

                current_row = rows[i]

                # Calculate metrics for this bar
                rvol = self._calculate_rvol(df.slice(max(0, i - 20), 21))
                vpin = self._calculate_vpin(df.slice(max(0, i - 50), 51))
                ldr = current_row.get("ldr", 1.0)
                cvd = current_row.get("cum_delta", 0)

                # Detect signals in recent window
                absorption_signals = signal_detector.detect_absorption(recent_df)
                delta_unwind_signals = signal_detector.detect_delta_unwind(recent_df)
                exhaustion_signals = signal_detector.detect_exhaustion(recent_df)

                abs_dicts = [{"direction": s.direction.value, "strength": s.strength} for s in absorption_signals]
                du_dicts = [{"direction": s.direction.value, "strength": s.strength} for s in delta_unwind_signals]
                exh_dicts = [{"direction": s.direction.value, "strength": s.strength} for s in exhaustion_signals]

                # Calculate bias
                bias_result = self.bias_calculator.calculate_total_bias(
                    df=lookback_df,
                    sr_levels=None,
                    rvol=rvol,
                    vpin=vpin,
                    obi_ratio=ldr,
                    ldr=ldr,
                    absorption_signals=abs_dicts,
                    delta_unwind_signals=du_dicts,
                    exhaustion_signals=exh_dicts,
                    cvd=cvd,
                )

                snapshots.append(BiasSnapshot(
                    timestamp=current_row["timestamp"],
                    price=current_row["close"],
                    total_score=bias_result.total_score,
                    mode=bias_result.mode.value,
                    trend_score=bias_result.trend_structure.score,
                    intensity_score=bias_result.market_intensity.score,
                    orderflow_score=bias_result.orderflow_alpha.score,
                    orderflow_mode=bias_result.orderflow_alpha.active_mode,
                    confidence=bias_result.confidence,
                    active_signals=bias_result.orderflow_alpha.active_signals,
                ))

            except Exception as e:
                logger.debug(f"Error calculating bias at bar {i}: {e}")
                continue

        logger.info(f"Calculated {len(snapshots)} bias snapshots")
        return snapshots

    def _calculate_rvol(self, df: pl.DataFrame) -> float:
        """Calculate relative volume for the last bar"""
        if len(df) < 2:
            return 1.0

        volumes = df["volume"].to_list()
        if len(volumes) < 2:
            return 1.0

        current_vol = volumes[-1]
        avg_vol = sum(volumes[:-1]) / len(volumes[:-1])

        return current_vol / avg_vol if avg_vol > 0 else 1.0

    def _calculate_vpin(self, df: pl.DataFrame) -> float:
        """Calculate VPIN estimate"""
        if len(df) < 10 or "bar_delta" not in df.columns:
            return 0.5

        # VPIN approximation: abs(delta) / volume ratio
        deltas = df["bar_delta"].to_list()
        volumes = df["volume"].to_list()

        total_abs_delta = sum(abs(d) for d in deltas if d is not None)
        total_volume = sum(v for v in volumes if v is not None)

        if total_volume == 0:
            return 0.5

        vpin = total_abs_delta / (total_volume * 2)  # Normalized to ~0-1
        return min(1.0, max(0.0, vpin))

    def detect_mode_transitions(
        self,
        snapshots: List[BiasSnapshot]
    ) -> List[ModeTransition]:
        """Detect mode transitions in the bias series"""
        transitions = []

        if len(snapshots) < self.min_mode_duration + 1:
            return transitions

        current_mode = snapshots[0].mode
        mode_start_idx = 0

        for i in range(1, len(snapshots)):
            if snapshots[i].mode != current_mode:
                # Mode changed - check duration requirement
                duration = i - mode_start_idx

                if duration >= self.min_mode_duration:
                    transitions.append(ModeTransition(
                        timestamp=snapshots[i].timestamp,
                        price=snapshots[i].price,
                        from_mode=current_mode,
                        to_mode=snapshots[i].mode,
                        score=snapshots[i].total_score,
                        confidence=snapshots[i].confidence,
                    ))

                current_mode = snapshots[i].mode
                mode_start_idx = i

        logger.info(f"Detected {len(transitions)} mode transitions")
        return transitions

    def calculate_transition_returns(
        self,
        snapshots: List[BiasSnapshot],
        transitions: List[ModeTransition],
    ) -> List[TransitionResult]:
        """Calculate forward returns for mode transitions"""
        results = []

        # Build timestamp -> index mapping
        ts_to_idx = {s.timestamp: i for i, s in enumerate(snapshots)}

        for transition in transitions:
            idx = ts_to_idx.get(transition.timestamp)
            if idx is None:
                continue

            # Get forward prices
            price_1 = snapshots[idx + 1].price if idx + 1 < len(snapshots) else None
            price_5 = snapshots[idx + 5].price if idx + 5 < len(snapshots) else None
            price_10 = snapshots[idx + 10].price if idx + 10 < len(snapshots) else None
            price_20 = snapshots[idx + 20].price if idx + 20 < len(snapshots) else None

            if price_5 is None:
                continue

            # Calculate returns
            ret_1 = (price_1 - transition.price) / transition.price if price_1 else 0
            ret_5 = (price_5 - transition.price) / transition.price if price_5 else 0
            ret_10 = (price_10 - transition.price) / transition.price if price_10 else 0
            ret_20 = (price_20 - transition.price) / transition.price if price_20 else 0

            # Determine expected direction from new mode
            if transition.to_mode in ["HIGH_BULLISH", "WEAK_BULLISH"]:
                expected_dir = 1  # Expect up
            elif transition.to_mode in ["HIGH_BEARISH", "WEAK_BEARISH"]:
                expected_dir = -1  # Expect down
                ret_1, ret_5, ret_10, ret_20 = -ret_1, -ret_5, -ret_10, -ret_20
            else:
                continue  # Skip NEUTRAL transitions for hit rate

            results.append(TransitionResult(
                transition=transition,
                forward_return_1=ret_1,
                forward_return_5=ret_5,
                forward_return_10=ret_10,
                forward_return_20=ret_20,
                hit_5=ret_5 > 0,
                hit_10=ret_10 > 0,
                hit_20=ret_20 > 0,
            ))

        return results

    def calculate_score_accuracy(
        self,
        snapshots: List[BiasSnapshot],
    ) -> List[ScoreAccuracyResult]:
        """Calculate how well the score predicts direction"""
        results = []

        for i in range(len(snapshots) - 10):
            snapshot = snapshots[i]

            # Determine predicted direction from score
            if snapshot.total_score > self.bullish_threshold:
                predicted = "BULLISH"
            elif snapshot.total_score < self.bearish_threshold:
                predicted = "BEARISH"
            else:
                predicted = "NEUTRAL"

            # Calculate actual direction
            price_5 = snapshots[i + 5].price
            price_10 = snapshots[i + 10].price

            ret_5 = (price_5 - snapshot.price) / snapshot.price
            ret_10 = (price_10 - snapshot.price) / snapshot.price

            actual_5 = "BULLISH" if ret_5 > 0.001 else "BEARISH" if ret_5 < -0.001 else "NEUTRAL"
            actual_10 = "BULLISH" if ret_10 > 0.001 else "BEARISH" if ret_10 < -0.001 else "NEUTRAL"

            # Check accuracy (NEUTRAL predictions don't count as correct/incorrect)
            correct_5 = (predicted == actual_5) if predicted != "NEUTRAL" else None
            correct_10 = (predicted == actual_10) if predicted != "NEUTRAL" else None

            results.append(ScoreAccuracyResult(
                timestamp=snapshot.timestamp,
                score=snapshot.total_score,
                predicted_direction=predicted,
                actual_direction_5=actual_5,
                actual_direction_10=actual_10,
                correct_5=correct_5 if correct_5 is not None else False,
                correct_10=correct_10 if correct_10 is not None else False,
            ))

        return results

    def calculate_summary(
        self,
        snapshots: List[BiasSnapshot],
        transitions: List[ModeTransition],
        transition_results: List[TransitionResult],
        accuracy_results: List[ScoreAccuracyResult],
    ) -> BacktestSummary:
        """Calculate complete backtest summary"""

        # Score Direction Accuracy
        bullish_preds = [r for r in accuracy_results if r.predicted_direction == "BULLISH"]
        bearish_preds = [r for r in accuracy_results if r.predicted_direction == "BEARISH"]
        neutral_preds = [r for r in accuracy_results if r.predicted_direction == "NEUTRAL"]

        bullish_acc_5 = sum(1 for r in bullish_preds if r.correct_5) / len(bullish_preds) * 100 if bullish_preds else 0
        bullish_acc_10 = sum(1 for r in bullish_preds if r.correct_10) / len(bullish_preds) * 100 if bullish_preds else 0
        bearish_acc_5 = sum(1 for r in bearish_preds if r.correct_5) / len(bearish_preds) * 100 if bearish_preds else 0
        bearish_acc_10 = sum(1 for r in bearish_preds if r.correct_10) / len(bearish_preds) * 100 if bearish_preds else 0

        # Mode Transition Performance
        transitions_by_mode = {}
        hit_rate_by_mode_5 = {}
        hit_rate_by_mode_10 = {}
        avg_return_by_mode_5 = {}

        for mode in ["HIGH_BULLISH", "WEAK_BULLISH", "HIGH_BEARISH", "WEAK_BEARISH"]:
            mode_results = [r for r in transition_results if r.transition.to_mode == mode]
            transitions_by_mode[mode] = len(mode_results)

            if mode_results:
                hit_rate_by_mode_5[mode] = sum(1 for r in mode_results if r.hit_5) / len(mode_results) * 100
                hit_rate_by_mode_10[mode] = sum(1 for r in mode_results if r.hit_10) / len(mode_results) * 100
                avg_return_by_mode_5[mode] = sum(r.forward_return_5 for r in mode_results) / len(mode_results) * 100
            else:
                hit_rate_by_mode_5[mode] = 0
                hit_rate_by_mode_10[mode] = 0
                avg_return_by_mode_5[mode] = 0

        # Overall Performance
        if transition_results:
            overall_hit_5 = sum(1 for r in transition_results if r.hit_5) / len(transition_results) * 100
            overall_hit_10 = sum(1 for r in transition_results if r.hit_10) / len(transition_results) * 100
            avg_ret_5 = sum(r.forward_return_5 for r in transition_results) / len(transition_results) * 100
            avg_ret_10 = sum(r.forward_return_10 for r in transition_results) / len(transition_results) * 100

            wins = [r.forward_return_5 for r in transition_results if r.forward_return_5 > 0]
            losses = [r.forward_return_5 for r in transition_results if r.forward_return_5 < 0]
            total_wins = sum(wins) if wins else 0
            total_losses = abs(sum(losses)) if losses else 0
            profit_factor = total_wins / total_losses if total_losses > 0 else 0
        else:
            overall_hit_5 = overall_hit_10 = avg_ret_5 = avg_ret_10 = profit_factor = 0

        # High Conviction Performance
        high_conviction = [r for r in transition_results
                         if r.transition.score > self.high_conviction_threshold
                         or r.transition.score < (100 - self.high_conviction_threshold)]

        if high_conviction:
            hc_hit_rate = sum(1 for r in high_conviction if r.hit_5) / len(high_conviction) * 100
            hc_avg_return = sum(r.forward_return_5 for r in high_conviction) / len(high_conviction) * 100
        else:
            hc_hit_rate = hc_avg_return = 0

        return BacktestSummary(
            parameters=self.get_parameters(),
            total_bars=len(snapshots),
            bullish_predictions=len(bullish_preds),
            bearish_predictions=len(bearish_preds),
            neutral_predictions=len(neutral_preds),
            bullish_accuracy_5=bullish_acc_5,
            bullish_accuracy_10=bullish_acc_10,
            bearish_accuracy_5=bearish_acc_5,
            bearish_accuracy_10=bearish_acc_10,
            mode_transitions=len(transitions),
            transitions_by_mode=transitions_by_mode,
            hit_rate_by_mode_5=hit_rate_by_mode_5,
            hit_rate_by_mode_10=hit_rate_by_mode_10,
            avg_return_by_mode_5=avg_return_by_mode_5,
            overall_hit_rate_5=overall_hit_5,
            overall_hit_rate_10=overall_hit_10,
            avg_return_5=avg_ret_5,
            avg_return_10=avg_ret_10,
            profit_factor=profit_factor,
            high_conviction_signals=len(high_conviction),
            high_conviction_hit_rate=hc_hit_rate,
            high_conviction_avg_return=hc_avg_return,
        )

    def run_backtest(
        self,
        timeframe: str = "15M",
        symbol: str = "MNQ",
        limit: int = 10000,
    ) -> Tuple[BacktestSummary, List[BiasSnapshot], List[TransitionResult]]:
        """Run complete backtest"""

        # Load data
        df = self.load_data(timeframe=timeframe, symbol=symbol, limit=limit)

        if len(df) == 0:
            logger.error("No data loaded")
            return None, [], []

        # Calculate bias series
        logger.info("Calculating Agent Bias Score for each bar...")
        snapshots = self.calculate_bias_series(df, timeframe=timeframe)

        if len(snapshots) == 0:
            logger.error("Could not calculate bias snapshots")
            return None, [], []

        # Detect transitions
        transitions = self.detect_mode_transitions(snapshots)

        # Calculate returns
        transition_results = self.calculate_transition_returns(snapshots, transitions)
        accuracy_results = self.calculate_score_accuracy(snapshots)

        # Calculate summary
        summary = self.calculate_summary(
            snapshots, transitions, transition_results, accuracy_results
        )

        return summary, snapshots, transition_results

    def run_parameter_sweep(
        self,
        timeframe: str = "15M",
        symbol: str = "MNQ",
        limit: int = 10000,
    ) -> List[BacktestSummary]:
        """Run parameter sweep to find optimal thresholds"""

        # Load data once
        df = self.load_data(timeframe=timeframe, symbol=symbol, limit=limit)
        if len(df) == 0:
            return []

        # Calculate bias series once
        logger.info("Calculating Agent Bias Score for each bar...")
        snapshots = self.calculate_bias_series(df, timeframe=timeframe)
        if len(snapshots) == 0:
            return []

        # Parameter ranges
        bullish_thresholds = [52, 55, 58, 60]
        bearish_thresholds = [48, 45, 42, 40]
        high_conviction_thresholds = [65, 70, 75]
        min_durations = [2, 3, 5]

        results = []

        for bull_thresh in bullish_thresholds:
            for bear_thresh in bearish_thresholds:
                if bull_thresh <= bear_thresh:
                    continue

                for hc_thresh in high_conviction_thresholds:
                    for min_dur in min_durations:
                        self.bullish_threshold = bull_thresh
                        self.bearish_threshold = bear_thresh
                        self.high_conviction_threshold = hc_thresh
                        self.min_mode_duration = min_dur

                        transitions = self.detect_mode_transitions(snapshots)
                        if len(transitions) < 5:
                            continue

                        transition_results = self.calculate_transition_returns(snapshots, transitions)
                        if len(transition_results) < 5:
                            continue

                        accuracy_results = self.calculate_score_accuracy(snapshots)

                        summary = self.calculate_summary(
                            snapshots, transitions, transition_results, accuracy_results
                        )

                        if summary.overall_hit_rate_5 > 50:
                            results.append(summary)

        results.sort(key=lambda x: (x.overall_hit_rate_5, x.profit_factor), reverse=True)
        return results


def print_summary(summary: BacktestSummary):
    """Pretty print backtest summary"""
    print("\n" + "=" * 70)
    print("AGENT BIAS SCORE BACKTEST RESULTS")
    print("=" * 70)

    print(f"\nParameters:")
    for k, v in summary.parameters.items():
        print(f"  {k}: {v}")

    print(f"\n{'-' * 70}")
    print("SCORE DIRECTION ACCURACY")
    print(f"{'-' * 70}")
    print(f"  Total Bars Analyzed: {summary.total_bars}")
    print(f"  Bullish Predictions: {summary.bullish_predictions}")
    print(f"  Bearish Predictions: {summary.bearish_predictions}")
    print(f"  Neutral (No Trade): {summary.neutral_predictions}")

    print(f"\n  Bullish Accuracy:")
    print(f"    5-bar:  {summary.bullish_accuracy_5:.1f}%")
    print(f"    10-bar: {summary.bullish_accuracy_10:.1f}%")

    print(f"\n  Bearish Accuracy:")
    print(f"    5-bar:  {summary.bearish_accuracy_5:.1f}%")
    print(f"    10-bar: {summary.bearish_accuracy_10:.1f}%")

    print(f"\n{'-' * 70}")
    print("MODE TRANSITION PERFORMANCE")
    print(f"{'-' * 70}")
    print(f"  Total Transitions: {summary.mode_transitions}")

    print(f"\n  {'Mode':<15} {'Count':>8} {'Hit 5-bar':>12} {'Hit 10-bar':>12} {'Avg Ret 5':>12}")
    print(f"  {'-' * 60}")

    for mode in ["HIGH_BULLISH", "WEAK_BULLISH", "HIGH_BEARISH", "WEAK_BEARISH"]:
        count = summary.transitions_by_mode.get(mode, 0)
        hit_5 = summary.hit_rate_by_mode_5.get(mode, 0)
        hit_10 = summary.hit_rate_by_mode_10.get(mode, 0)
        avg_ret = summary.avg_return_by_mode_5.get(mode, 0)

        print(f"  {mode:<15} {count:>8} {hit_5:>11.1f}% {hit_10:>11.1f}% {avg_ret:>11.4f}%")

    print(f"\n{'-' * 70}")
    print("OVERALL PERFORMANCE")
    print(f"{'-' * 70}")
    print(f"  Hit Rate (5-bar):  {summary.overall_hit_rate_5:.1f}%")
    print(f"  Hit Rate (10-bar): {summary.overall_hit_rate_10:.1f}%")
    print(f"  Avg Return (5-bar):  {summary.avg_return_5:.4f}%")
    print(f"  Avg Return (10-bar): {summary.avg_return_10:.4f}%")
    print(f"  Profit Factor: {summary.profit_factor:.2f}")

    print(f"\n{'-' * 70}")
    print("HIGH CONVICTION SIGNALS (Score > 70 or < 30)")
    print(f"{'-' * 70}")
    print(f"  Signals: {summary.high_conviction_signals}")
    print(f"  Hit Rate: {summary.high_conviction_hit_rate:.1f}%")
    print(f"  Avg Return: {summary.high_conviction_avg_return:.4f}%")

    # Interpretation
    print(f"\n{'-' * 70}")
    print("INTERPRETATION")
    print(f"{'-' * 70}")

    if summary.overall_hit_rate_5 > 55:
        print(f"  [+] Mode transitions have predictive value (hit rate > 55%)")
        if summary.profit_factor > 1.5:
            print(f"  [+] Strong edge detected (profit factor > 1.5)")
        elif summary.profit_factor > 1.0:
            print(f"  [~] Modest edge (profit factor > 1.0)")
    elif summary.overall_hit_rate_5 > 50:
        print(f"  [~] Marginal predictive value (hit rate 50-55%)")
    else:
        print(f"  [-] No clear edge (hit rate <= 50%)")

    if summary.high_conviction_hit_rate > 60:
        print(f"  [+] High conviction signals show strong edge ({summary.high_conviction_hit_rate:.1f}%)")

    best_mode = max(summary.hit_rate_by_mode_5.items(), key=lambda x: x[1]) if summary.hit_rate_by_mode_5 else None
    if best_mode and best_mode[1] > 55:
        print(f"  [+] Best performing mode: {best_mode[0]} ({best_mode[1]:.1f}% hit rate)")

    print("=" * 70)


def print_transitions(results: List[TransitionResult], limit: int = 30):
    """Print individual mode transitions"""
    print("\n" + "=" * 110)
    print("MODE TRANSITION DETAILS")
    print("=" * 110)

    print(f"\n{'Timestamp':<20} {'From Mode':<15} {'To Mode':<15} {'Score':>6} {'Conf':>6} {'Ret 5':>10} {'Ret 10':>10} {'Hit5':>5}")
    print("-" * 110)

    for result in results[:limit]:
        t = result.transition
        ts_str = t.timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(t.timestamp, "strftime") else str(t.timestamp)[:16]
        hit = "Yes" if result.hit_5 else "No"

        print(f"{ts_str:<20} {t.from_mode:<15} {t.to_mode:<15} {t.score:>6.1f} {t.confidence:>6} "
              f"{result.forward_return_5*100:>9.4f}% {result.forward_return_10*100:>9.4f}% {hit:>5}")

    print("-" * 110)
    print(f"Showing {min(limit, len(results))} of {len(results)} transitions")


def print_score_distribution(snapshots: List[BiasSnapshot]):
    """Print score distribution analysis"""
    print("\n" + "=" * 70)
    print("SCORE DISTRIBUTION")
    print("=" * 70)

    scores = [s.total_score for s in snapshots]

    # Distribution by range
    ranges = [
        (0, 30, "HIGH_BEARISH"),
        (30, 45, "WEAK_BEARISH"),
        (45, 55, "NEUTRAL"),
        (55, 70, "WEAK_BULLISH"),
        (70, 100, "HIGH_BULLISH"),
    ]

    print(f"\n  {'Score Range':<15} {'Mode':<15} {'Count':>8} {'%':>8}")
    print(f"  {'-' * 50}")

    for low, high, mode in ranges:
        count = sum(1 for s in scores if low <= s < high)
        pct = count / len(scores) * 100 if scores else 0
        print(f"  {f'{low}-{high}':<15} {mode:<15} {count:>8} {pct:>7.1f}%")

    # Order Flow Alpha modes distribution
    print(f"\n  Order Flow Alpha Mode Distribution:")
    of_modes = {}
    for s in snapshots:
        of_modes[s.orderflow_mode] = of_modes.get(s.orderflow_mode, 0) + 1

    for mode, count in sorted(of_modes.items(), key=lambda x: -x[1]):
        pct = count / len(snapshots) * 100
        print(f"    {mode:<15} {count:>8} ({pct:>5.1f}%)")


def main():
    parser = argparse.ArgumentParser(description="Backtest Agent Bias Score")
    parser.add_argument("--timeframe", "-t", default="15M", help="Bar timeframe")
    parser.add_argument("--symbol", "-s", default="MNQ", help="Trading symbol")
    parser.add_argument("--limit", "-l", type=int, default=10000, help="Max bars to load")
    parser.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    parser.add_argument("--show-transitions", action="store_true", help="Show mode transitions")
    parser.add_argument("--show-distribution", action="store_true", help="Show score distribution")

    # Threshold parameters
    parser.add_argument("--bullish-thresh", type=float, default=55.0, help="Bullish score threshold")
    parser.add_argument("--bearish-thresh", type=float, default=45.0, help="Bearish score threshold")
    parser.add_argument("--hc-thresh", type=float, default=70.0, help="High conviction threshold")
    parser.add_argument("--min-duration", type=int, default=3, help="Min mode duration for transitions")

    args = parser.parse_args()

    if args.sweep:
        print(f"\nRunning Agent Bias parameter sweep on {args.timeframe}...")
        backtester = AgentBiasBacktester()
        results = backtester.run_parameter_sweep(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
        )

        if not results:
            print("No valid parameter combinations found")
        else:
            print(f"\nTop 10 parameter combinations (by 5-bar hit rate):\n")
            print(f"{'Bull':>6} {'Bear':>6} {'HC':>6} {'MinD':>6} {'Trans':>6} {'Hit5%':>8} {'PF':>8}")
            print("-" * 55)

            for r in results[:10]:
                p = r.parameters
                print(f"{p['bullish_threshold']:>6.0f} {p['bearish_threshold']:>6.0f} "
                      f"{p['high_conviction_threshold']:>6.0f} {p['min_mode_duration']:>6} "
                      f"{r.mode_transitions:>6} {r.overall_hit_rate_5:>7.1f}% {r.profit_factor:>7.2f}")

            print("\n" + "=" * 70)
            print("Best parameters:")
            print_summary(results[0])

    else:
        backtester = AgentBiasBacktester(
            bullish_threshold=args.bullish_thresh,
            bearish_threshold=args.bearish_thresh,
            high_conviction_threshold=args.hc_thresh,
            min_mode_duration=args.min_duration,
        )

        summary, snapshots, transition_results = backtester.run_backtest(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
        )

        if summary:
            print_summary(summary)

            if args.show_distribution and snapshots:
                print_score_distribution(snapshots)

            if args.show_transitions and transition_results:
                print_transitions(transition_results)


if __name__ == "__main__":
    main()
