"""Regime classification based on order flow analysis"""
import logging
from typing import Literal, Optional
from dataclasses import dataclass
import polars as pl

from config import get_config

logger = logging.getLogger(__name__)

RegimeType = Literal["BULLISH", "BEARISH", "NEUTRAL"]


@dataclass
class RegimeSignal:
    """Individual regime signal from a specific indicator"""
    regime: RegimeType
    confidence: float
    reason: str


class RegimeClassifier:
    """Classify market regime based on order flow metrics"""

    def __init__(
        self,
        dom_threshold: Optional[float] = None,
        delta_threshold: Optional[int] = None,
        vwap_threshold: Optional[float] = None,
    ):
        """Initialize classifier with thresholds

        Args:
            dom_threshold: DOM imbalance threshold for bullish/bearish (default from config)
            delta_threshold: Delta threshold for bullish/bearish (default from config)
            vwap_threshold: VWAP distance threshold as percentage (default from config)
        """
        # Load defaults from config
        config = get_config()
        regime_config = config.regime.thresholds

        self.dom_threshold = dom_threshold or regime_config.dom_threshold
        self.delta_threshold = delta_threshold or int(regime_config.cvd_threshold)
        self.vwap_threshold = vwap_threshold or regime_config.vwap_threshold
        logger.info("RegimeClassifier initialized")

    def classify_from_dom(self, dom_imbalance: float) -> RegimeSignal:
        """Classify regime from DOM imbalance

        Args:
            dom_imbalance: DOM imbalance ratio (0 to 1)

        Returns:
            RegimeSignal
        """
        if dom_imbalance > self.dom_threshold:
            confidence = min((dom_imbalance - 0.5) * 2, 1.0)
            return RegimeSignal(
                regime="BULLISH",
                confidence=confidence,
                reason=f"DOM Imb {dom_imbalance:.2f} (bid heavy)"
            )
        elif dom_imbalance < (1 - self.dom_threshold):
            confidence = min((0.5 - dom_imbalance) * 2, 1.0)
            return RegimeSignal(
                regime="BEARISH",
                confidence=confidence,
                reason=f"DOM Imb {dom_imbalance:.2f} (ask heavy)"
            )
        else:
            return RegimeSignal(
                regime="NEUTRAL",
                confidence=1.0 - abs(dom_imbalance - 0.5) * 2,
                reason=f"DOM Imb {dom_imbalance:.2f} (balanced)"
            )

    def classify_from_delta(self, delta: float) -> RegimeSignal:
        """Classify regime from cumulative delta

        Args:
            delta: Cumulative delta value (can be None for insufficient data)

        Returns:
            RegimeSignal
        """
        # Handle None or null values (insufficient data for rolling window)
        if delta is None or (isinstance(delta, float) and delta != delta):  # Check for NaN
            return RegimeSignal(
                regime="NEUTRAL",
                confidence=0.5,
                reason="CVD insufficient data"
            )

        if delta > self.delta_threshold:
            confidence = min(delta / (self.delta_threshold * 2), 1.0)
            return RegimeSignal(
                regime="BULLISH",
                confidence=confidence,
                reason=f"Delta +{int(delta)} (net buying)"
            )
        elif delta < -self.delta_threshold:
            confidence = min(abs(delta) / (self.delta_threshold * 2), 1.0)
            return RegimeSignal(
                regime="BEARISH",
                confidence=confidence,
                reason=f"Delta {int(delta)} (net selling)"
            )
        else:
            return RegimeSignal(
                regime="NEUTRAL",
                confidence=1.0 - abs(delta) / self.delta_threshold,
                reason=f"Delta {int(delta)} (neutral)"
            )

    def classify_from_vwap(
        self,
        price: float,
        vwap: float
    ) -> RegimeSignal:
        """Classify regime from price vs VWAP

        Args:
            price: Current price
            vwap: VWAP value

        Returns:
            RegimeSignal
        """
        distance_pct = (price - vwap) / vwap

        if distance_pct > self.vwap_threshold:
            confidence = min(distance_pct / (self.vwap_threshold * 2), 1.0)
            return RegimeSignal(
                regime="BULLISH",
                confidence=confidence,
                reason=f"Price {distance_pct*100:.2f}% above VWAP"
            )
        elif distance_pct < -self.vwap_threshold:
            confidence = min(abs(distance_pct) / (self.vwap_threshold * 2), 1.0)
            return RegimeSignal(
                regime="BEARISH",
                confidence=confidence,
                reason=f"Price {abs(distance_pct)*100:.2f}% below VWAP"
            )
        else:
            return RegimeSignal(
                regime="NEUTRAL",
                confidence=1.0 - abs(distance_pct) / self.vwap_threshold,
                reason="Price at VWAP"
            )

    def synthesize_signals(
        self,
        signals: list[RegimeSignal],
        weights: list[float] = None
    ) -> tuple[RegimeType, float, str]:
        """Synthesize multiple signals into final classification

        Args:
            signals: List of regime signals
            weights: Optional list of weights for each signal (must match length of signals)
                    Default from config: [DOM, CVD, VWAP]

        Returns:
            Tuple of (regime, confidence, key_signal)
        """
        # Load default weights from config
        if weights is None:
            config = get_config()
            sw = config.regime.signal_weights
            weights = [sw.dom, sw.cvd, sw.vwap]

        if len(weights) != len(signals):
            # Fallback to equal weights if mismatch
            weights = [1.0 / len(signals)] * len(signals)

        # Apply weights to signal confidence
        bullish_score = sum(
            s.confidence * w for s, w in zip(signals, weights) if s.regime == "BULLISH"
        )
        bearish_score = sum(
            s.confidence * w for s, w in zip(signals, weights) if s.regime == "BEARISH"
        )
        neutral_score = sum(
            s.confidence * w for s, w in zip(signals, weights) if s.regime == "NEUTRAL"
        )

        total_score = bullish_score + bearish_score + neutral_score

        if total_score == 0:
            return "NEUTRAL", 0.5, "No signals"

        # Determine regime based on highest score
        scores = {
            "BULLISH": bullish_score,
            "BEARISH": bearish_score,
            "NEUTRAL": neutral_score,
        }

        regime = max(scores, key=scores.get)  # type: ignore
        confidence = scores[regime] / total_score

        # Get most confident signal as key_signal (prefer highest weighted signal)
        weighted_signals = [(s, s.confidence * w) for s, w in zip(signals, weights) if s.regime == regime]
        if weighted_signals:
            key_signal = max(weighted_signals, key=lambda x: x[1])[0].reason
        else:
            key_signal = "Unknown"

        return regime, confidence, key_signal

    def classify(
        self,
        dom_imbalance: float,
        cvd: float,
        price: float,
        vwap: float,
        signal_weights: list[float] = None
    ) -> tuple[RegimeType, float, str]:
        """Classify regime from all metrics

        Args:
            dom_imbalance: DOM imbalance ratio
            cvd: Rolling Cumulative Volume Delta
            price: Current price
            vwap: VWAP value
            signal_weights: Optional custom weights [DOM, CVD, VWAP]. Default: [0.6, 0.2, 0.2]

        Returns:
            Tuple of (regime, confidence, key_signal)
        """
        signals = [
            self.classify_from_dom(dom_imbalance),
            self.classify_from_delta(cvd),  # Using CVD instead of old cumulative delta
            self.classify_from_vwap(price, vwap),
        ]

        return self.synthesize_signals(signals, weights=signal_weights)

    def classify_dataframe(
        self,
        df: pl.DataFrame
    ) -> pl.DataFrame:
        """Classify regime for each row in DataFrame

        Args:
            df: DataFrame with order flow metrics

        Returns:
            DataFrame with regime, confidence, key_signal columns
        """
        logger.info(f"Classifying regime for {len(df)} records")

        # Apply classification to each row
        results = []
        for row in df.iter_rows(named=True):
            regime, confidence, key_signal = self.classify(
                dom_imbalance=row["dom_imbalance"],
                cvd=row.get("cvd", 0),  # Use CVD if available, else 0
                price=row["close"],
                vwap=row["vwap"],
            )
            results.append({
                "regime": regime,
                "confidence": confidence,
                "key_signal": key_signal,
            })

        # Add results as new columns
        results_df = pl.DataFrame(results)
        df = df.hstack(results_df)

        logger.info("Regime classification completed")
        return df
