"""Agent Bias Scoring System

Combines multiple signal categories into a unified 0-100 bias score.

Calculation Order (ORDER FLOW FIRST):
  1. Order Flow Alpha (60%) - calculated FIRST to derive direction
  2. Trend & Structure (20%) - adjusted based on orderflow alignment
  3. Market Intensity (20%) - amplified/dampened based on orderflow agreement

Categories:
1. Trend & Structure (20%): EMA trend + market structure + S/R levels
2. Market Intensity (20%): RVOL + VPIN - measures conviction behind moves
3. Order Flow Alpha (60%): Context-aware scoring based on active primary signals

Order Flow Alpha - Context-Aware Scoring:
  When PRIMARY SIGNAL is active (Absorption/Exhaustion/Delta Unwind):
    - Primary Signal: 50%
    - LDR: 20%
    - OBI: 15%
    - CVD: 15%

  When NO PRIMARY SIGNAL is active (BASE mode):
    - LDR: 33%
    - OBI: 33%
    - CVD: 34%

Component Alignment:
  Order Flow direction (derived from score: >55=BULLISH, <45=BEARISH) affects other components:

  Market Intensity Alignment:
    - If price direction AGREES with orderflow → +20% amplification (push away from 50)
    - If price direction CONFLICTS with orderflow → -20% dampening (pull toward 50)

  Trend & Structure Confidence:
    - If trend CONFIRMS orderflow → +15% boost
    - If trend CONTRADICTS orderflow → -15% reduction

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

from config import get_config

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
    """Market Intensity component (20% weight)"""
    score: float  # 0-100
    rvol: float
    rvol_contribution: float  # 0-50
    vpin: float
    vpin_contribution: float  # 0-50
    is_high_conviction: bool
    details: str


@dataclass
class OrderFlowAlphaScore:
    """Order Flow Alpha component (60% weight)

    Context-aware scoring based on active primary signals:

    When PRIMARY SIGNAL is active (Absorption/Exhaustion/Delta Unwind):
    - Primary Signal: 50%
    - LDR: 20%
    - OBI: 15%
    - CVD: 15%

    When NO PRIMARY SIGNAL is active:
    - LDR: 33%
    - OBI: 33%
    - CVD: 33%

    Primary signals ranked by conviction (based on backtesting):
    1. Delta Unwind: 86.7% hit rate on 5M
    2. Exhaustion: 81.8% hit rate on 5M
    3. Absorption: 66.7% hit rate on 15M
    """
    score: float  # 0-100
    active_mode: str  # "ABSORPTION", "EXHAUSTION", "DELTA_UNWIND", or "BASE"
    primary_score: float  # Primary signal score contribution
    ldr_score: float  # LDR contribution
    obi_score: float  # OBI contribution
    cvd_score: float  # CVD contribution
    active_signals: List[str]  # All active signals in strength order
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
        self._config = config  # Store for timeframe-specific lookups

        # Default category weights (from config)
        self.TREND_STRUCTURE_WEIGHT = config.scoring.trend_structure_weight / 100
        self.MARKET_INTENSITY_WEIGHT = config.scoring.market_intensity_weight / 100
        self.ORDERFLOW_ALPHA_WEIGHT = config.scoring.orderflow_alpha_weight / 100

        # Trend & Structure params
        self.ema_fast = config.trend_structure.ema_fast
        self.ema_slow = config.trend_structure.ema_slow
        self.structure_lookback = config.trend_structure.structure_lookback

        # Market Intensity params
        self.rvol_high_threshold = config.market_intensity.rvol_high
        self.vpin_alert_threshold = config.market_intensity.vpin_alert

        # Orderflow Alpha params
        self.ldr_wall_threshold = config.orderflow_alpha.ldr_wall_threshold
        self.cvd_threshold = config.orderflow_alpha.cvd_threshold

        # Score thresholds
        self.thresholds = config.thresholds

        logger.debug(f"AgentBiasCalculator initialized with default weights: "
                    f"Trend={self.TREND_STRUCTURE_WEIGHT}, "
                    f"Intensity={self.MARKET_INTENSITY_WEIGHT}, "
                    f"Orderflow={self.ORDERFLOW_ALPHA_WEIGHT}")

    def get_weights_for_timeframe(self, timeframe: str) -> tuple:
        """Get component weights for a specific timeframe.

        Returns:
            tuple: (trend_weight, intensity_weight, orderflow_weight) as decimals (0-1)

        Backtested optimal weights (explore_edge_improvements.py):
            5M:  T20/I20/O60 = 1.17 PF (default optimal)
            15M: T10/I30/O60 = 0.89 PF (best)
            1H:  T10/I30/O60 = 1.08 PF (best)
            4H:  T10/I30/O60 = 1.13 PF (best)
        """
        # Check for timeframe-specific weights in config
        by_tf = getattr(self._config.scoring, 'by_timeframe', None)
        if by_tf and timeframe in by_tf:
            tf_weights = by_tf[timeframe]
            return (
                tf_weights.get('trend', self.TREND_STRUCTURE_WEIGHT * 100) / 100,
                tf_weights.get('intensity', self.MARKET_INTENSITY_WEIGHT * 100) / 100,
                tf_weights.get('orderflow', self.ORDERFLOW_ALPHA_WEIGHT * 100) / 100,
            )
        # Return default weights
        return (self.TREND_STRUCTURE_WEIGHT, self.MARKET_INTENSITY_WEIGHT, self.ORDERFLOW_ALPHA_WEIGHT)

    def calculate_trend_structure_score(
        self,
        df: pl.DataFrame,
        sr_levels: Optional[List[Dict]] = None,
        orderflow_direction: Optional[str] = None,  # "BULLISH", "BEARISH", or "NEUTRAL"
    ) -> TrendStructureScore:
        """Calculate Trend & Structure score (20% of total)

        Components:
        - EMA 12/25 trend direction (40% of this category)
        - Market structure - HH/HL vs LH/LL (40% of this category)
        - Price position vs S/R levels (20% of this category)

        Confidence Modifier (based on orderflow alignment):
        - If orderflow confirms trend direction → boost score by 10%
        - If orderflow contradicts trend direction → reduce score by 10%
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

        # Apply orderflow confidence modifier
        # Determine trend direction from score (>55 = bullish, <45 = bearish)
        trend_direction = "BULLISH" if total_score > 55 else "BEARISH" if total_score < 45 else "NEUTRAL"
        alignment_modifier = ""

        if orderflow_direction and orderflow_direction != "NEUTRAL" and trend_direction != "NEUTRAL":
            if orderflow_direction == trend_direction:
                # Orderflow confirms trend - boost score by amplifying distance from 50
                boost = abs(total_score - 50) * 0.15  # 15% boost
                if total_score > 50:
                    total_score = min(100, total_score + boost)
                else:
                    total_score = max(0, total_score - boost)
                alignment_modifier = " [OF+]"
            else:
                # Orderflow contradicts trend - dampen by pulling toward 50
                dampen = abs(total_score - 50) * 0.15  # 15% dampen
                if total_score > 50:
                    total_score = total_score - dampen
                else:
                    total_score = total_score + dampen
                alignment_modifier = " [OF-]"

        details = f"EMA {ema_trend.value} ({ema_score:.0f}) | Structure {market_structure.value} ({structure_score:.0f}) | S/R {price_vs_sr} ({sr_score:.0f}){alignment_modifier}"

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
        orderflow_direction: Optional[str] = None,  # "BULLISH", "BEARISH", or "NEUTRAL"
    ) -> MarketIntensityScore:
        """Calculate Market Intensity score (20% of total)

        Components:
        - RVOL contribution (50% of this category)
        - VPIN contribution (50% of this category)

        Logic:
        - High RVOL + price direction = conviction in that direction
        - High VPIN = informed trading happening (amplifies direction)

        Alignment Modifier:
        - If price and orderflow directions AGREE → amplify score (push away from 50)
        - If price and orderflow directions CONFLICT → dampen score (push toward 50)
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

        # Apply alignment modifier based on price vs orderflow agreement
        alignment_modifier = ""
        price_dir_normalized = "BULLISH" if price_direction == "UP" else "BEARISH" if price_direction == "DOWN" else "NEUTRAL"

        if orderflow_direction and orderflow_direction != "NEUTRAL" and price_dir_normalized != "NEUTRAL":
            if orderflow_direction == price_dir_normalized:
                # Price and orderflow AGREE - amplify the score
                # Push further from 50 by 20%
                amplify = abs(total_score - 50) * 0.20
                if total_score > 50:
                    total_score = min(100, total_score + amplify)
                else:
                    total_score = max(0, total_score - amplify)
                alignment_modifier = " [ALIGNED]"
                is_high_conviction = is_high_conviction or (rvol is not None and rvol >= 1.0)
            else:
                # Price and orderflow CONFLICT - dampen the score
                # Pull toward 50 by 20%
                dampen = abs(total_score - 50) * 0.20
                if total_score > 50:
                    total_score = total_score - dampen
                else:
                    total_score = total_score + dampen
                alignment_modifier = " [CONFLICT]"
                is_high_conviction = False  # Can't be high conviction if signals conflict

        details = f"RVOL {rvol:.2f}x ({rvol_score:.0f}) | VPIN {vpin:.1%} ({vpin_score:.0f}){alignment_modifier}"

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
        cvd: Optional[float] = None,     # Cumulative Volume Delta from trades
        delta_unwind_signals: Optional[List[Dict]] = None,  # Delta unwind signals
        exhaustion_signals: Optional[List[Dict]] = None,    # Exhaustion signals
    ) -> OrderFlowAlphaScore:
        """Calculate Order Flow Alpha score (60% of total)

        Context-aware scoring based on active primary signals:

        When PRIMARY SIGNAL is active (Absorption/Exhaustion/Delta Unwind):
        - Primary Signal: 50%
        - LDR: 20%
        - OBI: 15%
        - CVD: 15%

        When NO PRIMARY SIGNAL is active:
        - LDR: 33%
        - OBI: 33%
        - CVD: 33%

        Primary signals ranked by conviction:
        1. Delta Unwind: 86.7% hit rate
        2. Exhaustion: 81.8% hit rate
        3. Absorption: 66.7% hit rate
        """
        # Weights when primary signal is active
        PRIMARY_WEIGHT = 0.50
        LDR_WEIGHT_WITH_PRIMARY = 0.20
        OBI_WEIGHT_WITH_PRIMARY = 0.15
        CVD_WEIGHT_WITH_PRIMARY = 0.15

        # Weights when no primary signal (base mode)
        LDR_WEIGHT_BASE = 0.33
        OBI_WEIGHT_BASE = 0.33
        CVD_WEIGHT_BASE = 0.34  # Slightly higher to sum to 1.0

        # ============================================
        # Calculate base scores for supporting signals
        # ============================================

        # OBI Score (0-100)
        obi_raw = 50.0
        if obi_ratio is not None:
            if obi_ratio >= self.ldr_wall_threshold:
                obi_raw = 90  # Strong bid imbalance
            elif obi_ratio >= 1.5:
                obi_raw = 70
            elif obi_ratio >= 1.1:
                obi_raw = 55
            elif obi_ratio <= 1 / self.ldr_wall_threshold:
                obi_raw = 10  # Strong ask imbalance
            elif obi_ratio <= 0.67:
                obi_raw = 30
            elif obi_ratio <= 0.9:
                obi_raw = 45
            else:
                obi_raw = 50

        # LDR Score (0-100)
        ldr_raw = 50.0
        if ldr is not None:
            if ldr >= self.ldr_wall_threshold:
                ldr_raw = 95  # Support wall - very bullish
            elif ldr >= 2.0:
                ldr_raw = 80
            elif ldr >= 1.3:
                ldr_raw = 60
            elif ldr <= 1 / self.ldr_wall_threshold:
                ldr_raw = 5  # Resistance wall - very bearish
            elif ldr <= 0.5:
                ldr_raw = 20
            elif ldr <= 0.77:
                ldr_raw = 40
            else:
                ldr_raw = 50

        # CVD Score (0-100)
        cvd_raw = 50.0
        cvd_threshold = self.cvd_threshold
        if cvd is not None and cvd != 0:
            if cvd >= cvd_threshold * 2:
                cvd_raw = 90 + min(10, (cvd - cvd_threshold * 2) / cvd_threshold * 10)
            elif cvd >= cvd_threshold:
                cvd_raw = 60 + (cvd - cvd_threshold) / cvd_threshold * 30
            elif cvd > 0:
                cvd_raw = 50 + cvd / cvd_threshold * 10
            elif cvd <= -cvd_threshold * 2:
                cvd_raw = 10 - min(10, (abs(cvd) - cvd_threshold * 2) / cvd_threshold * 10)
            elif cvd <= -cvd_threshold:
                cvd_raw = 40 - (abs(cvd) - cvd_threshold) / cvd_threshold * 30
            else:
                cvd_raw = 50 - abs(cvd) / cvd_threshold * 10
            cvd_raw = max(0, min(100, cvd_raw))

        # ============================================
        # Calculate primary signal scores
        # ============================================

        # Absorption Score (0-100)
        absorption_raw = 50.0
        absorption_active = False
        if absorption_signals:
            bullish_abs = sum(1 for s in absorption_signals if s.get("direction") == "BULLISH")
            bearish_abs = sum(1 for s in absorption_signals if s.get("direction") == "BEARISH")
            total_abs = bullish_abs + bearish_abs

            if total_abs > 0:
                net_ratio = (bullish_abs - bearish_abs) / total_abs
                absorption_raw = 50 + (net_ratio * 40)  # 10-90 range
                absorption_active = True

        # Delta Unwind Score (0-100) - HIGHEST conviction
        delta_unwind_raw = 50.0
        delta_unwind_active = False
        if delta_unwind_signals:
            bullish_du = sum(1 for s in delta_unwind_signals if s.get("direction") == "BULLISH")
            bearish_du = sum(1 for s in delta_unwind_signals if s.get("direction") == "BEARISH")

            if bullish_du > bearish_du:
                delta_unwind_raw = 80 + min(20, bullish_du * 15)  # 80-100
                delta_unwind_active = True
            elif bearish_du > bullish_du:
                delta_unwind_raw = 20 - min(20, bearish_du * 15)  # 0-20
                delta_unwind_active = True

        # Exhaustion Score (0-100) - HIGH conviction
        exhaustion_raw = 50.0
        exhaustion_active = False
        if exhaustion_signals:
            bullish_exh = sum(1 for s in exhaustion_signals if s.get("direction") == "BULLISH")
            bearish_exh = sum(1 for s in exhaustion_signals if s.get("direction") == "BEARISH")

            if bullish_exh > bearish_exh:
                exhaustion_raw = 75 + min(25, bullish_exh * 12)  # 75-100
                exhaustion_active = True
            elif bearish_exh > bullish_exh:
                exhaustion_raw = 25 - min(25, bearish_exh * 12)  # 0-25
                exhaustion_active = True

        # ============================================
        # Determine active mode and calculate score
        # ============================================

        # Collect active primary signals with their scores and conviction rank
        # Rank: 1=Delta Unwind (86.7%), 2=Exhaustion (81.8%), 3=Absorption (66.7%)
        primary_signals = []
        if delta_unwind_active:
            primary_signals.append(("DELTA_UNWIND", delta_unwind_raw, 1, abs(delta_unwind_raw - 50)))
        if exhaustion_active:
            primary_signals.append(("EXHAUSTION", exhaustion_raw, 2, abs(exhaustion_raw - 50)))
        if absorption_active:
            primary_signals.append(("ABSORPTION", absorption_raw, 3, abs(absorption_raw - 50)))

        # Sort by strength (distance from neutral), then by conviction rank
        primary_signals.sort(key=lambda x: (-x[3], x[2]))

        if primary_signals:
            # Use strongest primary signal
            active_mode, primary_raw, _, _ = primary_signals[0]

            # Calculate score with primary signal weights
            total_score = (
                primary_raw * PRIMARY_WEIGHT +
                ldr_raw * LDR_WEIGHT_WITH_PRIMARY +
                obi_raw * OBI_WEIGHT_WITH_PRIMARY +
                cvd_raw * CVD_WEIGHT_WITH_PRIMARY
            )

            primary_contribution = primary_raw * PRIMARY_WEIGHT
            ldr_contribution = ldr_raw * LDR_WEIGHT_WITH_PRIMARY
            obi_contribution = obi_raw * OBI_WEIGHT_WITH_PRIMARY
            cvd_contribution = cvd_raw * CVD_WEIGHT_WITH_PRIMARY
        else:
            # Base mode - no primary signal active
            active_mode = "BASE"

            total_score = (
                ldr_raw * LDR_WEIGHT_BASE +
                obi_raw * OBI_WEIGHT_BASE +
                cvd_raw * CVD_WEIGHT_BASE
            )

            primary_contribution = 0.0
            ldr_contribution = ldr_raw * LDR_WEIGHT_BASE
            obi_contribution = obi_raw * OBI_WEIGHT_BASE
            cvd_contribution = cvd_raw * CVD_WEIGHT_BASE

        # ============================================
        # Build active signals list (strength order)
        # ============================================

        active_signals = []

        # Add primary signals in strength order
        for sig_name, sig_score, _, _ in primary_signals:
            direction = "+" if sig_score > 50 else "-"
            if sig_name == "DELTA_UNWIND":
                active_signals.append(f"DU{direction}")
            elif sig_name == "EXHAUSTION":
                active_signals.append(f"EXH{direction}")
            elif sig_name == "ABSORPTION":
                active_signals.append(f"ABS{direction}")

        # Add supporting signals if significant
        if ldr is not None and (ldr > 1.5 or ldr < 0.67):
            direction = "+" if ldr_raw > 50 else "-"
            active_signals.append(f"LDR{direction}")
        if obi_ratio is not None and (obi_ratio > 1.3 or obi_ratio < 0.77):
            direction = "+" if obi_raw > 50 else "-"
            active_signals.append(f"OBI{direction}")
        if cvd is not None and abs(cvd) >= cvd_threshold:
            direction = "+" if cvd > 0 else "-"
            active_signals.append(f"CVD{direction}")

        # Build details string
        mode_label = active_mode.replace("_", " ")
        if active_mode == "BASE":
            details = f"Mode: {mode_label} | LDR {ldr_raw:.0f} | OBI {obi_raw:.0f} | CVD {cvd_raw:.0f}"
        else:
            primary_label = {"DELTA_UNWIND": "DU", "EXHAUSTION": "EXH", "ABSORPTION": "ABS"}[active_mode]
            details = f"Mode: {mode_label} | {primary_label} {primary_raw:.0f} | LDR {ldr_raw:.0f} | OBI {obi_raw:.0f} | CVD {cvd_raw:.0f}"

        return OrderFlowAlphaScore(
            score=round(total_score, 1),
            active_mode=active_mode,
            primary_score=round(primary_contribution, 1),
            ldr_score=round(ldr_contribution, 1),
            obi_score=round(obi_contribution, 1),
            cvd_score=round(cvd_contribution, 1),
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
        cvd: Optional[float] = None,
        delta_unwind_signals: Optional[List[Dict]] = None,
        exhaustion_signals: Optional[List[Dict]] = None,
        timeframe: Optional[str] = None,
    ) -> AgentBiasResult:
        """Calculate total agent bias score (0-100)

        Calculation Order (ORDER FLOW FIRST):
        1. Order Flow Alpha (60%) - calculated first to get direction
        2. Trend & Structure (20%) - adjusted by orderflow alignment
        3. Market Intensity (20%) - amplified/dampened by agreement with orderflow

        Component Alignment:
        - Market Intensity: +20% if price agrees with orderflow, -20% if conflict
        - Trend & Structure: +15% if trend confirms orderflow, -15% if contradicts
        """
        # Determine price direction from recent bars
        if len(df) >= 5:
            recent = df.tail(5)
            price_change = recent["close"][-1] - recent["close"][0]
            price_direction = "UP" if price_change > 0 else "DOWN" if price_change < 0 else "NEUTRAL"
        else:
            price_direction = "NEUTRAL"

        # ============================================
        # 1. Calculate Order Flow Alpha FIRST
        # ============================================
        orderflow_alpha = self.calculate_orderflow_alpha_score(
            obi_ratio, ldr,
            absorption_signals or [],
            cvd=cvd,
            delta_unwind_signals=delta_unwind_signals or [],
            exhaustion_signals=exhaustion_signals or [],
        )

        # Derive orderflow direction from score
        # >55 = BULLISH, <45 = BEARISH, else NEUTRAL
        if orderflow_alpha.score > 55:
            orderflow_direction = "BULLISH"
        elif orderflow_alpha.score < 45:
            orderflow_direction = "BEARISH"
        else:
            orderflow_direction = "NEUTRAL"

        # ============================================
        # 2. Calculate Trend & Structure (with orderflow alignment)
        # ============================================
        trend_structure = self.calculate_trend_structure_score(
            df, sr_levels,
            orderflow_direction=orderflow_direction
        )

        # ============================================
        # 3. Calculate Market Intensity (with orderflow alignment)
        # ============================================
        market_intensity = self.calculate_market_intensity_score(
            rvol, vpin, price_direction,
            orderflow_direction=orderflow_direction
        )

        # Get weights (timeframe-specific if available)
        if timeframe:
            trend_w, intensity_w, orderflow_w = self.get_weights_for_timeframe(timeframe)
        else:
            trend_w = self.TREND_STRUCTURE_WEIGHT
            intensity_w = self.MARKET_INTENSITY_WEIGHT
            orderflow_w = self.ORDERFLOW_ALPHA_WEIGHT

        # Calculate weighted total
        total_score = (
            trend_structure.score * trend_w +
            market_intensity.score * intensity_w +
            orderflow_alpha.score * orderflow_w
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
            f"Trend/Structure ({trend_w*100:.0f}%): {trend_structure.score:.1f}\n"
            f"Market Intensity ({intensity_w*100:.0f}%): {market_intensity.score:.1f}\n"
            f"Order Flow ({orderflow_w*100:.0f}%): {orderflow_alpha.score:.1f}"
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
