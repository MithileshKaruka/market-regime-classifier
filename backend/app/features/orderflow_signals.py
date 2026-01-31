"""Orderflow Signal Detection - Absorption, LSF, OBI, Delta Unwind, Exhaustion"""
import logging
import math
from typing import List, Optional
from dataclasses import dataclass
from enum import Enum
import polars as pl

from config import get_config

logger = logging.getLogger(__name__)


class SignalType(str, Enum):
    ABSORPTION = "Absorption"
    LSF = "LSF"  # Liquidity Sweep Fade (Pure Price)
    OBI = "OB Imb"  # Order Book Imbalance
    DELTA_UNWIND = "Delta Unwind"  # Cumulative delta reversal
    EXHAUSTION = "Exhaustion"  # High volume, low range
    INSTITUTIONAL = "Institutional"  # Large trades with directional flow
    TRADE_FLOW_DIV = "TF Div"  # Trade flow diverges from price


class SignalDirection(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


@dataclass
class OrderflowSignal:
    """Represents a detected orderflow signal"""
    timestamp: int  # Unix timestamp
    signal_type: SignalType
    direction: SignalDirection
    price: float
    strength: float  # 0.0 to 1.0
    details: str


class OrderflowSignalDetector:
    """Detects orderflow signals from MBP-10 data

    Strategies:
    1. Absorption: Large aggressive delta hitting a level but price not moving
    2. LSF (Liquidity Sweep Fade): Price sweeps beyond range then snaps back (pure price)
    3. OBI (Order Book Imbalance): Weighted imbalance across top 10 levels
    4. Delta Unwind: Cumulative delta reaches extreme then reverses
    5. Exhaustion: High volume with minimal price movement

    All parameters can be overridden, but defaults are loaded from config.
    """

    def __init__(
        self,
        timeframe: Optional[str] = None,
        # Absorption params
        absorption_volume_mult: Optional[float] = None,
        absorption_price_tol: Optional[float] = None,
        absorption_dom_threshold: Optional[float] = None,
        # LSF params (pure price)
        lsf_sweep_threshold_pct: Optional[float] = None,
        lsf_snapback_pct: Optional[float] = None,
        lsf_snapback_bars: Optional[int] = None,
        # OBI params
        obi_threshold: Optional[float] = None,
        # Delta Unwind params
        delta_zscore_threshold: Optional[float] = None,
        delta_unwind_pct: Optional[float] = None,
        delta_unwind_bars: Optional[int] = None,
        # Exhaustion params
        exhaustion_volume_mult: Optional[float] = None,
        exhaustion_range_ratio_max: Optional[float] = None,
        # Institutional Activity params (trades data)
        inst_large_trade_min: Optional[int] = None,
        inst_flow_threshold: Optional[float] = None,
        # Trade Flow Divergence params (trades data)
        tfd_flow_threshold: Optional[float] = None,
        tfd_price_change_pct: Optional[float] = None,
        tfd_lookback_bars: Optional[int] = None,
        # General
        lookback_bars: Optional[int] = None,
    ):
        # Load defaults from config
        config = get_config()
        of_config = config.orderflow_alpha

        # Check for timeframe-specific absorption parameters
        tf_absorption = None
        if timeframe and hasattr(of_config, 'absorption_by_tf'):
            tf_absorption = of_config.absorption_by_tf.get(timeframe)

        # Use provided values, then timeframe-specific, then global defaults
        if tf_absorption:
            self.absorption_volume_mult = absorption_volume_mult or tf_absorption.get('volume_mult', of_config.absorption_volume_mult)
            self.absorption_price_tol = absorption_price_tol or tf_absorption.get('price_tol', of_config.absorption_price_tol)
            self.absorption_dom_threshold = absorption_dom_threshold or tf_absorption.get('dom_threshold', of_config.absorption_dom_threshold)
            self.lookback_bars = lookback_bars or tf_absorption.get('lookback', of_config.absorption_lookback)
        else:
            self.absorption_volume_mult = absorption_volume_mult or of_config.absorption_volume_mult
            self.absorption_price_tol = absorption_price_tol or of_config.absorption_price_tol
            self.absorption_dom_threshold = absorption_dom_threshold or of_config.absorption_dom_threshold
            self.lookback_bars = lookback_bars or of_config.absorption_lookback

        # LSF params (pure price - no delta requirement)
        self.lsf_sweep_threshold_pct = lsf_sweep_threshold_pct or getattr(of_config, 'lsf_sweep_threshold_pct', 0.001)
        self.lsf_snapback_pct = lsf_snapback_pct or of_config.lsf_snapback_pct
        self.lsf_snapback_bars = lsf_snapback_bars or getattr(of_config, 'lsf_snapback_bars', 3)

        # OBI params
        self.obi_threshold = obi_threshold or of_config.obi_threshold

        # Delta Unwind params
        self.delta_zscore_threshold = delta_zscore_threshold or getattr(of_config, 'delta_zscore_threshold', 2.0)
        self.delta_unwind_pct = delta_unwind_pct or getattr(of_config, 'delta_unwind_pct', 0.1)
        self.delta_unwind_bars = delta_unwind_bars or getattr(of_config, 'delta_unwind_bars', 3)

        # Exhaustion params
        self.exhaustion_volume_mult = exhaustion_volume_mult or getattr(of_config, 'exhaustion_volume_mult', 1.5)
        self.exhaustion_range_ratio_max = exhaustion_range_ratio_max or getattr(of_config, 'exhaustion_range_ratio_max', 0.5)

        # Institutional Activity params (from trades data)
        # Detects large trades with directional flow (accumulation/distribution)
        self.inst_large_trade_min = inst_large_trade_min or getattr(of_config, 'inst_large_trade_min', 3)
        self.inst_flow_threshold = inst_flow_threshold or getattr(of_config, 'inst_flow_threshold', 0.65)

        # Trade Flow Divergence params (from trades data)
        # Detects when trade flow diverges from price direction (contrarian signal)
        self.tfd_flow_threshold = tfd_flow_threshold or getattr(of_config, 'tfd_flow_threshold', 0.60)
        self.tfd_price_change_pct = tfd_price_change_pct or getattr(of_config, 'tfd_price_change_pct', 0.002)
        self.tfd_lookback_bars = tfd_lookback_bars or getattr(of_config, 'tfd_lookback_bars', 5)

        self.timeframe = timeframe

        logger.info(f"OrderflowSignalDetector initialized for {timeframe or 'default'}: absorption_mult={self.absorption_volume_mult}, "
                    f"price_tol={self.absorption_price_tol}, dom_threshold={self.absorption_dom_threshold}, "
                    f"lookback={self.lookback_bars}")

    def detect_absorption(self, df: pl.DataFrame) -> List[OrderflowSignal]:
        """Detect Absorption signals

        Absorption: Large volume hitting a level but price stays stable.
        This indicates a large buyer/seller is absorbing all the aggressive orders.

        Signal Logic:
        - Volume > average * multiplier
        - Price change approx 0 (within tolerance)
        - Bid/Ask depth remains steady (not depleting)

        Args:
            df: DataFrame with columns: timestamp, volume, open, close, dom_imbalance,
                total_bid_depth, total_ask_depth

        Returns:
            List of Absorption signals
        """
        signals = []

        if len(df) < self.lookback_bars + 1:
            return signals

        # Calculate rolling averages
        df = df.with_columns([
            pl.col("volume").rolling_mean(window_size=self.lookback_bars).alias("avg_volume"),
            pl.col("total_bid_depth").rolling_mean(window_size=self.lookback_bars).alias("avg_bid_depth"),
            pl.col("total_ask_depth").rolling_mean(window_size=self.lookback_bars).alias("avg_ask_depth"),
        ])

        # Calculate price change percentage
        df = df.with_columns([
            ((pl.col("close") - pl.col("open")).abs() / pl.col("open")).alias("price_change_pct"),
            # Depth stability: current depth vs average (ratio close to 1 = stable)
            (pl.col("total_bid_depth") / pl.col("avg_bid_depth")).alias("bid_depth_ratio"),
            (pl.col("total_ask_depth") / pl.col("avg_ask_depth")).alias("ask_depth_ratio"),
        ])

        # Detect absorption conditions
        for row in df.iter_rows(named=True):
            if row["avg_volume"] is None or row["avg_volume"] == 0:
                continue

            volume_high = row["volume"] > row["avg_volume"] * self.absorption_volume_mult
            price_stable = row["price_change_pct"] < self.absorption_price_tol

            # Check depth stability (both sides should be maintaining levels)
            bid_stable = row["bid_depth_ratio"] is not None and 0.7 < row["bid_depth_ratio"] < 1.5
            ask_stable = row["ask_depth_ratio"] is not None and 0.7 < row["ask_depth_ratio"] < 1.5

            if volume_high and price_stable and (bid_stable or ask_stable):
                # Determine direction based on DOM imbalance
                dom = row["dom_imbalance"]
                if dom > self.absorption_dom_threshold:
                    direction = SignalDirection.BULLISH
                    details = f"Bid absorption: Vol {row['volume']:,.0f} (avg {row['avg_volume']:,.0f}), DOM {dom:.2f}"
                elif dom < (1 - self.absorption_dom_threshold):
                    direction = SignalDirection.BEARISH
                    details = f"Ask absorption: Vol {row['volume']:,.0f} (avg {row['avg_volume']:,.0f}), DOM {dom:.2f}"
                else:
                    continue  # Neutral DOM, skip

                # Strength based on how much volume exceeded average
                strength = min(1.0, (row["volume"] / row["avg_volume"] - 1) / 2)

                signals.append(OrderflowSignal(
                    timestamp=int(row["timestamp"].timestamp()) if hasattr(row["timestamp"], "timestamp") else row["timestamp"],
                    signal_type=SignalType.ABSORPTION,
                    direction=direction,
                    price=row["close"],
                    strength=strength,
                    details=details,
                ))

        logger.info(f"Detected {len(signals)} Absorption signals")
        return signals

    def detect_lsf(self, df: pl.DataFrame) -> List[OrderflowSignal]:
        """Detect Liquidity Sweep Fade (LSF) signals - Pure Price Based

        LSF: Price sweeps beyond prior range then snaps back.
        No delta requirement - focuses purely on price action pattern.

        Signal Logic:
        - Price makes new high/low beyond prior rolling range (sweep)
        - Price snaps back into prior range within N bars (fade)

        Args:
            df: DataFrame with columns: timestamp, high, low, close

        Returns:
            List of LSF signals
        """
        signals = []

        if len(df) < self.lookback_bars + self.lsf_snapback_bars + 2:
            return signals

        # Calculate rolling high/low (shifted to exclude current bar)
        df = df.with_columns([
            pl.col("high").rolling_max(window_size=self.lookback_bars).shift(1).alias("prior_high"),
            pl.col("low").rolling_min(window_size=self.lookback_bars).shift(1).alias("prior_low"),
        ])

        rows = df.to_dicts()

        for i in range(self.lookback_bars, len(rows) - self.lsf_snapback_bars - 1):
            row = rows[i]

            if row["prior_high"] is None or row["prior_low"] is None:
                continue
            if row["prior_high"] == 0 or row["prior_low"] == 0:
                continue

            # Check for bearish LSF (sweep high then reverse down)
            sweep_depth_high = (row["high"] - row["prior_high"]) / row["prior_high"]
            if sweep_depth_high > self.lsf_sweep_threshold_pct:
                # Look for snapback within N bars
                for j in range(1, self.lsf_snapback_bars + 1):
                    if i + j >= len(rows):
                        break
                    future_row = rows[i + j]
                    snapback_pct = (row["high"] - future_row["close"]) / row["high"]

                    if snapback_pct > self.lsf_snapback_pct:
                        strength = min(1.0, snapback_pct / (self.lsf_snapback_pct * 3))
                        signals.append(OrderflowSignal(
                            timestamp=int(future_row["timestamp"].timestamp()) if hasattr(future_row["timestamp"], "timestamp") else future_row["timestamp"],
                            signal_type=SignalType.LSF,
                            direction=SignalDirection.BEARISH,
                            price=future_row["close"],
                            strength=strength,
                            details=f"High sweep ${row['high']:.2f} (prior ${row['prior_high']:.2f}) -> snapback ${future_row['close']:.2f} (-{snapback_pct*100:.2f}%)",
                        ))
                        break  # Only one signal per sweep

            # Check for bullish LSF (sweep low then reverse up)
            sweep_depth_low = (row["prior_low"] - row["low"]) / row["prior_low"]
            if sweep_depth_low > self.lsf_sweep_threshold_pct:
                # Look for snapback within N bars
                for j in range(1, self.lsf_snapback_bars + 1):
                    if i + j >= len(rows):
                        break
                    future_row = rows[i + j]
                    snapback_pct = (future_row["close"] - row["low"]) / row["low"]

                    if snapback_pct > self.lsf_snapback_pct:
                        strength = min(1.0, snapback_pct / (self.lsf_snapback_pct * 3))
                        signals.append(OrderflowSignal(
                            timestamp=int(future_row["timestamp"].timestamp()) if hasattr(future_row["timestamp"], "timestamp") else future_row["timestamp"],
                            signal_type=SignalType.LSF,
                            direction=SignalDirection.BULLISH,
                            price=future_row["close"],
                            strength=strength,
                            details=f"Low sweep ${row['low']:.2f} (prior ${row['prior_low']:.2f}) -> snapback ${future_row['close']:.2f} (+{snapback_pct*100:.2f}%)",
                        ))
                        break  # Only one signal per sweep

        logger.info(f"Detected {len(signals)} LSF signals")
        return signals

    def detect_obi(self, df: pl.DataFrame) -> List[OrderflowSignal]:
        """Detect Order Book Imbalance (OBI) signals

        OBI: Weighted imbalance across all 10 levels of the order book.
        Gives more weight to levels closer to mid-price.

        Signal Logic:
        - Calculate weighted imbalance: sum(bid_size * weight) / sum(ask_size * weight)
        - Weight decreases with distance from mid (level 0 = weight 1.0, level 9 = weight 0.1)
        - Strong imbalance (>threshold) indicates directional pressure

        Args:
            df: DataFrame with bid_sz_00 through bid_sz_09, ask_sz_00 through ask_sz_09

        Returns:
            List of OBI signals
        """
        signals = []

        # Check if we have the level columns
        has_levels = all(f"bid_sz_{i:02d}" in df.columns for i in range(10))

        if not has_levels:
            # Fall back to total depth columns if individual levels not available
            if "total_bid_depth" in df.columns and "total_ask_depth" in df.columns:
                return self._detect_obi_from_totals(df)
            logger.warning("OBI detection requires level data or total depth columns")
            return signals

        # Calculate weighted imbalance
        # Weights: level 0 = 1.0, level 1 = 0.9, ..., level 9 = 0.1
        weights = [1.0 - i * 0.1 for i in range(10)]

        bid_weighted_expr = sum(
            pl.col(f"bid_sz_{i:02d}") * weights[i] for i in range(10)
        )
        ask_weighted_expr = sum(
            pl.col(f"ask_sz_{i:02d}") * weights[i] for i in range(10)
        )

        df = df.with_columns([
            bid_weighted_expr.alias("weighted_bid"),
            ask_weighted_expr.alias("weighted_ask"),
        ])

        df = df.with_columns([
            (pl.col("weighted_bid") / pl.col("weighted_ask")).alias("weighted_imbalance"),
        ])

        for row in df.iter_rows(named=True):
            imb = row["weighted_imbalance"]

            if imb is None or imb == 0:
                continue

            if imb > self.obi_threshold:
                # Strong bid imbalance - bullish
                strength = min(1.0, (imb - self.obi_threshold) / self.obi_threshold)
                signals.append(OrderflowSignal(
                    timestamp=int(row["timestamp"].timestamp()) if hasattr(row["timestamp"], "timestamp") else row["timestamp"],
                    signal_type=SignalType.OBI,
                    direction=SignalDirection.BULLISH,
                    price=row.get("close", row.get("mid_price", 0)),
                    strength=strength,
                    details=f"Bid heavy: {imb:.1f}x weighted imbalance",
                ))
            elif imb < 1 / self.obi_threshold:
                # Strong ask imbalance - bearish
                inv_imb = 1 / imb
                strength = min(1.0, (inv_imb - self.obi_threshold) / self.obi_threshold)
                signals.append(OrderflowSignal(
                    timestamp=int(row["timestamp"].timestamp()) if hasattr(row["timestamp"], "timestamp") else row["timestamp"],
                    signal_type=SignalType.OBI,
                    direction=SignalDirection.BEARISH,
                    price=row.get("close", row.get("mid_price", 0)),
                    strength=strength,
                    details=f"Ask heavy: {inv_imb:.1f}x weighted imbalance",
                ))

        logger.info(f"Detected {len(signals)} OBI signals")
        return signals

    def _detect_obi_from_totals(self, df: pl.DataFrame) -> List[OrderflowSignal]:
        """Fallback OBI detection using total depth columns"""
        signals = []

        df = df.with_columns([
            (pl.col("total_bid_depth") / pl.col("total_ask_depth")).alias("simple_imbalance"),
        ])

        for row in df.iter_rows(named=True):
            imb = row["simple_imbalance"]

            if imb is None or imb == 0:
                continue

            if imb > self.obi_threshold:
                strength = min(1.0, (imb - self.obi_threshold) / self.obi_threshold)
                signals.append(OrderflowSignal(
                    timestamp=int(row["timestamp"].timestamp()) if hasattr(row["timestamp"], "timestamp") else row["timestamp"],
                    signal_type=SignalType.OBI,
                    direction=SignalDirection.BULLISH,
                    price=row.get("close", row.get("mid_price", 0)),
                    strength=strength,
                    details=f"Bid heavy: {imb:.1f}x total depth imbalance",
                ))
            elif imb < 1 / self.obi_threshold:
                inv_imb = 1 / imb
                strength = min(1.0, (inv_imb - self.obi_threshold) / self.obi_threshold)
                signals.append(OrderflowSignal(
                    timestamp=int(row["timestamp"].timestamp()) if hasattr(row["timestamp"], "timestamp") else row["timestamp"],
                    signal_type=SignalType.OBI,
                    direction=SignalDirection.BEARISH,
                    price=row.get("close", row.get("mid_price", 0)),
                    strength=strength,
                    details=f"Ask heavy: {inv_imb:.1f}x total depth imbalance",
                ))

        logger.info(f"Detected {len(signals)} OBI signals (from totals)")
        return signals

    def detect_delta_unwind(self, df: pl.DataFrame) -> List[OrderflowSignal]:
        """Detect Delta Unwind signals

        Delta Unwind: Cumulative delta reaches extreme then starts reversing.
        When accumulated buying/selling pressure unwinds, price tends to follow.

        Signal Logic:
        - Cumulative delta reaches extreme (high z-score)
        - Delta starts reversing direction
        - Trade in direction of unwind (fade the prior move)

        Args:
            df: DataFrame with columns: timestamp, close, instant_delta (or bar_delta)

        Returns:
            List of Delta Unwind signals
        """
        signals = []

        delta_col = "bar_delta" if "bar_delta" in df.columns else "instant_delta"
        if delta_col not in df.columns:
            logger.warning("Delta Unwind detection requires delta column")
            return signals

        if len(df) < self.lookback_bars + self.delta_unwind_bars + 2:
            return signals

        # Calculate cumulative delta and rolling stats
        df = df.with_columns([
            pl.col(delta_col).cum_sum().alias("cum_delta"),
        ])

        df = df.with_columns([
            pl.col("cum_delta").rolling_mean(window_size=self.lookback_bars).alias("delta_mean"),
            pl.col("cum_delta").rolling_std(window_size=self.lookback_bars).alias("delta_std"),
        ])

        # Calculate z-score
        df = df.with_columns([
            ((pl.col("cum_delta") - pl.col("delta_mean")) / pl.col("delta_std")).alias("delta_zscore"),
        ])

        rows = df.to_dicts()

        for i in range(self.lookback_bars, len(rows) - self.delta_unwind_bars - 1):
            row = rows[i]

            if row["delta_std"] is None or row["delta_std"] == 0:
                continue
            if row["delta_zscore"] is None:
                continue

            zscore = row["delta_zscore"]
            cum_delta = row["cum_delta"]

            # Check for extreme positive delta (potential bearish unwind)
            if zscore > self.delta_zscore_threshold and cum_delta > 0:
                peak_delta = cum_delta
                for j in range(1, self.delta_unwind_bars + 1):
                    if i + j >= len(rows):
                        break
                    future_row = rows[i + j]
                    future_delta = future_row["cum_delta"]

                    if future_delta is None:
                        continue

                    unwind_amount = peak_delta - future_delta
                    unwind_pct = unwind_amount / abs(peak_delta) if peak_delta != 0 else 0

                    if unwind_pct > self.delta_unwind_pct:
                        strength = min(1.0, abs(zscore) / (self.delta_zscore_threshold * 2))
                        signals.append(OrderflowSignal(
                            timestamp=int(future_row["timestamp"].timestamp()) if hasattr(future_row["timestamp"], "timestamp") else future_row["timestamp"],
                            signal_type=SignalType.DELTA_UNWIND,
                            direction=SignalDirection.BEARISH,
                            price=future_row["close"],
                            strength=strength,
                            details=f"Delta unwind: z={zscore:.1f}, unwind {unwind_pct*100:.1f}%",
                        ))
                        break

            # Check for extreme negative delta (potential bullish unwind)
            elif zscore < -self.delta_zscore_threshold and cum_delta < 0:
                trough_delta = cum_delta
                for j in range(1, self.delta_unwind_bars + 1):
                    if i + j >= len(rows):
                        break
                    future_row = rows[i + j]
                    future_delta = future_row["cum_delta"]

                    if future_delta is None:
                        continue

                    unwind_amount = future_delta - trough_delta
                    unwind_pct = unwind_amount / abs(trough_delta) if trough_delta != 0 else 0

                    if unwind_pct > self.delta_unwind_pct:
                        strength = min(1.0, abs(zscore) / (self.delta_zscore_threshold * 2))
                        signals.append(OrderflowSignal(
                            timestamp=int(future_row["timestamp"].timestamp()) if hasattr(future_row["timestamp"], "timestamp") else future_row["timestamp"],
                            signal_type=SignalType.DELTA_UNWIND,
                            direction=SignalDirection.BULLISH,
                            price=future_row["close"],
                            strength=strength,
                            details=f"Delta unwind: z={zscore:.1f}, unwind {unwind_pct*100:.1f}%",
                        ))
                        break

        logger.info(f"Detected {len(signals)} Delta Unwind signals")
        return signals

    def detect_exhaustion(self, df: pl.DataFrame) -> List[OrderflowSignal]:
        """Detect Exhaustion signals

        Exhaustion: High volume/activity with minimal price movement.
        Indicates the current move is running out of steam.

        Signal Logic:
        - High volume (spike above average)
        - Small price range relative to volume
        - Trade for reversal

        Args:
            df: DataFrame with columns: timestamp, open, high, low, close, volume

        Returns:
            List of Exhaustion signals
        """
        signals = []

        if "volume" not in df.columns:
            logger.warning("Exhaustion detection requires volume column")
            return signals

        if len(df) < self.lookback_bars + 2:
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

        # Calculate price change for trend direction
        df = df.with_columns([
            (pl.col("close") - pl.col("close").shift(5)).alias("trend_change"),
        ])

        rows = df.to_dicts()

        for i in range(self.lookback_bars + 5, len(rows) - 1):
            row = rows[i]

            if row["avg_volume"] is None or row["avg_volume"] == 0:
                continue
            if row["avg_range"] is None or row["avg_range"] == 0:
                continue

            # Check for volume spike
            volume_ratio = row["volume"] / row["avg_volume"]
            if volume_ratio < self.exhaustion_volume_mult:
                continue

            # Calculate expected range based on volume
            expected_range = row["avg_range"] * math.sqrt(volume_ratio)
            actual_range = row["bar_range"]

            range_ratio = actual_range / expected_range if expected_range > 0 else 1.0

            # Check for exhaustion (range is small relative to what volume suggests)
            if range_ratio > self.exhaustion_range_ratio_max:
                continue

            # Determine direction from body and trend
            bar_body = row["bar_body"] if row["bar_body"] is not None else 0
            trend_change = row["trend_change"] if row["trend_change"] is not None else 0

            # Signal direction: fade the exhausted move
            if bar_body > 0 or trend_change > 0:
                direction = SignalDirection.BEARISH  # Exhausted buying
            else:
                direction = SignalDirection.BULLISH  # Exhausted selling

            strength = min(1.0, volume_ratio / (self.exhaustion_volume_mult * 2) * (1 - range_ratio))

            signals.append(OrderflowSignal(
                timestamp=int(row["timestamp"].timestamp()) if hasattr(row["timestamp"], "timestamp") else row["timestamp"],
                signal_type=SignalType.EXHAUSTION,
                direction=direction,
                price=row["close"],
                strength=strength,
                details=f"Vol {volume_ratio:.1f}x avg, range {range_ratio:.2f}x expected",
            ))

        logger.info(f"Detected {len(signals)} Exhaustion signals")
        return signals

    def detect_institutional(self, df: pl.DataFrame) -> List[OrderflowSignal]:
        """Detect Institutional Activity signals from trades data

        Institutional Activity: Multiple large trades (>=50 contracts) with
        directional trade flow indicates institutional accumulation/distribution.

        Signal Logic:
        - Large trade count >= threshold (default 3)
        - Trade flow ratio strongly directional (>0.65 bullish, <0.35 bearish)
        - Indicates "smart money" activity in a direction

        Args:
            df: DataFrame with columns: timestamp, close, large_trade_count, trade_flow_ratio

        Returns:
            List of Institutional signals
        """
        signals = []

        # Check for required columns (from trades data)
        if "large_trade_count" not in df.columns or "trade_flow_ratio" not in df.columns:
            logger.debug("Institutional detection requires large_trade_count and trade_flow_ratio columns (from trades data)")
            return signals

        for row in df.iter_rows(named=True):
            large_count = row.get("large_trade_count")
            flow_ratio = row.get("trade_flow_ratio")

            # Skip if no trade data for this bar
            if large_count is None or flow_ratio is None:
                continue

            # Check for significant institutional activity
            if large_count < self.inst_large_trade_min:
                continue

            # Determine direction from trade flow
            if flow_ratio > self.inst_flow_threshold:
                # Strong buy flow with institutional trades = bullish accumulation
                direction = SignalDirection.BULLISH
                strength = min(1.0, (flow_ratio - 0.5) * 2 * (large_count / self.inst_large_trade_min) / 2)
                details = f"Institutional BUY: {large_count} large trades, {flow_ratio:.0%} buy flow"

            elif flow_ratio < (1 - self.inst_flow_threshold):
                # Strong sell flow with institutional trades = bearish distribution
                direction = SignalDirection.BEARISH
                strength = min(1.0, (0.5 - flow_ratio) * 2 * (large_count / self.inst_large_trade_min) / 2)
                details = f"Institutional SELL: {large_count} large trades, {1-flow_ratio:.0%} sell flow"

            else:
                continue  # Trade flow not directional enough

            signals.append(OrderflowSignal(
                timestamp=int(row["timestamp"].timestamp()) if hasattr(row["timestamp"], "timestamp") else row["timestamp"],
                signal_type=SignalType.INSTITUTIONAL,
                direction=direction,
                price=row.get("close", 0),
                strength=strength,
                details=details,
            ))

        logger.info(f"Detected {len(signals)} Institutional signals")
        return signals

    def detect_trade_flow_divergence(self, df: pl.DataFrame) -> List[OrderflowSignal]:
        """Detect Trade Flow Divergence signals from trades data

        Trade Flow Divergence: When trade flow (buy/sell ratio) diverges from
        price direction, it's a contrarian signal indicating hidden accumulation
        or distribution.

        Signal Logic:
        - Price falling (negative change over lookback) but trade_flow_ratio > threshold
          = Bullish divergence (accumulation despite falling price)
        - Price rising (positive change over lookback) but trade_flow_ratio < 1-threshold
          = Bearish divergence (distribution despite rising price)

        Args:
            df: DataFrame with columns: timestamp, close, trade_flow_ratio

        Returns:
            List of Trade Flow Divergence signals
        """
        signals = []

        if "trade_flow_ratio" not in df.columns:
            logger.debug("Trade Flow Divergence requires trade_flow_ratio column (from trades data)")
            return signals

        if len(df) < self.tfd_lookback_bars + 1:
            return signals

        # Calculate price change over lookback period
        df = df.with_columns([
            ((pl.col("close") - pl.col("close").shift(self.tfd_lookback_bars)) / pl.col("close").shift(self.tfd_lookback_bars)).alias("price_change_pct"),
        ])

        for row in df.iter_rows(named=True):
            flow_ratio = row.get("trade_flow_ratio")
            price_change = row.get("price_change_pct")

            # Skip if no data
            if flow_ratio is None or price_change is None:
                continue

            # Bullish divergence: price falling but buyers dominating
            if price_change < -self.tfd_price_change_pct and flow_ratio > self.tfd_flow_threshold:
                strength = min(1.0, (flow_ratio - 0.5) * 2 * abs(price_change) / self.tfd_price_change_pct / 2)
                signals.append(OrderflowSignal(
                    timestamp=int(row["timestamp"].timestamp()) if hasattr(row["timestamp"], "timestamp") else row["timestamp"],
                    signal_type=SignalType.TRADE_FLOW_DIV,
                    direction=SignalDirection.BULLISH,
                    price=row.get("close", 0),
                    strength=strength,
                    details=f"Bullish divergence: price {price_change*100:+.2f}% but {flow_ratio:.0%} buy flow",
                ))

            # Bearish divergence: price rising but sellers dominating
            elif price_change > self.tfd_price_change_pct and flow_ratio < (1 - self.tfd_flow_threshold):
                strength = min(1.0, (0.5 - flow_ratio) * 2 * abs(price_change) / self.tfd_price_change_pct / 2)
                signals.append(OrderflowSignal(
                    timestamp=int(row["timestamp"].timestamp()) if hasattr(row["timestamp"], "timestamp") else row["timestamp"],
                    signal_type=SignalType.TRADE_FLOW_DIV,
                    direction=SignalDirection.BEARISH,
                    price=row.get("close", 0),
                    strength=strength,
                    details=f"Bearish divergence: price {price_change*100:+.2f}% but {1-flow_ratio:.0%} sell flow",
                ))

        logger.info(f"Detected {len(signals)} Trade Flow Divergence signals")
        return signals

    def detect_all_signals(
        self,
        df: pl.DataFrame,
        detect_absorption: bool = True,
        detect_lsf: bool = True,
        detect_obi: bool = True,
        detect_delta_unwind: bool = True,
        detect_exhaustion: bool = True,
        detect_institutional: bool = True,
        detect_trade_flow_div: bool = True,
    ) -> List[OrderflowSignal]:
        """Detect all orderflow signals

        Args:
            df: DataFrame with orderflow data
            detect_absorption: Whether to detect absorption signals
            detect_lsf: Whether to detect LSF signals (pure price)
            detect_obi: Whether to detect OBI signals
            detect_delta_unwind: Whether to detect delta unwind signals
            detect_exhaustion: Whether to detect exhaustion signals
            detect_institutional: Whether to detect institutional activity (requires trades data)
            detect_trade_flow_div: Whether to detect trade flow divergence (requires trades data)

        Returns:
            List of all detected signals, sorted by timestamp
        """
        all_signals = []

        if detect_absorption:
            all_signals.extend(self.detect_absorption(df))

        if detect_lsf:
            all_signals.extend(self.detect_lsf(df))

        if detect_obi:
            all_signals.extend(self.detect_obi(df))

        if detect_delta_unwind:
            all_signals.extend(self.detect_delta_unwind(df))

        if detect_exhaustion:
            all_signals.extend(self.detect_exhaustion(df))

        # Trades-based signals (require trade_flow_ratio, large_trade_count columns)
        if detect_institutional:
            all_signals.extend(self.detect_institutional(df))

        if detect_trade_flow_div:
            all_signals.extend(self.detect_trade_flow_divergence(df))

        # Sort by timestamp
        all_signals.sort(key=lambda s: s.timestamp)

        logger.info(f"Total signals detected: {len(all_signals)}")
        return all_signals
