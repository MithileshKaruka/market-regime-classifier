"""Orderflow signals API endpoints"""
from fastapi import APIRouter, Query, Path, HTTPException
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime
import polars as pl
from app.data.storage import DuckDBStorage
from app.features.orderflow_signals import OrderflowSignalDetector, SignalType, SignalDirection
from app.features.orderflow_metrics import OrderflowMetricsCalculator, BiasStrength
from app.features.agent_bias import AgentBiasCalculator, AgentMode
from app.agent.graph import run_agent, TradeAction, PositionState
from config import get_config

router = APIRouter()


class OrderflowSignalResponse(BaseModel):
    """Single orderflow signal"""
    timestamp: int  # Unix timestamp
    signal_type: str  # Absorption, LSF, OB Imb
    direction: str  # BULLISH or BEARISH
    price: float
    strength: float  # 0.0 to 1.0
    details: str


class OrderflowSignalsResponse(BaseModel):
    """Response containing all orderflow signals"""
    timeframe: str
    signals: List[OrderflowSignalResponse]
    total_count: int


class DOMSummary(BaseModel):
    """DOM imbalance summary for a single timeframe"""
    timeframe: str
    dom_imbalance: float  # 0 to 1 (0.5 = balanced)
    direction: str  # BULLISH, BEARISH, NEUTRAL
    timestamp: datetime


class VWAPStatus(BaseModel):
    """Daily VWAP status"""
    vwap: float
    current_price: float
    position: str  # ABOVE or BELOW
    distance_pct: float  # % distance from VWAP


class SimplifiedMetrics(BaseModel):
    """Simplified metrics: DOM by timeframe + VWAP"""
    dom_by_timeframe: List[DOMSummary]
    daily_vwap: VWAPStatus
    timestamp: datetime


# Advanced Orderflow Metrics Response Models

class RVOLResponse(BaseModel):
    """Relative Volume metrics"""
    rvol: float  # Current volume / 20-period MA
    rvol_20ma: float  # 20-period volume moving average
    current_volume: int
    poc_price: float  # Point of Control price
    poc_distance_pct: float  # % distance from POC
    price_vs_poc: str  # ABOVE, BELOW, AT
    bias: str  # STRONG_BULLISH, BULLISH, NEUTRAL, BEARISH, STRONG_BEARISH
    conviction: str  # HIGH, MEDIUM, LOW
    details: str


class VPINResponse(BaseModel):
    """VPIN (Volume-Synchronized Probability of Informed Trading)"""
    vpin: float  # 0.0 to 1.0
    vpin_threshold: float
    is_elevated: bool
    toxicity_level: str  # LOW, MODERATE, HIGH, EXTREME
    recent_trend: str  # RISING, STABLE, FALLING
    details: str


class LDRResponse(BaseModel):
    """Liquidity Depth Ratio"""
    ldr: float  # Bid depth / Ask depth ratio
    total_bid_depth: float
    total_ask_depth: float
    bid_concentration: float  # 0-1, how tight bids are near BBO
    ask_concentration: float  # 0-1, how tight asks are near BBO
    support_wall: bool  # Strong bid wall detected
    resistance_wall: bool  # Strong ask wall detected
    bias: str
    details: str


class AdvancedMetricsResponse(BaseModel):
    """Complete advanced orderflow metrics dashboard"""
    timestamp: int
    timeframe: str
    rvol: Optional[RVOLResponse] = None
    vpin: Optional[VPINResponse] = None
    ldr: Optional[LDRResponse] = None
    overall_bias: str  # Combined bias from all metrics
    alert_level: str  # NORMAL, ELEVATED, HIGH_ALERT


@router.get("/signals/{timeframe}", response_model=OrderflowSignalsResponse)
async def get_orderflow_signals(
    timeframe: str = Path(..., pattern="^(5M|15M|1H|4H|1D)$"),
    limit: int = Query(500, ge=100, le=5000, description="Number of bars to analyze"),
    detect_absorption: bool = Query(True, description="Detect absorption signals"),
    detect_lsf: bool = Query(True, description="Detect liquidity sweep fade signals"),
    detect_obi: bool = Query(True, description="Detect order book imbalance signals"),
    detect_delta_unwind: bool = Query(True, description="Detect delta unwind signals"),
    detect_exhaustion: bool = Query(True, description="Detect exhaustion signals"),
):
    """
    Get orderflow signals for a timeframe

    Detects five types of signals:
    1. **Absorption**: Large volume hitting a level but price stays stable
    2. **LSF** (Liquidity Sweep Fade): Stop run followed by snap-back
    3. **OB Imb** (Order Book Imbalance): Strong weighted imbalance in order book
    4. **Delta Unwind**: Cumulative delta reached extreme and is now reversing
    5. **Exhaustion**: High volume with minimal price movement
    """
    import logging
    logger = logging.getLogger(__name__)

    try:
        with DuckDBStorage() as storage:
            # Query pre-aggregated OHLCV data from ohlcv_ticks table
            # This table is pre-computed from mbp_ticks with accurate DOM/delta data
            # Get most recent N bars, then order chronologically for signal detection
            # Filter out spurious low-volume bars
            df = storage.conn.execute(f"""
                SELECT * FROM (
                    SELECT
                        timestamp,
                        open,
                        high,
                        low,
                        close,
                        volume,
                        instant_delta,
                        dom_imbalance,
                        total_bid_depth,
                        total_ask_depth
                    FROM ohlcv_ticks
                    WHERE symbol = 'MNQ' AND timeframe = '{timeframe}'
                      AND volume > 100
                    ORDER BY timestamp DESC
                    LIMIT {limit}
                ) ORDER BY timestamp ASC
            """).pl()

            if len(df) == 0:
                return OrderflowSignalsResponse(
                    timeframe=timeframe,
                    signals=[],
                    total_count=0,
                )

            # Check if orderflow data is available (not all NULL)
            # If instant_delta is all NULL, orderflow signals won't work properly
            has_orderflow = df.select(pl.col("instant_delta").is_not_null().any()).item()
            if not has_orderflow:
                logger.warning(f"No orderflow data for {timeframe} - returning empty signals")
                return OrderflowSignalsResponse(
                    timeframe=timeframe,
                    signals=[],
                    total_count=0,
                )

            # Fill NULL orderflow values with 0 to prevent calculation errors
            df = df.with_columns([
                pl.col("instant_delta").fill_null(0).alias("instant_delta"),
                pl.col("dom_imbalance").fill_null(0.5).alias("dom_imbalance"),
                pl.col("total_bid_depth").fill_null(0).alias("total_bid_depth"),
                pl.col("total_ask_depth").fill_null(0).alias("total_ask_depth"),
            ])

            # Initialize detector with config values
            config = get_config()
            detector = OrderflowSignalDetector(
                timeframe=timeframe,
                absorption_volume_mult=config.orderflow_alpha.absorption_volume_mult,
                absorption_price_tol=config.orderflow_alpha.absorption_price_tol,
                absorption_dom_threshold=config.orderflow_alpha.absorption_dom_threshold,
                lsf_sweep_threshold_pct=config.orderflow_alpha.lsf_sweep_threshold_pct,
                lsf_snapback_pct=config.orderflow_alpha.lsf_snapback_pct,
                lsf_snapback_bars=config.orderflow_alpha.lsf_snapback_bars,
                obi_threshold=config.orderflow_alpha.obi_threshold,
                delta_zscore_threshold=config.orderflow_alpha.delta_zscore_threshold,
                delta_unwind_pct=config.orderflow_alpha.delta_unwind_pct,
                delta_unwind_bars=config.orderflow_alpha.delta_unwind_bars,
                exhaustion_volume_mult=config.orderflow_alpha.exhaustion_volume_mult,
                exhaustion_range_ratio_max=config.orderflow_alpha.exhaustion_range_ratio_max,
                lookback_bars=config.orderflow_alpha.absorption_lookback,
            )

            # Detect signals
            signals = detector.detect_all_signals(
                df,
                detect_absorption=detect_absorption,
                detect_lsf=detect_lsf,
                detect_obi=detect_obi,
                detect_delta_unwind=detect_delta_unwind,
                detect_exhaustion=detect_exhaustion,
            )

            # Convert to response format
            signal_responses = [
                OrderflowSignalResponse(
                    timestamp=sig.timestamp,
                    signal_type=sig.signal_type.value,
                    direction=sig.direction.value,
                    price=sig.price,
                    strength=sig.strength,
                    details=sig.details,
                )
                for sig in signals
            ]

            return OrderflowSignalsResponse(
                timeframe=timeframe,
                signals=signal_responses,
                total_count=len(signal_responses),
            )

    except Exception as e:
        # Log the error but return empty signals instead of 500 error
        logger.error(f"Error detecting orderflow signals for {timeframe}: {e}", exc_info=True)
        return OrderflowSignalsResponse(
            timeframe=timeframe,
            signals=[],
            total_count=0,
        )


@router.get("/metrics", response_model=SimplifiedMetrics)
async def get_simplified_metrics(
    lookback_bars: int = Query(1, ge=1, le=20, description="Number of bars to average for DOM imbalance"),
):
    """
    Get simplified orderflow metrics

    Returns:
    - DOM imbalance for each timeframe (5M, 15M, 1H, 4H, 1D)
    - Daily VWAP level with current price position (above/below)

    Args:
        lookback_bars: Number of recent bars to average for DOM imbalance (1-20, default 1)
    """
    with DuckDBStorage() as storage:
        timeframes = ['5M', '15M', '1H', '4H', '1D']
        dom_summaries = []

        for tf in timeframes:
            # Get latest N bars for DOM imbalance averaging
            # Filter out spurious low-volume bars (volume > 100)
            df = storage.conn.execute(f"""
                SELECT timestamp, dom_imbalance
                FROM ohlcv_ticks
                WHERE symbol = 'MNQ' AND timeframe = '{tf}'
                  AND volume > 100
                  AND dom_imbalance IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT {lookback_bars}
            """).pl()

            if len(df) > 0:
                # Average DOM imbalance over the lookback period
                dom_values = [
                    row["dom_imbalance"]
                    for row in df.to_dicts()
                    if row["dom_imbalance"] is not None
                       and not (isinstance(row["dom_imbalance"], float) and row["dom_imbalance"] != row["dom_imbalance"])
                ]

                if not dom_values:
                    continue

                dom = sum(dom_values) / len(dom_values)
                latest_ts = df.row(0, named=True)["timestamp"]

                # Classify direction using config threshold
                config = get_config()
                dom_threshold = config.regime.thresholds.dom_threshold
                if dom > dom_threshold:
                    direction = "BULLISH"
                elif dom < (1 - dom_threshold):
                    direction = "BEARISH"
                else:
                    direction = "NEUTRAL"

                dom_summaries.append(DOMSummary(
                    timeframe=tf,
                    dom_imbalance=dom,
                    direction=direction,
                    timestamp=latest_ts,
                ))

        # Calculate daily VWAP from intraday bars (15M for precision)
        # Use today's bars only for true daily VWAP
        # Filter out spurious low-volume bars
        # Use 5M timeframe for current_price (most granular, most up-to-date)
        vwap_df = storage.conn.execute("""
            SELECT
                SUM((high + low + close) / 3 * volume) / SUM(volume) as vwap,
                (SELECT close FROM ohlcv_ticks WHERE symbol = 'MNQ' AND timeframe = '5M' AND volume > 100 ORDER BY timestamp DESC LIMIT 1) as current_price
            FROM ohlcv_ticks
            WHERE symbol = 'MNQ' AND timeframe = '15M' AND volume > 100
            AND DATE(timestamp) = (SELECT DATE(MAX(timestamp)) FROM ohlcv_ticks WHERE symbol = 'MNQ' AND timeframe = '15M' AND volume > 100)
        """).pl()

        if len(vwap_df) == 0 or vwap_df["vwap"][0] is None:
            # Fall back to 1H data if no 15M
            vwap_df = storage.conn.execute("""
                SELECT
                    SUM((high + low + close) / 3 * volume) / SUM(volume) as vwap,
                    (SELECT close FROM ohlcv_ticks WHERE symbol = 'MNQ' AND timeframe = '5M' AND volume > 100 ORDER BY timestamp DESC LIMIT 1) as current_price
                FROM ohlcv_ticks
                WHERE symbol = 'MNQ' AND timeframe = '1H' AND volume > 100
                AND DATE(timestamp) = (SELECT DATE(MAX(timestamp)) FROM ohlcv_ticks WHERE symbol = 'MNQ' AND timeframe = '1H' AND volume > 100)
            """).pl()

        if len(vwap_df) > 0 and vwap_df["vwap"][0] is not None:
            vwap = vwap_df["vwap"][0]
            price = vwap_df["current_price"][0]

            if vwap and vwap > 0:
                position = "ABOVE" if price > vwap else "BELOW"
                distance_pct = abs(price - vwap) / vwap * 100
            else:
                vwap = price
                position = "AT"
                distance_pct = 0.0

            vwap_status = VWAPStatus(
                vwap=vwap,
                current_price=price,
                position=position,
                distance_pct=distance_pct,
            )
        else:
            # No data available
            vwap_status = VWAPStatus(
                vwap=0.0,
                current_price=0.0,
                position="UNKNOWN",
                distance_pct=0.0,
            )

        return SimplifiedMetrics(
            dom_by_timeframe=dom_summaries,
            daily_vwap=vwap_status,
            timestamp=datetime.utcnow(),
        )


@router.get("/advanced/{timeframe}", response_model=AdvancedMetricsResponse)
async def get_advanced_metrics(
    timeframe: str = Path(..., pattern="^(5M|15M|1H|4H|1D)$"),
    limit: int = Query(200, ge=50, le=1000, description="Number of bars for calculation"),
):
    """
    Get advanced orderflow metrics for a timeframe

    Returns:
    - **RVOL**: Relative Volume vs 20-period MA with POC (Point of Control) context
    - **VPIN**: Volume-Synchronized Probability of Informed Trading (institutional flow detector)
    - **LDR**: Liquidity Depth Ratio from order book (support/resistance wall detection)
    - **Overall Bias**: Combined signal from all metrics
    - **Alert Level**: NORMAL, ELEVATED, or HIGH_ALERT

    Interpretation:
    - RVOL > 1.5 with price above POC = strong bullish conviction
    - VPIN > 0.7 = high probability of informed (institutional) trading
    - LDR > 2.5 = support wall (bullish even if price falling)
    - LDR < 0.4 = resistance wall (bearish even if price rising)
    """
    with DuckDBStorage() as storage:
        # Get OHLCV + orderflow data (most recent N bars, ordered chronologically)
        # Filter out spurious low-volume bars
        df = storage.conn.execute(f"""
            SELECT * FROM (
                SELECT
                    timestamp,
                    open,
                    high,
                    low,
                    close,
                    volume,
                    dom_imbalance,
                    cvd as instant_delta
                FROM ohlcv_ticks
                WHERE symbol = 'MNQ' AND timeframe = '{timeframe}'
                  AND volume > 100
                ORDER BY timestamp DESC
                LIMIT {limit}
            ) ORDER BY timestamp ASC
        """).pl()

        if len(df) == 0:
            raise HTTPException(status_code=404, detail=f"No data for {timeframe}")

        # Add estimated depth from DOM imbalance
        df = df.with_columns([
            (pl.col("volume") * pl.col("dom_imbalance")).alias("total_bid_depth"),
            (pl.col("volume") * (1 - pl.col("dom_imbalance"))).alias("total_ask_depth"),
        ])

        # Calculate advanced metrics using config values
        config = get_config()
        calculator = OrderflowMetricsCalculator(
            rvol_lookback=config.market_intensity.rvol_lookback,
            rvol_high_threshold=config.market_intensity.rvol_high,
            vpin_num_buckets=config.market_intensity.vpin_num_buckets,
            vpin_alert_threshold=config.market_intensity.vpin_alert,
            ldr_wall_threshold=config.orderflow_alpha.ldr_wall_threshold,
            poc_lookback=config.market_intensity.poc_lookback,
        )

        dashboard = calculator.calculate_all_metrics(df)

        # Convert to response
        rvol_response = None
        if dashboard.rvol:
            rvol_response = RVOLResponse(
                rvol=dashboard.rvol.rvol,
                rvol_20ma=dashboard.rvol.rvol_20ma,
                current_volume=dashboard.rvol.current_volume,
                poc_price=dashboard.rvol.poc_price,
                poc_distance_pct=dashboard.rvol.poc_distance_pct,
                price_vs_poc=dashboard.rvol.price_vs_poc,
                bias=dashboard.rvol.bias.value,
                conviction=dashboard.rvol.conviction,
                details=dashboard.rvol.details,
            )

        vpin_response = None
        if dashboard.vpin:
            vpin_response = VPINResponse(
                vpin=dashboard.vpin.vpin,
                vpin_threshold=dashboard.vpin.vpin_threshold,
                is_elevated=dashboard.vpin.is_elevated,
                toxicity_level=dashboard.vpin.toxicity_level,
                recent_trend=dashboard.vpin.recent_trend,
                details=dashboard.vpin.details,
            )

        ldr_response = None
        if dashboard.ldr:
            ldr_response = LDRResponse(
                ldr=dashboard.ldr.ldr,
                total_bid_depth=dashboard.ldr.total_bid_depth,
                total_ask_depth=dashboard.ldr.total_ask_depth,
                bid_concentration=dashboard.ldr.bid_concentration,
                ask_concentration=dashboard.ldr.ask_concentration,
                support_wall=dashboard.ldr.support_wall,
                resistance_wall=dashboard.ldr.resistance_wall,
                bias=dashboard.ldr.bias.value,
                details=dashboard.ldr.details,
            )

        return AdvancedMetricsResponse(
            timestamp=dashboard.timestamp,
            timeframe=timeframe,
            rvol=rvol_response,
            vpin=vpin_response,
            ldr=ldr_response,
            overall_bias=dashboard.overall_bias.value,
            alert_level=dashboard.alert_level,
        )


# Agent Bias Response Models

class TrendStructureResponse(BaseModel):
    """Trend & Structure component (20% weight)"""
    score: float
    ema_trend: str
    market_structure: str
    price_vs_sr: str
    details: str


class MarketIntensityResponse(BaseModel):
    """Market Intensity component (30% weight)"""
    score: float
    rvol: float
    rvol_contribution: float
    vpin: float
    vpin_contribution: float
    is_high_conviction: bool
    details: str


class OrderFlowAlphaResponse(BaseModel):
    """Order Flow Alpha component (60% weight) - Context-aware scoring

    When PRIMARY SIGNAL is active (Absorption/Exhaustion/Delta Unwind):
    - Primary: 50%, LDR: 20%, OBI: 15%, CVD: 15%

    When NO PRIMARY SIGNAL (BASE mode):
    - LDR: 33%, OBI: 33%, CVD: 34%
    """
    score: float
    active_mode: str  # ABSORPTION, EXHAUSTION, DELTA_UNWIND, or BASE
    primary_score: float  # Primary signal contribution
    ldr_score: float
    obi_score: float
    cvd_score: float
    active_signals: List[str]  # All active signals in strength order
    details: str


class AgentBiasResponse(BaseModel):
    """Complete agent bias assessment"""
    timestamp: int
    timeframe: str
    total_score: float  # 0-100
    mode: str  # HIGH_BEARISH, WEAK_BEARISH, NEUTRAL, WEAK_BULLISH, HIGH_BULLISH
    recommendation: str
    confidence: str  # HIGH, MEDIUM, LOW
    trend_structure: TrendStructureResponse
    market_intensity: MarketIntensityResponse
    orderflow_alpha: OrderFlowAlphaResponse
    details: str


@router.get("/agent-bias/{timeframe}", response_model=AgentBiasResponse)
async def get_agent_bias(
    timeframe: str = Path(..., pattern="^(5M|15M|1H|4H|1D)$"),
    limit: int = Query(200, ge=50, le=1000, description="Number of bars for calculation"),
):
    """
    Get unified agent bias score for trading decisions

    Returns a 0-100 score combining:
    - **Trend & Structure (20%)**: EMA 12/25 trend + market structure (HH/HL vs LH/LL) + S/R position
    - **Market Intensity (20%)**: RVOL + VPIN - measures conviction behind moves
    - **Order Flow Alpha (60%)**: Context-aware scoring based on active primary signals

    Order Flow Alpha Modes:
    - **DELTA_UNWIND**: Primary 50% + LDR 20% + OBI 15% + CVD 15% (86.7% hit rate)
    - **EXHAUSTION**: Primary 50% + LDR 20% + OBI 15% + CVD 15% (81.8% hit rate)
    - **ABSORPTION**: Primary 50% + LDR 20% + OBI 15% + CVD 15% (66.7% hit rate)
    - **BASE**: LDR 33% + OBI 33% + CVD 34% (no primary signal active)

    Score Interpretation:
    - **0-30 (HIGH_BEARISH)**: Short entries only, ignore support bounces
    - **30-45 (WEAK_BEARISH)**: Exit longs, don't enter shorts yet
    - **45-55 (NEUTRAL)**: Wait mode, avoid trading (chop zone)
    - **55-70 (WEAK_BULLISH)**: Cautious longs at proven S/R only
    - **70-100 (HIGH_BULLISH)**: Aggressive mode, buy breakouts
    """
    with DuckDBStorage() as storage:
        # Get OHLCV data with indicators (most recent N bars, ordered chronologically)
        # Filter out spurious low-volume bars
        df = storage.conn.execute(f"""
            SELECT * FROM (
                SELECT
                    timestamp,
                    open,
                    high,
                    low,
                    close,
                    volume,
                    dom_imbalance,
                    cvd as instant_delta
                FROM ohlcv_ticks
                WHERE symbol = 'MNQ' AND timeframe = '{timeframe}'
                  AND volume > 100
                ORDER BY timestamp DESC
                LIMIT {limit}
            ) ORDER BY timestamp ASC
        """).pl()

        if len(df) == 0:
            raise HTTPException(status_code=404, detail=f"No data for {timeframe}")

        # Add depth estimates
        df = df.with_columns([
            (pl.col("volume") * pl.col("dom_imbalance")).alias("total_bid_depth"),
            (pl.col("volume") * (1 - pl.col("dom_imbalance"))).alias("total_ask_depth"),
        ])

        # Calculate advanced metrics for RVOL, VPIN, LDR
        metrics_calc = OrderflowMetricsCalculator()
        rvol_metrics = metrics_calc.calculate_rvol(df)
        vpin_metrics = metrics_calc.calculate_vpin(df)
        ldr_metrics = metrics_calc.calculate_ldr(df)

        # Get orderflow signals using config
        config = get_config()
        detector = OrderflowSignalDetector(
            timeframe=timeframe,
            absorption_volume_mult=config.orderflow_alpha.absorption_volume_mult,
            lsf_sweep_threshold_pct=config.orderflow_alpha.lsf_sweep_threshold_pct,
            lsf_snapback_pct=config.orderflow_alpha.lsf_snapback_pct,
            obi_threshold=config.orderflow_alpha.obi_threshold,
            delta_zscore_threshold=config.orderflow_alpha.delta_zscore_threshold,
            delta_unwind_pct=config.orderflow_alpha.delta_unwind_pct,
            delta_unwind_bars=config.orderflow_alpha.delta_unwind_bars,
            exhaustion_volume_mult=config.orderflow_alpha.exhaustion_volume_mult,
            exhaustion_range_ratio_max=config.orderflow_alpha.exhaustion_range_ratio_max,
            lookback_bars=config.orderflow_alpha.absorption_lookback,
        )

        # Detect signals on full df (rolling calculations need history), then filter to recent
        # This matches the improved backtest approach that showed better signal accuracy
        all_absorption = detector.detect_absorption(df)
        all_delta_unwind = detector.detect_delta_unwind(df)
        all_exhaustion = detector.detect_exhaustion(df)

        # Filter to only signals from recent N bars
        signal_window_bars = 20
        recent_cutoff_ts = df.tail(signal_window_bars)["timestamp"].min()

        def filter_recent(signals):
            """Keep only signals from recent window"""
            recent = []
            for s in signals:
                sig_ts = s.timestamp
                cutoff = recent_cutoff_ts
                if hasattr(sig_ts, "timestamp"):
                    sig_ts = sig_ts.timestamp()
                if hasattr(cutoff, "timestamp"):
                    cutoff = cutoff.timestamp()
                if sig_ts >= cutoff:
                    recent.append({"direction": s.direction.value, "strength": s.strength})
            return recent

        abs_dicts = filter_recent(all_absorption)
        du_dicts = filter_recent(all_delta_unwind)
        exh_dicts = filter_recent(all_exhaustion)

        # Get S/R levels
        sr_levels = None
        try:
            sr_df = storage.conn.execute(f"""
                SELECT price, type, touches
                FROM support_resistance
                WHERE symbol = 'MNQ' AND timeframe = '{timeframe}'
                ORDER BY touches DESC
                LIMIT 10
            """).pl()
            if len(sr_df) > 0:
                sr_levels = sr_df.to_dicts()
        except Exception:
            pass  # S/R table might not exist

        # Calculate agent bias with timeframe-specific weights
        bias_calc = AgentBiasCalculator()
        bias_result = bias_calc.calculate_total_bias(
            df=df,
            sr_levels=sr_levels,
            rvol=rvol_metrics.rvol if rvol_metrics else None,
            vpin=vpin_metrics.vpin if vpin_metrics else None,
            obi_ratio=ldr_metrics.ldr if ldr_metrics else None,  # Using LDR as OBI proxy
            ldr=ldr_metrics.ldr if ldr_metrics else None,
            absorption_signals=abs_dicts,
            delta_unwind_signals=du_dicts,
            exhaustion_signals=exh_dicts,
            timeframe=timeframe,  # Use timeframe-specific weights
        )

        return AgentBiasResponse(
            timestamp=int(datetime.utcnow().timestamp()),
            timeframe=timeframe,
            total_score=bias_result.total_score,
            mode=bias_result.mode.value,
            recommendation=bias_result.recommendation,
            confidence=bias_result.confidence,
            trend_structure=TrendStructureResponse(
                score=bias_result.trend_structure.score,
                ema_trend=bias_result.trend_structure.ema_trend.value,
                market_structure=bias_result.trend_structure.market_structure.value,
                price_vs_sr=bias_result.trend_structure.price_vs_sr,
                details=bias_result.trend_structure.details,
            ),
            market_intensity=MarketIntensityResponse(
                score=bias_result.market_intensity.score,
                rvol=bias_result.market_intensity.rvol,
                rvol_contribution=bias_result.market_intensity.rvol_contribution,
                vpin=bias_result.market_intensity.vpin,
                vpin_contribution=bias_result.market_intensity.vpin_contribution,
                is_high_conviction=bias_result.market_intensity.is_high_conviction,
                details=bias_result.market_intensity.details,
            ),
            orderflow_alpha=OrderFlowAlphaResponse(
                score=bias_result.orderflow_alpha.score,
                active_mode=bias_result.orderflow_alpha.active_mode,
                primary_score=bias_result.orderflow_alpha.primary_score,
                ldr_score=bias_result.orderflow_alpha.ldr_score,
                obi_score=bias_result.orderflow_alpha.obi_score,
                cvd_score=bias_result.orderflow_alpha.cvd_score,
                active_signals=bias_result.orderflow_alpha.active_signals,
                details=bias_result.orderflow_alpha.details,
            ),
            details=bias_result.details,
        )


# Agent Decision Response Models

class AgentDecisionResponse(BaseModel):
    """Response from the trading agent"""
    timestamp: int
    timeframe: str
    symbol: str
    current_price: float

    # Bias assessment
    bias_score: float
    agent_mode: str
    confidence: str

    # Component scores
    trend_score: float
    intensity_score: float
    orderflow_score: float

    # Zone info (when price is near/inside a S/D zone)
    zone_bias: Optional[float] = None
    zone_type: Optional[str] = None  # "DEMAND" or "SUPPLY"
    zone_quality: Optional[float] = None
    zone_confirmed: Optional[bool] = None
    zone_distance_pct: Optional[float] = None  # 0 = inside zone, >0 = outside

    # Position info
    position: str
    entry_price: Optional[float] = None

    # Decision
    action: str
    action_reason: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None

    # Execution info
    iterations: int
    messages: List[str]


@router.get("/agent/{timeframe}", response_model=AgentDecisionResponse)
async def run_trading_agent(
    timeframe: str = Path(..., pattern="^(5M|15M|1H|4H|1D)$"),
    position: str = Query("FLAT", pattern="^(FLAT|LONG|SHORT)$", description="Current position"),
    entry_price: Optional[float] = Query(None, description="Entry price if in position"),
):
    """
    Run the LangGraph trading agent

    The agent follows a state machine:
    1. **Observe**: Fetch current market data (OHLCV, orderflow metrics)
    2. **Evaluate**: Calculate bias score (0-100) from Trend/Intensity/Orderflow
    3. **Decide**: Make trading decision based on score and current position

    Decision Matrix:
    - **0-30 (HIGH_BEARISH)**: Short only, exit longs immediately
    - **30-45 (WEAK_BEARISH)**: Exit longs, wait for clarity
    - **45-55 (NEUTRAL)**: Wait mode, avoid new positions
    - **55-70 (WEAK_BULLISH)**: Cautious longs at S/R only
    - **70-100 (HIGH_BULLISH)**: Aggressive longs, add to winners

    Possible Actions:
    - WAIT: No action, continue monitoring
    - ENTER_LONG / ENTER_SHORT: Open new position
    - EXIT_LONG / EXIT_SHORT: Close position
    - ADD_TO_LONG / ADD_TO_SHORT: Scale into position
    """
    try:
        # Run the agent
        result = await run_agent(
            timeframe=timeframe,
            symbol="MNQ",
            current_position=position,
            entry_price=entry_price,
        )

        # Extract message contents
        messages = []
        for msg in result.get("messages", []):
            if isinstance(msg, dict):
                messages.append(msg.get("content", ""))
            elif hasattr(msg, "content"):
                messages.append(msg.content)

        return AgentDecisionResponse(
            timestamp=result.get("timestamp", int(datetime.utcnow().timestamp())),
            timeframe=timeframe,
            symbol=result.get("symbol", "MNQ"),
            current_price=result.get("current_price", 0),
            bias_score=result.get("bias_score", 50),
            agent_mode=result.get("agent_mode", "NEUTRAL"),
            confidence=result.get("confidence", "LOW"),
            trend_score=result.get("trend_score", 50),
            intensity_score=result.get("intensity_score", 50),
            orderflow_score=result.get("orderflow_score", 50),
            zone_bias=result.get("zone_bias"),
            zone_type=result.get("zone_type"),
            zone_quality=result.get("zone_quality"),
            zone_confirmed=result.get("zone_confirmed"),
            zone_distance_pct=result.get("zone_distance_pct"),
            position=result.get("position", "FLAT"),
            entry_price=result.get("entry_price"),
            action=result.get("action", "WAIT"),
            action_reason=result.get("action_reason", ""),
            stop_loss=result.get("stop_loss"),
            take_profit=result.get("take_profit"),
            iterations=result.get("iteration", 0),
            messages=messages,
        )
    except Exception as e:
        # Return a safe fallback response on any error
        import logging
        logging.getLogger(__name__).error(f"Agent endpoint error: {e}", exc_info=True)
        return AgentDecisionResponse(
            timestamp=int(datetime.utcnow().timestamp()),
            timeframe=timeframe,
            symbol="MNQ",
            current_price=0,
            bias_score=50,
            agent_mode="NEUTRAL",
            confidence="LOW",
            trend_score=50,
            intensity_score=50,
            orderflow_score=50,
            position=position,
            entry_price=entry_price,
            action="WAIT",
            action_reason=f"Service temporarily unavailable: {str(e)}",
            stop_loss=None,
            take_profit=None,
            iterations=0,
            messages=[f"Error: {str(e)}"],
        )
