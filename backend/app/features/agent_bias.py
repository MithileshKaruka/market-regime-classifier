"""Agent Bias Scoring System

Combines multiple signal categories into a unified 0-100 bias score.

Categories:
1. Trend & Structure (20%): EMA trend + market structure + S/R levels
2. Market Intensity (30%): RVOL + VPIN - measures conviction behind moves
3. Order Flow Alpha (50%): OBI + LSF + Absorption + LDR - what big money is doing

Score Interpretation:
- 0-30: High Bearish Conviction - Short entries only, ignore support bounces
- 30-45: Weak Bearish - Exit longs, don't enter shorts yet
- 45-55: Neutral/Chop - Wait mode, avoid trading
- 55-70: Weak Bullish - Cautious longs at proven S/R only
- 70-100: High Bullish Conviction - Aggressive mode, buy breakouts

Configuration is loaded from config/agent_config.yaml
"""
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from enum import Enum
import polars as pl

from app.config import get_config

logger = logging.getLogger(__name__)


class AgentMode(str, Enum):
    """Agent trading mode based on bias score"""
    HIGH_BEARISH = "HIGH_BEARISH"      # 0-30: Short entries only
    WEAK_BEARISH = "WEAK_BEARISH"      # 30-45: Exit longs, no new shorts
    NEUTRAL = "NEUTRAL"                 # 45-55: Wait mode, no trades
    WEAK_BULLISH = "WEAK_BULLISH"      # 55-70: Cautious longs at S/R
    HIGH_BULLISH = "HIGH_BULLISH"      # 70-100: Aggressive long mode


class TrendDirection(str, Enum):
    """Trend direction from EMA analysis"""
    STRONG_UP = "STRONG_UP"
    UP = "UP"
    NEUTRAL = "NEUTRAL"
    DOWN = "DOWN"
    STRONG_DOWN = "STRONG_DOWN"


class MarketStructure(str, Enum):
    """Market structure classification"""
    HIGHER_HIGHS_HIGHER_LOWS = "HH_HL"  # Bullish structure
    LOWER_HIGHS_LOWER_LOWS = "LH_LL"    # Bearish structure
    CONSOLIDATION = "CONSOLIDATION"      # Range-bound
    BREAKOUT_UP = "BREAKOUT_UP"          # Breaking above resistance
    BREAKOUT_DOWN = "BREAKOUT_DOWN"      # Breaking below support


@dataclass
class TrendStructureScore:
    """Trend & Structure component (20% weight)"""
    score: float  # 0-100
    ema_trend: TrendDirection
    market_structure: MarketStructure
    price_vs_sr: str  # "ABOVE_RESISTANCE", "BELOW_SUPPORT", "IN_RANGE"
    details: str


@dataclass
class MarketIntensityScore:
    """Market Intensity component (30% weight)"""
    score: float  # 0-100
    rvol: float
    rvol_contribution: float  # 0-50
    vpin: float
    vpin_contribution: float  # 0-50
    is_high_conviction: bool
    details: str


@dataclass
class OrderFlowAlphaScore:
    """Order Flow Alpha component (50% weight)"""
    score: float  # 0-100
    obi_score: float  # 0-25
    ldr_score: float  # 0-25
    absorption_score: float  # 0-25
    lsf_score: float  # 0-25
    active_signals: List[str]
    details: str


@dataclass
class AgentBiasResult:
    """Complete agent bias assessment"""
    total_score: float  # 0-100
    mode: AgentMode
    trend_structure: TrendStructureScore
    market_intensity: MarketIntensityScore
    orderflow_alpha: OrderFlowAlphaScore
    recommendation: str
    confidence: str  # "HIGH", "MEDIUM", "LOW"
    details: str


class AgentBiasCalculator:
    """Calculates unified agent bias score from multiple signal sources

    All parameters are loaded from config/agent_config.yaml
    """

    def __init__(self):
        # Load configuration
        config = get_config()

        # Category weights (from config)
        self.TREND_STRUCTURE_WEIGHT = config.scoring.trend_structure_weight / 100
        self.MARKET_INTENSITY_WEIGHT = config.scoring.market_intensity_weight / 100
        self.ORDERFLOW_ALPHA_WEIGHT = config.scoring.orderflow_alpha_weight / 100

        # Trend & Structure params
        self.ema_fast = config.trend_structure.ema_fast
        self.ema_slow = config.trend_structure.ema_slow
        self.structure_lookback = 20  # Could add to config

        # Market Intensity params
        self.rvol_high_threshold = config.market_intensity.rvol_high
        self.vpin_alert_threshold = config.market_intensity.vpin_alert

        # Orderflow Alpha params
        self.ldr_wall_threshold = config.orderflow_alpha.ldr_wall_threshold

        # Score thresholds
        self.thresholds = config.thresholds

        logger.debug(f"AgentBiasCalculator initialized with weights: "
                    f"Trend={self.TREND_STRUCTURE_WEIGHT}, "
                    f"Intensity={self.MARKET_INTENSITY_WEIGHT}, "
                    f"Orderflow={self.ORDERFLOW_ALPHA_WEIGHT}")

    def calculate_trend_structure_score(
        self,
        df: pl.DataFrame,
        sr_levels: Optional[List[Dict]] = None,
    ) -> TrendStructureScore:
        """Calculate Trend & Structure score (20% of total)

        Components:
        - EMA 12/25 trend direction (40% of this category)
        - Market structure - HH/HL vs LH/LL (40% of this category)
        - Price position vs S/R levels (20% of this category)
        """
        if len(df) < self.structure_lookback:
            return TrendStructureScore(
                score=50.0,
                ema_trend=TrendDirection.NEUTRAL,
                market_structure=MarketStructure.CONSOLIDATION,
                price_vs_sr="UNKNOWN",
                details="Insufficient data for trend analysis",
            )

        # Calculate EMAs if not present
        if f"ema_{self.ema_fast}" not in df.columns:
            df = self._calculate_ema(df, self.ema_fast)
        if f"ema_{self.ema_slow}" not in df.columns:
            df = self._calculate_ema(df, self.ema_slow)

        latest = df.tail(1).to_dicts()[0]
        current_price = latest["close"]
        ema_fast_val = latest.get(f"ema_{self.ema_fast}")
        ema_slow_val = latest.get(f"ema_{self.ema_slow}")

        # 1. EMA Trend Analysis (40 points max)
        ema_score = 50.0  # Neutral default
        ema_trend = TrendDirection.NEUTRAL

        if ema_fast_val and ema_slow_val:
            ema_spread_pct = ((ema_fast_val - ema_slow_val) / ema_slow_val) * 100
            price_vs_ema = ((current_price - ema_fast_val) / ema_fast_val) * 100

            # Strong trend: EMAs spread > 0.5% and price confirms
            if ema_spread_pct > 0.5 and price_vs_ema > 0.2:
                ema_trend = TrendDirection.STRONG_UP
                ema_score = 85 + min(15, ema_spread_pct * 5)  # 85-100
            elif ema_spread_pct > 0.1 and price_vs_ema > 0:
                ema_trend = TrendDirection.UP
                ema_score = 65 + min(20, ema_spread_pct * 10)  # 65-85
            elif ema_spread_pct < -0.5 and price_vs_ema < -0.2:
                ema_trend = TrendDirection.STRONG_DOWN
                ema_score = 15 - min(15, abs(ema_spread_pct) * 5)  # 0-15
            elif ema_spread_pct < -0.1 and price_vs_ema < 0:
                ema_trend = TrendDirection.DOWN
                ema_score = 35 - min(20, abs(ema_spread_pct) * 10)  # 15-35
            else:
                ema_trend = TrendDirection.NEUTRAL
                ema_score = 50

        # 2. Market Structure Analysis (40 points max)
        structure_score = 50.0
        market_structure = MarketStructure.CONSOLIDATION

        lookback_df = df.tail(self.structure_lookback)
        highs = lookback_df["high"].to_list()
        lows = lookback_df["low"].to_list()

        if len(highs) >= 10:
            # Find swing highs and lows (local maxima/minima)
            swing_highs = self._find_swing_points(highs, is_high=True)
            swing_lows = self._find_swing_points(lows, is_high=False)

            if len(swing_highs) >= 2 and len(swing_lows) >= 2:
                # Compare recent swings
                hh = swing_highs[-1] > swing_highs[-2]  # Higher high
                hl = swing_lows[-1] > swing_lows[-2]    # Higher low
                lh = swing_highs[-1] < swing_highs[-2]  # Lower high
                ll = swing_lows[-1] < swing_lows[-2]    # Lower low

                if hh and hl:
                    market_structure = MarketStructure.HIGHER_HIGHS_HIGHER_LOWS
                    structure_score = 80
                elif lh and ll:
                    market_structure = MarketStructure.LOWER_HIGHS_LOWER_LOWS
                    structure_score = 20
                elif hh and ll:
                    # Expanding range - check breakout direction
                    if current_price > swing_highs[-2]:
                        market_structure = MarketStructure.BREAKOUT_UP
                        structure_score = 90
                    elif current_price < swing_lows[-2]:
                        market_structure = MarketStructure.BREAKOUT_DOWN
                        structure_score = 10
                    else:
                        market_structure = MarketStructure.CONSOLIDATION
                        structure_score = 50
                else:
                    market_structure = MarketStructure.CONSOLIDATION
                    structure_score = 50

        # 3. Price vs S/R Levels (20 points max)
        sr_score = 50.0
        price_vs_sr = "IN_RANGE"

        if sr_levels:
            supports = [l["price"] for l in sr_levels if l.get("type") == "support"]
            resistances = [l["price"] for l in sr_levels if l.get("type") == "resistance"]

            if resistances:
                nearest_resistance = min(resistances, key=lambda x: abs(x - current_price))
                if current_price > nearest_resistance * 1.002:  # 0.2% above
                    price_vs_sr = "ABOVE_RESISTANCE"
                    sr_score = 75

            if supports:
                nearest_support = min(supports, key=lambda x: abs(x - current_price))
                if current_price < nearest_support * 0.998:  # 0.2% below
                    price_vs_sr = "BELOW_SUPPORT"
                    sr_score = 25

        # Combine scores (40% EMA + 40% Structure + 20% S/R)
        total_score = (ema_score * 0.40) + (structure_score * 0.40) + (sr_score * 0.20)

        details = f"EMA {ema_trend.value} ({ema_score:.0f}) | Structure {market_structure.value} ({structure_score:.0f}) | S/R {price_vs_sr} ({sr_score:.0f})"

        return TrendStructureScore(
            score=round(total_score, 1),
            ema_trend=ema_trend,
            market_structure=market_structure,
            price_vs_sr=price_vs_sr,
            details=details,
        )

    def calculate_market_intensity_score(
        self,
        rvol: Optional[float],
        vpin: Optional[float],
        price_direction: str,  # "UP" or "DOWN" from recent price action
    ) -> MarketIntensityScore:
        """Calculate Market Intensity score (30% of total)

        Components:
        - RVOL contribution (50% of this category)
        - VPIN contribution (50% of this category)

        Logic:
        - High RVOL + price direction = conviction in that direction
        - High VPIN = informed trading happening (amplifies direction)
        """
        rvol_score = 50.0
        vpin_score = 50.0

        # RVOL scoring (0-100)
        if rvol is not None:
            if rvol >= 2.0:
                # Very high volume - strong conviction
                rvol_base = 80
            elif rvol >= self.rvol_high_threshold:
                # High volume
                rvol_base = 70
            elif rvol >= 1.0:
                # Normal volume
                rvol_base = 50
            elif rvol >= 0.5:
                # Low volume - weak conviction
                rvol_base = 35
            else:
                # Very low volume - likely fakeout
                rvol_base = 20

            # Apply direction bias
            if price_direction == "UP":
                rvol_score = rvol_base + (rvol_base - 50) * 0.3  # Amplify bullish
            elif price_direction == "DOWN":
                rvol_score = 100 - rvol_base - (rvol_base - 50) * 0.3  # Invert for bearish
            else:
                rvol_score = 50  # Neutral

        # VPIN scoring (0-100)
        if vpin is not None:
            if vpin >= self.vpin_alert_threshold:
                # High informed trading - amplifies existing direction
                vpin_intensity = min(1.0, (vpin - 0.3) / 0.5)  # 0-1 scale
                if price_direction == "UP":
                    vpin_score = 60 + (vpin_intensity * 40)  # 60-100
                elif price_direction == "DOWN":
                    vpin_score = 40 - (vpin_intensity * 40)  # 0-40
                else:
                    vpin_score = 50  # High VPIN but unclear direction = caution
            elif vpin >= 0.5:
                # Moderate informed trading
                if price_direction == "UP":
                    vpin_score = 55 + (vpin * 20)
                elif price_direction == "DOWN":
                    vpin_score = 45 - (vpin * 20)
                else:
                    vpin_score = 50
            else:
                # Low informed trading - retail flow
                vpin_score = 50  # Neutral contribution

        # Calculate contributions
        rvol_contribution = rvol_score * 0.5
        vpin_contribution = vpin_score * 0.5
        total_score = rvol_contribution + vpin_contribution

        is_high_conviction = (rvol is not None and rvol >= self.rvol_high_threshold) and \
                            (vpin is not None and vpin >= 0.5)

        details = f"RVOL {rvol:.2f}x ({rvol_score:.0f}) | VPIN {vpin:.1%} ({vpin_score:.0f})"

        return MarketIntensityScore(
            score=round(total_score, 1),
            rvol=rvol or 0,
            rvol_contribution=round(rvol_contribution, 1),
            vpin=vpin or 0,
            vpin_contribution=round(vpin_contribution, 1),
            is_high_conviction=is_high_conviction,
            details=details,
        )

    def calculate_orderflow_alpha_score(
        self,
        obi_ratio: Optional[float],  # Bid/Ask ratio
        ldr: Optional[float],        # Liquidity Depth Ratio
        absorption_signals: List[Dict],  # Recent absorption signals
        lsf_signals: List[Dict],         # Recent LSF signals
    ) -> OrderFlowAlphaScore:
        """Calculate Order Flow Alpha score (50% of total)

        Components (25 points each):
        - OBI: Order Book Imbalance direction
        - LDR: Liquidity Depth Ratio (wall detection)
        - Absorption: Recent absorption signals
        - LSF: Recent liquidity sweep fade signals
        """
        # OBI Score (0-100 -> 0-25 contribution)
        obi_score = 50.0
        if obi_ratio is not None:
            if obi_ratio >= self.ldr_wall_threshold:
                obi_score = 90  # Strong bid imbalance
            elif obi_ratio >= 1.5:
                obi_score = 70
            elif obi_ratio >= 1.1:
                obi_score = 55
            elif obi_ratio <= 1 / self.ldr_wall_threshold:
                obi_score = 10  # Strong ask imbalance
            elif obi_ratio <= 0.67:
                obi_score = 30
            elif obi_ratio <= 0.9:
                obi_score = 45
            else:
                obi_score = 50

        # LDR Score (0-100 -> 0-25 contribution)
        ldr_score = 50.0
        if ldr is not None:
            if ldr >= self.ldr_wall_threshold:
                ldr_score = 95  # Support wall - very bullish
            elif ldr >= 2.0:
                ldr_score = 80
            elif ldr >= 1.3:
                ldr_score = 60
            elif ldr <= 1 / self.ldr_wall_threshold:
                ldr_score = 5  # Resistance wall - very bearish
            elif ldr <= 0.5:
                ldr_score = 20
            elif ldr <= 0.77:
                ldr_score = 40
            else:
                ldr_score = 50

        # Absorption Score (0-100 -> 0-25 contribution)
        absorption_score = 50.0
        if absorption_signals:
            bullish_abs = sum(1 for s in absorption_signals if s.get("direction") == "BULLISH")
            bearish_abs = sum(1 for s in absorption_signals if s.get("direction") == "BEARISH")
            total_abs = bullish_abs + bearish_abs

            if total_abs > 0:
                # Weight by recency (more recent = higher weight)
                net_ratio = (bullish_abs - bearish_abs) / total_abs
                absorption_score = 50 + (net_ratio * 40)  # 10-90 range

        # LSF Score (0-100 -> 0-25 contribution)
        lsf_score = 50.0
        if lsf_signals:
            # LSF signals are reversal signals - very high conviction
            bullish_lsf = sum(1 for s in lsf_signals if s.get("direction") == "BULLISH")
            bearish_lsf = sum(1 for s in lsf_signals if s.get("direction") == "BEARISH")

            if bullish_lsf > bearish_lsf:
                lsf_score = 75 + min(25, bullish_lsf * 10)  # 75-100
            elif bearish_lsf > bullish_lsf:
                lsf_score = 25 - min(25, bearish_lsf * 10)  # 0-25
            else:
                lsf_score = 50

        # Combine scores (25% each)
        total_score = (obi_score * 0.25) + (ldr_score * 0.25) + \
                     (absorption_score * 0.25) + (lsf_score * 0.25)

        active_signals = []
        if obi_ratio and (obi_ratio > 1.3 or obi_ratio < 0.77):
            active_signals.append("OBI")
        if ldr and (ldr > 1.5 or ldr < 0.67):
            active_signals.append("LDR")
        if absorption_signals:
            active_signals.append(f"ABS({len(absorption_signals)})")
        if lsf_signals:
            active_signals.append(f"LSF({len(lsf_signals)})")

        details = f"OBI {obi_score:.0f} | LDR {ldr_score:.0f} | Abs {absorption_score:.0f} | LSF {lsf_score:.0f}"

        return OrderFlowAlphaScore(
            score=round(total_score, 1),
            obi_score=round(obi_score * 0.25, 1),
            ldr_score=round(ldr_score * 0.25, 1),
            absorption_score=round(absorption_score * 0.25, 1),
            lsf_score=round(lsf_score * 0.25, 1),
            active_signals=active_signals,
            details=details,
        )

    def calculate_total_bias(
        self,
        df: pl.DataFrame,
        sr_levels: Optional[List[Dict]] = None,
        rvol: Optional[float] = None,
        vpin: Optional[float] = None,
        obi_ratio: Optional[float] = None,
        ldr: Optional[float] = None,
        absorption_signals: Optional[List[Dict]] = None,
        lsf_signals: Optional[List[Dict]] = None,
    ) -> AgentBiasResult:
        """Calculate total agent bias score (0-100)

        Combines:
        - Trend & Structure: 20%
        - Market Intensity: 30%
        - Order Flow Alpha: 50%
        """
        # Determine price direction from recent bars
        if len(df) >= 5:
            recent = df.tail(5)
            price_change = recent["close"][-1] - recent["close"][0]
            price_direction = "UP" if price_change > 0 else "DOWN" if price_change < 0 else "NEUTRAL"
        else:
            price_direction = "NEUTRAL"

        # Calculate component scores
        trend_structure = self.calculate_trend_structure_score(df, sr_levels)
        market_intensity = self.calculate_market_intensity_score(rvol, vpin, price_direction)
        orderflow_alpha = self.calculate_orderflow_alpha_score(
            obi_ratio, ldr,
            absorption_signals or [],
            lsf_signals or [],
        )

        # Calculate weighted total
        total_score = (
            trend_structure.score * self.TREND_STRUCTURE_WEIGHT +
            market_intensity.score * self.MARKET_INTENSITY_WEIGHT +
            orderflow_alpha.score * self.ORDERFLOW_ALPHA_WEIGHT
        )

        # Determine agent mode (using config thresholds)
        if total_score <= self.thresholds.high_bearish_max:
            mode = AgentMode.HIGH_BEARISH
            recommendation = "SHORT ONLY - Ignore support bounces, sell rallies"
        elif total_score <= self.thresholds.weak_bearish_max:
            mode = AgentMode.WEAK_BEARISH
            recommendation = "EXIT LONGS - Market cooling, wait for clarity"
        elif total_score <= self.thresholds.neutral_max:
            mode = AgentMode.NEUTRAL
            recommendation = "WAIT MODE - High chop risk, avoid new positions"
        elif total_score <= self.thresholds.weak_bullish_max:
            mode = AgentMode.WEAK_BULLISH
            recommendation = "CAUTIOUS LONGS - Only at proven S/R levels"
        else:
            mode = AgentMode.HIGH_BULLISH
            recommendation = "AGGRESSIVE LONGS - Buy breakouts, add to winners"

        # Determine confidence
        score_components = [trend_structure.score, market_intensity.score, orderflow_alpha.score]
        score_variance = max(score_components) - min(score_components)

        if score_variance < 15 and market_intensity.is_high_conviction:
            confidence = "HIGH"
        elif score_variance < 25:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        details = (
            f"Score: {total_score:.1f}/100 | Mode: {mode.value}\n"
            f"Trend/Structure ({self.TREND_STRUCTURE_WEIGHT*100:.0f}%): {trend_structure.score:.1f}\n"
            f"Market Intensity ({self.MARKET_INTENSITY_WEIGHT*100:.0f}%): {market_intensity.score:.1f}\n"
            f"Order Flow ({self.ORDERFLOW_ALPHA_WEIGHT*100:.0f}%): {orderflow_alpha.score:.1f}"
        )

        return AgentBiasResult(
            total_score=round(total_score, 1),
            mode=mode,
            trend_structure=trend_structure,
            market_intensity=market_intensity,
            orderflow_alpha=orderflow_alpha,
            recommendation=recommendation,
            confidence=confidence,
            details=details,
        )

    def _calculate_ema(self, df: pl.DataFrame, period: int) -> pl.DataFrame:
        """Calculate EMA for a given period"""
        alpha = 2 / (period + 1)
        return df.with_columns([
            pl.col("close").ewm_mean(span=period, adjust=False).alias(f"ema_{period}")
        ])

    def _find_swing_points(self, values: List[float], is_high: bool, window: int = 5) -> List[float]:
        """Find swing highs or lows in a price series"""
        swings = []
        for i in range(window, len(values) - window):
            if is_high:
                if all(values[i] >= values[i-j] for j in range(1, window+1)) and \
                   all(values[i] >= values[i+j] for j in range(1, window+1)):
                    swings.append(values[i])
            else:
                if all(values[i] <= values[i-j] for j in range(1, window+1)) and \
                   all(values[i] <= values[i+j] for j in range(1, window+1)):
                    swings.append(values[i])
        return swings
