"""Zone Detection Backtester

Dedicated backtester to evaluate zone detection accuracy across all timeframes.
Uses the improved N-bar trend analysis for zone classification.

Key Metrics:
- Zone Count: How many zones are detected per timeframe
- Test Rate: What % of zones get tested (price returns to zone)
- Hold Rate: What % of tested zones hold (price bounces)
- Break Rate: What % of tested zones break (price goes through)
- Quality Correlation: Do higher quality zones perform better?

Zone Detection Logic (DBR/RBD):
- DEMAND (DBR): Downtrend -> Base -> Rally (bullish displacement)
- SUPPLY (RBD): Uptrend -> Base -> Drop (bearish displacement)

Uses N-bar trend analysis (5 bars) instead of single pre-base candle.

Usage:
    python scripts/backtesting/backtest_zone_detection.py
    python scripts/backtesting/backtest_zone_detection.py --timeframe 1H --show-zones
    python scripts/backtesting/backtest_zone_detection.py --all-timeframes
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import argparse
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from enum import Enum
from datetime import datetime
import polars as pl
import numpy as np

from app.data.storage import DuckDBStorage

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ============================================================================
# Configuration
# ============================================================================

TIMEFRAMES = ["5M", "15M", "1H", "4H", "1D"]

# Zone decay half-life by timeframe (in bars)
ZONE_HALFLIFE = {
    "5M": 100,
    "15M": 60,
    "1H": 40,
    "4H": 30,
    "1D": 20,
}

# Detection parameters by timeframe
# More relaxed for HTFs since big moves are rarer
TIMEFRAME_PARAMS = {
    "5M": {"displacement_ratio": 1.5, "base_body_ratio": 0.7, "trend_lookback": 5},
    "15M": {"displacement_ratio": 1.5, "base_body_ratio": 0.7, "trend_lookback": 5},
    "1H": {"displacement_ratio": 1.5, "base_body_ratio": 0.7, "trend_lookback": 5},
    "4H": {"displacement_ratio": 1.5, "base_body_ratio": 0.7, "trend_lookback": 5},
    "1D": {"displacement_ratio": 1.3, "base_body_ratio": 0.7, "trend_lookback": 5},
}


class ZoneType(str, Enum):
    DEMAND = "DEMAND"
    SUPPLY = "SUPPLY"


class ZoneOutcome(str, Enum):
    UNTESTED = "UNTESTED"  # Price never returned to zone
    HELD = "HELD"          # Price tested zone and bounced
    BROKEN = "BROKEN"      # Price went through zone


@dataclass
class DetectedZone:
    """A detected supply or demand zone."""
    zone_type: ZoneType
    price_low: float
    price_high: float
    formed_at: datetime
    formed_bar_idx: int
    timeframe: str

    # Formation characteristics
    displacement_strength: float  # Body size / ATR
    trend_clarity: float          # % of trend bars in expected direction
    base_candle_count: int        # Number of base candles
    base_tightness: float         # Zone height / ATR (smaller = tighter)

    # Quality score (0-100)
    quality_score: float

    # Outcome tracking (filled during backtest)
    outcome: ZoneOutcome = ZoneOutcome.UNTESTED
    tested_at: Optional[datetime] = None
    tested_bar_idx: Optional[int] = None
    bars_until_test: Optional[int] = None
    bounce_strength: Optional[float] = None  # If held, how strong was bounce

    @property
    def zone_midpoint(self) -> float:
        return (self.price_low + self.price_high) / 2

    @property
    def zone_height(self) -> float:
        return self.price_high - self.price_low


@dataclass
class TimeframeResults:
    """Results for a single timeframe."""
    timeframe: str
    total_bars: int

    # Zone counts
    total_zones: int
    supply_zones: int
    demand_zones: int

    # Outcome breakdown
    untested_zones: int
    held_zones: int
    broken_zones: int

    # Rates
    test_rate: float      # % of zones that got tested
    hold_rate: float      # % of tested zones that held

    # Quality analysis
    avg_quality_held: float
    avg_quality_broken: float
    high_quality_hold_rate: float  # Hold rate for quality >= 60

    # Time analysis
    avg_bars_to_test: float

    # Detailed zones (for export/inspection)
    zones: List[DetectedZone] = field(default_factory=list)


# ============================================================================
# Zone Detector
# ============================================================================

class ZoneDetector:
    """Detects supply and demand zones using N-bar trend analysis."""

    def __init__(
        self,
        atr_period: int = 14,
        displacement_ratio: float = 1.5,
        base_body_ratio: float = 0.7,
        trend_lookback: int = 5,
        max_base_candles: int = 3,
    ):
        self.atr_period = atr_period
        self.displacement_ratio = displacement_ratio
        self.base_body_ratio = base_body_ratio
        self.trend_lookback = trend_lookback
        self.max_base_candles = max_base_candles

        self.db = DuckDBStorage()

    @classmethod
    def for_timeframe(cls, timeframe: str) -> "ZoneDetector":
        """Create detector with timeframe-specific parameters."""
        params = TIMEFRAME_PARAMS.get(timeframe, TIMEFRAME_PARAMS["1H"])
        return cls(
            displacement_ratio=params["displacement_ratio"],
            base_body_ratio=params["base_body_ratio"],
            trend_lookback=params["trend_lookback"],
        )

    def load_data(
        self,
        timeframe: str,
        symbol: str = "MNQ",
        limit: int = 10000,
    ) -> pl.DataFrame:
        """Load OHLCV data for zone detection."""
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

    def prepare_data(self, df: pl.DataFrame) -> pl.DataFrame:
        """Add derived columns for zone detection."""
        df = df.with_columns([
            (pl.col("close") - pl.col("open")).abs().alias("body_size"),
            (pl.col("high") - pl.col("low")).alias("candle_range"),
            (pl.col("close") > pl.col("open")).alias("is_bullish"),
        ])
        df = df.with_columns([
            pl.col("candle_range").rolling_mean(window_size=self.atr_period).alias("atr"),
        ])
        return df

    def detect_zones(
        self,
        df: pl.DataFrame,
        timeframe: str,
    ) -> List[DetectedZone]:
        """Detect all zones in the data using N-bar trend analysis.

        Zone Detection Logic:
        1. Find displacement candles (body > displacement_ratio * ATR)
        2. Look for base candle(s) before displacement (1-3 small candles)
        3. Analyze N-bar trend BEFORE the base to determine zone type
        4. DEMAND: Downtrend -> Base -> Rally (bullish displacement)
        5. SUPPLY: Uptrend -> Base -> Drop (bearish displacement)
        """
        df = self.prepare_data(df)
        rows = df.to_dicts()
        zones = []

        min_idx = self.atr_period + self.trend_lookback + self.max_base_candles + 1

        for i in range(min_idx, len(rows)):
            curr = rows[i]
            atr = curr.get("atr")

            if atr is None or atr == 0:
                continue

            body_size = curr["body_size"]

            # Check for displacement candle
            if body_size <= atr * self.displacement_ratio:
                continue

            is_bullish = curr["is_bullish"]

            # Find base candle(s) before displacement
            base_start = i - 1
            base_end = i - 1

            # Extend base backwards if multiple small candles
            for j in range(i - 1, max(i - self.max_base_candles - 1, min_idx - 1), -1):
                candidate = rows[j]
                candidate_atr = candidate.get("atr", atr) or atr
                if candidate["body_size"] <= candidate_atr * self.base_body_ratio:
                    base_start = j
                else:
                    break

            # Need at least one valid base candle
            if base_start > base_end:
                continue

            base_candles = rows[base_start:base_end + 1]
            if not base_candles:
                continue

            # Verify immediate pre-displacement candle is valid base
            immediate_base = rows[i - 1]
            immediate_atr = immediate_base.get("atr", atr) or atr
            if immediate_base["body_size"] > immediate_atr * self.base_body_ratio:
                continue

            # Analyze N-bar trend BEFORE base
            trend_start = base_start - self.trend_lookback
            if trend_start < 0:
                continue

            trend_bars = rows[trend_start:base_start]
            if len(trend_bars) < 3:
                continue

            # Count bullish/bearish candles in trend
            bullish_candles = sum(1 for b in trend_bars if b["is_bullish"])
            bearish_candles = len(trend_bars) - bullish_candles

            # Check price movement
            trend_price_start = trend_bars[0]["open"]
            trend_price_end = trend_bars[-1]["close"]
            price_change = trend_price_end - trend_price_start
            price_trending_up = price_change > 0

            # Determine zone type
            zone_type = None
            trend_clarity = 0.0

            # DEMAND (DBR): Downtrend -> Base -> Rally
            if is_bullish:
                if bearish_candles >= bullish_candles or not price_trending_up:
                    zone_type = ZoneType.DEMAND
                    trend_clarity = bearish_candles / len(trend_bars)

            # SUPPLY (RBD): Uptrend -> Base -> Drop
            else:
                if bullish_candles >= bearish_candles or price_trending_up:
                    zone_type = ZoneType.SUPPLY
                    trend_clarity = bullish_candles / len(trend_bars)

            if zone_type is None:
                continue

            # Calculate zone boundaries from base candles
            zone_low = min(b["low"] for b in base_candles)
            zone_high = max(b["high"] for b in base_candles)

            # Calculate quality metrics
            displacement_strength = body_size / atr
            base_tightness = (zone_high - zone_low) / atr
            base_candle_count = len(base_candles)

            # Quality score (0-100)
            # IMPORTANT: Backtesting reveals optimal characteristics:
            #
            # Displacement strength (body/ATR ratio):
            #   - Optimal: 1.5-2.5x ATR (~85% hold rate)
            #   - Too strong (>2.5x): indicates continuation, not reversal (~25% hold)
            #
            # Trend clarity (% bars in trend direction before base):
            #   - Optimal: 40-80% (~90% hold rate)
            #   - Too clear (>80%): strong unbroken trend likely to continue (~69% hold)
            #   - Too mixed (<40%): not a clear setup (~67% hold)

            # Displacement score
            if displacement_strength <= 2.0:
                # Good zone: 1.5-2.0x ATR range
                disp_score = (displacement_strength - self.displacement_ratio) * 20  # 0-10 pts
            elif displacement_strength <= 2.5:
                # Optimal zone: 2.0-2.5x ATR range
                disp_score = 10 + (displacement_strength - 2.0) * 10  # 10-15 pts
            else:
                # Excessive displacement (>2.5x): likely continuation, penalize
                excess = displacement_strength - 2.5
                disp_score = max(-15.0, 15 - excess * 20)  # 15 pts minus penalty

            # Trend clarity score (penalize extremes)
            if trend_clarity < 0.4:
                # Too mixed - not a clear trend before base
                trend_score = trend_clarity * 25  # 0-10 pts (reduced)
            elif trend_clarity <= 0.8:
                # Optimal range: 40-80%
                trend_score = 10 + (trend_clarity - 0.4) * 25  # 10-20 pts
            else:
                # Too clear (>80%): strong trend likely to continue
                excess = trend_clarity - 0.8
                trend_score = max(5.0, 20 - excess * 50)  # 20 pts minus penalty

            # Base candle count (1 or 3 bases best, 2 bases worst based on data)
            if base_candle_count == 1:
                base_count_bonus = 10.0  # Clean single base - best (91.3% hold)
            elif base_candle_count == 3:
                base_count_bonus = 8.0   # Extended consolidation - good (83.1% hold)
            else:  # 2 bases
                base_count_bonus = 3.0   # Indecision - worst (75.0% hold)

            # Base tightness - INVERTED from intuition!
            # Wider bases (>1.0x ATR) have 92% hold rate
            # Tight bases (0.5-1.0x ATR) have only 64.7% hold rate
            # Wider base = more consolidation = more order absorption
            if base_tightness >= 1.0:
                base_tightness_score = 10.0  # Wide base - best
            elif base_tightness >= 0.5:
                base_tightness_score = 0.0   # Tight base - no bonus
            else:
                base_tightness_score = -5.0  # Very tight - penalize

            quality_score = 30 + disp_score + trend_score + base_count_bonus + base_tightness_score
            quality_score = min(100.0, max(0.0, quality_score))

            zone = DetectedZone(
                zone_type=zone_type,
                price_low=zone_low,
                price_high=zone_high,
                formed_at=curr["timestamp"],
                formed_bar_idx=i,
                timeframe=timeframe,
                displacement_strength=displacement_strength,
                trend_clarity=trend_clarity,
                base_candle_count=base_candle_count,
                base_tightness=base_tightness,
                quality_score=quality_score,
            )
            zones.append(zone)

        supply_count = sum(1 for z in zones if z.zone_type == ZoneType.SUPPLY)
        demand_count = sum(1 for z in zones if z.zone_type == ZoneType.DEMAND)
        logger.info(f"Detected {len(zones)} zones ({supply_count} supply, {demand_count} demand)")

        return zones


# ============================================================================
# Zone Backtester
# ============================================================================

class ZoneBacktester:
    """Backtests zone detection by tracking outcomes."""

    def __init__(
        self,
        test_buffer_pct: float = 0.001,  # Price within 0.1% of zone = test
        break_threshold_pct: float = 0.002,  # Price 0.2% through zone = break
    ):
        self.test_buffer_pct = test_buffer_pct
        self.break_threshold_pct = break_threshold_pct

    def evaluate_zones(
        self,
        df: pl.DataFrame,
        zones: List[DetectedZone],
    ) -> List[DetectedZone]:
        """Evaluate each zone's outcome by tracking forward price action."""
        rows = df.to_dicts()

        for zone in zones:
            # Start looking from bar after zone formed
            start_idx = zone.formed_bar_idx + 1

            for i in range(start_idx, len(rows)):
                row = rows[i]
                low = row["low"]
                high = row["high"]

                # Check if price reached zone
                zone_buffer = zone.zone_height * self.test_buffer_pct
                break_depth = zone.zone_height * self.break_threshold_pct

                if zone.zone_type == ZoneType.DEMAND:
                    # Demand zone: look for price to come DOWN to zone
                    if low <= zone.price_high + zone_buffer:
                        # Zone was tested
                        zone.tested_at = row["timestamp"]
                        zone.tested_bar_idx = i
                        zone.bars_until_test = i - zone.formed_bar_idx

                        # Check if broken (price went through zone)
                        if low < zone.price_low - break_depth:
                            zone.outcome = ZoneOutcome.BROKEN
                        else:
                            zone.outcome = ZoneOutcome.HELD
                            # Calculate bounce strength
                            # Look ahead a few bars to see how strong the bounce was
                            bounce_high = high
                            for j in range(i + 1, min(i + 5, len(rows))):
                                bounce_high = max(bounce_high, rows[j]["high"])
                            zone.bounce_strength = (bounce_high - zone.price_high) / zone.zone_height
                        break

                else:  # SUPPLY
                    # Supply zone: look for price to come UP to zone
                    if high >= zone.price_low - zone_buffer:
                        # Zone was tested
                        zone.tested_at = row["timestamp"]
                        zone.tested_bar_idx = i
                        zone.bars_until_test = i - zone.formed_bar_idx

                        # Check if broken (price went through zone)
                        if high > zone.price_high + break_depth:
                            zone.outcome = ZoneOutcome.BROKEN
                        else:
                            zone.outcome = ZoneOutcome.HELD
                            # Calculate bounce strength (downward)
                            bounce_low = low
                            for j in range(i + 1, min(i + 5, len(rows))):
                                bounce_low = min(bounce_low, rows[j]["low"])
                            zone.bounce_strength = (zone.price_low - bounce_low) / zone.zone_height
                        break

        return zones

    def calculate_results(
        self,
        timeframe: str,
        total_bars: int,
        zones: List[DetectedZone],
    ) -> TimeframeResults:
        """Calculate statistics for evaluated zones."""

        supply_zones = [z for z in zones if z.zone_type == ZoneType.SUPPLY]
        demand_zones = [z for z in zones if z.zone_type == ZoneType.DEMAND]

        untested = [z for z in zones if z.outcome == ZoneOutcome.UNTESTED]
        held = [z for z in zones if z.outcome == ZoneOutcome.HELD]
        broken = [z for z in zones if z.outcome == ZoneOutcome.BROKEN]
        tested = held + broken

        # Calculate rates
        test_rate = len(tested) / len(zones) if zones else 0.0
        hold_rate = len(held) / len(tested) if tested else 0.0

        # Quality analysis
        avg_quality_held = np.mean([z.quality_score for z in held]) if held else 0.0
        avg_quality_broken = np.mean([z.quality_score for z in broken]) if broken else 0.0

        high_quality_zones = [z for z in tested if z.quality_score >= 60]
        high_quality_held = [z for z in high_quality_zones if z.outcome == ZoneOutcome.HELD]
        high_quality_hold_rate = len(high_quality_held) / len(high_quality_zones) if high_quality_zones else 0.0

        # Time analysis
        tested_with_time = [z for z in tested if z.bars_until_test is not None]
        avg_bars_to_test = np.mean([z.bars_until_test for z in tested_with_time]) if tested_with_time else 0.0

        return TimeframeResults(
            timeframe=timeframe,
            total_bars=total_bars,
            total_zones=len(zones),
            supply_zones=len(supply_zones),
            demand_zones=len(demand_zones),
            untested_zones=len(untested),
            held_zones=len(held),
            broken_zones=len(broken),
            test_rate=test_rate,
            hold_rate=hold_rate,
            avg_quality_held=avg_quality_held,
            avg_quality_broken=avg_quality_broken,
            high_quality_hold_rate=high_quality_hold_rate,
            avg_bars_to_test=avg_bars_to_test,
            zones=zones,
        )


# ============================================================================
# Output Functions
# ============================================================================

def print_results(results: TimeframeResults):
    """Print results for a single timeframe."""

    print(f"\n{'=' * 70}")
    print(f"ZONE DETECTION RESULTS: {results.timeframe}")
    print(f"{'=' * 70}")

    print(f"\nData: {results.total_bars} bars")

    print(f"\nZones Detected: {results.total_zones}")
    print(f"  Supply zones: {results.supply_zones}")
    print(f"  Demand zones: {results.demand_zones}")

    print(f"\nOutcomes:")
    print(f"  Untested: {results.untested_zones} ({results.untested_zones/results.total_zones*100:.1f}%)" if results.total_zones else "  Untested: 0")
    print(f"  Held:     {results.held_zones}")
    print(f"  Broken:   {results.broken_zones}")

    print(f"\nKey Metrics:")
    print(f"  Test Rate: {results.test_rate:.1%} (zones that got tested)")
    print(f"  Hold Rate: {results.hold_rate:.1%} (tested zones that held)")

    print(f"\nQuality Analysis:")
    print(f"  Avg Quality (Held):   {results.avg_quality_held:.1f}")
    print(f"  Avg Quality (Broken): {results.avg_quality_broken:.1f}")
    print(f"  High-Quality Hold Rate (Q>=60): {results.high_quality_hold_rate:.1%}")

    print(f"\nTiming:")
    print(f"  Avg Bars to Test: {results.avg_bars_to_test:.1f}")


def print_zones_detail(zones: List[DetectedZone], limit: int = 20):
    """Print detailed zone information."""

    print(f"\n{'=' * 120}")
    print("ZONE DETAILS")
    print(f"{'=' * 120}")

    print(f"\n{'Formed':<20} {'Type':<8} {'Low':>10} {'High':>10} {'Quality':>8} "
          f"{'Disp':>6} {'Trend':>6} {'Bases':>6} {'Outcome':<10} {'Test@':>8}")
    print("-" * 120)

    for zone in zones[:limit]:
        ts_str = zone.formed_at.strftime("%Y-%m-%d %H:%M") if hasattr(zone.formed_at, "strftime") else str(zone.formed_at)[:16]
        test_bars = str(zone.bars_until_test) if zone.bars_until_test else "-"

        print(f"{ts_str:<20} {zone.zone_type.value:<8} {zone.price_low:>10.2f} {zone.price_high:>10.2f} "
              f"{zone.quality_score:>8.1f} {zone.displacement_strength:>6.2f} {zone.trend_clarity:>6.1%} "
              f"{zone.base_candle_count:>6} {zone.outcome.value:<10} {test_bars:>8}")

    print("-" * 120)
    print(f"Showing {min(limit, len(zones))} of {len(zones)} zones")


def print_comparison(all_results: List[TimeframeResults]):
    """Print comparison across all timeframes."""

    print(f"\n{'=' * 90}")
    print("CROSS-TIMEFRAME COMPARISON")
    print(f"{'=' * 90}")

    print(f"\n{'TF':<6} {'Bars':>8} {'Zones':>8} {'Supply':>8} {'Demand':>8} "
          f"{'Test%':>8} {'Hold%':>8} {'HQ Hold%':>10}")
    print("-" * 90)

    for r in all_results:
        print(f"{r.timeframe:<6} {r.total_bars:>8} {r.total_zones:>8} {r.supply_zones:>8} {r.demand_zones:>8} "
              f"{r.test_rate:>8.1%} {r.hold_rate:>8.1%} {r.high_quality_hold_rate:>10.1%}")

    print("-" * 90)

    # Summary insights
    print("\nInsights:")

    # Best hold rate
    tested_results = [r for r in all_results if r.held_zones + r.broken_zones > 0]
    if tested_results:
        best_hold = max(tested_results, key=lambda r: r.hold_rate)
        print(f"  Best Hold Rate: {best_hold.timeframe} ({best_hold.hold_rate:.1%})")

    # Best high-quality performance
    hq_results = [r for r in all_results if len([z for z in r.zones if z.quality_score >= 60 and z.outcome != ZoneOutcome.UNTESTED]) > 0]
    if hq_results:
        best_hq = max(hq_results, key=lambda r: r.high_quality_hold_rate)
        print(f"  Best HQ Hold Rate: {best_hq.timeframe} ({best_hq.high_quality_hold_rate:.1%})")

    # Check for imbalances
    for r in all_results:
        if r.supply_zones > 0 and r.demand_zones == 0:
            print(f"  WARNING: {r.timeframe} has {r.supply_zones} supply but 0 demand zones")
        elif r.demand_zones > 0 and r.supply_zones == 0:
            print(f"  WARNING: {r.timeframe} has {r.demand_zones} demand but 0 supply zones")


def print_quality_breakdown(zones: List[DetectedZone]):
    """Print breakdown by quality tier."""

    print(f"\n{'=' * 70}")
    print("QUALITY TIER BREAKDOWN")
    print(f"{'=' * 70}")

    tiers = [
        ("High (Q>=70)", [z for z in zones if z.quality_score >= 70]),
        ("Mid (50<=Q<70)", [z for z in zones if 50 <= z.quality_score < 70]),
        ("Low (Q<50)", [z for z in zones if z.quality_score < 50]),
    ]

    print(f"\n{'Tier':<20} {'Count':>8} {'Tested':>8} {'Held':>8} {'Broken':>8} {'Hold%':>10}")
    print("-" * 70)

    for tier_name, tier_zones in tiers:
        if not tier_zones:
            print(f"{tier_name:<20} {0:>8} {'-':>8} {'-':>8} {'-':>8} {'-':>10}")
            continue

        tested = [z for z in tier_zones if z.outcome != ZoneOutcome.UNTESTED]
        held = [z for z in tier_zones if z.outcome == ZoneOutcome.HELD]
        broken = [z for z in tier_zones if z.outcome == ZoneOutcome.BROKEN]
        hold_rate = len(held) / len(tested) if tested else 0.0

        print(f"{tier_name:<20} {len(tier_zones):>8} {len(tested):>8} {len(held):>8} {len(broken):>8} {hold_rate:>10.1%}")

    print("-" * 70)


# ============================================================================
# Main
# ============================================================================

def run_backtest(
    timeframe: str,
    symbol: str = "MNQ",
    limit: int = 10000,
    show_zones: bool = False,
) -> TimeframeResults:
    """Run backtest for a single timeframe."""

    detector = ZoneDetector.for_timeframe(timeframe)
    backtester = ZoneBacktester()

    # Load and detect
    df = detector.load_data(timeframe, symbol, limit)
    if len(df) == 0:
        logger.error(f"No data for {timeframe}")
        return TimeframeResults(
            timeframe=timeframe, total_bars=0, total_zones=0,
            supply_zones=0, demand_zones=0, untested_zones=0,
            held_zones=0, broken_zones=0, test_rate=0.0, hold_rate=0.0,
            avg_quality_held=0.0, avg_quality_broken=0.0,
            high_quality_hold_rate=0.0, avg_bars_to_test=0.0, zones=[],
        )

    zones = detector.detect_zones(df, timeframe)

    if not zones:
        logger.warning(f"No zones detected for {timeframe}")
        return TimeframeResults(
            timeframe=timeframe, total_bars=len(df), total_zones=0,
            supply_zones=0, demand_zones=0, untested_zones=0,
            held_zones=0, broken_zones=0, test_rate=0.0, hold_rate=0.0,
            avg_quality_held=0.0, avg_quality_broken=0.0,
            high_quality_hold_rate=0.0, avg_bars_to_test=0.0, zones=[],
        )

    # Prepare data and evaluate
    df = detector.prepare_data(df)
    zones = backtester.evaluate_zones(df, zones)

    # Calculate results
    results = backtester.calculate_results(timeframe, len(df), zones)

    # Print results
    print_results(results)

    if show_zones:
        print_zones_detail(zones)
        print_quality_breakdown(zones)

    return results


def main():
    parser = argparse.ArgumentParser(description="Backtest zone detection accuracy")
    parser.add_argument("--timeframe", "-t", default="15M", help="Bar timeframe")
    parser.add_argument("--symbol", "-s", default="MNQ", help="Trading symbol")
    parser.add_argument("--limit", "-l", type=int, default=10000, help="Max bars")
    parser.add_argument("--all-timeframes", "-a", action="store_true", help="Run all timeframes")
    parser.add_argument("--show-zones", action="store_true", help="Show zone details")

    args = parser.parse_args()

    if args.all_timeframes:
        all_results = []
        for tf in TIMEFRAMES:
            results = run_backtest(
                timeframe=tf,
                symbol=args.symbol,
                limit=args.limit,
                show_zones=args.show_zones,
            )
            all_results.append(results)

        # Print comparison
        print_comparison(all_results)
    else:
        run_backtest(
            timeframe=args.timeframe,
            symbol=args.symbol,
            limit=args.limit,
            show_zones=args.show_zones,
        )


if __name__ == "__main__":
    main()
