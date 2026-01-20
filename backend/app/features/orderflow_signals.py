"""Orderflow Signal Detection - Absorption, Liquidity Sweep Fade (LSF), and OBI"""
import logging
from typing import List, Optional
from dataclasses import dataclass
from enum import Enum
import polars as pl

logger = logging.getLogger(__name__)


class SignalType(str, Enum):
    ABSORPTION = "Absorption"
    LSF = "LSF"  # Liquidity Sweep Fade
    OBI = "OB Imb"  # Order Book Imbalance


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
    2. LSF (Liquidity Sweep Fade): Stop run followed by snap-back
    3. OBI (Order Book Imbalance): Weighted imbalance across top 10 levels
    """

    def __init__(
        self,
        absorption_volume_mult: float = 1.3,  # Volume must be 1.3x average (lowered from 1.5)
        absorption_price_tol: float = 0.001,  # 0.1% price tolerance (raised from 0.05%)
        absorption_dom_threshold: float = 0.52,  # DOM > 0.52 bullish, < 0.48 bearish (tighter than regime)
        lsf_spike_mult: float = 1.5,  # Delta spike must be 1.5x average (lowered from 2.0)
        lsf_snapback_pct: float = 0.002,  # 0.2% snapback required (lowered from 0.3%)
        obi_threshold: float = 1.2,  # 1.2:1 imbalance ratio triggers signal (lowered from 3.0)
        lookback_bars: int = 20,  # Bars to look back for averages
    ):
        self.absorption_volume_mult = absorption_volume_mult
        self.absorption_price_tol = absorption_price_tol
        self.absorption_dom_threshold = absorption_dom_threshold
        self.lsf_spike_mult = lsf_spike_mult
        self.lsf_snapback_pct = lsf_snapback_pct
        self.obi_threshold = obi_threshold
        self.lookback_bars = lookback_bars
        logger.info(f"OrderflowSignalDetector initialized: absorption_mult={absorption_volume_mult}, "
                    f"obi_threshold={obi_threshold}")

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
        """Detect Liquidity Sweep Fade (LSF) signals

        LSF: Stop run followed by price snapping back into range.
        Market makers sweep stops then price reverses.

        Signal Logic:
        - Sudden spike in delta (stops being triggered)
        - Price makes new high/low (sweep)
        - Price quickly reverses back into prior range (fade)

        Args:
            df: DataFrame with columns: timestamp, high, low, close, instant_delta

        Returns:
            List of LSF signals
        """
        signals = []

        if len(df) < self.lookback_bars + 3:  # Need extra bars for lookback + snapback
            return signals

        # Calculate rolling stats
        df = df.with_columns([
            pl.col("instant_delta").abs().rolling_mean(window_size=self.lookback_bars).alias("avg_delta_abs"),
            pl.col("high").rolling_max(window_size=self.lookback_bars).alias("rolling_high"),
            pl.col("low").rolling_min(window_size=self.lookback_bars).alias("rolling_low"),
        ])

        rows = df.to_dicts()

        for i in range(self.lookback_bars, len(rows) - 2):
            row = rows[i]
            next_row = rows[i + 1]

            if row["avg_delta_abs"] is None or row["avg_delta_abs"] == 0:
                continue

            delta_spike = abs(row["instant_delta"]) > row["avg_delta_abs"] * self.lsf_spike_mult

            if not delta_spike:
                continue

            # Check for bullish LSF (sweep low then reverse up)
            sweep_low = row["low"] < row["rolling_low"]
            if sweep_low:
                # Check for snapback (next bar closes above the sweep low)
                snapback_pct = (next_row["close"] - row["low"]) / row["low"]
                if snapback_pct > self.lsf_snapback_pct:
                    strength = min(1.0, snapback_pct / (self.lsf_snapback_pct * 3))
                    signals.append(OrderflowSignal(
                        timestamp=int(next_row["timestamp"].timestamp()) if hasattr(next_row["timestamp"], "timestamp") else next_row["timestamp"],
                        signal_type=SignalType.LSF,
                        direction=SignalDirection.BULLISH,
                        price=next_row["close"],
                        strength=strength,
                        details=f"Low sweep ${row['low']:.2f} -> snapback ${next_row['close']:.2f} (+{snapback_pct*100:.2f}%)",
                    ))

            # Check for bearish LSF (sweep high then reverse down)
            sweep_high = row["high"] > row["rolling_high"]
            if sweep_high:
                # Check for snapback (next bar closes below the sweep high)
                snapback_pct = (row["high"] - next_row["close"]) / row["high"]
                if snapback_pct > self.lsf_snapback_pct:
                    strength = min(1.0, snapback_pct / (self.lsf_snapback_pct * 3))
                    signals.append(OrderflowSignal(
                        timestamp=int(next_row["timestamp"].timestamp()) if hasattr(next_row["timestamp"], "timestamp") else next_row["timestamp"],
                        signal_type=SignalType.LSF,
                        direction=SignalDirection.BEARISH,
                        price=next_row["close"],
                        strength=strength,
                        details=f"High sweep ${row['high']:.2f} -> snapback ${next_row['close']:.2f} (-{snapback_pct*100:.2f}%)",
                    ))

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

    def detect_all_signals(
        self,
        df: pl.DataFrame,
        detect_absorption: bool = True,
        detect_lsf: bool = True,
        detect_obi: bool = True,
    ) -> List[OrderflowSignal]:
        """Detect all orderflow signals

        Args:
            df: DataFrame with orderflow data
            detect_absorption: Whether to detect absorption signals
            detect_lsf: Whether to detect LSF signals
            detect_obi: Whether to detect OBI signals

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

        # Sort by timestamp
        all_signals.sort(key=lambda s: s.timestamp)

        logger.info(f"Total signals detected: {len(all_signals)}")
        return all_signals
