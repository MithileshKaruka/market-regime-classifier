"""Supply/Demand Zone Backtester

Detects supply and demand zones using the RBD/DBR (Explosive Displacement) pattern
and evaluates zone quality combined with agent bias for trade entries.

Zone Detection Logic:
- Supply Zone (RBD): Rally -> Base -> Drop (explosive displacement down)
- Demand Zone (DBR): Drop -> Base -> Rally (explosive displacement up)

Quality Scoring (0-100 points):
- Displacement strength: 0-20 pts (body > 2.5x ATR)
- Volume confirmation: 0-15 pts (volume > 1.5x avg)
- Fair Value Gap (FVG): 0-20 pts (gap created during displacement)
- Structure Shift: 0-25 pts (swing high/low break)
- Orderflow signals: 0-20 pts (absorption, exhaustion, delta unwind)

Recency Factor:
- Exponential decay with timeframe-specific half-lives
- Test penalty: each zone test reduces quality by 20%

Usage:
    python scripts/backtesting/backtest_sd_zones.py --timeframe 15M
    python scripts/backtesting/backtest_sd_zones.py --timeframe 1H --show-zones
"""
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import argparse
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from enum import Enum
import polars as pl
import numpy as np
from datetime import datetime, timedelta

from app.data.storage import DuckDBStorage
from app.features.orderflow_signals import OrderflowSignalDetector
from config import get_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ============================================================================
# Recency Factor Configuration
# ============================================================================

ZONE_HALFLIFE = {
    "5M": 100,   # ~8 hours of 5M bars
    "15M": 60,   # ~15 hours of 15M bars
    "1H": 40,    # ~40 hours of 1H bars
    "4H": 30,    # ~5 days of 4H bars
    "1D": 20,    # ~20 days
}

# Timeframe-specific detection parameters
# Higher timeframes need lower displacement ratios (big moves are rarer)
TIMEFRAME_DEFAULTS = {
    "5M": {
        "displacement_ratio": 2.5,
        "base_body_ratio": 0.5,
        "volume_mult": 1.5,
        "swing_lookback": 10,
    },
    "15M": {
        "displacement_ratio": 2.5,
        "base_body_ratio": 0.5,
        "volume_mult": 1.5,
        "swing_lookback": 10,
    },
    "1H": {
        "displacement_ratio": 2.5,
        "base_body_ratio": 0.5,
        "volume_mult": 1.5,
        "swing_lookback": 10,
    },
    "4H": {
        "displacement_ratio": 1.8,  # Lower - 2.5x ATR moves are rare on 4H
        "base_body_ratio": 0.6,     # Slightly more lenient base
        "volume_mult": 1.3,         # Lower volume requirement
        "swing_lookback": 7,        # Fewer bars for swing detection
    },
    "1D": {
        "displacement_ratio": 1.5,  # Lower - big daily moves are rare
        "base_body_ratio": 0.7,     # More lenient base (daily consolidation)
        "volume_mult": 1.2,         # Lower volume requirement
        "swing_lookback": 5,        # Fewer bars for swing detection
    },
}


def get_timeframe_defaults(timeframe: str) -> dict:
    """Get default parameters for a timeframe."""
    return TIMEFRAME_DEFAULTS.get(timeframe, TIMEFRAME_DEFAULTS["1H"])


def recency_factor(bars_since_formed: int, timeframe: str) -> float:
    """Calculate zone recency factor using exponential decay.

    Args:
        bars_since_formed: Number of bars since zone was formed
        timeframe: Zone timeframe

    Returns:
        Recency multiplier between 0 and 1
    """
    halflife = ZONE_HALFLIFE.get(timeframe, 50)
    return 0.5 ** (bars_since_formed / halflife)


def test_penalty(times_tested: int) -> float:
    """Calculate penalty for zone tests.

    Each test reduces zone quality by 20%, minimum 40%.

    Args:
        times_tested: Number of times zone has been tested

    Returns:
        Test penalty multiplier between 0.4 and 1.0
    """
    return max(0.4, 1.0 - (times_tested * 0.2))


# ============================================================================
# Data Classes
# ============================================================================

class ZoneType(Enum):
    DEMAND = "DEMAND"
    SUPPLY = "SUPPLY"


class ZoneStatus(Enum):
    FRESH = "FRESH"       # Never tested
    AGING = "AGING"       # Recency factor < 0.7
    TESTED = "TESTED"     # Has been tested but held
    BROKEN = "BROKEN"     # Price broke through zone


@dataclass
class SDZone:
    """Supply or Demand Zone with quality scoring."""

    zone_type: ZoneType
    price_low: float
    price_high: float
    formed_at: datetime
    formed_bar_idx: int
    timeframe: str

    # Base candle info
    base_start_idx: int
    base_end_idx: int

    # Quality factors (calculated at formation)
    displacement_score: float = 0.0  # 0-20 pts
    volume_score: float = 0.0        # 0-15 pts
    fvg_score: float = 0.0           # 0-20 pts (0 if no FVG)
    structure_score: float = 0.0     # 0-25 pts (0 if no BOS)
    orderflow_score: float = 0.0     # 0-20 pts

    # Dynamic state
    times_tested: int = 0
    status: ZoneStatus = ZoneStatus.FRESH
    broken_at: Optional[datetime] = None

    @property
    def base_quality(self) -> float:
        """Total quality score before recency adjustment."""
        return (self.displacement_score + self.volume_score +
                self.fvg_score + self.structure_score + self.orderflow_score)

    @property
    def zone_midpoint(self) -> float:
        """Zone midpoint price."""
        return (self.price_low + self.price_high) / 2

    @property
    def zone_height(self) -> float:
        """Zone height in price."""
        return self.price_high - self.price_low

    def effective_quality(self, current_bar_idx: int) -> float:
        """Calculate effective quality with recency and test penalties.

        Args:
            current_bar_idx: Current bar index for recency calculation

        Returns:
            Effective quality score (0-100)
        """
        if self.status == ZoneStatus.BROKEN:
            return 0

        bars_age = current_bar_idx - self.formed_bar_idx
        recency = recency_factor(bars_age, self.timeframe)
        test_mult = test_penalty(self.times_tested)

        return self.base_quality * recency * test_mult


@dataclass
class FVG:
    """Fair Value Gap detection."""

    direction: str  # BULLISH (gap up) or BEARISH (gap down)
    gap_low: float
    gap_high: float
    bar_idx: int
    gap_size: float  # In price


@dataclass
class StructureShift:
    """Market structure shift (swing break)."""

    direction: str  # BULLISH (broke swing high) or BEARISH (broke swing low)
    broken_level: float
    bar_idx: int


@dataclass
class ZoneEntry:
    """Record of a zone entry for backtesting."""

    zone: SDZone
    entry_bar_idx: int
    entry_timestamp: datetime
    entry_price: float
    zone_quality: float

    # Forward returns (filled after entry)
    forward_return_5: float = 0.0
    forward_return_10: float = 0.0
    forward_return_20: float = 0.0
    hit_5: bool = False
    hit_10: bool = False
    hit_20: bool = False


@dataclass
class BacktestSummary:
    """Summary statistics for S/D zone backtest."""

    total_zones: int
    supply_zones: int
    demand_zones: int
    total_entries: int

    # Hit rates by quality tier
    hit_rate_high_quality: float  # quality >= 70
    hit_rate_mid_quality: float   # 50 <= quality < 70
    hit_rate_low_quality: float   # quality < 50

    # Overall hit rates
    hit_rate_5: float
    hit_rate_10: float
    hit_rate_20: float

    # Returns
    avg_return_5: float
    avg_return_10: float
    avg_return_20: float

    profit_factor: float

    parameters: dict


# ============================================================================
# S/D Zone Detector
# ============================================================================

class SDZoneDetector:
    """Detects Supply and Demand zones using RBD/DBR patterns."""

    def __init__(
        self,
        atr_period: int = 14,
        base_body_ratio: float = 0.5,      # Base candle body < 0.5 * ATR
        displacement_ratio: float = 2.5,    # Displacement body > 2.5 * ATR
        volume_mult: float = 1.5,           # Displacement volume > 1.5x avg
        max_base_candles: int = 3,          # Max candles in base
        min_base_candles: int = 1,          # Min candles in base
        lookback_bars: int = 20,
        swing_lookback: int = 10,           # For structure shift detection
    ):
        self.atr_period = atr_period
        self.base_body_ratio = base_body_ratio
        self.displacement_ratio = displacement_ratio
        self.volume_mult = volume_mult
        self.max_base_candles = max_base_candles
        self.min_base_candles = min_base_candles
        self.lookback_bars = lookback_bars
        self.swing_lookback = swing_lookback

        self.db = DuckDBStorage()

    @classmethod
    def for_timeframe(cls, timeframe: str) -> "SDZoneDetector":
        """Create detector with timeframe-specific default parameters.

        Higher timeframes use lower displacement ratios since big moves are rarer.

        Args:
            timeframe: Bar timeframe (5M, 15M, 1H, 4H, 1D)

        Returns:
            SDZoneDetector configured for the timeframe
        """
        defaults = get_timeframe_defaults(timeframe)
        return cls(
            displacement_ratio=defaults["displacement_ratio"],
            base_body_ratio=defaults["base_body_ratio"],
            volume_mult=defaults["volume_mult"],
            swing_lookback=defaults["swing_lookback"],
        )

    def get_parameters(self) -> dict:
        """Return current parameters."""
        return {
            "atr_period": self.atr_period,
            "base_body_ratio": self.base_body_ratio,
            "displacement_ratio": self.displacement_ratio,
            "volume_mult": self.volume_mult,
            "max_base_candles": self.max_base_candles,
            "min_base_candles": self.min_base_candles,
            "lookback_bars": self.lookback_bars,
            "swing_lookback": self.swing_lookback,
        }

    def load_data(
        self,
        timeframe: str = "15M",
        symbol: str = "MNQ",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50000,
    ) -> pl.DataFrame:
        """Load historical data for zone detection."""

        where_clauses = [f"symbol = '{symbol}'", f"timeframe = '{timeframe}'"]

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
                cvd,
                instant_delta,
                trade_flow_ratio
            FROM ohlcv_ticks
            WHERE {where_str}
            ORDER BY timestamp ASC
            LIMIT {limit}
        """

        df = self.db.conn.execute(query).pl()
        logger.info(f"Loaded {len(df)} bars for {symbol} {timeframe}")

        return df

    def prepare_data(self, df: pl.DataFrame) -> pl.DataFrame:
        """Add derived columns for zone detection."""

        # Body size
        df = df.with_columns([
            (pl.col("close") - pl.col("open")).abs().alias("body_size"),
            (pl.col("high") - pl.col("low")).alias("candle_range"),
            (pl.col("close") > pl.col("open")).alias("is_bullish"),
        ])

        # ATR
        df = df.with_columns([
            pl.col("candle_range").rolling_mean(window_size=self.atr_period).alias("atr"),
        ])

        # Volume average
        df = df.with_columns([
            pl.col("volume").rolling_mean(window_size=self.lookback_bars).alias("avg_volume"),
        ])

        return df

    def find_swing_points(self, rows: List[dict], lookback: int = 5) -> Tuple[List[int], List[int]]:
        """Find actual swing high and low pivot points.

        A swing high is a bar where the high is higher than the N bars before AND after.
        A swing low is a bar where the low is lower than the N bars before AND after.

        Args:
            rows: List of OHLCV dicts
            lookback: Number of bars on each side to confirm swing

        Returns:
            Tuple of (swing_high_indices, swing_low_indices)
        """
        swing_highs = []
        swing_lows = []

        for i in range(lookback, len(rows) - lookback):
            high = rows[i]["high"]
            low = rows[i]["low"]

            # Check if this is a swing high
            is_swing_high = True
            for j in range(1, lookback + 1):
                if rows[i - j]["high"] >= high or rows[i + j]["high"] >= high:
                    is_swing_high = False
                    break

            if is_swing_high:
                swing_highs.append(i)

            # Check if this is a swing low
            is_swing_low = True
            for j in range(1, lookback + 1):
                if rows[i - j]["low"] <= low or rows[i + j]["low"] <= low:
                    is_swing_low = False
                    break

            if is_swing_low:
                swing_lows.append(i)

        return swing_highs, swing_lows

    def detect_fvg(self, rows: List[dict], idx: int) -> Optional[FVG]:
        """Detect Fair Value Gap at given index.

        FVG forms when there's a gap between candle i-2's high/low
        and candle i's low/high (3-candle pattern).

        Args:
            rows: List of OHLCV dicts
            idx: Index of the third candle (displacement candle)

        Returns:
            FVG if detected, None otherwise
        """
        if idx < 2:
            return None

        prev2 = rows[idx - 2]
        curr = rows[idx]

        # Bullish FVG: gap between prev2 high and curr low
        if curr["low"] > prev2["high"]:
            gap_size = curr["low"] - prev2["high"]
            return FVG(
                direction="BULLISH",
                gap_low=prev2["high"],
                gap_high=curr["low"],
                bar_idx=idx,
                gap_size=gap_size,
            )

        # Bearish FVG: gap between prev2 low and curr high
        if curr["high"] < prev2["low"]:
            gap_size = prev2["low"] - curr["high"]
            return FVG(
                direction="BEARISH",
                gap_low=curr["high"],
                gap_high=prev2["low"],
                bar_idx=idx,
                gap_size=gap_size,
            )

        return None

    def detect_structure_shift(
        self,
        rows: List[dict],
        idx: int,
        direction: str,
        swing_highs: List[int],
        swing_lows: List[int],
    ) -> Optional[StructureShift]:
        """Detect market structure shift (swing break).

        A structure shift occurs when the displacement candle breaks a
        recent swing high (bullish) or swing low (bearish) that formed
        before the pattern started.

        Args:
            rows: List of OHLCV dicts
            idx: Current bar index (displacement candle)
            direction: Expected direction (BULLISH = break swing high, BEARISH = break swing low)
            swing_highs: List of swing high bar indices
            swing_lows: List of swing low bar indices

        Returns:
            StructureShift if detected, None otherwise
        """
        if idx < self.swing_lookback * 2:
            return None

        curr = rows[idx]
        lookback_window = self.swing_lookback * 3  # Look for swings in recent history

        if direction == "BULLISH":
            # Find most recent swing high BEFORE current bar (with some buffer)
            recent_swing_highs = [
                sh for sh in swing_highs
                if idx - lookback_window < sh < idx - 2  # Must be before base
            ]

            if not recent_swing_highs:
                return None

            # Get the most recent swing high
            last_swing_idx = max(recent_swing_highs)
            swing_high_price = rows[last_swing_idx]["high"]

            # Check if displacement candle breaks above this swing high
            if curr["close"] > swing_high_price:
                return StructureShift(
                    direction="BULLISH",
                    broken_level=swing_high_price,
                    bar_idx=idx,
                )

        else:  # BEARISH
            # Find most recent swing low BEFORE current bar
            recent_swing_lows = [
                sl for sl in swing_lows
                if idx - lookback_window < sl < idx - 2  # Must be before base
            ]

            if not recent_swing_lows:
                return None

            # Get the most recent swing low
            last_swing_idx = max(recent_swing_lows)
            swing_low_price = rows[last_swing_idx]["low"]

            # Check if displacement candle breaks below this swing low
            if curr["close"] < swing_low_price:
                return StructureShift(
                    direction="BEARISH",
                    broken_level=swing_low_price,
                    bar_idx=idx,
                )

        return None

    def calculate_displacement_score(self, body_size: float, atr: float) -> float:
        """Calculate displacement quality score (0-20).

        Score based on how much body exceeds displacement threshold.
        """
        if atr is None or atr == 0:
            return 0.0

        ratio = body_size / atr

        # Score: linear scale from 2.5x (0 pts) to 5x+ (20 pts)
        if ratio < self.displacement_ratio:
            return 0.0

        score = min(20.0, (ratio - self.displacement_ratio) * 8)
        return score

    def calculate_volume_score(self, volume: float, avg_volume: float) -> float:
        """Calculate volume confirmation score (0-15).

        Score based on volume multiple above average.
        """
        if avg_volume is None or avg_volume == 0:
            return 0.0

        ratio = volume / avg_volume

        if ratio < self.volume_mult:
            return 0.0

        # Score: linear scale from 1.5x (0 pts) to 3x+ (15 pts)
        score = min(15.0, (ratio - self.volume_mult) * 10)
        return score

    def calculate_fvg_score(self, fvg: Optional[FVG], atr: float) -> float:
        """Calculate FVG quality score (0-20).

        Score based on gap size relative to ATR.
        """
        if fvg is None:
            return 0.0

        if atr is None or atr == 0:
            return 10.0  # Give base score if we can't calculate ATR ratio

        # Score: linear scale from 0.5x ATR (5 pts) to 2x+ ATR (20 pts)
        ratio = fvg.gap_size / atr
        score = min(20.0, 5 + ratio * 7.5)
        return score

    def calculate_structure_score(self, shift: Optional[StructureShift]) -> float:
        """Calculate structure shift score (0-25).

        Full score if structure shifted, 0 otherwise.
        """
        return 25.0 if shift is not None else 0.0

    def detect_zones(
        self,
        df: pl.DataFrame,
        timeframe: str,
    ) -> List[SDZone]:
        """Detect Supply and Demand zones in the data.

        RBD Pattern (Supply Zone):
        1. Rally: Bullish candle(s) before base
        2. Base: 1-3 small candles (body < 0.5 * ATR)
        3. Drop: Large bearish candle (body > 2.5 * ATR)

        DBR Pattern (Demand Zone):
        1. Drop: Bearish candle(s) before base
        2. Base: 1-3 small candles (body < 0.5 * ATR)
        3. Rally: Large bullish candle (body > 2.5 * ATR)

        Returns:
            List of detected SDZone objects
        """
        df = self.prepare_data(df)
        rows = df.to_dicts()
        zones = []

        if len(rows) < self.atr_period + self.max_base_candles + 2:
            logger.warning("Not enough data for zone detection")
            return zones

        # Detect orderflow signals on full dataset
        detector = OrderflowSignalDetector(lookback_bars=self.lookback_bars)
        try:
            absorption_signals = detector.detect_absorption(df)
            exhaustion_signals = detector.detect_exhaustion(df)
            delta_unwind_signals = detector.detect_delta_unwind(df)
        except Exception as e:
            logger.warning(f"Orderflow signal detection failed: {e}")
            absorption_signals = []
            exhaustion_signals = []
            delta_unwind_signals = []

        # Build signal lookup by timestamp
        signal_lookup: Dict[datetime, List[dict]] = {}
        for sig in absorption_signals + exhaustion_signals + delta_unwind_signals:
            ts = sig.timestamp
            if ts not in signal_lookup:
                signal_lookup[ts] = []
            signal_lookup[ts].append({
                "type": sig.__class__.__name__,
                "direction": sig.direction.value if hasattr(sig.direction, 'value') else sig.direction,
                "strength": sig.strength,
            })

        # Find swing points for structure shift detection
        swing_highs, swing_lows = self.find_swing_points(rows, lookback=self.swing_lookback)
        logger.info(f"Found {len(swing_highs)} swing highs and {len(swing_lows)} swing lows")

        # Scan for patterns
        for i in range(self.atr_period + self.max_base_candles + 1, len(rows)):
            curr = rows[i]
            atr = curr.get("atr")
            avg_vol = curr.get("avg_volume")

            if atr is None or atr == 0:
                continue

            body_size = curr["body_size"]
            is_displacement = body_size > atr * self.displacement_ratio

            if not is_displacement:
                continue

            is_bullish_displacement = curr["is_bullish"]

            # Look for base candles before displacement
            for base_length in range(self.min_base_candles, self.max_base_candles + 1):
                base_start = i - base_length

                if base_start < 1:
                    continue

                # Check if all base candles have small bodies
                is_valid_base = True
                base_high = -float('inf')
                base_low = float('inf')

                for j in range(base_start, i):
                    base_candle = rows[j]
                    base_atr = base_candle.get("atr", atr)
                    if base_atr is None or base_atr == 0:
                        base_atr = atr

                    if base_candle["body_size"] > base_atr * self.base_body_ratio:
                        is_valid_base = False
                        break

                    base_high = max(base_high, base_candle["high"])
                    base_low = min(base_low, base_candle["low"])

                if not is_valid_base:
                    continue

                # Check pre-base candle direction (rally before RBD, drop before DBR)
                pre_base = rows[base_start - 1]
                pre_bullish = pre_base["is_bullish"]

                # DBR (Demand): Drop -> Base -> Rally
                if is_bullish_displacement and not pre_bullish:
                    zone_type = ZoneType.DEMAND
                    expected_signal_dir = "BULLISH"

                # RBD (Supply): Rally -> Base -> Drop
                elif not is_bullish_displacement and pre_bullish:
                    zone_type = ZoneType.SUPPLY
                    expected_signal_dir = "BEARISH"
                else:
                    continue  # Pattern doesn't match

                # Calculate quality scores
                disp_score = self.calculate_displacement_score(body_size, atr)
                vol_score = self.calculate_volume_score(curr["volume"], avg_vol)

                # Check for FVG
                fvg = self.detect_fvg(rows, i)
                fvg_score = self.calculate_fvg_score(fvg, atr)

                # Check for structure shift
                shift = self.detect_structure_shift(rows, i, expected_signal_dir, swing_highs, swing_lows)
                struct_score = self.calculate_structure_score(shift)

                # Calculate orderflow score from signals at/near displacement
                of_score = 0.0
                for offset in range(-2, 1):  # Check displacement bar and 2 prior
                    check_idx = i + offset
                    if 0 <= check_idx < len(rows):
                        ts = rows[check_idx]["timestamp"]
                        if ts in signal_lookup:
                            for sig in signal_lookup[ts]:
                                if sig["direction"] == expected_signal_dir:
                                    of_score += sig["strength"] * 10
                of_score = min(20.0, of_score)

                zone = SDZone(
                    zone_type=zone_type,
                    price_low=base_low,
                    price_high=base_high,
                    formed_at=curr["timestamp"],
                    formed_bar_idx=i,
                    timeframe=timeframe,
                    base_start_idx=base_start,
                    base_end_idx=i - 1,
                    displacement_score=disp_score,
                    volume_score=vol_score,
                    fvg_score=fvg_score,
                    structure_score=struct_score,
                    orderflow_score=of_score,
                )

                zones.append(zone)
                break  # Found valid base, move to next bar

        logger.info(f"Detected {len(zones)} S/D zones "
                   f"({sum(1 for z in zones if z.zone_type == ZoneType.SUPPLY)} supply, "
                   f"{sum(1 for z in zones if z.zone_type == ZoneType.DEMAND)} demand)")

        return zones


# ============================================================================
# Backtester
# ============================================================================

class SDZoneBacktester:
    """Backtester for S/D zone entries."""

    def __init__(
        self,
        entry_buffer_pct: float = 0.001,  # Enter when price within 0.1% of zone
        stop_beyond_zone: float = 1.5,    # Stop at 1.5x zone height beyond
        min_quality: float = 30.0,        # Minimum zone quality to enter
        use_timeframe_defaults: bool = True,  # Auto-adjust params per timeframe
    ):
        self.entry_buffer_pct = entry_buffer_pct
        self.stop_beyond_zone = stop_beyond_zone
        self.min_quality = min_quality
        self.use_timeframe_defaults = use_timeframe_defaults
        self.detector = SDZoneDetector()

    def get_parameters(self) -> dict:
        """Return combined parameters."""
        params = self.detector.get_parameters()
        params.update({
            "entry_buffer_pct": self.entry_buffer_pct,
            "stop_beyond_zone": self.stop_beyond_zone,
            "min_quality": self.min_quality,
        })
        return params

    def simulate_entries(
        self,
        df: pl.DataFrame,
        zones: List[SDZone],
    ) -> List[ZoneEntry]:
        """Simulate entries when price reaches zones.

        Args:
            df: Price data
            zones: Detected zones

        Returns:
            List of zone entries with forward returns
        """
        entries = []
        rows = df.to_dicts()

        # Track which zones are still active
        active_zones = list(zones)

        for i, row in enumerate(rows):
            price = row["close"]
            low = row["low"]
            high = row["high"]
            ts = row["timestamp"]

            # Check each active zone for entry
            zones_to_remove = []

            for zone in active_zones:
                # Skip if zone formed at or after this bar
                if zone.formed_bar_idx >= i:
                    continue

                # Calculate effective quality
                quality = zone.effective_quality(i)

                if quality < self.min_quality:
                    continue

                # Calculate entry zone (with buffer)
                buffer = zone.zone_height * self.entry_buffer_pct

                # For demand zone: enter when price touches top of zone
                # For supply zone: enter when price touches bottom of zone
                if zone.zone_type == ZoneType.DEMAND:
                    entry_trigger = low <= zone.price_high + buffer
                    stop_level = zone.price_low - zone.zone_height * self.stop_beyond_zone
                    is_stopped = low < stop_level
                else:  # SUPPLY
                    entry_trigger = high >= zone.price_low - buffer
                    stop_level = zone.price_high + zone.zone_height * self.stop_beyond_zone
                    is_stopped = high > stop_level

                if is_stopped:
                    # Zone broken
                    zone.status = ZoneStatus.BROKEN
                    zone.broken_at = ts
                    zones_to_remove.append(zone)
                    continue

                if entry_trigger:
                    # Record zone test
                    zone.times_tested += 1
                    if zone.status == ZoneStatus.FRESH:
                        zone.status = ZoneStatus.TESTED

                    # Create entry
                    entry = ZoneEntry(
                        zone=zone,
                        entry_bar_idx=i,
                        entry_timestamp=ts,
                        entry_price=price,
                        zone_quality=quality,
                    )

                    # Calculate forward returns
                    self._calculate_forward_returns(entry, rows, i)
                    entries.append(entry)

                    # Don't enter same zone multiple times in quick succession
                    # Mark zone as aging after entry
                    if zone.times_tested >= 2:
                        zones_to_remove.append(zone)

            for z in zones_to_remove:
                if z in active_zones:
                    active_zones.remove(z)

        logger.info(f"Simulated {len(entries)} zone entries")
        return entries

    def _calculate_forward_returns(
        self,
        entry: ZoneEntry,
        rows: List[dict],
        idx: int,
    ):
        """Calculate forward returns for an entry."""

        entry_price = entry.entry_price
        is_long = entry.zone.zone_type == ZoneType.DEMAND

        def get_return(bars_ahead: int) -> Tuple[float, bool]:
            if idx + bars_ahead >= len(rows):
                return 0.0, False

            exit_price = rows[idx + bars_ahead]["close"]
            ret = (exit_price - entry_price) / entry_price

            if not is_long:  # Short for supply zone
                ret = -ret

            return ret, ret > 0

        entry.forward_return_5, entry.hit_5 = get_return(5)
        entry.forward_return_10, entry.hit_10 = get_return(10)
        entry.forward_return_20, entry.hit_20 = get_return(20)

    def calculate_summary(
        self,
        zones: List[SDZone],
        entries: List[ZoneEntry],
    ) -> BacktestSummary:
        """Calculate summary statistics."""

        if not entries:
            return BacktestSummary(
                total_zones=len(zones),
                supply_zones=sum(1 for z in zones if z.zone_type == ZoneType.SUPPLY),
                demand_zones=sum(1 for z in zones if z.zone_type == ZoneType.DEMAND),
                total_entries=0,
                hit_rate_high_quality=0.0,
                hit_rate_mid_quality=0.0,
                hit_rate_low_quality=0.0,
                hit_rate_5=0.0,
                hit_rate_10=0.0,
                hit_rate_20=0.0,
                avg_return_5=0.0,
                avg_return_10=0.0,
                avg_return_20=0.0,
                profit_factor=0.0,
                parameters=self.get_parameters(),
            )

        # Split by quality tier
        high_quality = [e for e in entries if e.zone_quality >= 70]
        mid_quality = [e for e in entries if 50 <= e.zone_quality < 70]
        low_quality = [e for e in entries if e.zone_quality < 50]

        def hit_rate(entry_list: List[ZoneEntry], horizon: int = 10) -> float:
            if not entry_list:
                return 0.0
            if horizon == 5:
                return sum(1 for e in entry_list if e.hit_5) / len(entry_list)
            elif horizon == 10:
                return sum(1 for e in entry_list if e.hit_10) / len(entry_list)
            else:
                return sum(1 for e in entry_list if e.hit_20) / len(entry_list)

        # Calculate profit factor (using 10-bar returns)
        wins = [e.forward_return_10 for e in entries if e.forward_return_10 > 0]
        losses = [e.forward_return_10 for e in entries if e.forward_return_10 < 0]

        sum_wins = sum(wins) if wins else 0.0
        sum_losses = abs(sum(losses)) if losses else 0.0001
        pf = sum_wins / sum_losses

        return BacktestSummary(
            total_zones=len(zones),
            supply_zones=sum(1 for z in zones if z.zone_type == ZoneType.SUPPLY),
            demand_zones=sum(1 for z in zones if z.zone_type == ZoneType.DEMAND),
            total_entries=len(entries),
            hit_rate_high_quality=hit_rate(high_quality),
            hit_rate_mid_quality=hit_rate(mid_quality),
            hit_rate_low_quality=hit_rate(low_quality),
            hit_rate_5=sum(1 for e in entries if e.hit_5) / len(entries),
            hit_rate_10=sum(1 for e in entries if e.hit_10) / len(entries),
            hit_rate_20=sum(1 for e in entries if e.hit_20) / len(entries),
            avg_return_5=np.mean([e.forward_return_5 for e in entries]),
            avg_return_10=np.mean([e.forward_return_10 for e in entries]),
            avg_return_20=np.mean([e.forward_return_20 for e in entries]),
            profit_factor=pf,
            parameters=self.get_parameters(),
        )

    def run_backtest(
        self,
        timeframe: str = "15M",
        symbol: str = "MNQ",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50000,
    ) -> Tuple[BacktestSummary, List[SDZone], List[ZoneEntry]]:
        """Run complete backtest.

        Returns:
            Tuple of (summary, zones, entries)
        """
        # Apply timeframe-specific defaults if enabled
        if self.use_timeframe_defaults:
            defaults = get_timeframe_defaults(timeframe)
            self.detector.displacement_ratio = defaults["displacement_ratio"]
            self.detector.base_body_ratio = defaults["base_body_ratio"]
            self.detector.volume_mult = defaults["volume_mult"]
            self.detector.swing_lookback = defaults["swing_lookback"]
            logger.info(f"Using timeframe defaults for {timeframe}: displacement={defaults['displacement_ratio']}, "
                       f"base_body={defaults['base_body_ratio']}, volume={defaults['volume_mult']}")

        # Load data
        df = self.detector.load_data(timeframe, symbol, start_date, end_date, limit)

        if len(df) == 0:
            logger.error("No data loaded")
            return self.calculate_summary([], []), [], []

        # Detect zones
        zones = self.detector.detect_zones(df, timeframe)

        if not zones:
            logger.warning("No zones detected")
            return self.calculate_summary([], []), [], []

        # Prepare data for entry simulation
        df = self.detector.prepare_data(df)

        # Simulate entries
        entries = self.simulate_entries(df, zones)

        # Calculate summary
        summary = self.calculate_summary(zones, entries)

        return summary, zones, entries


# ============================================================================
# Output Functions
# ============================================================================

def print_zones(zones: List[SDZone], limit: int = 20):
    """Print detected zones for review."""

    print("\n" + "=" * 120)
    print("DETECTED S/D ZONES")
    print("=" * 120)
    print(f"\n{'Formed':<20} {'Type':<8} {'Low':>10} {'High':>10} {'Quality':>8} "
          f"{'Disp':>6} {'Vol':>6} {'FVG':>6} {'BOS':>6} {'OF':>6} {'Status':<8}")
    print("-" * 120)

    for zone in zones[:limit]:
        ts_str = zone.formed_at.strftime("%Y-%m-%d %H:%M") if hasattr(zone.formed_at, "strftime") else str(zone.formed_at)[:16]

        print(f"{ts_str:<20} {zone.zone_type.value:<8} {zone.price_low:>10.2f} {zone.price_high:>10.2f} "
              f"{zone.base_quality:>8.1f} {zone.displacement_score:>6.1f} {zone.volume_score:>6.1f} "
              f"{zone.fvg_score:>6.1f} {zone.structure_score:>6.1f} {zone.orderflow_score:>6.1f} "
              f"{zone.status.value:<8}")

    print("-" * 120)
    print(f"Showing {min(limit, len(zones))} of {len(zones)} zones")


def print_entries(entries: List[ZoneEntry], limit: int = 20):
    """Print zone entries for review."""

    print("\n" + "=" * 120)
    print("ZONE ENTRIES")
    print("=" * 120)
    print(f"\n{'Entry Time':<20} {'Type':<8} {'Price':>10} {'Quality':>8} "
          f"{'Ret 5':>10} {'Ret 10':>10} {'Ret 20':>10} {'Hit 10':>8}")
    print("-" * 120)

    for entry in entries[:limit]:
        ts_str = entry.entry_timestamp.strftime("%Y-%m-%d %H:%M") if hasattr(entry.entry_timestamp, "strftime") else str(entry.entry_timestamp)[:16]
        hit = "Yes" if entry.hit_10 else "No"

        print(f"{ts_str:<20} {entry.zone.zone_type.value:<8} {entry.entry_price:>10.2f} "
              f"{entry.zone_quality:>8.1f} {entry.forward_return_5*100:>10.4f}% "
              f"{entry.forward_return_10*100:>10.4f}% {entry.forward_return_20*100:>10.4f}% {hit:>8}")

    print("-" * 120)
    print(f"Showing {min(limit, len(entries))} of {len(entries)} entries")


def print_summary(summary: BacktestSummary):
    """Pretty print backtest summary."""

    print("\n" + "=" * 70)
    print("S/D ZONE BACKTEST RESULTS")
    print("=" * 70)

    print(f"\nParameters:")
    for k, v in summary.parameters.items():
        print(f"  {k}: {v}")

    print(f"\nZones Detected: {summary.total_zones}")
    print(f"  Supply zones: {summary.supply_zones}")
    print(f"  Demand zones: {summary.demand_zones}")

    print(f"\nEntries Simulated: {summary.total_entries}")

    print(f"\nHit Rates by Quality Tier (10-bar horizon):")
    print(f"  High Quality (>=70): {summary.hit_rate_high_quality:.1%}")
    print(f"  Mid Quality (50-69): {summary.hit_rate_mid_quality:.1%}")
    print(f"  Low Quality (<50):   {summary.hit_rate_low_quality:.1%}")

    print(f"\nOverall Hit Rates:")
    print(f"  5-bar:  {summary.hit_rate_5:.1%}")
    print(f"  10-bar: {summary.hit_rate_10:.1%}")
    print(f"  20-bar: {summary.hit_rate_20:.1%}")

    print(f"\nAverage Returns:")
    print(f"  5-bar:  {summary.avg_return_5:.4%}")
    print(f"  10-bar: {summary.avg_return_10:.4%}")
    print(f"  20-bar: {summary.avg_return_20:.4%}")

    print(f"\nProfit Factor: {summary.profit_factor:.2f}")

    print(f"\nInterpretation:")
    if summary.hit_rate_high_quality > 0.65:
        print("  High quality zones show strong predictive value (>65% hit rate)")
    elif summary.hit_rate_high_quality > 0.55:
        print("  High quality zones show moderate predictive value (55-65% hit rate)")
    else:
        print("  High quality zones need parameter tuning (<55% hit rate)")

    if summary.profit_factor > 1.5:
        print("  Strong edge (profit factor > 1.5)")
    elif summary.profit_factor > 1.0:
        print("  Slight edge (profit factor > 1.0)")
    else:
        print("  No edge (profit factor < 1.0)")

    print("=" * 70 + "\n")


def export_zones_csv(zones: List[SDZone], entries: List[ZoneEntry], filepath: str):
    """Export zones and entries to CSV."""
    import csv

    # Export zones
    zones_path = filepath.replace(".csv", "_zones.csv")
    with open(zones_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'formed_at', 'zone_type', 'price_low', 'price_high', 'base_quality',
            'displacement_score', 'volume_score', 'fvg_score', 'structure_score',
            'orderflow_score', 'times_tested', 'status'
        ])

        for zone in zones:
            ts_str = zone.formed_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(zone.formed_at, "strftime") else str(zone.formed_at)
            writer.writerow([
                ts_str, zone.zone_type.value, zone.price_low, zone.price_high,
                zone.base_quality, zone.displacement_score, zone.volume_score,
                zone.fvg_score, zone.structure_score, zone.orderflow_score,
                zone.times_tested, zone.status.value
            ])

    # Export entries
    entries_path = filepath.replace(".csv", "_entries.csv")
    with open(entries_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'entry_time', 'zone_type', 'entry_price', 'zone_quality',
            'return_5', 'return_10', 'return_20', 'hit_5', 'hit_10', 'hit_20'
        ])

        for entry in entries:
            ts_str = entry.entry_timestamp.strftime("%Y-%m-%d %H:%M:%S") if hasattr(entry.entry_timestamp, "strftime") else str(entry.entry_timestamp)
            writer.writerow([
                ts_str, entry.zone.zone_type.value, entry.entry_price,
                entry.zone_quality, entry.forward_return_5, entry.forward_return_10,
                entry.forward_return_20, entry.hit_5, entry.hit_10, entry.hit_20
            ])

    print(f"\nExported zones to: {zones_path}")
    print(f"Exported entries to: {entries_path}")


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Backtest S/D zone detection")
    parser.add_argument("--timeframe", "-t", default="15M", help="Bar timeframe")
    parser.add_argument("--symbol", "-s", default="MNQ", help="Trading symbol")
    parser.add_argument("--limit", "-l", type=int, default=50000, help="Max bars")
    parser.add_argument("--start-date", type=str, help="Start date (ISO format)")
    parser.add_argument("--end-date", type=str, help="End date (ISO format)")

    # Zone detection parameters (if specified, disables timeframe defaults for that param)
    parser.add_argument("--displacement-ratio", type=float, default=None,
                       help="Displacement body/ATR ratio threshold (overrides timeframe default)")
    parser.add_argument("--base-body-ratio", type=float, default=None,
                       help="Base candle body/ATR max ratio (overrides timeframe default)")
    parser.add_argument("--volume-mult", type=float, default=None,
                       help="Volume multiplier threshold (overrides timeframe default)")
    parser.add_argument("--min-quality", type=float, default=20.0,
                       help="Minimum zone quality for entry")
    parser.add_argument("--no-timeframe-defaults", action="store_true",
                       help="Disable automatic timeframe-specific parameter adjustment")

    # Output options
    parser.add_argument("--show-zones", action="store_true", help="Show detected zones")
    parser.add_argument("--show-entries", action="store_true", help="Show zone entries")
    parser.add_argument("--export-csv", type=str, help="Export to CSV file")

    args = parser.parse_args()

    # Determine if using timeframe defaults
    use_tf_defaults = not args.no_timeframe_defaults

    # Create backtester
    backtester = SDZoneBacktester(
        min_quality=args.min_quality,
        use_timeframe_defaults=use_tf_defaults,
    )

    # Override specific params if user provided them (even when using TF defaults)
    if args.displacement_ratio is not None:
        backtester.detector.displacement_ratio = args.displacement_ratio
        backtester.use_timeframe_defaults = False  # User wants manual control
    if args.base_body_ratio is not None:
        backtester.detector.base_body_ratio = args.base_body_ratio
        backtester.use_timeframe_defaults = False
    if args.volume_mult is not None:
        backtester.detector.volume_mult = args.volume_mult
        backtester.use_timeframe_defaults = False

    # Run backtest
    summary, zones, entries = backtester.run_backtest(
        timeframe=args.timeframe,
        symbol=args.symbol,
        start_date=args.start_date,
        end_date=args.end_date,
        limit=args.limit,
    )

    # Print results
    print_summary(summary)

    if args.show_zones and zones:
        print_zones(zones)

    if args.show_entries and entries:
        print_entries(entries)

    if args.export_csv:
        export_zones_csv(zones, entries, args.export_csv)


if __name__ == "__main__":
    main()
