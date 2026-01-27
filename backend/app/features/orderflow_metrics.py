"""Advanced Orderflow Metrics - RVOL, VPIN, LDR, POC"""
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from enum import Enum
import polars as pl
import numpy as np

from config import get_config

logger = logging.getLogger(__name__)


class BiasStrength(str, Enum):
    """Market bias strength levels"""
    STRONG_BULLISH = "STRONG_BULLISH"
    BULLISH = "BULLISH"
    NEUTRAL = "NEUTRAL"
    BEARISH = "BEARISH"
    STRONG_BEARISH = "STRONG_BEARISH"


@dataclass
class RVOLMetrics:
    """Relative Volume metrics with POC context"""
    rvol: float  # Current volume / 20-period MA
    rvol_20ma: float  # 20-period volume moving average
    current_volume: int
    poc_price: float  # Point of Control - price level with highest volume
    poc_distance_pct: float  # Distance from current price to POC (%)
    price_vs_poc: str  # "ABOVE", "BELOW", or "AT"
    bias: BiasStrength
    conviction: str  # "HIGH", "MEDIUM", "LOW"
    details: str


@dataclass
class VPINMetrics:
    """Volume-Synchronized Probability of Informed Trading"""
    vpin: float  # 0.0 to 1.0 (probability of informed trading)
    vpin_threshold: float  # Alert threshold (typically 0.7)
    is_elevated: bool  # True if VPIN > threshold
    toxicity_level: str  # "LOW", "MODERATE", "HIGH", "EXTREME"
    recent_trend: str  # "RISING", "STABLE", "FALLING"
    details: str


@dataclass
class LDRMetrics:
    """Liquidity Depth Ratio from order book"""
    ldr: float  # Total bid depth / Total ask depth
    total_bid_depth: float
    total_ask_depth: float
    bid_concentration: float  # How concentrated bids are near best bid (0-1)
    ask_concentration: float  # How concentrated asks are near best ask (0-1)
    support_wall: bool  # True if strong bid wall detected
    resistance_wall: bool  # True if strong ask wall detected
    bias: BiasStrength
    details: str


@dataclass
class OrderflowDashboard:
    """Combined orderflow metrics dashboard"""
    timestamp: int
    rvol: Optional[RVOLMetrics]
    vpin: Optional[VPINMetrics]
    ldr: Optional[LDRMetrics]
    overall_bias: BiasStrength
    alert_level: str  # "NORMAL", "ELEVATED", "HIGH_ALERT"


class OrderflowMetricsCalculator:
    """Calculates advanced orderflow metrics

    All parameters can be overridden, but defaults are loaded from config.
    """

    def __init__(
        self,
        rvol_lookback: Optional[int] = None,
        rvol_high_threshold: Optional[float] = None,
        rvol_low_threshold: Optional[float] = None,
        vpin_bucket_size: Optional[int] = None,
        vpin_num_buckets: Optional[int] = None,
        vpin_alert_threshold: Optional[float] = None,
        ldr_wall_threshold: Optional[float] = None,
        poc_lookback: Optional[int] = None,
    ):
        # Load defaults from config
        config = get_config()
        mi_config = config.market_intensity
        of_config = config.orderflow_alpha

        # Use provided values or fall back to config defaults
        self.rvol_lookback = rvol_lookback or mi_config.rvol_lookback
        self.rvol_high_threshold = rvol_high_threshold or mi_config.rvol_high
        self.rvol_low_threshold = rvol_low_threshold or mi_config.rvol_low
        self.vpin_bucket_size = vpin_bucket_size or mi_config.vpin_buckets
        self.vpin_num_buckets = vpin_num_buckets or mi_config.vpin_num_buckets
        self.vpin_alert_threshold = vpin_alert_threshold or mi_config.vpin_alert
        self.ldr_wall_threshold = ldr_wall_threshold or of_config.ldr_wall_threshold
        self.poc_lookback = poc_lookback or mi_config.poc_lookback

    def calculate_rvol(self, df: pl.DataFrame) -> Optional[RVOLMetrics]:
        """Calculate Relative Volume with POC context

        RVOL = Current Volume / 20-period Volume MA
        POC = Price level with highest traded volume in lookback period

        Bias Logic:
        - Price above POC + RVOL > 1.5 = STRONG_BULLISH
        - Price above POC + RVOL > 1.0 = BULLISH
        - Price below POC + RVOL > 1.5 = STRONG_BEARISH
        - Price below POC + RVOL > 1.0 = BEARISH
        - Low RVOL = low conviction move (potential fakeout)
        """
        if len(df) < self.rvol_lookback + 1:
            logger.warning(f"Not enough data for RVOL (need {self.rvol_lookback + 1}, have {len(df)})")
            return None

        # Calculate 20-period volume MA
        df = df.with_columns([
            pl.col("volume").rolling_mean(window_size=self.rvol_lookback).alias("vol_ma")
        ])

        # Get latest values
        latest = df.tail(1).to_dicts()[0]
        current_volume = latest["volume"]
        vol_ma = latest["vol_ma"]

        if vol_ma is None or vol_ma == 0:
            return None

        rvol = current_volume / vol_ma

        # Calculate POC (Point of Control) - price with highest volume TODAY
        # Filter to current day's bars only
        latest_ts = latest["timestamp"]
        if hasattr(latest_ts, "date"):
            current_date = latest_ts.date()
        else:
            # Handle unix timestamp
            from datetime import datetime
            current_date = datetime.fromtimestamp(latest_ts).date()

        # Filter to today's bars
        today_df = df.filter(
            pl.col("timestamp").dt.date() == current_date
        )

        # Fall back to lookback if no today data (e.g., weekend/holiday)
        if len(today_df) < 5:
            today_df = df.tail(self.poc_lookback)
            poc_source = "lookback"
        else:
            poc_source = "today"

        # Create price buckets and find highest volume price
        # Round prices to tick size for grouping
        config = get_config()
        tick_size = config.instrument.tick_size

        # Group by rounded close price and sum volume
        volume_by_price = today_df.with_columns([
            ((pl.col("close") / tick_size).round() * tick_size).alias("price_bucket")
        ]).group_by("price_bucket").agg([
            pl.col("volume").sum().alias("total_vol")
        ]).sort("total_vol", descending=True)

        if len(volume_by_price) == 0:
            return None

        poc_price = volume_by_price[0, "price_bucket"]
        current_price = latest["close"]

        # Calculate POC distance
        poc_distance_pct = ((current_price - poc_price) / poc_price) * 100

        # Determine price vs POC
        if poc_distance_pct > 0.1:
            price_vs_poc = "ABOVE"
        elif poc_distance_pct < -0.1:
            price_vs_poc = "BELOW"
        else:
            price_vs_poc = "AT"

        # Determine bias based on RVOL and POC position
        if price_vs_poc == "ABOVE":
            if rvol >= self.rvol_high_threshold:
                bias = BiasStrength.STRONG_BULLISH
                conviction = "HIGH"
            elif rvol >= 1.0:
                bias = BiasStrength.BULLISH
                conviction = "MEDIUM"
            else:
                bias = BiasStrength.BULLISH
                conviction = "LOW"  # Low volume = potential fakeout
        elif price_vs_poc == "BELOW":
            if rvol >= self.rvol_high_threshold:
                bias = BiasStrength.STRONG_BEARISH
                conviction = "HIGH"
            elif rvol >= 1.0:
                bias = BiasStrength.BEARISH
                conviction = "MEDIUM"
            else:
                bias = BiasStrength.BEARISH
                conviction = "LOW"
        else:
            bias = BiasStrength.NEUTRAL
            conviction = "LOW" if rvol < 1.0 else "MEDIUM"

        poc_label = "Daily POC" if poc_source == "today" else "POC"
        details = f"RVOL {rvol:.2f}x | Price {price_vs_poc} {poc_label} (${poc_price:.2f}) by {abs(poc_distance_pct):.2f}%"

        return RVOLMetrics(
            rvol=round(rvol, 2),
            rvol_20ma=round(vol_ma, 0),
            current_volume=int(current_volume),
            poc_price=round(poc_price, 2),
            poc_distance_pct=round(poc_distance_pct, 2),
            price_vs_poc=price_vs_poc,
            bias=bias,
            conviction=conviction,
            details=details,
        )

    def calculate_vpin(self, df: pl.DataFrame) -> Optional[VPINMetrics]:
        """Calculate VPIN (Volume-Synchronized Probability of Informed Trading)

        VPIN measures the probability that informed traders are active.
        High VPIN often precedes volatility spikes or sharp reversals.

        Method:
        1. Divide volume into equal-sized buckets
        2. Classify each bucket as buy or sell based on price change
        3. VPIN = |Buy Volume - Sell Volume| / Total Volume (rolling average)

        Interpretation:
        - VPIN < 0.3: Low informed activity (retail flow)
        - VPIN 0.3-0.5: Normal market conditions
        - VPIN 0.5-0.7: Elevated informed activity
        - VPIN > 0.7: High probability of informed trading (alert!)
        """
        if len(df) < self.vpin_num_buckets:
            logger.warning(f"Not enough data for VPIN (need {self.vpin_num_buckets}, have {len(df)})")
            return None

        # Classify volume as buy or sell based on price change
        # Using tick rule: close > open = buy volume, close < open = sell volume
        df = df.with_columns([
            pl.when(pl.col("close") > pl.col("open"))
            .then(pl.col("volume"))
            .otherwise(pl.lit(0))
            .alias("buy_volume"),

            pl.when(pl.col("close") < pl.col("open"))
            .then(pl.col("volume"))
            .otherwise(pl.lit(0))
            .alias("sell_volume"),
        ])

        # Calculate rolling VPIN over buckets
        # VPIN = |sum(buy) - sum(sell)| / sum(total)
        df = df.with_columns([
            pl.col("buy_volume").rolling_sum(window_size=self.vpin_num_buckets).alias("rolling_buy"),
            pl.col("sell_volume").rolling_sum(window_size=self.vpin_num_buckets).alias("rolling_sell"),
            pl.col("volume").rolling_sum(window_size=self.vpin_num_buckets).alias("rolling_total"),
        ])

        df = df.with_columns([
            ((pl.col("rolling_buy") - pl.col("rolling_sell")).abs() / pl.col("rolling_total")).alias("vpin")
        ])

        # Get recent VPIN values for trend
        recent = df.tail(5)
        latest = df.tail(1).to_dicts()[0]

        vpin = latest.get("vpin")
        if vpin is None:
            return None

        # Determine toxicity level
        if vpin >= 0.7:
            toxicity_level = "EXTREME"
        elif vpin >= 0.5:
            toxicity_level = "HIGH"
        elif vpin >= 0.3:
            toxicity_level = "MODERATE"
        else:
            toxicity_level = "LOW"

        # Determine trend
        vpin_values = recent["vpin"].to_list()
        if len(vpin_values) >= 3:
            vpin_values = [v for v in vpin_values if v is not None]
            if len(vpin_values) >= 3:
                avg_first = sum(vpin_values[:2]) / 2
                avg_last = sum(vpin_values[-2:]) / 2
                if avg_last > avg_first * 1.1:
                    recent_trend = "RISING"
                elif avg_last < avg_first * 0.9:
                    recent_trend = "FALLING"
                else:
                    recent_trend = "STABLE"
            else:
                recent_trend = "STABLE"
        else:
            recent_trend = "STABLE"

        is_elevated = vpin >= self.vpin_alert_threshold

        details = f"VPIN {vpin:.1%} ({toxicity_level}) | Trend: {recent_trend}"
        if is_elevated:
            details += " | ⚠️ ALERT: High informed trading probability"

        return VPINMetrics(
            vpin=round(vpin, 3),
            vpin_threshold=self.vpin_alert_threshold,
            is_elevated=is_elevated,
            toxicity_level=toxicity_level,
            recent_trend=recent_trend,
            details=details,
        )

    def calculate_ldr(self, df: pl.DataFrame) -> Optional[LDRMetrics]:
        """Calculate Liquidity Depth Ratio from order book data

        LDR = Total Bid Depth / Total Ask Depth

        Uses MBP-10 data (10 levels of bid/ask) to measure:
        1. Overall depth imbalance
        2. Concentration near best bid/ask (are orders stacked tight or spread out?)
        3. Wall detection (significant one-sided liquidity)

        Bias Logic:
        - LDR > 2.5 = Wall of support below, bullish bias even if price falling
        - LDR < 0.4 = Wall of resistance above, bearish bias even if price rising
        - LDR 0.8-1.2 = Balanced book, neutral
        """
        # Check for MBP-10 level columns
        has_levels = all(f"bid_sz_{i:02d}" in df.columns for i in range(10))

        if has_levels:
            return self._calculate_ldr_from_levels(df)
        elif "total_bid_depth" in df.columns and "total_ask_depth" in df.columns:
            return self._calculate_ldr_from_totals(df)
        else:
            logger.warning("LDR calculation requires order book depth data")
            return None

    def _calculate_ldr_from_levels(self, df: pl.DataFrame) -> Optional[LDRMetrics]:
        """Calculate LDR using full MBP-10 level data"""
        latest = df.tail(1).to_dicts()[0]

        # Sum up all 10 levels
        total_bid = sum(latest.get(f"bid_sz_{i:02d}", 0) or 0 for i in range(10))
        total_ask = sum(latest.get(f"ask_sz_{i:02d}", 0) or 0 for i in range(10))

        if total_ask == 0:
            return None

        ldr = total_bid / total_ask

        # Calculate concentration (how much is at best bid/ask vs deeper levels)
        # Weights: level 0 has weight 10, level 9 has weight 1
        weights = [10 - i for i in range(10)]
        total_weight = sum(weights)

        bid_weighted = sum(
            (latest.get(f"bid_sz_{i:02d}", 0) or 0) * weights[i]
            for i in range(10)
        )
        ask_weighted = sum(
            (latest.get(f"ask_sz_{i:02d}", 0) or 0) * weights[i]
            for i in range(10)
        )

        # Concentration = weighted / (total * max_weight) normalized to 0-1
        bid_concentration = (bid_weighted / (total_bid * max(weights))) if total_bid > 0 else 0
        ask_concentration = (ask_weighted / (total_ask * max(weights))) if total_ask > 0 else 0

        # Detect walls
        support_wall = ldr >= self.ldr_wall_threshold
        resistance_wall = ldr <= (1 / self.ldr_wall_threshold)

        # Determine bias
        if ldr >= self.ldr_wall_threshold:
            bias = BiasStrength.STRONG_BULLISH
        elif ldr >= 1.5:
            bias = BiasStrength.BULLISH
        elif ldr <= (1 / self.ldr_wall_threshold):
            bias = BiasStrength.STRONG_BEARISH
        elif ldr <= 0.67:
            bias = BiasStrength.BEARISH
        else:
            bias = BiasStrength.NEUTRAL

        details = f"LDR {ldr:.2f}:1 | Bids: {total_bid:,.0f} | Asks: {total_ask:,.0f}"
        if support_wall:
            details += " | 🛡️ Support wall detected"
        if resistance_wall:
            details += " | 🧱 Resistance wall detected"

        return LDRMetrics(
            ldr=round(ldr, 2),
            total_bid_depth=total_bid,
            total_ask_depth=total_ask,
            bid_concentration=round(bid_concentration, 2),
            ask_concentration=round(ask_concentration, 2),
            support_wall=support_wall,
            resistance_wall=resistance_wall,
            bias=bias,
            details=details,
        )

    def _calculate_ldr_from_totals(self, df: pl.DataFrame) -> Optional[LDRMetrics]:
        """Fallback LDR using aggregated depth columns"""
        latest = df.tail(1).to_dicts()[0]

        total_bid = latest.get("total_bid_depth", 0) or 0
        total_ask = latest.get("total_ask_depth", 0) or 0

        if total_ask == 0:
            return None

        ldr = total_bid / total_ask

        # Without level data, we can't calculate concentration
        bid_concentration = 0.5
        ask_concentration = 0.5

        support_wall = ldr >= self.ldr_wall_threshold
        resistance_wall = ldr <= (1 / self.ldr_wall_threshold)

        if ldr >= self.ldr_wall_threshold:
            bias = BiasStrength.STRONG_BULLISH
        elif ldr >= 1.5:
            bias = BiasStrength.BULLISH
        elif ldr <= (1 / self.ldr_wall_threshold):
            bias = BiasStrength.STRONG_BEARISH
        elif ldr <= 0.67:
            bias = BiasStrength.BEARISH
        else:
            bias = BiasStrength.NEUTRAL

        details = f"LDR {ldr:.2f}:1 (from totals) | Bids: {total_bid:,.0f} | Asks: {total_ask:,.0f}"
        if support_wall:
            details += " | 🛡️ Support wall"
        if resistance_wall:
            details += " | 🧱 Resistance wall"

        return LDRMetrics(
            ldr=round(ldr, 2),
            total_bid_depth=total_bid,
            total_ask_depth=total_ask,
            bid_concentration=bid_concentration,
            ask_concentration=ask_concentration,
            support_wall=support_wall,
            resistance_wall=resistance_wall,
            bias=bias,
            details=details,
        )

    def calculate_all_metrics(self, df: pl.DataFrame) -> OrderflowDashboard:
        """Calculate all orderflow metrics and determine overall bias"""
        from datetime import datetime

        rvol = self.calculate_rvol(df)
        vpin = self.calculate_vpin(df)
        ldr = self.calculate_ldr(df)

        # Determine overall bias by combining signals
        bias_scores = {
            BiasStrength.STRONG_BULLISH: 2,
            BiasStrength.BULLISH: 1,
            BiasStrength.NEUTRAL: 0,
            BiasStrength.BEARISH: -1,
            BiasStrength.STRONG_BEARISH: -2,
        }

        total_score = 0
        weight_sum = 0

        if rvol:
            # RVOL gets higher weight when conviction is high
            weight = 2 if rvol.conviction == "HIGH" else 1
            total_score += bias_scores[rvol.bias] * weight
            weight_sum += weight

        if ldr:
            # LDR gets high weight since it shows actual liquidity
            weight = 2
            total_score += bias_scores[ldr.bias] * weight
            weight_sum += weight

        # Determine overall bias from combined score
        if weight_sum > 0:
            avg_score = total_score / weight_sum
            if avg_score >= 1.5:
                overall_bias = BiasStrength.STRONG_BULLISH
            elif avg_score >= 0.5:
                overall_bias = BiasStrength.BULLISH
            elif avg_score <= -1.5:
                overall_bias = BiasStrength.STRONG_BEARISH
            elif avg_score <= -0.5:
                overall_bias = BiasStrength.BEARISH
            else:
                overall_bias = BiasStrength.NEUTRAL
        else:
            overall_bias = BiasStrength.NEUTRAL

        # Determine alert level
        if vpin and vpin.is_elevated:
            alert_level = "HIGH_ALERT"
        elif (rvol and rvol.rvol >= 2.0) or (ldr and (ldr.support_wall or ldr.resistance_wall)):
            alert_level = "ELEVATED"
        else:
            alert_level = "NORMAL"

        return OrderflowDashboard(
            timestamp=int(datetime.utcnow().timestamp()),
            rvol=rvol,
            vpin=vpin,
            ldr=ldr,
            overall_bias=overall_bias,
            alert_level=alert_level,
        )
