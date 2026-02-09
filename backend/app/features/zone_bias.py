"""Zone-Aware Bias Scoring

Integrates Supply/Demand zone proximity into agent bias calculation.
Uses 15M orderflow signals for all timeframes since orderflow signals
(absorption, exhaustion, delta_unwind) work best on 15M.

Key insight from backtesting:
- 60% of agent score comes from orderflow signals
- These signals (absorption, exhaustion, delta_unwind) only work well on 15M
- For 1H/4H/1D S/D zones, we should use 15M orderflow to confirm entries

Usage:
    zone_scorer = ZoneBiasScorer()
    zone_bias = zone_scorer.calculate_zone_bias(
        timeframe="1H",
        symbol="MNQ",
        current_price=21500.0,
        current_bar_idx=100,
    )
"""
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple
from datetime import datetime, timedelta
from enum import Enum
import polars as pl

from app.data.storage import DuckDBStorage
from app.features.orderflow_signals import OrderflowSignalDetector, SignalDirection

logger = logging.getLogger(__name__)


# Zone detection parameters by timeframe (from backtest_sd_zones.py)
ZONE_HALFLIFE = {
    "5M": 100,
    "15M": 60,
    "1H": 40,
    "4H": 30,
    "1D": 20,
}

# Zone detection parameters - institutional base approach
# ERC (Extended Range Candle) triggers zone detection
# Base = consolidation of "boring candles" before ERC departure
ZONE_PARAMS = {
    "erc_body_multiplier": 1.2,     # ERC body must be > 1.2x ATR
    "boring_body_ratio": 0.6,       # Boring candle: body < 60% of range
    "min_base_candles": 1,          # Minimum candles in base
    "max_base_candles": 8,          # Maximum candles in base
    "min_departure_atr": 0.3,       # ERC must move at least 0.3 ATR (reduced from 0.7)
    "zone_extend_candles": 1,       # Extend zone boundaries by ±N candles from swing (reduced from 2)
    "max_zone_width_atr": 2.5,      # Max zone width in ATR (increased from 2.0)
}


class ZoneType(str, Enum):
    DEMAND = "DEMAND"
    SUPPLY = "SUPPLY"


class ZoneStatus(str, Enum):
    FRESH = "FRESH"
    AGING = "AGING"
    TESTED = "TESTED"
    BROKEN = "BROKEN"


@dataclass
class ActiveZone:
    """Represents an active S/D zone for bias calculation."""

    zone_type: ZoneType
    price_low: float
    price_high: float
    formed_at: datetime
    formed_bar_idx: int
    timeframe: str
    base_quality: float  # 0-100
    times_tested: int = 0
    status: ZoneStatus = ZoneStatus.FRESH

    @property
    def zone_midpoint(self) -> float:
        return (self.price_low + self.price_high) / 2

    @property
    def zone_height(self) -> float:
        return self.price_high - self.price_low

    def effective_quality(self, bars_since_formed: int) -> float:
        """Calculate quality with recency and test penalties."""
        if self.status == ZoneStatus.BROKEN:
            return 0

        halflife = ZONE_HALFLIFE.get(self.timeframe, 50)
        recency = 0.5 ** (bars_since_formed / halflife)
        test_mult = max(0.4, 1.0 - (self.times_tested * 0.2))

        return self.base_quality * recency * test_mult


@dataclass
class ZoneBiasResult:
    """Result of zone bias calculation."""

    zone_bias: float  # -15 to +15 points adjustment
    active_zone: Optional[ActiveZone]
    zone_quality: float
    distance_to_zone_pct: float
    orderflow_confirmation: bool
    orderflow_signals: List[str]  # List of confirming signals
    details: str


class ZoneBiasScorer:
    """Calculates bias adjustment based on S/D zone proximity.

    Key features:
    - Detects zones on the analysis timeframe (1H, 4H, 1D)
    - Uses 15M orderflow signals for confirmation (always)
    - Applies zone quality and recency weighting
    """

    def __init__(
        self,
        entry_buffer_pct: float = 0.003,  # 0.3% buffer to consider "at zone"
        min_quality: float = 40.0,         # Minimum zone quality
        max_zone_bias: float = 15.0,       # Maximum bias adjustment points
        orderflow_tf: str = "15M",         # Timeframe for orderflow signals
        atr_period: int = 14,
        lookback_bars: int = 20,
    ):
        self.entry_buffer_pct = entry_buffer_pct
        self.min_quality = min_quality
        self.max_zone_bias = max_zone_bias
        self.orderflow_tf = orderflow_tf
        self.atr_period = atr_period
        self.lookback_bars = lookback_bars

        self.db = DuckDBStorage()

    def load_zone_data(
        self,
        timeframe: str,
        symbol: str = "MNQ",
        limit: int = 500,
    ) -> pl.DataFrame:
        """Load recent OHLCV data for zone detection."""
        query = f"""
            SELECT
                timestamp,
                open, high, low, close, volume,
                dom_imbalance, cvd, instant_delta, trade_flow_ratio
            FROM ohlcv_ticks
            WHERE symbol = '{symbol}' AND timeframe = '{timeframe}'
            ORDER BY timestamp DESC
            LIMIT {limit}
        """
        df = self.db.conn.execute(query).pl()
        return df.reverse() if len(df) > 0 else df

    def load_15m_orderflow_data(
        self,
        symbol: str = "MNQ",
        limit: int = 100,
    ) -> pl.DataFrame:
        """Load recent 15M data for orderflow signal detection."""
        query = f"""
            SELECT
                timestamp,
                open, high, low, close, volume,
                dom_imbalance, cvd, instant_delta, trade_flow_ratio
            FROM ohlcv_ticks
            WHERE symbol = '{symbol}' AND timeframe = '15M'
            ORDER BY timestamp DESC
            LIMIT {limit}
        """
        df = self.db.conn.execute(query).pl()
        return df.reverse() if len(df) > 0 else df

    def detect_active_zones(
        self,
        df: pl.DataFrame,
        timeframe: str,
        current_bar_idx: int,
    ) -> List[ActiveZone]:
        """Detect active S/D zones using institutional base approach.

        Pattern-based detection:
        - DEMAND: Drop → Base (boring candles) → Rally (ERC up)
        - SUPPLY: Rally → Base (boring candles) → Drop (ERC down)

        Zone = the consolidation "base" before the ERC departure.
        Validated with orderflow data (dom_imbalance, cvd, instant_delta).
        """
        zones = []

        if len(df) < self.atr_period + 20:
            return zones

        # Calculate candle metrics
        df = df.with_columns([
            (pl.col("high") - pl.col("low")).alias("candle_range"),
            (pl.col("close") - pl.col("open")).abs().alias("body_size"),
        ])
        df = df.with_columns([
            pl.col("candle_range").rolling_mean(window_size=self.atr_period).alias("atr"),
        ])

        rows = df.to_dicts()

        # Scan window by timeframe
        scan_bars_by_tf = {
            "5M": 3000,
            "15M": 4000,
            "1H": 4800,
            "4H": 1200,
            "1D": 200,
        }
        scan_bars = scan_bars_by_tf.get(timeframe, 500)
        scan_start = max(self.atr_period + 10, len(rows) - scan_bars)

        # Parameters
        erc_body_mult = ZONE_PARAMS["erc_body_multiplier"]
        boring_ratio = ZONE_PARAMS["boring_body_ratio"]
        min_base = ZONE_PARAMS["min_base_candles"]
        max_base = ZONE_PARAMS["max_base_candles"]
        min_departure = ZONE_PARAMS["min_departure_atr"]
        zone_extend = ZONE_PARAMS.get("zone_extend_candles", 1)
        max_zone_width = ZONE_PARAMS.get("max_zone_width_atr", 2.5)

        for i in range(scan_start, len(rows)):
            curr = rows[i]
            atr = curr.get("atr")

            if atr is None or atr <= 0:
                continue

            # Check if this is an ERC (Extended Range Candle)
            body = curr["body_size"]
            candle_range = curr["candle_range"]

            if body < atr * erc_body_mult:
                continue  # Not an ERC

            # Determine ERC direction
            is_bullish_erc = curr["close"] > curr["open"]
            is_bearish_erc = curr["close"] < curr["open"]

            if not is_bullish_erc and not is_bearish_erc:
                continue

            # Look backwards for a "base" (1-8 boring candles)
            base_candles = []
            for j in range(i - 1, max(scan_start - 1, i - max_base - 1), -1):
                bar = rows[j]
                bar_body = bar["body_size"]
                bar_range = bar["candle_range"]

                # Check if boring candle (body < 60% of range)
                if bar_range > 0 and bar_body / bar_range < boring_ratio:
                    base_candles.append(j)
                else:
                    break  # No longer boring, stop looking

            # Track if this is a V-reversal (no boring base found)
            is_v_reversal = len(base_candles) < min_base

            # If no boring candles found, use 1-2 candles before ERC as base
            # This handles V-shaped reversals where the swing point IS the ERC
            if is_v_reversal:
                # Fallback: use candles immediately before ERC
                for j in range(i - 1, max(scan_start - 1, i - 3), -1):
                    base_candles.append(j)
                if len(base_candles) == 0:
                    continue

            # Determine base start for leg-in calculation
            base_start_idx = base_candles[-1]  # Earliest base candle
            if base_start_idx < scan_start + 3:
                continue

            # Find swing point for leg-in validation (search 20 bars before base)
            swing_search_start = max(scan_start, base_start_idx - 20)
            swing_search_indices = list(base_candles) + list(range(swing_search_start, base_start_idx))

            # For V-reversals, include the ERC in swing search
            if is_v_reversal:
                swing_search_indices.append(i)

            if is_bullish_erc:
                # DEMAND zone: find the actual swing LOW for leg-in validation
                swing_low_idx = min(swing_search_indices, key=lambda k: rows[k]["low"])
                swing_low = rows[swing_low_idx]["low"]

                # Check for leg-in: high before swing low
                leg_in_bars = min(5, swing_low_idx - scan_start)
                if leg_in_bars < 2:
                    continue
                leg_check_start = max(scan_start, swing_low_idx - leg_in_bars)
                if leg_check_start >= swing_low_idx:
                    continue
                high_before = max(rows[k]["high"] for k in range(leg_check_start, swing_low_idx))
                leg_in_move = (high_before - swing_low) / atr

                if leg_in_move < min_departure:
                    continue  # Weak leg-in

                zone_type = ZoneType.DEMAND

                # Zone boundaries: centered on the swing point (where reversal actually happened)
                # Include swing candle + N candles on each side for a tight zone
                extended_base = [swing_low_idx]
                for offset in range(1, zone_extend + 1):
                    if swing_low_idx - offset >= scan_start:
                        extended_base.append(swing_low_idx - offset)
                    if swing_low_idx + offset < i:  # Don't include ERC
                        extended_base.append(swing_low_idx + offset)
            else:
                # SUPPLY zone: find the actual swing HIGH for leg-in validation
                swing_high_idx = max(swing_search_indices, key=lambda k: rows[k]["high"])
                swing_high = rows[swing_high_idx]["high"]

                # Check for leg-in: low before swing high
                leg_in_bars = min(5, swing_high_idx - scan_start)
                if leg_in_bars < 2:
                    continue
                leg_check_start = max(scan_start, swing_high_idx - leg_in_bars)
                if leg_check_start >= swing_high_idx:
                    continue
                low_before = min(rows[k]["low"] for k in range(leg_check_start, swing_high_idx))
                leg_in_move = (swing_high - low_before) / atr

                if leg_in_move < min_departure:
                    continue  # Weak leg-in

                zone_type = ZoneType.SUPPLY

                # Zone boundaries: centered on the swing point (where reversal actually happened)
                # Include swing candle + N candles on each side for a tight zone
                extended_base = [swing_high_idx]
                for offset in range(1, zone_extend + 1):
                    if swing_high_idx - offset >= scan_start:
                        extended_base.append(swing_high_idx - offset)
                    if swing_high_idx + offset < i:  # Don't include ERC
                        extended_base.append(swing_high_idx + offset)

            # Zone boundaries = high/low of the consolidation range (swing point to ERC)
            zone_high = max(rows[k]["high"] for k in extended_base)
            zone_low = min(rows[k]["low"] for k in extended_base)
            zone_height = zone_high - zone_low

            # Cap zone height at max_zone_width * ATR to prevent overly wide zones
            # This is especially important for demand zones at swing lows which tend to be volatile
            if zone_height > atr * max_zone_width:
                if zone_type == ZoneType.DEMAND:
                    # For demand, cap by raising zone_low (keep the swing low as lower bound)
                    swing_low = rows[swing_low_idx]["low"]
                    capped_height = atr * max_zone_width
                    zone_low = swing_low
                    zone_high = min(zone_high, swing_low + capped_height)
                else:
                    # For supply, cap by lowering zone_high (keep the swing high as upper bound)
                    swing_high = rows[swing_high_idx]["high"]
                    capped_height = atr * max_zone_width
                    zone_high = swing_high
                    zone_low = max(zone_low, swing_high - capped_height)

                zone_height = zone_high - zone_low

            # Departure strength (how far ERC moved from zone)
            if zone_type == ZoneType.DEMAND:
                departure = (curr["close"] - zone_high) / atr
            else:
                departure = (zone_low - curr["close"]) / atr

            if departure < min_departure:
                continue  # ERC didn't move far enough

            # Quality scoring (0-100)
            # Base quality from: leg-in strength, departure strength, orderflow
            leg_score = min(25.0, leg_in_move * 10)
            departure_score = min(25.0, departure * 10)

            # Width score (prefer 0.3-2.0 ATR wide bases)
            width_ratio = zone_height / atr
            if 0.3 <= width_ratio <= 2.0:
                width_score = 20.0
            elif width_ratio < 0.3:
                width_score = width_ratio * 66  # Too narrow
            else:
                width_score = max(0.0, 20 - (width_ratio - 2.0) * 8)

            # Orderflow validation bonus (up to 30 points)
            of_score = self._calc_orderflow_score(rows, base_candles, zone_type)

            base_quality = 25 + leg_score + departure_score + width_score + of_score
            base_quality = min(100.0, max(0.0, base_quality))

            # Zone formation time: use the swing point (lowest low for demand, highest high for supply)
            if zone_type == ZoneType.DEMAND:
                swing_idx = min(extended_base, key=lambda k: rows[k]["low"])
            else:
                swing_idx = max(extended_base, key=lambda k: rows[k]["high"])
            formed_at = rows[swing_idx]["timestamp"]
            formed_idx = swing_idx

            zone = ActiveZone(
                zone_type=zone_type,
                price_low=zone_low,
                price_high=zone_high,
                formed_at=formed_at,
                formed_bar_idx=formed_idx,
                timeframe=timeframe,
                base_quality=base_quality,
            )
            zones.append(zone)

        logger.debug(f"Detected {len(zones)} base zones for {timeframe}")

        # Merge overlapping zones
        merged_zones = self._merge_overlapping_zones(zones, df)
        logger.debug(f"After merging: {len(merged_zones)} zones for {timeframe}")

        return merged_zones

    def _calc_orderflow_score(
        self,
        rows: List[dict],
        base_indices: List[int],
        zone_type: ZoneType,
    ) -> float:
        """Calculate orderflow validation score for zone quality.

        Uses dom_imbalance, cvd, instant_delta from base candles.
        Returns 0-30 points based on orderflow confirmation.
        """
        if not base_indices:
            return 0.0

        # Collect orderflow metrics from base candles
        dom_values = []
        delta_values = []
        cvd_values = []

        for idx in base_indices:
            bar = rows[idx]
            if bar.get("dom_imbalance") is not None:
                dom_values.append(bar["dom_imbalance"])
            if bar.get("instant_delta") is not None:
                delta_values.append(bar["instant_delta"])
            if bar.get("cvd") is not None:
                cvd_values.append(bar["cvd"])

        if not dom_values:
            return 0.0  # No orderflow data

        avg_dom = sum(dom_values) / len(dom_values)

        # For DEMAND: want to see buying pressure (dom > 0.5, positive delta)
        # For SUPPLY: want to see selling pressure (dom < 0.5, negative delta)
        score = 0.0

        if zone_type == ZoneType.DEMAND:
            # DOM imbalance shows buying (> 0.5 = more bids)
            if avg_dom > 0.55:
                score += min(15.0, (avg_dom - 0.5) * 60)  # Up to 15 pts
            # Delta shows buying
            if delta_values:
                avg_delta = sum(delta_values) / len(delta_values)
                if avg_delta > 0:
                    score += min(15.0, 15.0)  # Up to 15 pts for positive delta
        else:  # SUPPLY
            # DOM imbalance shows selling (< 0.5 = more asks)
            if avg_dom < 0.45:
                score += min(15.0, (0.5 - avg_dom) * 60)  # Up to 15 pts
            # Delta shows selling
            if delta_values:
                avg_delta = sum(delta_values) / len(delta_values)
                if avg_delta < 0:
                    score += min(15.0, 15.0)  # Up to 15 pts for negative delta

        return score

    def _merge_overlapping_zones(
        self,
        zones: List[ActiveZone],
        df: pl.DataFrame,
    ) -> List[ActiveZone]:
        """Filter overlapping zones - keep highest quality non-overlapping zones.

        Separate by zone type (DEMAND vs SUPPLY), then deduplicate each.
        """
        if len(zones) < 2:
            return zones

        # Separate by zone type
        demand_zones = [z for z in zones if z.zone_type == ZoneType.DEMAND]
        supply_zones = [z for z in zones if z.zone_type == ZoneType.SUPPLY]

        merged = []
        merged.extend(self._merge_zones_of_same_type(demand_zones))
        merged.extend(self._merge_zones_of_same_type(supply_zones))

        return merged

    def _merge_zones_of_same_type(
        self,
        zones: List[ActiveZone],
        max_width: float = float('inf'),
    ) -> List[ActiveZone]:
        """Deduplicate overlapping zones of the same type.

        When zones overlap: keep the one with higher quality.
        - Sort by quality (descending) then by recency
        - For each zone, skip if it overlaps with a kept zone
        """
        if len(zones) < 2:
            return zones

        # Sort by quality descending, then by recency (newer = higher formed_bar_idx)
        zones = sorted(zones, key=lambda z: (z.base_quality, z.formed_bar_idx), reverse=True)

        kept = []
        for zone in zones:
            # Check if this zone overlaps with any already-kept zone
            overlaps = False
            for kept_zone in kept:
                if (zone.price_low <= kept_zone.price_high and
                    zone.price_high >= kept_zone.price_low):
                    overlaps = True
                    break

            if not overlaps:
                kept.append(zone)

        return kept

    def get_recent_orderflow_signals(
        self,
        df_15m: pl.DataFrame,
        signal_window_bars: int = 10,
    ) -> Tuple[List[dict], List[dict], List[dict]]:
        """Get recent orderflow signals from 15M data.

        Returns:
            Tuple of (absorption_signals, exhaustion_signals, delta_unwind_signals)
        """
        if len(df_15m) < 20:
            return [], [], []

        detector = OrderflowSignalDetector(timeframe="15M", lookback_bars=self.lookback_bars)

        try:
            absorption = detector.detect_absorption(df_15m)
            exhaustion = detector.detect_exhaustion(df_15m)
            delta_unwind = detector.detect_delta_unwind(df_15m)
        except Exception as e:
            logger.warning(f"Orderflow signal detection failed: {e}")
            return [], [], []

        # Filter to recent signals
        recent_ts = df_15m.tail(signal_window_bars)["timestamp"].min()

        def filter_recent(signals):
            recent = []
            for s in signals:
                sig_ts = s.timestamp
                cutoff = recent_ts
                if hasattr(sig_ts, "timestamp"):
                    sig_ts = sig_ts.timestamp()
                if hasattr(cutoff, "timestamp"):
                    cutoff = cutoff.timestamp()
                if sig_ts >= cutoff:
                    recent.append({
                        "direction": s.direction.value if hasattr(s.direction, 'value') else s.direction,
                        "strength": s.strength,
                        "type": s.signal_type.value if hasattr(s.signal_type, 'value') else str(s.signal_type),
                    })
            return recent

        return (
            filter_recent(absorption),
            filter_recent(exhaustion),
            filter_recent(delta_unwind),
        )

    def find_nearest_zone(
        self,
        zones: List[ActiveZone],
        current_price: float,
        current_bar_idx: int,
    ) -> Tuple[Optional[ActiveZone], float, float]:
        """Find the nearest active zone to current price.

        Returns:
            Tuple of (zone, effective_quality, distance_pct)
        """
        if not zones:
            return None, 0.0, float('inf')

        best_zone = None
        best_quality = 0.0
        best_distance = float('inf')

        for zone in zones:
            # Calculate distance to zone
            if current_price > zone.price_high:
                distance = (current_price - zone.price_high) / current_price
            elif current_price < zone.price_low:
                distance = (zone.price_low - current_price) / current_price
            else:
                distance = 0.0  # Inside zone

            # Only consider zones within buffer
            if distance > self.entry_buffer_pct * 3:  # 3x buffer for consideration
                continue

            bars_age = current_bar_idx - zone.formed_bar_idx
            quality = zone.effective_quality(bars_age)

            if quality < self.min_quality:
                continue

            # Prefer higher quality zones, then closer ones
            if quality > best_quality or (quality == best_quality and distance < best_distance):
                best_zone = zone
                best_quality = quality
                best_distance = distance

        return best_zone, best_quality, best_distance

    def calculate_zone_bias(
        self,
        timeframe: str,
        symbol: str,
        current_price: float,
        current_bar_idx: Optional[int] = None,
        zones: Optional[List[ActiveZone]] = None,
    ) -> ZoneBiasResult:
        """Calculate bias adjustment based on zone proximity.

        Uses 15M orderflow signals regardless of zone timeframe.

        Args:
            timeframe: Zone detection timeframe (1H, 4H, 1D)
            symbol: Trading symbol
            current_price: Current price
            current_bar_idx: Current bar index (auto-detected if None)
            zones: Pre-detected zones (auto-detected if None)

        Returns:
            ZoneBiasResult with bias adjustment
        """
        # Load zone timeframe data if zones not provided
        if zones is None:
            df_zone = self.load_zone_data(timeframe, symbol)
            if len(df_zone) == 0:
                return ZoneBiasResult(
                    zone_bias=0.0,
                    active_zone=None,
                    zone_quality=0.0,
                    distance_to_zone_pct=float('inf'),
                    orderflow_confirmation=False,
                    orderflow_signals=[],
                    details="No zone data available",
                )

            if current_bar_idx is None:
                current_bar_idx = len(df_zone) - 1

            zones = self.detect_active_zones(df_zone, timeframe, current_bar_idx)
        else:
            if current_bar_idx is None:
                current_bar_idx = 100  # Default

        # Find nearest zone
        zone, quality, distance_pct = self.find_nearest_zone(zones, current_price, current_bar_idx)

        if zone is None:
            return ZoneBiasResult(
                zone_bias=0.0,
                active_zone=None,
                zone_quality=0.0,
                distance_to_zone_pct=float('inf'),
                orderflow_confirmation=False,
                orderflow_signals=[],
                details="No active zones near current price",
            )

        # Check if price is at/near zone
        is_at_zone = distance_pct <= self.entry_buffer_pct

        if not is_at_zone:
            return ZoneBiasResult(
                zone_bias=0.0,
                active_zone=zone,
                zone_quality=quality,
                distance_to_zone_pct=distance_pct,
                orderflow_confirmation=False,
                orderflow_signals=[],
                details=f"Price {distance_pct:.2%} from {zone.zone_type.value} zone (quality {quality:.0f})",
            )

        # Price is at zone - get 15M orderflow signals for confirmation
        df_15m = self.load_15m_orderflow_data(symbol)
        abs_signals, exh_signals, du_signals = self.get_recent_orderflow_signals(df_15m)

        # Determine expected direction for zone
        # Demand zone -> expect bullish signals (reaction up)
        # Supply zone -> expect bearish signals (reaction down)
        expected_dir = "BULLISH" if zone.zone_type == ZoneType.DEMAND else "BEARISH"

        # Count confirming signals
        confirming_signals = []

        for sig in abs_signals:
            if sig["direction"] == expected_dir:
                confirming_signals.append(f"ABS {sig['strength']:.1%}")

        for sig in exh_signals:
            if sig["direction"] == expected_dir:
                confirming_signals.append(f"EXH {sig['strength']:.1%}")

        for sig in du_signals:
            if sig["direction"] == expected_dir:
                confirming_signals.append(f"DU {sig['strength']:.1%}")

        has_confirmation = len(confirming_signals) > 0

        # Calculate zone bias
        # Base bias: (quality / 100) * max_zone_bias
        # Confirmation bonus: +50% if orderflow confirms
        quality_factor = quality / 100
        base_bias = quality_factor * self.max_zone_bias

        if has_confirmation:
            # Boost by 50% for confirmation
            base_bias *= 1.5
            base_bias = min(base_bias, self.max_zone_bias)

        # Apply direction
        if zone.zone_type == ZoneType.DEMAND:
            zone_bias = base_bias  # Bullish adjustment
        else:
            zone_bias = -base_bias  # Bearish adjustment

        details = (
            f"At {zone.zone_type.value} zone (quality {quality:.0f}, "
            f"{'confirmed' if has_confirmation else 'unconfirmed'}): "
            f"bias {zone_bias:+.1f}"
        )

        return ZoneBiasResult(
            zone_bias=zone_bias,
            active_zone=zone,
            zone_quality=quality,
            distance_to_zone_pct=distance_pct,
            orderflow_confirmation=has_confirmation,
            orderflow_signals=confirming_signals,
            details=details,
        )

    def get_15m_orderflow_score(
        self,
        symbol: str = "MNQ",
    ) -> Tuple[float, List[str]]:
        """Calculate orderflow score from 15M data.

        This can be used to override the same-timeframe orderflow score
        when analyzing higher timeframes (1H, 4H, 1D).

        Returns:
            Tuple of (orderflow_score 0-100, active_signal_names)
        """
        df_15m = self.load_15m_orderflow_data(symbol)

        if len(df_15m) < 30:
            return 50.0, []  # Neutral if insufficient data

        abs_signals, exh_signals, du_signals = self.get_recent_orderflow_signals(df_15m)

        # Calculate net direction from all signals
        bullish_strength = 0.0
        bearish_strength = 0.0
        active_signals = []

        for sig in abs_signals:
            if sig["direction"] == "BULLISH":
                bullish_strength += sig["strength"]
                active_signals.append("ABS+")
            else:
                bearish_strength += sig["strength"]
                active_signals.append("ABS-")

        for sig in exh_signals:
            if sig["direction"] == "BULLISH":
                bullish_strength += sig["strength"]
                active_signals.append("EXH+")
            else:
                bearish_strength += sig["strength"]
                active_signals.append("EXH-")

        for sig in du_signals:
            if sig["direction"] == "BULLISH":
                bullish_strength += sig["strength"]
                active_signals.append("DU+")
            else:
                bearish_strength += sig["strength"]
                active_signals.append("DU-")

        # Convert to 0-100 score
        # Net bullish -> score > 50, net bearish -> score < 50
        total_strength = bullish_strength + bearish_strength

        if total_strength == 0:
            return 50.0, []

        net_ratio = (bullish_strength - bearish_strength) / total_strength

        # Scale to 0-100 (net_ratio is -1 to +1)
        score = 50 + net_ratio * 40  # Range 10-90
        score = max(10, min(90, score))

        return score, active_signals
