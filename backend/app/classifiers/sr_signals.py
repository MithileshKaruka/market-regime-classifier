"""Buy/Sell signal generation at S/R levels based on regime"""
import logging
from typing import Literal, Optional
from dataclasses import dataclass
import polars as pl

from config import get_config

logger = logging.getLogger(__name__)

SignalType = Literal["BUY", "SELL", "NONE"]


@dataclass
class SRSignal:
    """Trading signal at S/R level"""
    signal: SignalType
    price: float
    level_type: str  # "support" or "resistance"
    confidence: float
    dom_score: float
    cvd_score: float
    reason: str
    timestamp: pl.Datetime


class SRSignalGenerator:
    """Generate buy/sell signals at S/R levels based on regime"""

    def __init__(
        self,
        dom_threshold: Optional[float] = None,
        cvd_threshold: Optional[int] = None,
        proximity_pct: Optional[float] = None,
    ):
        """Initialize signal generator

        Args:
            dom_threshold: DOM imbalance threshold (default from config)
            cvd_threshold: CVD threshold (default from config)
            proximity_pct: Price proximity to S/R level as percentage (default from config)
        """
        # Load defaults from config
        config = get_config()
        sr_config = config.support_resistance

        self.dom_threshold = dom_threshold or sr_config.signal_thresholds.dom_threshold
        self.cvd_threshold = cvd_threshold or int(sr_config.signal_thresholds.cvd_threshold)
        self.proximity_pct = proximity_pct or sr_config.proximity_pct

        # Signal weights from config
        self.dom_weight = sr_config.signal_weights.dom
        self.cvd_weight = sr_config.signal_weights.cvd
        logger.info("SRSignalGenerator initialized")

    def calculate_dom_score(self, dom_imbalance: float) -> tuple[float, str]:
        """Calculate DOM score (-1 to 1) and bias

        Args:
            dom_imbalance: DOM imbalance ratio (0 to 1)

        Returns:
            Tuple of (score, bias_description)
        """
        # Convert DOM imbalance to -1 to 1 scale
        # 0 = strong ask, 0.5 = balanced, 1 = strong bid
        score = (dom_imbalance - 0.5) * 2

        if dom_imbalance > self.dom_threshold:
            bias = "bullish"
        elif dom_imbalance < (1 - self.dom_threshold):
            bias = "bearish"
        else:
            bias = "neutral"

        return score, bias

    def calculate_cvd_score(self, cvd: float) -> tuple[float, str]:
        """Calculate CVD score (-1 to 1) and bias

        Args:
            cvd: Rolling Cumulative Volume Delta

        Returns:
            Tuple of (score, bias_description)
        """
        # Handle None/NaN
        if cvd is None or (isinstance(cvd, float) and cvd != cvd):
            return 0.0, "neutral"

        # Normalize CVD to -1 to 1 scale
        score = max(-1.0, min(1.0, cvd / (self.cvd_threshold * 2)))

        if cvd > self.cvd_threshold:
            bias = "bullish"
        elif cvd < -self.cvd_threshold:
            bias = "bearish"
        else:
            bias = "neutral"

        return score, bias

    def generate_signal_at_support(
        self,
        price: float,
        support_level: float,
        dom_imbalance: float,
        cvd: float,
        timestamp: pl.Datetime
    ) -> Optional[SRSignal]:
        """Generate buy signal at support level

        Logic:
        - Price near support (within proximity)
        - DOM shows bid pressure (bullish)
        - CVD shows buying (bullish)
        - Signal: BUY with weighted confidence (DOM 50%, CVD 50%)

        Args:
            price: Current price
            support_level: Support level price
            dom_imbalance: DOM imbalance ratio
            cvd: Rolling CVD
            timestamp: Current timestamp

        Returns:
            SRSignal or None if no signal
        """
        # Check if price is near support
        distance_pct = abs(price - support_level) / support_level
        if distance_pct > self.proximity_pct:
            return None

        # Calculate scores
        dom_score, dom_bias = self.calculate_dom_score(dom_imbalance)
        cvd_score, cvd_bias = self.calculate_cvd_score(cvd)

        # Weighted score using config weights
        combined_score = (dom_score * self.dom_weight) + (cvd_score * self.cvd_weight)

        # Generate BUY signal if bullish (combined_score > 0)
        if combined_score > 0:
            confidence = abs(combined_score)  # 0 to 1
            reason = f"Support @ {support_level:.2f} | DOM {dom_bias} ({dom_imbalance:.2f}) | CVD {cvd_bias} ({int(cvd)})"

            return SRSignal(
                signal="BUY",
                price=price,
                level_type="support",
                confidence=confidence,
                dom_score=dom_score,
                cvd_score=cvd_score,
                reason=reason,
                timestamp=timestamp
            )

        return None

    def generate_signal_at_resistance(
        self,
        price: float,
        resistance_level: float,
        dom_imbalance: float,
        cvd: float,
        timestamp: pl.Datetime
    ) -> Optional[SRSignal]:
        """Generate sell signal at resistance level

        Logic:
        - Price near resistance (within proximity)
        - DOM shows ask pressure (bearish)
        - CVD shows selling (bearish)
        - Signal: SELL with weighted confidence (DOM 50%, CVD 50%)

        Args:
            price: Current price
            resistance_level: Resistance level price
            dom_imbalance: DOM imbalance ratio
            cvd: Rolling CVD
            timestamp: Current timestamp

        Returns:
            SRSignal or None if no signal
        """
        # Check if price is near resistance
        distance_pct = abs(price - resistance_level) / resistance_level
        if distance_pct > self.proximity_pct:
            return None

        # Calculate scores
        dom_score, dom_bias = self.calculate_dom_score(dom_imbalance)
        cvd_score, cvd_bias = self.calculate_cvd_score(cvd)

        # Weighted score using config weights
        combined_score = (dom_score * self.dom_weight) + (cvd_score * self.cvd_weight)

        # Generate SELL signal if bearish (combined_score < 0)
        if combined_score < 0:
            confidence = abs(combined_score)  # 0 to 1
            reason = f"Resistance @ {resistance_level:.2f} | DOM {dom_bias} ({dom_imbalance:.2f}) | CVD {cvd_bias} ({int(cvd)})"

            return SRSignal(
                signal="SELL",
                price=price,
                level_type="resistance",
                confidence=confidence,
                dom_score=dom_score,
                cvd_score=cvd_score,
                reason=reason,
                timestamp=timestamp
            )

        return None

    def scan_for_signals(
        self,
        df: pl.DataFrame,
        support_levels: list[float],
        resistance_levels: list[float]
    ) -> pl.DataFrame:
        """Scan DataFrame for signals at all S/R levels

        Args:
            df: DataFrame with OHLC, dom_imbalance, cvd columns
            support_levels: List of support prices
            resistance_levels: List of resistance prices

        Returns:
            DataFrame with signal, signal_confidence, signal_reason columns
        """
        logger.info(f"Scanning {len(df)} bars for signals at {len(support_levels)} support and {len(resistance_levels)} resistance levels")

        results = []
        for row in df.iter_rows(named=True):
            price = row["close"]
            dom_imbalance = row["dom_imbalance"]
            cvd = row.get("cvd", 0)
            timestamp = row.get("ts_event") or row.get("timestamp")

            signal = None

            # Check support levels for BUY signals
            for support_level in support_levels:
                signal = self.generate_signal_at_support(
                    price, support_level, dom_imbalance, cvd, timestamp
                )
                if signal:
                    break  # Take first matching signal

            # Check resistance levels for SELL signals (if no support signal)
            if not signal:
                for resistance_level in resistance_levels:
                    signal = self.generate_signal_at_resistance(
                        price, resistance_level, dom_imbalance, cvd, timestamp
                    )
                    if signal:
                        break

            # Store results
            if signal:
                results.append({
                    "signal": signal.signal,
                    "signal_confidence": signal.confidence,
                    "signal_reason": signal.reason,
                    "signal_level": signal.price,
                })
            else:
                results.append({
                    "signal": "NONE",
                    "signal_confidence": 0.0,
                    "signal_reason": "",
                    "signal_level": None,
                })

        # Add results as new columns
        results_df = pl.DataFrame(results)
        df = df.hstack(results_df)

        signals_count = len(df.filter(pl.col("signal") != "NONE"))
        logger.info(f"Found {signals_count} signals")

        return df
