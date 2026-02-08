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
    LSF = "LSF"  # Liquidity Sweep Fade (with orderflow confirmation)
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
    2. LSF (Liquidity Sweep Fade): Price sweeps beyond range then snaps back
       - With orderflow confirmation: delta divergence + volume spike
       - Best on 5M (PF 6.67) and 4H (PF 5.28)
    3. OBI (Order Book Imbalance): Weighted imbalance across top 10 levels
    4. Delta Unwind: Cumulative delta reaches extreme then reverses
    5. Exhaustion: High volume with minimal price movement

    All parameters can be overridden, but defaults are loaded from config.
    Timeframe-specific parameters are used when timeframe is provided.
    """

    def __init__(
        self,
        timeframe: Optional[str] = None,
        # Absorption params (trade flow based)
        absorption_volume_mult: Optional[float] = None,
        absorption_price_tol: Optional[float] = None,
        absorption_dom_threshold: Optional[float] = None,
        absorption_delta_z_threshold: Optional[float] = None,
        absorption_trade_flow_threshold: Optional[float] = None,
        # LSF params (with orderflow confirmation)
        lsf_sweep_threshold_pct: Optional[float] = None,
        lsf_snapback_pct: Optional[float] = None,
        lsf_snapback_bars: Optional[int] = None,
        lsf_lookback_bars: Optional[int] = None,
        lsf_require_delta_divergence: Optional[bool] = None,
        lsf_require_volume_spike: Optional[bool] = None,
        lsf_volume_mult: Optional[float] = None,
        # OBI params
        obi_threshold: Optional[float] = None,
        # Delta Unwind params
        delta_zscore_threshold: Optional[float] = None,
        delta_unwind_pct: Optional[float] = None,
        delta_unwind_bars: Optional[int] = None,
        # Exhaustion params
        exhaustion_volume_mult: Optional[float] = None,
        exhaustion_range_ratio_max: Optional[float] = None,
        exhaustion_trend_lookback: Optional[int] = None,
        exhaustion_lookback_bars: Optional[int] = None,
        # Institutional Activity params (trades data)
        inst_large_trade_min: Optional[int] = None,
        inst_flow_threshold: Optional[float] = None,
        inst_volume_mult: Optional[float] = None,  # Volume spike confirmation
        # Trade Flow Divergence params (trades data)
        tfd_flow_threshold: Optional[float] = None,
        tfd_price_change_pct: Optional[float] = None,
        tfd_lookback_bars: Optional[int] = None,
        tfd_persistence_bars: Optional[int] = None,  # Require N consecutive bars
        tfd_volume_mult: Optional[float] = None,  # Volume spike confirmation
        tfd_flow_avg_bars: Optional[int] = None,  # Rolling avg for flow smoothing
        # General
        lookback_bars: Optional[int] = None,
        volume_lookback: Optional[int] = None,  # Lookback for volume average
    ):
        # Load defaults from config
        config = get_config()
        of_config = config.orderflow_alpha

        # Timeframe-specific absorption parameters (trade flow based)
        # Absorption works best on 5M and 15M where trade_flow_ratio has variance
        absorption_tf_defaults = {
            '5M': {'volume_mult': 2.0, 'price_tol': 0.001, 'delta_z': 2.0, 'tf_threshold': 0.55, 'lookback': 10},
            '15M': {'volume_mult': 1.2, 'price_tol': 0.002, 'delta_z': 1.5, 'tf_threshold': 0.60, 'lookback': 20},
        }

        # Check for timeframe-specific absorption parameters
        tf_absorption = absorption_tf_defaults.get(timeframe) if timeframe else None
        if timeframe and hasattr(of_config, 'absorption_by_tf'):
            # Config overrides take precedence
            config_tf = of_config.absorption_by_tf.get(timeframe)
            if config_tf:
                tf_absorption = config_tf

        # Use provided values, then timeframe-specific, then global defaults
        if tf_absorption:
            self.absorption_volume_mult = absorption_volume_mult or tf_absorption.get('volume_mult', of_config.absorption_volume_mult)
            self.absorption_price_tol = absorption_price_tol or tf_absorption.get('price_tol', of_config.absorption_price_tol)
            self.absorption_dom_threshold = absorption_dom_threshold or tf_absorption.get('dom_threshold', of_config.absorption_dom_threshold)
            self.absorption_delta_z_threshold = absorption_delta_z_threshold or tf_absorption.get('delta_z', 1.5)
            self.absorption_trade_flow_threshold = absorption_trade_flow_threshold or tf_absorption.get('tf_threshold', 0.60)
            self.lookback_bars = lookback_bars or tf_absorption.get('lookback', of_config.absorption_lookback)
        else:
            self.absorption_volume_mult = absorption_volume_mult or of_config.absorption_volume_mult
            self.absorption_price_tol = absorption_price_tol or of_config.absorption_price_tol
            self.absorption_dom_threshold = absorption_dom_threshold or of_config.absorption_dom_threshold
            self.absorption_delta_z_threshold = absorption_delta_z_threshold or 1.5
            self.absorption_trade_flow_threshold = absorption_trade_flow_threshold or 0.60
            self.lookback_bars = lookback_bars or of_config.absorption_lookback

        # Absorption only works well on 5M and 15M (trade_flow_ratio gets smoothed on higher TFs)
        self.absorption_enabled = timeframe in ('5M', '15M') if timeframe else True

        # LSF params - timeframe-specific defaults based on backtest results
        # 5M: both mode (PF 6.67, 72.7% hit) - max selectivity with orderflow
        # 15M: pure price (PF 1.69, 51.1% hit) - orderflow modes weak edge
        # 1H: delta_div only (PF 5.29, 58.8% hit) - volume hurts (PF 1.44)
        # 4H: delta_div only (PF 18.26, 91.7% hit) - exceptional, volume weak (PF 1.58)
        lsf_tf_defaults = {
            '5M': {'sweep_pct': 0.003, 'snap_pct': 0.003, 'snap_bars': 5, 'lookback': 10, 'vol_mult': 1.5, 'delta_div': True, 'vol_spike': True},
            '15M': {'sweep_pct': 0.0015, 'snap_pct': 0.003, 'snap_bars': 5, 'lookback': 10, 'vol_mult': 1.5, 'delta_div': False, 'vol_spike': False},
            '1H': {'sweep_pct': 0.003, 'snap_pct': 0.003, 'snap_bars': 1, 'lookback': 30, 'vol_mult': 2.0, 'delta_div': True, 'vol_spike': False},
            '4H': {'sweep_pct': 0.0015, 'snap_pct': 0.005, 'snap_bars': 1, 'lookback': 30, 'vol_mult': 2.0, 'delta_div': True, 'vol_spike': False},
        }

        tf_lsf = lsf_tf_defaults.get(timeframe) if timeframe else None
        if timeframe and hasattr(of_config, 'lsf_by_tf'):
            config_tf = of_config.lsf_by_tf.get(timeframe)
            if config_tf:
                tf_lsf = config_tf

        if tf_lsf:
            self.lsf_sweep_threshold_pct = lsf_sweep_threshold_pct or tf_lsf.get('sweep_pct', 0.001)
            self.lsf_snapback_pct = lsf_snapback_pct or tf_lsf.get('snap_pct', 0.002)
            self.lsf_snapback_bars = lsf_snapback_bars or tf_lsf.get('snap_bars', 3)
            self.lsf_lookback_bars = lsf_lookback_bars or tf_lsf.get('lookback', 20)
            self.lsf_require_delta_divergence = lsf_require_delta_divergence if lsf_require_delta_divergence is not None else tf_lsf.get('delta_div', True)
            self.lsf_require_volume_spike = lsf_require_volume_spike if lsf_require_volume_spike is not None else tf_lsf.get('vol_spike', True)
            self.lsf_volume_mult = lsf_volume_mult or tf_lsf.get('vol_mult', 1.5)
        else:
            self.lsf_sweep_threshold_pct = lsf_sweep_threshold_pct or getattr(of_config, 'lsf_sweep_threshold_pct', 0.001)
            self.lsf_snapback_pct = lsf_snapback_pct or of_config.lsf_snapback_pct
            self.lsf_snapback_bars = lsf_snapback_bars or getattr(of_config, 'lsf_snapback_bars', 3)
            self.lsf_lookback_bars = lsf_lookback_bars or 20
            self.lsf_require_delta_divergence = lsf_require_delta_divergence if lsf_require_delta_divergence is not None else True
            self.lsf_require_volume_spike = lsf_require_volume_spike if lsf_require_volume_spike is not None else True
            self.lsf_volume_mult = lsf_volume_mult or 1.5

        # OBI params
        self.obi_threshold = obi_threshold or of_config.obi_threshold

        # Delta Unwind params - timeframe-specific defaults based on backtest results
        # Only 15M has predictive value (PF 8.59, 87.5% hit rate with trade_flow mode)
        # 5M/1H/4H: No valid signal combinations found
        # Trade flow confirmation improves hit rate from 77.8% to 87.5%
        delta_unwind_tf_defaults = {
            '15M': {'zscore': 1.5, 'unwind_pct': 0.15, 'unwind_bars': 8, 'lookback': 100, 'tf_threshold': 0.52},
        }

        # Delta Unwind only enabled on 15M (other TFs have no edge)
        self.delta_unwind_enabled = timeframe == '15M' if timeframe else True

        tf_delta = delta_unwind_tf_defaults.get(timeframe) if timeframe else None
        if tf_delta:
            self.delta_zscore_threshold = delta_zscore_threshold or tf_delta.get('zscore', 1.5)
            self.delta_unwind_pct = delta_unwind_pct or tf_delta.get('unwind_pct', 0.15)
            self.delta_unwind_bars = delta_unwind_bars or tf_delta.get('unwind_bars', 8)
            self.delta_unwind_lookback = tf_delta.get('lookback', 100)
            self.delta_unwind_tf_threshold = tf_delta.get('tf_threshold', 0.52)
        else:
            self.delta_zscore_threshold = delta_zscore_threshold or getattr(of_config, 'delta_zscore_threshold', 1.5)
            self.delta_unwind_pct = delta_unwind_pct or getattr(of_config, 'delta_unwind_pct', 0.15)
            self.delta_unwind_bars = delta_unwind_bars or getattr(of_config, 'delta_unwind_bars', 8)
            self.delta_unwind_lookback = 100
            self.delta_unwind_tf_threshold = 0.52

        # Exhaustion params - timeframe-specific defaults based on backtest results
        # 5M: vol=2.5, rng=0.70, trend=3, lb=15 (70.6% hit, PF 6.42)
        # 15M: vol=1.8, rng=0.60, trend=10, lb=20 (58.8% hit, PF 2.72)
        # 1H: vol=1.3, rng=0.70, trend=5, lb=30 (60.4% hit, PF 2.31)
        exhaustion_tf_defaults = {
            '5M': {'volume_mult': 2.5, 'range_ratio_max': 0.70, 'trend_lookback': 3, 'lookback': 15},
            '15M': {'volume_mult': 1.8, 'range_ratio_max': 0.60, 'trend_lookback': 10, 'lookback': 20},
            '1H': {'volume_mult': 1.3, 'range_ratio_max': 0.70, 'trend_lookback': 5, 'lookback': 30},
        }

        tf_exhaustion = exhaustion_tf_defaults.get(timeframe) if timeframe else None
        if timeframe and hasattr(of_config, 'exhaustion_by_tf'):
            config_tf = of_config.exhaustion_by_tf.get(timeframe)
            if config_tf:
                tf_exhaustion = config_tf

        if tf_exhaustion:
            self.exhaustion_volume_mult = exhaustion_volume_mult or tf_exhaustion.get('volume_mult', 1.5)
            self.exhaustion_range_ratio_max = exhaustion_range_ratio_max or tf_exhaustion.get('range_ratio_max', 0.5)
            self.exhaustion_trend_lookback = exhaustion_trend_lookback or tf_exhaustion.get('trend_lookback', 5)
            self.exhaustion_lookback_bars = exhaustion_lookback_bars or tf_exhaustion.get('lookback', 20)
        else:
            self.exhaustion_volume_mult = exhaustion_volume_mult or getattr(of_config, 'exhaustion_volume_mult', 1.5)
            self.exhaustion_range_ratio_max = exhaustion_range_ratio_max or getattr(of_config, 'exhaustion_range_ratio_max', 0.5)
            self.exhaustion_trend_lookback = exhaustion_trend_lookback or getattr(of_config, 'exhaustion_trend_lookback', 5)
            self.exhaustion_lookback_bars = exhaustion_lookback_bars or getattr(of_config, 'exhaustion_lookback_bars', 20)

        # Institutional Activity params (from trades data)
        # Detects large trades with directional flow (accumulation/distribution)
        # Backtest: 15M optimal - PF 8.99, 80% hit rate with volume confirmation
        # (large_trade_min=2, flow_threshold=0.55, volume_mult=1.2)
        self.inst_large_trade_min = inst_large_trade_min or getattr(of_config, 'inst_large_trade_min', 2)
        self.inst_flow_threshold = inst_flow_threshold or getattr(of_config, 'inst_flow_threshold', 0.55)
        self.inst_volume_mult = inst_volume_mult or getattr(of_config, 'inst_volume_mult', 1.2)

        # Trade Flow Divergence params (from trades data)
        # Detects when trade flow diverges from price direction (contrarian signal)
        # Backtest: 15M optimal - 100% hit rate with persistence + volume + flow avg
        # (flow=0.58, price_change=0.001, lookback=5, persistence=2, volume_mult=1.3, flow_avg=2)
        self.tfd_flow_threshold = tfd_flow_threshold or getattr(of_config, 'tfd_flow_threshold', 0.58)
        self.tfd_price_change_pct = tfd_price_change_pct or getattr(of_config, 'tfd_price_change_pct', 0.001)
        self.tfd_lookback_bars = tfd_lookback_bars or getattr(of_config, 'tfd_lookback_bars', 5)
        self.tfd_persistence_bars = tfd_persistence_bars or getattr(of_config, 'tfd_persistence_bars', 2)
        self.tfd_volume_mult = tfd_volume_mult or getattr(of_config, 'tfd_volume_mult', 1.3)
        self.tfd_flow_avg_bars = tfd_flow_avg_bars or getattr(of_config, 'tfd_flow_avg_bars', 2)

        # General
        self.volume_lookback = volume_lookback or getattr(of_config, 'volume_lookback', 20)

        self.timeframe = timeframe

        logger.info(f"OrderflowSignalDetector initialized for {timeframe or 'default'}: absorption_mult={self.absorption_volume_mult}, "
                    f"price_tol={self.absorption_price_tol}, dom_threshold={self.absorption_dom_threshold}, "
                    f"lookback={self.lookback_bars}")

    def detect_absorption(self, df: pl.DataFrame) -> List[OrderflowSignal]:
        """Detect Absorption signals using trade flow

        Absorption: Large volume hitting a level but price stays stable.
        Direction is OPPOSITE of aggressive flow (the absorber wins).

        Trade Flow Logic:
        - Strong buying (delta_z > threshold) + flat price = asks absorbing = BEARISH
        - Strong selling (delta_z < -threshold) + flat price = bids absorbing = BULLISH

        Signal Logic:
        1. Volume > average * multiplier (high activity)
        2. Price change < tolerance (absorption holding level)
        3. Delta z-score exceeds threshold (strong directional flow)
        4. Trade flow ratio confirms the flow direction

        Note: Only works well on 5M and 15M. On higher timeframes, trade_flow_ratio
        gets averaged to ~0.50 and provides no signal.

        Args:
            df: DataFrame with columns: timestamp, volume, open, close, instant_delta,
                trade_flow_ratio

        Returns:
            List of Absorption signals
        """
        signals = []

        # Skip if absorption is disabled for this timeframe
        if not self.absorption_enabled:
            logger.debug(f"Absorption disabled for timeframe {self.timeframe}")
            return signals

        if len(df) < self.lookback_bars + 1:
            return signals

        # Check required columns
        has_trade_flow = "instant_delta" in df.columns

        if not has_trade_flow:
            logger.warning("No instant_delta column - falling back to DOM-based absorption")
            return self._detect_absorption_dom_legacy(df)

        # Filter out bars with zero instant_delta (no orderflow data)
        df = df.filter(pl.col("instant_delta") != 0)
        if len(df) < self.lookback_bars + 1:
            logger.warning("Not enough bars with orderflow data")
            return signals

        # Calculate rolling averages for volume and delta
        df = df.with_columns([
            pl.col("volume").rolling_mean(window_size=self.lookback_bars).alias("avg_volume"),
            pl.col("instant_delta").rolling_mean(window_size=self.lookback_bars).alias("avg_delta"),
            pl.col("instant_delta").rolling_std(window_size=self.lookback_bars).alias("std_delta"),
        ])

        # Calculate delta z-score and price change
        df = df.with_columns([
            ((pl.col("instant_delta") - pl.col("avg_delta")) /
             (pl.col("std_delta") + 1)).alias("delta_z"),
            ((pl.col("close") - pl.col("open")).abs() / pl.col("open")).alias("price_change_pct"),
        ])

        # Detect absorption conditions
        for row in df.iter_rows(named=True):
            if row["avg_volume"] is None or row["avg_volume"] == 0:
                continue

            # Condition 1: Volume spike
            volume_high = row["volume"] > row["avg_volume"] * self.absorption_volume_mult
            if not volume_high:
                continue

            # Condition 2: Price stability
            price_stable = row["price_change_pct"] < self.absorption_price_tol
            if not price_stable:
                continue

            # Condition 3: Strong directional delta
            delta_z = row.get("delta_z")
            if delta_z is None:
                continue

            trade_flow = row.get("trade_flow_ratio")
            direction = None
            details = ""

            # Strong BUYING pressure (delta_z > threshold) + flat price
            # = Asks are absorbing all the buying = BEARISH (sellers in control)
            if delta_z > self.absorption_delta_z_threshold:
                # Condition 4: Trade flow confirms buying pressure
                if trade_flow is not None and trade_flow < self.absorption_trade_flow_threshold:
                    continue  # Trade flow doesn't confirm
                direction = SignalDirection.BEARISH
                details = f"Ask absorbing buys: delta_z={delta_z:.2f}, TFR={trade_flow:.2f}" if trade_flow else f"Ask absorbing: delta_z={delta_z:.2f}"

            # Strong SELLING pressure (delta_z < -threshold) + flat price
            # = Bids are absorbing all the selling = BULLISH (buyers in control)
            elif delta_z < -self.absorption_delta_z_threshold:
                # Condition 4: Trade flow confirms selling pressure
                if trade_flow is not None and trade_flow > (1 - self.absorption_trade_flow_threshold):
                    continue  # Trade flow doesn't confirm
                direction = SignalDirection.BULLISH
                details = f"Bid absorbing sells: delta_z={delta_z:.2f}, TFR={trade_flow:.2f}" if trade_flow else f"Bid absorbing: delta_z={delta_z:.2f}"

            if direction is None:
                continue

            # Strength based on volume excess and delta intensity
            vol_strength = min(1.0, (row["volume"] / row["avg_volume"] - 1) / 2)
            delta_strength = min(1.0, abs(delta_z) / 3)
            strength = (vol_strength + delta_strength) / 2

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

    def _detect_absorption_dom_legacy(self, df: pl.DataFrame) -> List[OrderflowSignal]:
        """Legacy DOM-based absorption detection (fallback when no trade flow data)"""
        signals = []

        # Calculate rolling averages
        df = df.with_columns([
            pl.col("volume").rolling_mean(window_size=self.lookback_bars).alias("avg_volume"),
        ])

        if "total_bid_depth" in df.columns and "total_ask_depth" in df.columns:
            df = df.with_columns([
                pl.col("total_bid_depth").rolling_mean(window_size=self.lookback_bars).alias("avg_bid_depth"),
                pl.col("total_ask_depth").rolling_mean(window_size=self.lookback_bars).alias("avg_ask_depth"),
            ])
            df = df.with_columns([
                (pl.col("total_bid_depth") / pl.col("avg_bid_depth")).alias("bid_depth_ratio"),
                (pl.col("total_ask_depth") / pl.col("avg_ask_depth")).alias("ask_depth_ratio"),
            ])

        df = df.with_columns([
            ((pl.col("close") - pl.col("open")).abs() / pl.col("open")).alias("price_change_pct"),
        ])

        for row in df.iter_rows(named=True):
            if row["avg_volume"] is None or row["avg_volume"] == 0:
                continue

            volume_high = row["volume"] > row["avg_volume"] * self.absorption_volume_mult
            price_stable = row["price_change_pct"] < self.absorption_price_tol

            if not (volume_high and price_stable):
                continue

            # Check depth stability if available
            bid_stable = row.get("bid_depth_ratio") is not None and 0.7 < row.get("bid_depth_ratio", 1) < 1.5
            ask_stable = row.get("ask_depth_ratio") is not None and 0.7 < row.get("ask_depth_ratio", 1) < 1.5

            if "bid_depth_ratio" in row and not (bid_stable or ask_stable):
                continue

            # Determine direction based on DOM imbalance
            dom = row.get("dom_imbalance")
            if dom is None:
                continue

            if dom > self.absorption_dom_threshold:
                direction = SignalDirection.BULLISH
                details = f"Bid absorption (DOM): Vol {row['volume']:,.0f}, DOM {dom:.2f}"
            elif dom < (1 - self.absorption_dom_threshold):
                direction = SignalDirection.BEARISH
                details = f"Ask absorption (DOM): Vol {row['volume']:,.0f}, DOM {dom:.2f}"
            else:
                continue

            strength = min(1.0, (row["volume"] / row["avg_volume"] - 1) / 2)

            signals.append(OrderflowSignal(
                timestamp=int(row["timestamp"].timestamp()) if hasattr(row["timestamp"], "timestamp") else row["timestamp"],
                signal_type=SignalType.ABSORPTION,
                direction=direction,
                price=row["close"],
                strength=strength,
                details=details,
            ))

        return signals

    def detect_lsf(self, df: pl.DataFrame) -> List[OrderflowSignal]:
        """Detect Liquidity Sweep Fade (LSF) signals with orderflow confirmation

        LSF: Price sweeps beyond prior range then snaps back.
        Orderflow confirmation significantly improves signal quality.

        Signal Logic:
        - Price makes new high/low beyond prior rolling range (sweep)
        - Price snaps back into prior range within N bars (fade)

        Orderflow Confirmations (optional but recommended):
        - Delta divergence: Delta opposes sweep direction (stop hunt pattern)
          - Sweep high + negative delta = strong bearish (sellers already in control)
          - Sweep low + positive delta = strong bullish (buyers already in control)
        - Volume spike: Elevated volume on sweep bar confirms liquidity grab

        Backtest Results (PF = Profit Factor):
        - 5M: PF 6.67, 72.7% hit rate with orderflow confirmation
        - 15M: PF 1.46, 64.9% hit rate (weaker edge)
        - 1H: PF 3.27, 55.6% hit rate
        - 4H: PF 5.28, 90% hit rate (best for swing trades)

        Args:
            df: DataFrame with columns: timestamp, high, low, close, volume (optional),
                instant_delta (optional but recommended for confirmation)

        Returns:
            List of LSF signals
        """
        signals = []

        lookback = self.lsf_lookback_bars

        if len(df) < lookback + self.lsf_snapback_bars + 2:
            return signals

        # Calculate rolling high/low (shifted to exclude current bar)
        df = df.with_columns([
            pl.col("high").rolling_max(window_size=lookback).shift(1).alias("prior_high"),
            pl.col("low").rolling_min(window_size=lookback).shift(1).alias("prior_low"),
        ])

        # Calculate rolling volume average if using volume spike confirmation
        has_volume = "volume" in df.columns
        if self.lsf_require_volume_spike and has_volume:
            df = df.with_columns([
                pl.col("volume").rolling_mean(window_size=lookback).alias("avg_volume"),
            ])

        rows = df.to_dicts()
        has_delta = "instant_delta" in df.columns
        has_avg_volume = "avg_volume" in df.columns

        for i in range(lookback, len(rows) - self.lsf_snapback_bars - 1):
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
            if self.lsf_require_volume_spike and has_avg_volume:
                if avg_volume is None or avg_volume == 0:
                    continue
                if volume < avg_volume * self.lsf_volume_mult:
                    continue  # No volume spike, skip

            # Check for bearish LSF (sweep high then reverse down)
            sweep_depth_high = (row["high"] - row["prior_high"]) / row["prior_high"]
            if sweep_depth_high > self.lsf_sweep_threshold_pct:
                # Check delta divergence if required
                # For bearish LSF: delta should be negative (sellers already winning despite price sweep up)
                if self.lsf_require_delta_divergence and has_delta:
                    if instant_delta >= 0:
                        continue  # Delta doesn't diverge, skip

                # Look for snapback within N bars
                for j in range(1, self.lsf_snapback_bars + 1):
                    if i + j >= len(rows):
                        break
                    future_row = rows[i + j]
                    snapback_pct = (row["high"] - future_row["close"]) / row["high"]

                    if snapback_pct > self.lsf_snapback_pct:
                        strength = min(1.0, snapback_pct / (self.lsf_snapback_pct * 3))
                        # Boost strength if delta diverged
                        if has_delta and instant_delta < 0:
                            strength = min(1.0, strength * 1.2)
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
                # Check delta divergence if required
                # For bullish LSF: delta should be positive (buyers already winning despite price sweep down)
                if self.lsf_require_delta_divergence and has_delta:
                    if instant_delta <= 0:
                        continue  # Delta doesn't diverge, skip

                # Look for snapback within N bars
                for j in range(1, self.lsf_snapback_bars + 1):
                    if i + j >= len(rows):
                        break
                    future_row = rows[i + j]
                    snapback_pct = (future_row["close"] - row["low"]) / row["low"]

                    if snapback_pct > self.lsf_snapback_pct:
                        strength = min(1.0, snapback_pct / (self.lsf_snapback_pct * 3))
                        # Boost strength if delta diverged
                        if has_delta and instant_delta > 0:
                            strength = min(1.0, strength * 1.2)
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
        - Trade flow ratio confirms unwind direction (optional but recommended)
        - Trade in direction of unwind (fade the prior move)

        Backtest Results (15M only):
        - trade_flow mode: PF 8.59, 87.5% hit rate (recommended)
        - delta_only mode: PF 7.18, 77.8% hit rate (baseline)
        - 5M/1H/4H: No valid combinations found

        Args:
            df: DataFrame with columns: timestamp, close, instant_delta (or bar_delta),
                trade_flow_ratio (optional for enhanced mode)

        Returns:
            List of Delta Unwind signals
        """
        signals = []

        # Only enabled on 15M (other TFs have no predictive edge)
        if not self.delta_unwind_enabled:
            return signals

        delta_col = "bar_delta" if "bar_delta" in df.columns else "instant_delta"
        if delta_col not in df.columns:
            logger.warning("Delta Unwind detection requires delta column")
            return signals

        # Check if trade_flow_ratio is available for enhanced mode
        has_trade_flow = "trade_flow_ratio" in df.columns

        lookback = self.delta_unwind_lookback

        if len(df) < lookback + self.delta_unwind_bars + 2:
            return signals

        # Calculate cumulative delta and rolling stats
        df = df.with_columns([
            pl.col(delta_col).cum_sum().alias("cum_delta"),
        ])

        df = df.with_columns([
            pl.col("cum_delta").rolling_mean(window_size=lookback).alias("delta_mean"),
            pl.col("cum_delta").rolling_std(window_size=lookback).alias("delta_std"),
        ])

        # Calculate z-score
        df = df.with_columns([
            ((pl.col("cum_delta") - pl.col("delta_mean")) / pl.col("delta_std")).alias("delta_zscore"),
        ])

        rows = df.to_dicts()

        for i in range(lookback, len(rows) - self.delta_unwind_bars - 1):
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
                        # Trade flow confirmation: BEARISH unwind needs more selling (tf_ratio < 1 - threshold)
                        if has_trade_flow:
                            tf_ratio = future_row.get("trade_flow_ratio")
                            if tf_ratio is not None and tf_ratio > (1 - self.delta_unwind_tf_threshold):
                                continue  # Trade flow not supporting bearish unwind

                        strength = min(1.0, abs(zscore) / (self.delta_zscore_threshold * 2))
                        tf_val = future_row.get('trade_flow_ratio')
                        tf_info = f", tf={tf_val:.2f}" if has_trade_flow and tf_val is not None else ""
                        signals.append(OrderflowSignal(
                            timestamp=int(future_row["timestamp"].timestamp()) if hasattr(future_row["timestamp"], "timestamp") else future_row["timestamp"],
                            signal_type=SignalType.DELTA_UNWIND,
                            direction=SignalDirection.BEARISH,
                            price=future_row["close"],
                            strength=strength,
                            details=f"Delta unwind: z={zscore:.1f}, unwind {unwind_pct*100:.1f}%{tf_info}",
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
                        # Trade flow confirmation: BULLISH unwind needs more buying (tf_ratio > threshold)
                        if has_trade_flow:
                            tf_ratio = future_row.get("trade_flow_ratio")
                            if tf_ratio is not None and tf_ratio < self.delta_unwind_tf_threshold:
                                continue  # Trade flow not supporting bullish unwind

                        strength = min(1.0, abs(zscore) / (self.delta_zscore_threshold * 2))
                        tf_val = future_row.get('trade_flow_ratio')
                        tf_info = f", tf={tf_val:.2f}" if has_trade_flow and tf_val is not None else ""
                        signals.append(OrderflowSignal(
                            timestamp=int(future_row["timestamp"].timestamp()) if hasattr(future_row["timestamp"], "timestamp") else future_row["timestamp"],
                            signal_type=SignalType.DELTA_UNWIND,
                            direction=SignalDirection.BULLISH,
                            price=future_row["close"],
                            strength=strength,
                            details=f"Delta unwind: z={zscore:.1f}, unwind {unwind_pct*100:.1f}%{tf_info}",
                        ))
                        break

        logger.info(f"Detected {len(signals)} Delta Unwind signals")
        return signals

    def detect_exhaustion(self, df: pl.DataFrame) -> List[OrderflowSignal]:
        """Detect Exhaustion signals

        Exhaustion: High volume/activity with minimal price movement.
        Indicates the current move is running out of steam.

        Signal Logic (AND logic - both must agree):
        - High volume (spike above average)
        - Small price range relative to volume
        - instant_delta AND trend_change must agree on direction
        - Trade for reversal (fade the exhausted move)

        Args:
            df: DataFrame with columns: timestamp, open, high, low, close, volume,
                instant_delta (optional but recommended)

        Returns:
            List of Exhaustion signals
        """
        signals = []

        if "volume" not in df.columns:
            logger.warning("Exhaustion detection requires volume column")
            return signals

        lookback = self.exhaustion_lookback_bars
        trend_lookback = self.exhaustion_trend_lookback

        if len(df) < lookback + trend_lookback + 2:
            return signals

        # Calculate bar range and rolling stats
        df = df.with_columns([
            (pl.col("high") - pl.col("low")).alias("bar_range"),
        ])

        df = df.with_columns([
            pl.col("volume").rolling_mean(window_size=lookback).alias("avg_volume"),
            pl.col("bar_range").rolling_mean(window_size=lookback).alias("avg_range"),
        ])

        # Calculate price change for trend direction using configurable lookback
        df = df.with_columns([
            (pl.col("close") - pl.col("close").shift(trend_lookback)).alias("trend_change"),
        ])

        has_delta = "instant_delta" in df.columns

        rows = df.to_dicts()

        for i in range(lookback + trend_lookback, len(rows) - 1):
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

            # Get direction indicators
            trend_change = row["trend_change"] if row["trend_change"] is not None else 0
            instant_delta = row.get("instant_delta", 0) if has_delta else 0

            # AND logic: both delta and trend must agree for clear direction
            # Skip if signals conflict (delta and trend disagree)
            if has_delta and instant_delta != 0:
                # Use instant_delta AND trend_change (both must agree)
                if instant_delta > 0 and trend_change > 0:
                    direction = SignalDirection.BEARISH  # Exhausted buying, fade to sell
                elif instant_delta < 0 and trend_change < 0:
                    direction = SignalDirection.BULLISH  # Exhausted selling, fade to buy
                else:
                    # Delta and trend disagree - skip signal
                    continue
            else:
                # No delta available - use trend only (less reliable)
                if trend_change > 0:
                    direction = SignalDirection.BEARISH
                elif trend_change < 0:
                    direction = SignalDirection.BULLISH
                else:
                    continue

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
        - Large trade count >= threshold (default 2)
        - Trade flow ratio strongly directional (>0.55 bullish, <0.45 bearish)
        - Volume spike confirmation (volume > avg * 1.2)
        - Indicates "smart money" activity in a direction

        Backtest: 15M optimal - PF 8.99, 80% hit rate with volume confirmation

        Args:
            df: DataFrame with columns: timestamp, close, volume, large_trade_count, trade_flow_ratio

        Returns:
            List of Institutional signals
        """
        signals = []

        # Check for required columns (from trades data)
        if "large_trade_count" not in df.columns or "trade_flow_ratio" not in df.columns:
            logger.debug("Institutional detection requires large_trade_count and trade_flow_ratio columns (from trades data)")
            return signals

        # Add volume average for volume confirmation
        if self.inst_volume_mult > 0 and "volume" in df.columns:
            df = df.with_columns([
                pl.col("volume").rolling_mean(window_size=self.volume_lookback).alias("avg_volume"),
            ])

        for row in df.iter_rows(named=True):
            large_count = row.get("large_trade_count")
            flow_ratio = row.get("trade_flow_ratio")

            # Skip if no trade data for this bar
            if large_count is None or flow_ratio is None:
                continue

            # Check for significant institutional activity
            if large_count < self.inst_large_trade_min:
                continue

            # Volume spike confirmation
            if self.inst_volume_mult > 0:
                avg_vol = row.get("avg_volume")
                vol = row.get("volume", 0) or 0
                if avg_vol is None or avg_vol == 0 or vol < avg_vol * self.inst_volume_mult:
                    continue  # Skip if volume not elevated

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
        - Persistence filter: Require N consecutive bars with divergence
        - Volume confirmation: Require elevated volume (volume > avg * mult)
        - Flow smoothing: Use rolling average of flow ratio

        Backtest: 15M optimal - 100% hit rate with persistence + volume + flow avg

        Args:
            df: DataFrame with columns: timestamp, close, volume, trade_flow_ratio

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

        # Add rolling average of flow ratio if using smoothing
        if self.tfd_flow_avg_bars > 1:
            df = df.with_columns([
                pl.col("trade_flow_ratio").rolling_mean(window_size=self.tfd_flow_avg_bars).alias("avg_flow_ratio"),
            ])

        # Add volume average if using volume confirmation
        if self.tfd_volume_mult > 0 and "volume" in df.columns:
            df = df.with_columns([
                pl.col("volume").rolling_mean(window_size=self.volume_lookback).alias("avg_volume"),
            ])

        rows = df.to_dicts()
        persistence_count = 0
        persistence_direction = None

        for row in rows:
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
                direction = SignalDirection.BULLISH
            # Bearish divergence: price rising but sellers dominating
            elif price_change > self.tfd_price_change_pct and flow_ratio < (1 - self.tfd_flow_threshold):
                direction = SignalDirection.BEARISH

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

            # Use raw flow ratio for details
            raw_flow = row.get("trade_flow_ratio", flow_ratio)
            strength = min(1.0, abs(flow_ratio - 0.5) * 2 * abs(price_change) / self.tfd_price_change_pct / 2)

            if direction == SignalDirection.BULLISH:
                details = f"Bullish divergence: price {price_change*100:+.2f}% but {raw_flow:.0%} buy flow"
            else:
                details = f"Bearish divergence: price {price_change*100:+.2f}% but {1-raw_flow:.0%} sell flow"

            signals.append(OrderflowSignal(
                timestamp=int(row["timestamp"].timestamp()) if hasattr(row["timestamp"], "timestamp") else row["timestamp"],
                signal_type=SignalType.TRADE_FLOW_DIV,
                direction=direction,
                price=row.get("close", 0),
                strength=strength,
                details=details,
            ))

            # Reset persistence to avoid duplicate signals
            persistence_count = 0

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
