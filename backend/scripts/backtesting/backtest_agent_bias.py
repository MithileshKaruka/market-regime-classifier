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
from app.features.zone_bias import ZoneBiasScorer

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ============================================================================
# Alternative Orderflow Scoring Approaches
# ============================================================================

class OrderflowApproach:
    """Different approaches for calculating orderflow score"""
    ORIGINAL = "original"              # Current logic: BASE uses LDR/OBI/CVD 33/33/34
    SIGNAL_REQUIRED = "signal_req"     # Neutral (50) unless primary signal fires
    CVD_DIRECTION = "cvd_dir"          # CVD contributes by direction, not magnitude
    INTENSITY_ZONE = "int_zone"        # Boost Intensity weight when at zone
    COMPONENT_AGREE = "comp_agree"     # Require 2+ components to agree
    TREND_INTENSITY = "trend_int"      # Only Trend + Intensity (no orderflow in BASE)


def calculate_alternative_orderflow_score(
    approach: str,
    # Signal data
    abs_dicts: List[dict],
    exh_dicts: List[dict],
    du_dicts: List[dict],
    # Supporting metrics
    ldr: Optional[float],
    obi_ratio: Optional[float],
    cvd: Optional[float],
    # Zone context
    at_zone: bool = False,
    zone_type: Optional[str] = None,
) -> Tuple[float, str, dict]:
    """Calculate orderflow score using alternative approaches.

    Returns:
        Tuple of (score 0-100, mode_name, details_dict)
    """
    # Count signals by direction
    abs_bull = sum(1 for s in abs_dicts if s.get("direction") == "BULLISH")
    abs_bear = sum(1 for s in abs_dicts if s.get("direction") == "BEARISH")
    exh_bull = sum(1 for s in exh_dicts if s.get("direction") == "BULLISH")
    exh_bear = sum(1 for s in exh_dicts if s.get("direction") == "BEARISH")
    du_bull = sum(1 for s in du_dicts if s.get("direction") == "BULLISH")
    du_bear = sum(1 for s in du_dicts if s.get("direction") == "BEARISH")

    has_primary = (abs_bull + abs_bear + exh_bull + exh_bear + du_bull + du_bear) > 0

    # Calculate base metric scores (same for all approaches)
    ldr_score = 50.0
    if ldr is not None:
        if ldr >= 3.0:
            ldr_score = 95
        elif ldr >= 2.0:
            ldr_score = 80
        elif ldr >= 1.3:
            ldr_score = 60
        elif ldr <= 0.33:
            ldr_score = 5
        elif ldr <= 0.5:
            ldr_score = 20
        elif ldr <= 0.77:
            ldr_score = 40

    obi_score = 50.0
    if obi_ratio is not None:
        if obi_ratio >= 3.0:
            obi_score = 90
        elif obi_ratio >= 1.5:
            obi_score = 70
        elif obi_ratio >= 1.1:
            obi_score = 55
        elif obi_ratio <= 0.33:
            obi_score = 10
        elif obi_ratio <= 0.67:
            obi_score = 30
        elif obi_ratio <= 0.9:
            obi_score = 45

    # CVD scoring varies by approach
    cvd_score = 50.0
    cvd_direction_score = 50.0  # Simple direction-based scoring
    if cvd is not None:
        # Direction-based: just bullish/bearish, no magnitude
        if cvd > 0:
            cvd_direction_score = 65  # Mildly bullish
        elif cvd < 0:
            cvd_direction_score = 35  # Mildly bearish

        # Magnitude-based (original style) - uses 50000 threshold
        cvd_threshold = 50000
        if cvd >= cvd_threshold * 2:
            cvd_score = 90 + min(10, (cvd - cvd_threshold * 2) / cvd_threshold * 10)
        elif cvd >= cvd_threshold:
            cvd_score = 60 + (cvd - cvd_threshold) / cvd_threshold * 30
        elif cvd > 0:
            cvd_score = 50 + cvd / cvd_threshold * 10
        elif cvd <= -cvd_threshold * 2:
            cvd_score = 10 - min(10, (abs(cvd) - cvd_threshold * 2) / cvd_threshold * 10)
        elif cvd <= -cvd_threshold:
            cvd_score = 40 - (abs(cvd) - cvd_threshold) / cvd_threshold * 30
        else:
            cvd_score = 50 - abs(cvd) / cvd_threshold * 10
        cvd_score = max(0, min(100, cvd_score))

    # Calculate primary signal score if active
    primary_score = 50.0
    primary_mode = "BASE"

    # Rank: Delta Unwind > Exhaustion > Absorption
    if du_bull > du_bear:
        primary_score = 80 + min(20, du_bull * 15)
        primary_mode = "DELTA_UNWIND"
    elif du_bear > du_bull:
        primary_score = 20 - min(20, du_bear * 15)
        primary_mode = "DELTA_UNWIND"
    elif exh_bull > exh_bear:
        primary_score = 75 + min(25, exh_bull * 12)
        primary_mode = "EXHAUSTION"
    elif exh_bear > exh_bull:
        primary_score = 25 - min(25, exh_bear * 12)
        primary_mode = "EXHAUSTION"
    elif abs_bull > abs_bear:
        primary_score = 50 + (abs_bull - abs_bear) / max(1, abs_bull + abs_bear) * 40
        primary_mode = "ABSORPTION"
    elif abs_bear > abs_bull:
        primary_score = 50 + (abs_bull - abs_bear) / max(1, abs_bull + abs_bear) * 40
        primary_mode = "ABSORPTION"

    primary_score = max(0, min(100, primary_score))

    details = {
        "ldr_score": ldr_score,
        "obi_score": obi_score,
        "cvd_score": cvd_score,
        "cvd_dir_score": cvd_direction_score,
        "primary_score": primary_score,
        "has_primary": has_primary,
    }

    # ============================================
    # Apply approach-specific logic
    # ============================================

    if approach == OrderflowApproach.ORIGINAL:
        # Original logic
        if has_primary:
            score = primary_score * 0.50 + ldr_score * 0.20 + obi_score * 0.15 + cvd_score * 0.15
            mode = primary_mode
        else:
            score = ldr_score * 0.33 + obi_score * 0.33 + cvd_score * 0.34
            mode = "BASE"

    elif approach == OrderflowApproach.SIGNAL_REQUIRED:
        # Neutral unless primary signal fires
        if has_primary:
            score = primary_score * 0.50 + ldr_score * 0.20 + obi_score * 0.15 + cvd_direction_score * 0.15
            mode = primary_mode
        else:
            # No primary signal = neutral score (50)
            score = 50.0
            mode = "BASE"

    elif approach == OrderflowApproach.CVD_DIRECTION:
        # CVD contributes by direction only (not magnitude)
        if has_primary:
            score = primary_score * 0.50 + ldr_score * 0.20 + obi_score * 0.15 + cvd_direction_score * 0.15
            mode = primary_mode
        else:
            score = ldr_score * 0.33 + obi_score * 0.33 + cvd_direction_score * 0.34
            mode = "BASE"

    elif approach == OrderflowApproach.INTENSITY_ZONE:
        # At zones: orderflow only counts if primary signal fires, otherwise neutral
        if at_zone:
            if has_primary:
                score = primary_score * 0.60 + ldr_score * 0.20 + obi_score * 0.10 + cvd_direction_score * 0.10
                mode = primary_mode
            else:
                score = 50.0  # Neutral at zone without confirmation
                mode = "BASE"
        else:
            # Not at zone: use original logic
            if has_primary:
                score = primary_score * 0.50 + ldr_score * 0.20 + obi_score * 0.15 + cvd_score * 0.15
                mode = primary_mode
            else:
                score = ldr_score * 0.33 + obi_score * 0.33 + cvd_score * 0.34
                mode = "BASE"

    elif approach == OrderflowApproach.COMPONENT_AGREE:
        # Require agreement between metrics for directional score
        bullish_votes = 0
        bearish_votes = 0

        if ldr_score > 55:
            bullish_votes += 1
        elif ldr_score < 45:
            bearish_votes += 1

        if obi_score > 55:
            bullish_votes += 1
        elif obi_score < 45:
            bearish_votes += 1

        if cvd_direction_score > 55:
            bullish_votes += 1
        elif cvd_direction_score < 45:
            bearish_votes += 1

        if has_primary:
            if primary_score > 55:
                bullish_votes += 2  # Primary signal counts double
            elif primary_score < 45:
                bearish_votes += 2

        # Need 2+ votes for directional score
        if bullish_votes >= 2 and bullish_votes > bearish_votes:
            if has_primary:
                score = primary_score * 0.50 + ldr_score * 0.20 + obi_score * 0.15 + cvd_direction_score * 0.15
            else:
                score = ldr_score * 0.33 + obi_score * 0.33 + cvd_direction_score * 0.34
            mode = primary_mode if has_primary else "AGREE_BULL"
        elif bearish_votes >= 2 and bearish_votes > bullish_votes:
            if has_primary:
                score = primary_score * 0.50 + ldr_score * 0.20 + obi_score * 0.15 + cvd_direction_score * 0.15
            else:
                score = ldr_score * 0.33 + obi_score * 0.33 + cvd_direction_score * 0.34
            mode = primary_mode if has_primary else "AGREE_BEAR"
        else:
            # No agreement = neutral
            score = 50.0
            mode = "DISAGREE"

    elif approach == OrderflowApproach.TREND_INTENSITY:
        # Orderflow only matters if primary signal fires
        # Otherwise, let Trend and Intensity dominate (orderflow = 50)
        if has_primary:
            score = primary_score * 0.60 + ldr_score * 0.20 + obi_score * 0.10 + cvd_direction_score * 0.10
            mode = primary_mode
        else:
            score = 50.0  # Let other components (Trend/Intensity) decide
            mode = "BASE"

    else:
        # Fallback to original
        if has_primary:
            score = primary_score * 0.50 + ldr_score * 0.20 + obi_score * 0.15 + cvd_score * 0.15
            mode = primary_mode
        else:
            score = ldr_score * 0.33 + obi_score * 0.33 + cvd_score * 0.34
            mode = "BASE"

    return round(score, 1), mode, details


def recalculate_total_score(
    approach: str,
    trend_score: float,
    intensity_score: float,
    orderflow_score: float,
    at_zone: bool = False,
) -> float:
    """Recalculate total bias score with alternative component weights.

    Standard weights: Trend 20%, Intensity 20%, Orderflow 60%
    """
    if approach == OrderflowApproach.INTENSITY_ZONE and at_zone:
        # At zones: boost Intensity weight since it was most accurate
        # Trend 20%, Intensity 40%, Orderflow 40%
        return trend_score * 0.20 + intensity_score * 0.40 + orderflow_score * 0.40
    elif approach == OrderflowApproach.TREND_INTENSITY:
        # When orderflow is neutral (50), effectively: Trend 50%, Intensity 50%
        # Otherwise keep orderflow dominant
        if orderflow_score == 50.0:
            return trend_score * 0.50 + intensity_score * 0.50
        else:
            return trend_score * 0.20 + intensity_score * 0.20 + orderflow_score * 0.60
    else:
        # Standard weights
        return trend_score * 0.20 + intensity_score * 0.20 + orderflow_score * 0.60


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
    # Detailed orderflow signal counts for zone analysis
    absorption_bullish: int = 0
    absorption_bearish: int = 0
    exhaustion_bullish: int = 0
    exhaustion_bearish: int = 0
    delta_unwind_bullish: int = 0
    delta_unwind_bearish: int = 0
    cvd_value: Optional[float] = None
    # Zone tracking fields (simplified - no bias adjustment)
    zone_status: Optional[str] = None  # APPROACHING, IN_ZONE, HELD, BROKEN
    zone_type: Optional[str] = None  # DEMAND or SUPPLY
    zone_quality: float = 0.0
    zone_price_low: Optional[float] = None
    zone_price_high: Optional[float] = None


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
class SwingPoint:
    """Represents a swing high or low"""
    timestamp: datetime
    price: float
    swing_type: str  # "HIGH" or "LOW"
    bar_index: int


@dataclass
class SwingBiasResult:
    """Result for bias analysis at a swing point"""
    swing: SwingPoint
    bias_score: float
    mode: str
    orderflow_score: float
    orderflow_mode: str
    predicted_reversal: bool  # Did bias predict reversal? (bearish at high, bullish at low)
    forward_return_5: float
    forward_return_10: float
    forward_return_20: float
    actual_reversal_5: bool  # Did price actually reverse?
    actual_reversal_10: bool
    actual_reversal_20: bool
    correct_prediction: bool  # Did bias + actual agree?


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
        orderflow_approach: str = OrderflowApproach.ORIGINAL,  # Orderflow scoring approach
    ):
        """Initialize backtester

        Args:
            bullish_threshold: Score threshold for bullish prediction (default 55)
            bearish_threshold: Score threshold for bearish prediction (default 45)
            high_conviction_threshold: Threshold for high conviction (score > this or < 100-this)
            min_mode_duration: Minimum bars a mode must last before transition counts
            orderflow_approach: Which orderflow scoring approach to use
        """
        self.bullish_threshold = bullish_threshold
        self.bearish_threshold = bearish_threshold
        self.high_conviction_threshold = high_conviction_threshold
        self.min_mode_duration = min_mode_duration
        self.orderflow_approach = orderflow_approach

        self.db = DuckDBStorage()
        self.bias_calculator = AgentBiasCalculator()

    def get_parameters(self) -> dict:
        """Return current parameters"""
        return {
            "bullish_threshold": self.bullish_threshold,
            "bearish_threshold": self.bearish_threshold,
            "high_conviction_threshold": self.high_conviction_threshold,
            "min_mode_duration": self.min_mode_duration,
            "orderflow_approach": self.orderflow_approach,
        }

    def load_data(
        self,
        timeframe: str = "15M",
        symbol: str = "MNQ",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 10000,
    ) -> pl.DataFrame:
        """Load historical data with all metrics needed for bias calculation

        Uses ohlcv_ticks table which has pre-aggregated orderflow metrics.
        """
        where_clauses = [
            f"symbol = '{symbol}'",
            f"timeframe = '{timeframe}'",
        ]
        if start_date:
            where_clauses.append(f"timestamp >= '{start_date}'")
        if end_date:
            where_clauses.append(f"timestamp <= '{end_date}'")

        where_str = " AND ".join(where_clauses)

        # Query from ohlcv_ticks which has pre-aggregated orderflow metrics
        # Note: Include instant_delta with both names - bar_delta for cumsum, instant_delta for signal detection
        # Use subquery to get the most recent N bars (older data may lack orderflow metrics)
        query = f"""
            SELECT * FROM (
                SELECT
                    timestamp,
                    open,
                    high,
                    low,
                    close,
                    volume,
                    instant_delta,
                    instant_delta as bar_delta,
                    dom_imbalance,
                    total_bid_depth,
                    total_ask_depth,
                    cvd,
                    trade_flow_ratio,
                    buy_trades,
                    sell_trades,
                    large_trade_count
                FROM ohlcv_ticks
                WHERE {where_str}
                ORDER BY timestamp DESC
                LIMIT {limit}
            ) subq
            ORDER BY timestamp ASC
        """

        df = self.db.conn.execute(query).pl()

        if len(df) == 0:
            logger.warning(f"No data in ohlcv_ticks for {symbol} {timeframe}")
            return df

        # Calculate cumulative delta if not already present
        if "bar_delta" in df.columns:
            df = df.with_columns([
                pl.col("bar_delta").cum_sum().alias("cum_delta"),
            ])
        elif "cvd" in df.columns:
            df = df.with_columns([
                pl.col("cvd").alias("cum_delta"),
            ])

        # Calculate LDR from bid/ask depth
        if "total_bid_depth" in df.columns and "total_ask_depth" in df.columns:
            df = df.with_columns([
                (pl.col("total_bid_depth") / pl.col("total_ask_depth").replace(0, 1)).alias("ldr"),
            ])
        elif "dom_imbalance" in df.columns:
            # Fallback: use dom_imbalance to estimate LDR
            df = df.with_columns([
                (pl.col("dom_imbalance") / (1 - pl.col("dom_imbalance")).replace(0, 0.5)).alias("ldr"),
            ])

        logger.info(f"Loaded {len(df)} bars for {symbol} {timeframe}")
        return df

    def calculate_bias_series(
        self,
        df: pl.DataFrame,
        timeframe: str = "15M",
        symbol: str = "MNQ",
    ) -> List[BiasSnapshot]:
        """Calculate Agent Bias Score at each bar

        For higher timeframes (1H, 4H, 1D), uses 15M orderflow signals
        since absorption/exhaustion/delta_unwind work best on 15M.

        Zone Tracking (simplified):
        - Detects S/D zones on the analysis timeframe
        - Tracks zone status: APPROACHING, IN_ZONE, HELD, BROKEN
        - NO bias score adjustment - just factual zone tracking
        """
        snapshots = []

        if len(df) < 50:  # Need minimum bars for calculations
            logger.warning("Not enough data for bias calculation")
            return snapshots

        rows = df.to_dicts()

        # Initialize zone scorer for zone detection
        zone_scorer = ZoneBiasScorer()

        # Pre-detect zones on the analysis timeframe for efficiency
        logger.info(f"Pre-detecting S/D zones for {timeframe}...")
        all_zones = zone_scorer.detect_active_zones(df, timeframe, current_bar_idx=len(df) - 1)
        logger.info(f"Found {len(all_zones)} potential S/D zones")

        # Track active zone engagement state
        # Key: zone index, Value: {"entered_at_bar": int, "entry_price": float, "status": str}
        zone_engagement = {}

        # Check if this is a higher timeframe that should use 15M orderflow
        is_higher_tf = timeframe in ("1H", "4H", "1D")

        if is_higher_tf:
            # Load 15M data and detect signals on that
            logger.info(f"Loading 15M data for orderflow signals (HTF={timeframe})...")
            df_15m = self._load_15m_data(symbol, limit=5000)

            if len(df_15m) > 50:
                signal_detector = OrderflowSignalDetector(timeframe="15M")
                logger.info("Pre-calculating orderflow signals on 15M dataset...")
                all_absorption = signal_detector.detect_absorption(df_15m)
                all_delta_unwind = signal_detector.detect_delta_unwind(df_15m)
                all_exhaustion = signal_detector.detect_exhaustion(df_15m)
                logger.info(f"Found {len(all_absorption)} absorption, {len(all_delta_unwind)} delta_unwind, {len(all_exhaustion)} exhaustion signals (from 15M)")

                # Build timestamp -> signals mapping from 15M data
                # We'll match HTF bars to recent 15M signals
                df_15m_rows = df_15m.to_dicts()
            else:
                logger.warning("Not enough 15M data, falling back to same-TF signals")
                is_higher_tf = False

        if not is_higher_tf:
            # Use same-timeframe signals for 5M and 15M
            signal_detector = OrderflowSignalDetector(timeframe=timeframe)
            logger.info("Pre-calculating orderflow signals on full dataset...")
            all_absorption = signal_detector.detect_absorption(df)
            all_delta_unwind = signal_detector.detect_delta_unwind(df)
            all_exhaustion = signal_detector.detect_exhaustion(df)
            logger.info(f"Found {len(all_absorption)} absorption, {len(all_delta_unwind)} delta_unwind, {len(all_exhaustion)} exhaustion signals")

        # For HTF: build timestamp-based signal lookup from 15M data
        # For same-TF: use bar index mapping
        if is_higher_tf:
            # Convert all 15M signals to list with unix timestamps
            def signals_to_list(signals):
                result = []
                for s in signals:
                    ts = s.timestamp
                    if hasattr(ts, "timestamp"):
                        ts = int(ts.timestamp())
                    result.append({
                        "timestamp": ts,
                        "direction": s.direction.value,
                        "strength": s.strength,
                    })
                return sorted(result, key=lambda x: x["timestamp"])

            all_abs_list = signals_to_list(all_absorption)
            all_du_list = signals_to_list(all_delta_unwind)
            all_exh_list = signals_to_list(all_exhaustion)

            def get_recent_signals(signal_list, htf_bar_ts, lookback_seconds=3600):
                """Get signals within lookback window before HTF bar timestamp"""
                if hasattr(htf_bar_ts, "timestamp"):
                    htf_ts = int(htf_bar_ts.timestamp())
                else:
                    htf_ts = htf_bar_ts
                cutoff = htf_ts - lookback_seconds
                return [{"direction": s["direction"], "strength": s["strength"]}
                        for s in signal_list if cutoff <= s["timestamp"] <= htf_ts]
        else:
            # Build bar-index mapping for same-TF
            ts_to_idx = {}
            for i, row in enumerate(rows):
                ts = row["timestamp"]
                if hasattr(ts, "timestamp"):
                    ts_to_idx[int(ts.timestamp())] = i
                else:
                    ts_to_idx[ts] = i

            def signals_to_bar_map(signals):
                bar_map = {}
                for s in signals:
                    idx = ts_to_idx.get(s.timestamp)
                    if idx is not None:
                        if idx not in bar_map:
                            bar_map[idx] = []
                        bar_map[idx].append({"direction": s.direction.value, "strength": s.strength})
                return bar_map

            absorption_by_bar = signals_to_bar_map(all_absorption)
            delta_unwind_by_bar = signals_to_bar_map(all_delta_unwind)
            exhaustion_by_bar = signals_to_bar_map(all_exhaustion)

        # Process each bar (need lookback for calculations)
        signal_window = 20  # Consider signals from last N bars (for same-TF)
        lookback_seconds_map = {"1H": 3600, "4H": 14400, "1D": 86400}  # For HTF
        lookback_seconds = lookback_seconds_map.get(timeframe, 3600)

        for i in range(50, len(rows)):
            try:
                # Get lookback window
                lookback_df = df.slice(max(0, i - 100), 100)

                current_row = rows[i]

                # Calculate metrics for this bar
                rvol = self._calculate_rvol(df.slice(max(0, i - 20), 21))
                vpin = self._calculate_vpin(df.slice(max(0, i - 50), 51))
                ldr = current_row.get("ldr", 1.0)
                cvd = current_row.get("cum_delta", 0)

                # Get signals based on timeframe
                if is_higher_tf:
                    # Get 15M signals within lookback window of this HTF bar
                    htf_bar_ts = current_row["timestamp"]
                    abs_dicts = get_recent_signals(all_abs_list, htf_bar_ts, lookback_seconds)
                    du_dicts = get_recent_signals(all_du_list, htf_bar_ts, lookback_seconds)
                    exh_dicts = get_recent_signals(all_exh_list, htf_bar_ts, lookback_seconds)
                else:
                    # Get signals from recent bar window (same-TF)
                    abs_dicts = []
                    du_dicts = []
                    exh_dicts = []
                    for j in range(max(0, i - signal_window + 1), i + 1):
                        abs_dicts.extend(absorption_by_bar.get(j, []))
                        du_dicts.extend(delta_unwind_by_bar.get(j, []))
                        exh_dicts.extend(exhaustion_by_bar.get(j, []))

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

                # ============================================================
                # Zone Status Tracking (simplified - no bias adjustment)
                # ============================================================
                current_price = current_row["close"]
                current_high = current_row["high"]
                current_low = current_row["low"]

                # Find nearest zone
                zone, zone_quality, distance_pct = zone_scorer.find_nearest_zone(
                    all_zones, current_price, current_bar_idx=i
                )

                zone_status = None
                zone_type = None
                zone_price_low = None
                zone_price_high = None

                from app.features.zone_bias import ZoneType

                if zone is not None:
                    zone_idx = all_zones.index(zone)
                    zone_type = zone.zone_type.value
                    zone_price_low = zone.price_low
                    zone_price_high = zone.price_high

                    # Check if price is inside zone (between zone_low and zone_high)
                    price_in_zone = zone.price_low <= current_price <= zone.price_high

                    # Check if price touched/entered zone this bar
                    bar_touched_zone = (
                        (current_low <= zone.price_high and current_high >= zone.price_low)
                    )

                    # Track zone engagement
                    if zone_idx not in zone_engagement:
                        # First time seeing this zone
                        if price_in_zone or bar_touched_zone:
                            # Price entered zone
                            zone_engagement[zone_idx] = {
                                "entered_at_bar": i,
                                "entry_price": current_price,
                                "status": "IN_ZONE",
                            }
                            zone_status = "IN_ZONE"
                        elif distance_pct <= zone_scorer.entry_buffer_pct * 2:
                            # Price approaching zone
                            zone_status = "APPROACHING"
                    else:
                        # We've tracked this zone before
                        eng = zone_engagement[zone_idx]

                        if eng["status"] in ("HELD", "BROKEN"):
                            # Zone outcome already determined
                            zone_status = eng["status"]
                        elif price_in_zone or bar_touched_zone:
                            # Still in zone
                            zone_status = "IN_ZONE"
                        else:
                            # Price has left the zone - determine outcome
                            if zone.zone_type == ZoneType.DEMAND:
                                # Demand zone: HELD if price above zone, BROKEN if price below
                                if current_price > zone.price_high:
                                    eng["status"] = "HELD"
                                    zone_status = "HELD"
                                elif current_price < zone.price_low:
                                    eng["status"] = "BROKEN"
                                    zone_status = "BROKEN"
                            else:  # SUPPLY
                                # Supply zone: HELD if price below zone, BROKEN if price above
                                if current_price < zone.price_low:
                                    eng["status"] = "HELD"
                                    zone_status = "HELD"
                                elif current_price > zone.price_high:
                                    eng["status"] = "BROKEN"
                                    zone_status = "BROKEN"

                # Count orderflow signals by direction for detailed analysis
                abs_bull = sum(1 for s in abs_dicts if s.get("direction") == "BULLISH")
                abs_bear = sum(1 for s in abs_dicts if s.get("direction") == "BEARISH")
                exh_bull = sum(1 for s in exh_dicts if s.get("direction") == "BULLISH")
                exh_bear = sum(1 for s in exh_dicts if s.get("direction") == "BEARISH")
                du_bull = sum(1 for s in du_dicts if s.get("direction") == "BULLISH")
                du_bear = sum(1 for s in du_dicts if s.get("direction") == "BEARISH")

                # ============================================================
                # Alternative Orderflow Scoring (if approach != ORIGINAL)
                # ============================================================
                at_zone = zone_status == "IN_ZONE"

                if self.orderflow_approach != OrderflowApproach.ORIGINAL:
                    # Use alternative orderflow scoring
                    alt_of_score, alt_of_mode, _ = calculate_alternative_orderflow_score(
                        approach=self.orderflow_approach,
                        abs_dicts=abs_dicts,
                        exh_dicts=exh_dicts,
                        du_dicts=du_dicts,
                        ldr=ldr,
                        obi_ratio=ldr,  # Using LDR as OBI proxy
                        cvd=cvd,
                        at_zone=at_zone,
                        zone_type=zone_type,
                    )

                    # Recalculate total score with alternative weights
                    alt_total_score = recalculate_total_score(
                        approach=self.orderflow_approach,
                        trend_score=bias_result.trend_structure.score,
                        intensity_score=bias_result.market_intensity.score,
                        orderflow_score=alt_of_score,
                        at_zone=at_zone,
                    )

                    # Determine mode from new total score
                    if alt_total_score <= 30:
                        alt_mode = "HIGH_BEARISH"
                    elif alt_total_score <= 45:
                        alt_mode = "WEAK_BEARISH"
                    elif alt_total_score <= 55:
                        alt_mode = "NEUTRAL"
                    elif alt_total_score <= 70:
                        alt_mode = "WEAK_BULLISH"
                    else:
                        alt_mode = "HIGH_BULLISH"

                    final_total_score = alt_total_score
                    final_mode = alt_mode
                    final_of_score = alt_of_score
                    final_of_mode = alt_of_mode
                else:
                    # Use original scores
                    final_total_score = bias_result.total_score
                    final_mode = bias_result.mode.value
                    final_of_score = bias_result.orderflow_alpha.score
                    final_of_mode = bias_result.orderflow_alpha.active_mode

                snapshots.append(BiasSnapshot(
                    timestamp=current_row["timestamp"],
                    price=current_price,
                    total_score=final_total_score,
                    mode=final_mode,
                    trend_score=bias_result.trend_structure.score,
                    intensity_score=bias_result.market_intensity.score,
                    orderflow_score=final_of_score,
                    orderflow_mode=final_of_mode,
                    confidence=bias_result.confidence,
                    active_signals=bias_result.orderflow_alpha.active_signals,
                    absorption_bullish=abs_bull,
                    absorption_bearish=abs_bear,
                    exhaustion_bullish=exh_bull,
                    exhaustion_bearish=exh_bear,
                    delta_unwind_bullish=du_bull,
                    delta_unwind_bearish=du_bear,
                    cvd_value=cvd,
                    zone_status=zone_status,
                    zone_type=zone_type,
                    zone_quality=zone_quality if zone is not None else 0.0,
                    zone_price_low=zone_price_low,
                    zone_price_high=zone_price_high,
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

    def _load_15m_data(self, symbol: str = "MNQ", limit: int = 5000) -> pl.DataFrame:
        """Load 15M data for orderflow signal detection on HTF"""
        query = f"""
            SELECT * FROM (
                SELECT
                    timestamp,
                    open, high, low, close, volume,
                    instant_delta, instant_delta as bar_delta,
                    dom_imbalance, cvd, trade_flow_ratio
                FROM ohlcv_ticks
                WHERE symbol = '{symbol}' AND timeframe = '15M'
                ORDER BY timestamp DESC
                LIMIT {limit}
            ) subq
            ORDER BY timestamp ASC
        """
        df = self.db.conn.execute(query).pl()
        logger.info(f"Loaded {len(df)} bars of 15M data for orderflow signals")
        return df

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
    ) -> Tuple[BacktestSummary, List[BiasSnapshot], List[TransitionResult], pl.DataFrame]:
        """Run complete backtest"""

        # Load data
        df = self.load_data(timeframe=timeframe, symbol=symbol, limit=limit)

        if len(df) == 0:
            logger.error("No data loaded")
            return None, [], [], df

        # Calculate bias series
        logger.info("Calculating Agent Bias Score for each bar...")
        snapshots = self.calculate_bias_series(df, timeframe=timeframe, symbol=symbol)

        if len(snapshots) == 0:
            logger.error("Could not calculate bias snapshots")
            return None, [], [], df

        # Detect transitions
        transitions = self.detect_mode_transitions(snapshots)

        # Calculate returns
        transition_results = self.calculate_transition_returns(snapshots, transitions)
        accuracy_results = self.calculate_score_accuracy(snapshots)

        # Calculate summary
        summary = self.calculate_summary(
            snapshots, transitions, transition_results, accuracy_results
        )

        return summary, snapshots, transition_results, df

    def detect_swing_points(
        self,
        df: pl.DataFrame,
        window: int = 5,
    ) -> List[SwingPoint]:
        """Detect swing highs and lows using window-based comparison

        A swing high is a local maximum where the high is >= all highs in window before and after.
        A swing low is a local minimum where the low is <= all lows in window before and after.

        Args:
            df: DataFrame with high, low, timestamp columns
            window: Number of bars on each side to compare (default 5)

        Returns:
            List of SwingPoint objects
        """
        swings = []
        rows = df.to_dicts()

        if len(rows) < window * 2 + 1:
            return swings

        for i in range(window, len(rows) - window):
            current_high = rows[i]["high"]
            current_low = rows[i]["low"]

            # Check for swing high
            is_swing_high = True
            for j in range(1, window + 1):
                if rows[i - j]["high"] > current_high or rows[i + j]["high"] > current_high:
                    is_swing_high = False
                    break

            if is_swing_high:
                swings.append(SwingPoint(
                    timestamp=rows[i]["timestamp"],
                    price=current_high,
                    swing_type="HIGH",
                    bar_index=i,
                ))

            # Check for swing low
            is_swing_low = True
            for j in range(1, window + 1):
                if rows[i - j]["low"] < current_low or rows[i + j]["low"] < current_low:
                    is_swing_low = False
                    break

            if is_swing_low:
                swings.append(SwingPoint(
                    timestamp=rows[i]["timestamp"],
                    price=current_low,
                    swing_type="LOW",
                    bar_index=i,
                ))

        # Sort by bar_index
        swings.sort(key=lambda x: x.bar_index)
        logger.info(f"Detected {len(swings)} swing points ({sum(1 for s in swings if s.swing_type == 'HIGH')} highs, {sum(1 for s in swings if s.swing_type == 'LOW')} lows)")
        return swings

    def analyze_bias_at_swings(
        self,
        swings: List[SwingPoint],
        snapshots: List[BiasSnapshot],
        df: pl.DataFrame,
    ) -> List[SwingBiasResult]:
        """Analyze agent bias score at each swing point

        For swing highs: Bearish bias (score < 45) = predicting reversal down
        For swing lows: Bullish bias (score > 55) = predicting reversal up

        Returns:
            List of SwingBiasResult with forward return analysis
        """
        results = []

        # Build timestamp -> snapshot mapping
        ts_to_snapshot = {s.timestamp: s for s in snapshots}
        rows = df.to_dicts()

        for swing in swings:
            # Find the bias snapshot at this swing
            snapshot = ts_to_snapshot.get(swing.timestamp)
            if snapshot is None:
                continue

            # Get forward prices
            idx = swing.bar_index
            price_5 = rows[idx + 5]["close"] if idx + 5 < len(rows) else None
            price_10 = rows[idx + 10]["close"] if idx + 10 < len(rows) else None
            price_20 = rows[idx + 20]["close"] if idx + 20 < len(rows) else None

            if price_5 is None:
                continue

            # Calculate forward returns
            ret_5 = (price_5 - swing.price) / swing.price
            ret_10 = (price_10 - swing.price) / swing.price if price_10 else 0
            ret_20 = (price_20 - swing.price) / swing.price if price_20 else 0

            # Determine if bias predicted reversal
            if swing.swing_type == "HIGH":
                # At swing high, bearish bias = predicting reversal (price goes down)
                predicted_reversal = snapshot.total_score < self.bearish_threshold
                # Actual reversal = price went down
                actual_5 = ret_5 < -0.001  # At least 0.1% down
                actual_10 = ret_10 < -0.001 if price_10 else False
                actual_20 = ret_20 < -0.001 if price_20 else False
            else:  # LOW
                # At swing low, bullish bias = predicting reversal (price goes up)
                predicted_reversal = snapshot.total_score > self.bullish_threshold
                # Actual reversal = price went up
                actual_5 = ret_5 > 0.001  # At least 0.1% up
                actual_10 = ret_10 > 0.001 if price_10 else False
                actual_20 = ret_20 > 0.001 if price_20 else False

            # Did the prediction match reality?
            correct = predicted_reversal == actual_5

            results.append(SwingBiasResult(
                swing=swing,
                bias_score=snapshot.total_score,
                mode=snapshot.mode,
                orderflow_score=snapshot.orderflow_score,
                orderflow_mode=snapshot.orderflow_mode,
                predicted_reversal=predicted_reversal,
                forward_return_5=ret_5,
                forward_return_10=ret_10,
                forward_return_20=ret_20,
                actual_reversal_5=actual_5,
                actual_reversal_10=actual_10,
                actual_reversal_20=actual_20,
                correct_prediction=correct,
            ))

        return results

    def run_swing_backtest(
        self,
        timeframe: str = "1H",
        symbol: str = "MNQ",
        limit: int = 10000,
        swing_window: int = 5,
    ) -> Tuple[List[SwingBiasResult], List[BiasSnapshot], pl.DataFrame]:
        """Run backtest focused on swing highs and lows

        Tests whether the agent bias score correctly predicts reversals at swing points.

        Args:
            timeframe: Bar timeframe
            symbol: Trading symbol
            limit: Max bars to load
            swing_window: Window size for swing detection (bars on each side)

        Returns:
            Tuple of (swing_results, snapshots, df)
        """
        # Load data
        df = self.load_data(timeframe=timeframe, symbol=symbol, limit=limit)

        if len(df) == 0:
            logger.error("No data loaded")
            return [], [], df

        # Calculate bias series
        logger.info("Calculating Agent Bias Score for each bar...")
        snapshots = self.calculate_bias_series(df, timeframe=timeframe, symbol=symbol)

        if len(snapshots) == 0:
            logger.error("Could not calculate bias snapshots")
            return [], [], df

        # Detect swing points
        logger.info(f"Detecting swing points (window={swing_window})...")
        swings = self.detect_swing_points(df, window=swing_window)

        if len(swings) == 0:
            logger.error("No swing points detected")
            return [], snapshots, df

        # Analyze bias at swing points
        logger.info("Analyzing bias at swing points...")
        swing_results = self.analyze_bias_at_swings(swings, snapshots, df)

        return swing_results, snapshots, df

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
        snapshots = self.calculate_bias_series(df, timeframe=timeframe, symbol=symbol)
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


def print_zone_tracking(snapshots: List[BiasSnapshot], df: pl.DataFrame):
    """Print zone tracking analysis - shows zone entries with full scores and prediction accuracy"""
    print("\n" + "=" * 140)
    print("ZONE TRACKING ANALYSIS - Score-Based Prediction")
    print("=" * 140)

    # Find first IN_ZONE bar for each unique zone entry
    rows = df.to_dicts()
    ts_to_idx = {}
    for i, row in enumerate(rows):
        ts = row["timestamp"]
        if hasattr(ts, "timestamp"):
            ts_to_idx[int(ts.timestamp())] = i
        else:
            ts_to_idx[ts] = i

    # Track zone entries (first IN_ZONE after not being in zone)
    zone_entries = []
    last_zone_key = None

    for s in snapshots:
        if s.zone_status == "IN_ZONE":
            zone_key = (s.zone_type, s.zone_price_low, s.zone_price_high)
            if zone_key != last_zone_key:
                # New zone entry
                zone_entries.append(s)
            last_zone_key = zone_key
        elif s.zone_status in ("HELD", "BROKEN"):
            pass
        else:
            last_zone_key = None

    if not zone_entries:
        print("\nNo zone entries detected")
        return

    # Build analysis data for each zone entry
    zone_analysis = []
    for entry in zone_entries:
        ts = entry.timestamp
        if hasattr(ts, "timestamp"):
            ts_key = int(ts.timestamp())
        else:
            ts_key = ts

        idx = ts_to_idx.get(ts_key)
        if idx is None:
            continue

        # Find final status for this zone entry
        final_status = None
        for s in snapshots:
            if s.zone_price_low == entry.zone_price_low and s.zone_price_high == entry.zone_price_high:
                if s.zone_status in ("HELD", "BROKEN"):
                    final_status = s.zone_status
                    break

        if final_status is None:
            continue  # Zone test not completed

        # Calculate forward returns
        price_5 = rows[idx + 5]["close"] if idx + 5 < len(rows) else None
        price_10 = rows[idx + 10]["close"] if idx + 10 < len(rows) else None
        ret_5 = ((price_5 - entry.price) / entry.price * 100) if price_5 else 0
        ret_10 = ((price_10 - entry.price) / entry.price * 100) if price_10 else 0

        # Determine score prediction
        # SUPPLY: bearish score (<45) predicts HOLD, bullish (>55) predicts BREAK
        # DEMAND: bullish score (>55) predicts HOLD, bearish (<45) predicts BREAK
        score = entry.total_score
        if entry.zone_type == "SUPPLY":
            if score < 45:
                predicted = "HOLD"
            elif score > 55:
                predicted = "BREAK"
            else:
                predicted = "NEUTRAL"
        else:  # DEMAND
            if score > 55:
                predicted = "HOLD"
            elif score < 45:
                predicted = "BREAK"
            else:
                predicted = "NEUTRAL"

        # Check if prediction was correct
        actual = "HOLD" if final_status == "HELD" else "BREAK"
        correct = (predicted == actual) if predicted != "NEUTRAL" else None

        zone_analysis.append({
            "timestamp": ts,
            "zone_type": entry.zone_type,
            "price": entry.price,
            "zone_low": entry.zone_price_low,
            "zone_high": entry.zone_price_high,
            "total_score": score,
            "trend_score": entry.trend_score,
            "intensity_score": entry.intensity_score,
            "of_score": entry.orderflow_score,
            "of_mode": entry.orderflow_mode,
            "of_signals": entry.active_signals,
            # Detailed orderflow signal counts
            "abs_bull": entry.absorption_bullish,
            "abs_bear": entry.absorption_bearish,
            "exh_bull": entry.exhaustion_bullish,
            "exh_bear": entry.exhaustion_bearish,
            "du_bull": entry.delta_unwind_bullish,
            "du_bear": entry.delta_unwind_bearish,
            "cvd": entry.cvd_value,
            "predicted": predicted,
            "actual": actual,
            "correct": correct,
            "ret_5": ret_5,
            "ret_10": ret_10,
        })

    # Summary statistics
    print(f"\nTotal Zone Tests: {len(zone_analysis)}")

    # Prediction accuracy
    predictions_with_direction = [z for z in zone_analysis if z["predicted"] != "NEUTRAL"]
    neutral_predictions = [z for z in zone_analysis if z["predicted"] == "NEUTRAL"]

    if predictions_with_direction:
        correct_count = sum(1 for z in predictions_with_direction if z["correct"])
        accuracy = correct_count / len(predictions_with_direction) * 100
        print(f"  Directional Predictions: {len(predictions_with_direction)} ({len(neutral_predictions)} neutral)")
        print(f"  Prediction Accuracy: {accuracy:.1f}% ({correct_count}/{len(predictions_with_direction)})")

    # Breakdown by zone type
    print(f"\n{'-' * 140}")
    print("PREDICTION ACCURACY BY ZONE TYPE")
    print(f"{'-' * 140}")

    for zone_type in ["SUPPLY", "DEMAND"]:
        zones = [z for z in zone_analysis if z["zone_type"] == zone_type]
        directional = [z for z in zones if z["predicted"] != "NEUTRAL"]

        print(f"\n  {zone_type} Zones ({len(zones)} tests):")

        if directional:
            correct = sum(1 for z in directional if z["correct"])
            print(f"    Directional predictions: {len(directional)}")
            print(f"    Correct: {correct} ({correct/len(directional)*100:.1f}%)")

            # Break down by prediction type
            hold_preds = [z for z in directional if z["predicted"] == "HOLD"]
            break_preds = [z for z in directional if z["predicted"] == "BREAK"]

            if hold_preds:
                hold_correct = sum(1 for z in hold_preds if z["correct"])
                print(f"    Predicted HOLD: {len(hold_preds)}, Correct: {hold_correct} ({hold_correct/len(hold_preds)*100:.1f}%)")
            if break_preds:
                break_correct = sum(1 for z in break_preds if z["correct"])
                print(f"    Predicted BREAK: {len(break_preds)}, Correct: {break_correct} ({break_correct/len(break_preds)*100:.1f}%)")
        else:
            print(f"    No directional predictions (all neutral)")

    # Component analysis - which component predicted best?
    print(f"\n{'-' * 140}")
    print("COMPONENT ANALYSIS - Which component predicted zone outcomes?")
    print(f"{'-' * 140}")

    # For each completed zone test, check if each component correctly predicted
    component_results = {
        "Total Score": {"correct": 0, "total": 0},
        "Trend (20%)": {"correct": 0, "total": 0},
        "Intensity (20%)": {"correct": 0, "total": 0},
        "Orderflow (60%)": {"correct": 0, "total": 0},
    }

    for z in zone_analysis:
        zone_type = z["zone_type"]
        actual = z["actual"]

        # For each component, check if it correctly predicted
        # SUPPLY: bearish (<45) = HOLD, bullish (>55) = BREAK
        # DEMAND: bullish (>55) = HOLD, bearish (<45) = BREAK
        for comp_name, score in [
            ("Total Score", z["total_score"]),
            ("Trend (20%)", z["trend_score"]),
            ("Intensity (20%)", z["intensity_score"]),
            ("Orderflow (60%)", z["of_score"]),
        ]:
            if zone_type == "SUPPLY":
                if score < 45:
                    comp_pred = "HOLD"
                elif score > 55:
                    comp_pred = "BREAK"
                else:
                    continue  # Neutral, skip
            else:  # DEMAND
                if score > 55:
                    comp_pred = "HOLD"
                elif score < 45:
                    comp_pred = "BREAK"
                else:
                    continue

            component_results[comp_name]["total"] += 1
            if comp_pred == actual:
                component_results[comp_name]["correct"] += 1

    print(f"\n  {'Component':<20} {'Predictions':>12} {'Correct':>10} {'Accuracy':>10}")
    print(f"  {'-' * 55}")

    for comp, data in component_results.items():
        if data["total"] > 0:
            acc = data["correct"] / data["total"] * 100
            print(f"  {comp:<20} {data['total']:>12} {data['correct']:>10} {acc:>9.1f}%")

    # Orderflow signal breakdown at zone entries
    print(f"\n{'-' * 140}")
    print("ORDERFLOW SIGNALS BREAKDOWN AT ZONE ENTRIES")
    print(f"{'-' * 140}")

    # Aggregate signal counts
    total_abs_bull = sum(z["abs_bull"] for z in zone_analysis)
    total_abs_bear = sum(z["abs_bear"] for z in zone_analysis)
    total_exh_bull = sum(z["exh_bull"] for z in zone_analysis)
    total_exh_bear = sum(z["exh_bear"] for z in zone_analysis)
    total_du_bull = sum(z["du_bull"] for z in zone_analysis)
    total_du_bear = sum(z["du_bear"] for z in zone_analysis)

    print(f"\n  Signal counts across all {len(zone_analysis)} zone entries:")
    print(f"    Absorption:    {total_abs_bull:>3} bullish, {total_abs_bear:>3} bearish")
    print(f"    Exhaustion:    {total_exh_bull:>3} bullish, {total_exh_bear:>3} bearish")
    print(f"    Delta Unwind:  {total_du_bull:>3} bullish, {total_du_bear:>3} bearish")

    # Analyze by zone type and outcome
    for zone_type in ["SUPPLY", "DEMAND"]:
        zones = [z for z in zone_analysis if z["zone_type"] == zone_type]
        if not zones:
            continue

        held = [z for z in zones if z["actual"] == "HOLD"]
        broken = [z for z in zones if z["actual"] == "BREAK"]

        print(f"\n  {zone_type} Zones:")

        if held:
            abs_b = sum(z["abs_bull"] for z in held)
            abs_s = sum(z["abs_bear"] for z in held)
            exh_b = sum(z["exh_bull"] for z in held)
            exh_s = sum(z["exh_bear"] for z in held)
            du_b = sum(z["du_bull"] for z in held)
            du_s = sum(z["du_bear"] for z in held)
            avg_cvd = sum(z["cvd"] or 0 for z in held) / len(held)
            print(f"    HELD ({len(held)} zones):   ABS {abs_b}b/{abs_s}s | EXH {exh_b}b/{exh_s}s | DU {du_b}b/{du_s}s | avgCVD {avg_cvd:+.0f}")

        if broken:
            abs_b = sum(z["abs_bull"] for z in broken)
            abs_s = sum(z["abs_bear"] for z in broken)
            exh_b = sum(z["exh_bull"] for z in broken)
            exh_s = sum(z["exh_bear"] for z in broken)
            du_b = sum(z["du_bull"] for z in broken)
            du_s = sum(z["du_bear"] for z in broken)
            avg_cvd = sum(z["cvd"] or 0 for z in broken) / len(broken)
            print(f"    BROKEN ({len(broken)} zones): ABS {abs_b}b/{abs_s}s | EXH {exh_b}b/{exh_s}s | DU {du_b}b/{du_s}s | avgCVD {avg_cvd:+.0f}")

    # Analyze wrong predictions - what orderflow signals were firing?
    wrong_preds = [z for z in zone_analysis if z["correct"] is False]
    if wrong_preds:
        print(f"\n  WRONG PREDICTIONS ({len(wrong_preds)} cases) - Orderflow signals that misled:")
        for z in wrong_preds[:10]:
            ts_str = z["timestamp"].strftime("%m-%d %H:%M") if hasattr(z["timestamp"], "strftime") else str(z["timestamp"])[:11]
            sig_str = f"ABS {z['abs_bull']}b/{z['abs_bear']}s EXH {z['exh_bull']}b/{z['exh_bear']}s DU {z['du_bull']}b/{z['du_bear']}s"
            cvd_str = f"CVD {z['cvd']:+.0f}" if z['cvd'] else "CVD -"
            print(f"    {ts_str} {z['zone_type']:<6} OF={z['of_score']:.0f} ({z['of_mode']:<8}) | {sig_str} | {cvd_str} | pred={z['predicted']} actual={z['actual']}")

    # Detailed zone entries
    print(f"\n{'-' * 140}")
    print("ZONE ENTRY DETAILS (with orderflow breakdown)")
    print(f"{'-' * 140}")

    print(f"\n  {'Timestamp':<16} {'Type':<7} {'Score':>5} {'Tr':>4} {'Int':>4} {'OF':>4} {'OFMode':<8} {'ABS':>5} {'EXH':>5} {'DU':>5} {'CVD':>7} {'Pred':>5} {'Actual':>5} {'OK':>3}")
    print(f"  {'-' * 120}")

    for z in zone_analysis[:25]:
        ts_str = z["timestamp"].strftime("%m-%d %H:%M") if hasattr(z["timestamp"], "strftime") else str(z["timestamp"])[:11]
        correct_str = "Yes" if z["correct"] else ("No" if z["correct"] is False else "-")
        # Format signal counts as bull/bear
        abs_str = f"{z['abs_bull']}/{z['abs_bear']}"
        exh_str = f"{z['exh_bull']}/{z['exh_bear']}"
        du_str = f"{z['du_bull']}/{z['du_bear']}"
        cvd_str = f"{z['cvd']:+.0f}" if z['cvd'] else "-"

        print(f"  {ts_str:<16} {z['zone_type']:<7} {z['total_score']:>5.0f} {z['trend_score']:>4.0f} "
              f"{z['intensity_score']:>4.0f} {z['of_score']:>4.0f} {z['of_mode']:<8} {abs_str:>5} {exh_str:>5} {du_str:>5} "
              f"{cvd_str:>7} {z['predicted']:>5} {z['actual']:>5} {correct_str:>3}")

    print(f"\n  Showing {min(25, len(zone_analysis))} of {len(zone_analysis)} zone tests")
    print("=" * 140)


def print_swing_results(results: List[SwingBiasResult]):
    """Print swing point backtest results"""
    print("\n" + "=" * 90)
    print("SWING POINT BIAS ANALYSIS")
    print("=" * 90)

    if not results:
        print("No swing point results")
        return

    # Separate swing highs and lows
    highs = [r for r in results if r.swing.swing_type == "HIGH"]
    lows = [r for r in results if r.swing.swing_type == "LOW"]

    print(f"\nTotal Swing Points: {len(results)}")
    print(f"  Swing Highs: {len(highs)}")
    print(f"  Swing Lows: {len(lows)}")

    # Calculate accuracy by swing type
    print(f"\n{'-' * 90}")
    print("SWING HIGH ANALYSIS (Did bearish bias predict reversal down?)")
    print(f"{'-' * 90}")

    if highs:
        # Predicted reversal = bearish bias at swing high
        predicted_reversals = [r for r in highs if r.predicted_reversal]
        no_prediction = [r for r in highs if not r.predicted_reversal]

        # Actual reversals (price went down)
        actual_reversals_5 = [r for r in highs if r.actual_reversal_5]
        actual_reversals_10 = [r for r in highs if r.actual_reversal_10]

        # Correct predictions (predicted reversal and it happened, or didn't predict and it didn't happen)
        correct_5 = sum(1 for r in highs if r.predicted_reversal == r.actual_reversal_5)
        correct_10 = sum(1 for r in highs if r.predicted_reversal == r.actual_reversal_10)

        print(f"  Swing Highs with Bearish Bias (predicted reversal): {len(predicted_reversals)}")
        print(f"  Swing Highs with Neutral/Bullish Bias (no reversal signal): {len(no_prediction)}")
        print(f"  Actual Reversals (price fell): {len(actual_reversals_5)} (5-bar), {len(actual_reversals_10)} (10-bar)")
        print(f"\n  Prediction Accuracy:")
        print(f"    5-bar:  {correct_5 / len(highs) * 100:.1f}%")
        print(f"    10-bar: {correct_10 / len(highs) * 100:.1f}%")

        # Hit rate when bearish bias present
        if predicted_reversals:
            hit_rate = sum(1 for r in predicted_reversals if r.actual_reversal_5) / len(predicted_reversals) * 100
            avg_ret = sum(r.forward_return_5 for r in predicted_reversals) / len(predicted_reversals) * 100
            print(f"\n  When Bearish Bias at Swing High:")
            print(f"    Hit Rate (price fell): {hit_rate:.1f}%")
            print(f"    Avg Return (5-bar): {-avg_ret:.4f}% (short direction)")
    else:
        print("  No swing highs detected")

    print(f"\n{'-' * 90}")
    print("SWING LOW ANALYSIS (Did bullish bias predict reversal up?)")
    print(f"{'-' * 90}")

    if lows:
        # Predicted reversal = bullish bias at swing low
        predicted_reversals = [r for r in lows if r.predicted_reversal]
        no_prediction = [r for r in lows if not r.predicted_reversal]

        # Actual reversals (price went up)
        actual_reversals_5 = [r for r in lows if r.actual_reversal_5]
        actual_reversals_10 = [r for r in lows if r.actual_reversal_10]

        # Correct predictions
        correct_5 = sum(1 for r in lows if r.predicted_reversal == r.actual_reversal_5)
        correct_10 = sum(1 for r in lows if r.predicted_reversal == r.actual_reversal_10)

        print(f"  Swing Lows with Bullish Bias (predicted reversal): {len(predicted_reversals)}")
        print(f"  Swing Lows with Neutral/Bearish Bias (no reversal signal): {len(no_prediction)}")
        print(f"  Actual Reversals (price rose): {len(actual_reversals_5)} (5-bar), {len(actual_reversals_10)} (10-bar)")
        print(f"\n  Prediction Accuracy:")
        print(f"    5-bar:  {correct_5 / len(lows) * 100:.1f}%")
        print(f"    10-bar: {correct_10 / len(lows) * 100:.1f}%")

        # Hit rate when bullish bias present
        if predicted_reversals:
            hit_rate = sum(1 for r in predicted_reversals if r.actual_reversal_5) / len(predicted_reversals) * 100
            avg_ret = sum(r.forward_return_5 for r in predicted_reversals) / len(predicted_reversals) * 100
            print(f"\n  When Bullish Bias at Swing Low:")
            print(f"    Hit Rate (price rose): {hit_rate:.1f}%")
            print(f"    Avg Return (5-bar): {avg_ret:.4f}% (long direction)")
    else:
        print("  No swing lows detected")

    # Overall stats
    print(f"\n{'-' * 90}")
    print("OVERALL PERFORMANCE")
    print(f"{'-' * 90}")

    all_predicted = [r for r in results if r.predicted_reversal]
    if all_predicted:
        overall_hit = sum(1 for r in all_predicted if r.correct_prediction) / len(all_predicted) * 100
        print(f"  Total Swing Points with Directional Bias: {len(all_predicted)}")
        print(f"  Overall Hit Rate (predicted reversal happened): {overall_hit:.1f}%")
    else:
        print("  No directional bias predictions at swing points")

    # Score distribution at swings
    print(f"\n{'-' * 90}")
    print("BIAS SCORE DISTRIBUTION AT SWING POINTS")
    print(f"{'-' * 90}")

    high_scores = [r.bias_score for r in highs] if highs else []
    low_scores = [r.bias_score for r in lows] if lows else []

    if high_scores:
        print(f"\n  At Swing Highs:")
        print(f"    Avg Score: {sum(high_scores)/len(high_scores):.1f}")
        print(f"    Min/Max: {min(high_scores):.1f} / {max(high_scores):.1f}")
        bearish_count = sum(1 for s in high_scores if s < 45)
        print(f"    Bearish (< 45): {bearish_count} ({bearish_count/len(high_scores)*100:.1f}%)")

    if low_scores:
        print(f"\n  At Swing Lows:")
        print(f"    Avg Score: {sum(low_scores)/len(low_scores):.1f}")
        print(f"    Min/Max: {min(low_scores):.1f} / {max(low_scores):.1f}")
        bullish_count = sum(1 for s in low_scores if s > 55)
        print(f"    Bullish (> 55): {bullish_count} ({bullish_count/len(low_scores)*100:.1f}%)")

    print("=" * 90)


def print_swing_details(results: List[SwingBiasResult], limit: int = 30):
    """Print individual swing point details"""
    print("\n" + "=" * 130)
    print("SWING POINT DETAILS")
    print("=" * 130)

    print(f"\n{'Timestamp':<20} {'Type':<6} {'Price':>10} {'Score':>6} {'Mode':<15} {'Predicted':>10} {'Actual5':>8} {'Ret5%':>10} {'Correct':>8}")
    print("-" * 130)

    for r in results[:limit]:
        ts_str = r.swing.timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(r.swing.timestamp, "strftime") else str(r.swing.timestamp)[:16]
        predicted = "Yes" if r.predicted_reversal else "No"
        actual = "Yes" if r.actual_reversal_5 else "No"
        correct = "Yes" if r.correct_prediction else "No"

        print(f"{ts_str:<20} {r.swing.swing_type:<6} {r.swing.price:>10.2f} {r.bias_score:>6.1f} "
              f"{r.mode:<15} {predicted:>10} {actual:>8} {r.forward_return_5*100:>9.4f}% {correct:>8}")

    print("-" * 130)
    print(f"Showing {min(limit, len(results))} of {len(results)} swing points")


def main():
    parser = argparse.ArgumentParser(description="Backtest Agent Bias Score")
    parser.add_argument("--timeframe", "-t", default="15M", help="Bar timeframe")
    parser.add_argument("--symbol", "-s", default="MNQ", help="Trading symbol")
    parser.add_argument("--limit", "-l", type=int, default=10000, help="Max bars to load")
    parser.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    parser.add_argument("--swing-points", action="store_true", help="Run swing point analysis")
    parser.add_argument("--swing-window", type=int, default=5, help="Swing detection window (bars on each side)")
    parser.add_argument("--show-transitions", action="store_true", help="Show mode transitions")
    parser.add_argument("--show-distribution", action="store_true", help="Show score distribution")
    parser.add_argument("--show-swings", action="store_true", help="Show individual swing points")
    parser.add_argument("--show-zones", action="store_true", help="Show zone tracking analysis")

    # Threshold parameters
    parser.add_argument("--bullish-thresh", type=float, default=55.0, help="Bullish score threshold")
    parser.add_argument("--bearish-thresh", type=float, default=45.0, help="Bearish score threshold")
    parser.add_argument("--hc-thresh", type=float, default=70.0, help="High conviction threshold")
    parser.add_argument("--min-duration", type=int, default=3, help="Min mode duration for transitions")

    # Orderflow approach parameters
    parser.add_argument("--of-approach", default="original",
                        choices=["original", "signal_req", "cvd_dir", "int_zone", "comp_agree", "trend_int"],
                        help="Orderflow scoring approach")
    parser.add_argument("--compare-approaches", action="store_true",
                        help="Run and compare all orderflow approaches")

    args = parser.parse_args()

    if args.swing_points:
        print(f"\nRunning Swing Point Analysis on {args.timeframe}...")
        backtester = AgentBiasBacktester(
            bullish_threshold=args.bullish_thresh,
            bearish_threshold=args.bearish_thresh,
        )

        swing_results, snapshots, df = backtester.run_swing_backtest(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
            swing_window=args.swing_window,
        )

        if swing_results:
            print_swing_results(swing_results)

            if args.show_swings:
                print_swing_details(swing_results)

            if args.show_distribution and snapshots:
                print_score_distribution(snapshots)

    elif args.sweep:
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

    elif args.compare_approaches:
        # Run comparison across all orderflow approaches
        print(f"\n{'='*100}")
        print(f"ORDERFLOW APPROACH COMPARISON - {args.timeframe} {args.symbol}")
        print(f"{'='*100}")

        approaches = [
            ("original", "Original (CVD magnitude in BASE)"),
            ("signal_req", "Signal Required (neutral if no ABS/EXH/DU)"),
            ("cvd_dir", "CVD Direction (direction not magnitude)"),
            ("int_zone", "Intensity Zone (boost Intensity at zones)"),
            ("comp_agree", "Component Agreement (2+ must agree)"),
            ("trend_int", "Trend+Intensity (orderflow neutral in BASE)"),
        ]

        results = []
        for approach, desc in approaches:
            backtester = AgentBiasBacktester(
                bullish_threshold=args.bullish_thresh,
                bearish_threshold=args.bearish_thresh,
                high_conviction_threshold=args.hc_thresh,
                min_mode_duration=args.min_duration,
                orderflow_approach=approach,
            )

            summary, snapshots, _, df = backtester.run_backtest(
                timeframe=args.timeframe,
                symbol=args.symbol,
                limit=args.limit,
            )

            if summary and snapshots:
                # Calculate zone prediction accuracy
                zone_entries = [s for s in snapshots if s.zone_status == "IN_ZONE"]
                zone_tests = []
                last_zone_key = None
                for s in snapshots:
                    if s.zone_status == "IN_ZONE":
                        zone_key = (s.zone_type, s.zone_price_low, s.zone_price_high)
                        if zone_key != last_zone_key:
                            zone_tests.append(s)
                        last_zone_key = zone_key
                    elif s.zone_status not in ("HELD", "BROKEN"):
                        last_zone_key = None

                # Get zone outcomes
                zone_correct = 0
                zone_total = 0
                for entry in zone_tests:
                    final_status = None
                    for s in snapshots:
                        if s.zone_price_low == entry.zone_price_low and s.zone_price_high == entry.zone_price_high:
                            if s.zone_status in ("HELD", "BROKEN"):
                                final_status = s.zone_status
                                break
                    if final_status:
                        zone_total += 1
                        score = entry.total_score
                        if entry.zone_type == "SUPPLY":
                            predicted = "HOLD" if score < 45 else ("BREAK" if score > 55 else "NEUTRAL")
                        else:
                            predicted = "HOLD" if score > 55 else ("BREAK" if score < 45 else "NEUTRAL")
                        actual = "HOLD" if final_status == "HELD" else "BREAK"
                        if predicted != "NEUTRAL" and predicted == actual:
                            zone_correct += 1

                zone_acc = (zone_correct / zone_total * 100) if zone_total > 0 else 0

                results.append({
                    "approach": approach,
                    "desc": desc,
                    "hit_rate_5": summary.overall_hit_rate_5,
                    "hit_rate_10": summary.overall_hit_rate_10,
                    "profit_factor": summary.profit_factor,
                    "zone_accuracy": zone_acc,
                    "zone_tests": zone_total,
                })

        # Print comparison table
        print(f"\n{'Approach':<15} {'Description':<40} {'Hit5%':>8} {'Hit10%':>8} {'PF':>8} {'Zone%':>8} {'#Zones':>7}")
        print("-" * 100)

        for r in results:
            print(f"{r['approach']:<15} {r['desc']:<40} {r['hit_rate_5']:>7.1f}% {r['hit_rate_10']:>7.1f}% "
                  f"{r['profit_factor']:>8.2f} {r['zone_accuracy']:>7.1f}% {r['zone_tests']:>7}")

        # Find best approach for each metric
        print(f"\n{'-'*100}")
        print("BEST APPROACH BY METRIC:")
        if results:
            best_hit5 = max(results, key=lambda x: x['hit_rate_5'])
            best_hit10 = max(results, key=lambda x: x['hit_rate_10'])
            best_pf = max(results, key=lambda x: x['profit_factor'])
            best_zone = max(results, key=lambda x: x['zone_accuracy'])

            print(f"  Best 5-bar Hit Rate:   {best_hit5['approach']} ({best_hit5['hit_rate_5']:.1f}%)")
            print(f"  Best 10-bar Hit Rate:  {best_hit10['approach']} ({best_hit10['hit_rate_10']:.1f}%)")
            print(f"  Best Profit Factor:    {best_pf['approach']} ({best_pf['profit_factor']:.2f})")
            print(f"  Best Zone Accuracy:    {best_zone['approach']} ({best_zone['zone_accuracy']:.1f}%)")

    else:
        backtester = AgentBiasBacktester(
            bullish_threshold=args.bullish_thresh,
            bearish_threshold=args.bearish_thresh,
            high_conviction_threshold=args.hc_thresh,
            min_mode_duration=args.min_duration,
            orderflow_approach=args.of_approach,
        )

        if args.of_approach != "original":
            print(f"\nUsing orderflow approach: {args.of_approach}")

        summary, snapshots, transition_results, df = backtester.run_backtest(
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

            if args.show_zones and snapshots:
                print_zone_tracking(snapshots, df)


if __name__ == "__main__":
    main()
