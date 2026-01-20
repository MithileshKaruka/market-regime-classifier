"""Regime classification endpoints"""
from fastapi import APIRouter, Query, Path, HTTPException
from typing import Optional, List
from pydantic import BaseModel
from datetime import datetime
import polars as pl
from app.data.storage import DuckDBStorage
from app.features.support_resistance import SupportResistanceDetector
from app.features.indicators import TechnicalIndicators
from app.classifiers.sr_signals import SRSignalGenerator
from app.config import get_config

router = APIRouter()


class RegimeClassification(BaseModel):
    """Regime classification for a single timeframe"""
    timeframe: str
    regime: str  # BULLISH, BEARISH, NEUTRAL
    confidence: float
    key_signal: str
    dom_imbalance: Optional[float] = None
    delta: Optional[float] = None
    timestamp: datetime


class MultiTimeframeRegime(BaseModel):
    """Multi-timeframe regime classification"""
    timeframes: dict[str, RegimeClassification]
    overall_regime: str
    alignment_score: float
    reasoning: Optional[str] = None
    timestamp: datetime


@router.get("/current", response_model=MultiTimeframeRegime)
async def get_current_regime():
    """Get current regime classification across all timeframes"""
    with DuckDBStorage() as storage:
        df = storage.get_latest_regimes()

        if len(df) == 0:
            raise HTTPException(status_code=404, detail="No regime data available")

        # Convert DataFrame rows to RegimeClassification objects
        timeframes = {}
        for row in df.iter_rows(named=True):
            timeframes[row["timeframe"]] = RegimeClassification(
                timeframe=row["timeframe"],
                regime=row["regime"],
                confidence=row["confidence"],
                key_signal=row["key_signal"],
                dom_imbalance=row.get("dom_imbalance"),
                delta=row.get("delta"),
                timestamp=row["timestamp"]
            )

        # Calculate overall regime and alignment
        regimes_list = [tf.regime for tf in timeframes.values()]
        bullish_count = regimes_list.count("BULLISH")
        bearish_count = regimes_list.count("BEARISH")
        neutral_count = regimes_list.count("NEUTRAL")

        total = len(regimes_list)
        if bullish_count > bearish_count and bullish_count > neutral_count:
            overall_regime = "BULLISH"
        elif bearish_count > bullish_count and bearish_count > neutral_count:
            overall_regime = "BEARISH"
        else:
            overall_regime = "NEUTRAL"

        # Alignment score: how many timeframes agree with overall regime
        alignment_score = max(bullish_count, bearish_count, neutral_count) / total

        return MultiTimeframeRegime(
            timeframes=timeframes,
            overall_regime=overall_regime,
            alignment_score=alignment_score,
            reasoning=f"{bullish_count} bullish, {bearish_count} bearish, {neutral_count} neutral timeframes",
            timestamp=datetime.utcnow()
        )


## MOVED TO END OF FILE - wildcard routes must be last
# @router.get("/{timeframe}", response_model=RegimeClassification)
# async def get_regime_by_timeframe(
#     timeframe: str = Path(..., pattern="^(5M|15M|1H|4H|1D)$")
# ):
#     """Get regime classification for a specific timeframe"""
#     with DuckDBStorage() as storage:
#         df = storage.get_latest_regimes(timeframes=[timeframe])
#
#         if len(df) == 0:
#             raise HTTPException(status_code=404, detail=f"No regime data available for {timeframe}")
#
#         row = df.row(0, named=True)
#         return RegimeClassification(
#             timeframe=row["timeframe"],
#             regime=row["regime"],
#             confidence=row["confidence"],
#             key_signal=row["key_signal"],
#             dom_imbalance=row.get("dom_imbalance"),
#             delta=row.get("delta"),
#             timestamp=row["timestamp"]
#         )


class RegimeHistory(BaseModel):
    """Historical regime classifications"""
    timeframe: str
    history: List[RegimeClassification]


@router.get("/history/{timeframe}", response_model=RegimeHistory)
async def get_regime_history(
    timeframe: str = Path(..., pattern="^(5M|15M|1H|4H|1D)$"),
    limit: int = Query(100, ge=1, le=1000)
):
    """Get historical regime classifications for a timeframe"""
    with DuckDBStorage() as storage:
        df = storage.get_regime_history(timeframe=timeframe, limit=limit)

        history = []
        for row in df.iter_rows(named=True):
            history.append(RegimeClassification(
                timeframe=row["timeframe"],
                regime=row["regime"],
                confidence=row["confidence"],
                key_signal=row["key_signal"],
                dom_imbalance=row.get("dom_imbalance"),
                delta=row.get("delta"),
                timestamp=row["timestamp"]
            ))

        return RegimeHistory(
            timeframe=timeframe,
            history=history
        )


class ExplainRequest(BaseModel):
    """Request for LLM explanation"""
    include_context: bool = True


class ExplainResponse(BaseModel):
    """LLM explanation of current regime"""
    reasoning: str
    key_factors: List[str]
    timestamp: datetime


@router.post("/explain", response_model=ExplainResponse)
async def explain_regime(request: ExplainRequest):
    """Get LLM reasoning for current regime"""
    # TODO: Implement LangGraph synthesis agent call
    return ExplainResponse(
        reasoning="Bearish 5M/15M flow (DOM imbalance 0.82, delta -850) but 1D buyers defending value area. Likely pullback in uptrend. Watch for absorption at key levels.",
        key_factors=[
            "Short-term bearish pressure (5M, 15M)",
            "Higher timeframe bullish structure intact (4H, 1D)",
            "Price holding daily value area",
            "DOM imbalance showing selling pressure",
            "Potential pullback before continuation"
        ],
        timestamp=datetime.utcnow()
    )


@router.get("/alignment")
async def get_alignment():
    """Get cross-timeframe alignment analysis"""
    # TODO: Implement alignment calculation
    return {
        "alignment_score": 0.45,
        "aligned_timeframes": ["4H", "1D"],
        "divergent_timeframes": ["5M", "15M"],
        "neutral_timeframes": ["1H"],
        "consensus": "MIXED",
        "description": "Higher timeframes bullish, lower timeframes bearish"
    }


class ChartBar(BaseModel):
    """Single OHLCV bar with regime"""
    time: int  # Unix timestamp in seconds
    open: float
    high: float
    low: float
    close: float
    volume: int
    regime: str
    vwap: Optional[float] = None
    rvwap_7: Optional[float] = None
    rvwap_30: Optional[float] = None
    rvwap_90: Optional[float] = None
    rvwap_200: Optional[float] = None
    ema_20: Optional[float] = None
    ema_50: Optional[float] = None
    ema_100: Optional[float] = None
    ema_200: Optional[float] = None
    bb_upper: Optional[float] = None
    bb_middle: Optional[float] = None
    bb_lower: Optional[float] = None
    atr: Optional[float] = None


@router.get("/chart/{timeframe}")
async def get_chart_data(
    timeframe: str = Path(..., pattern="^(5M|15M|1H|4H|1D)$"),
    limit: int = Query(100, ge=1, le=1000),
    include_indicators: bool = Query(False, description="Include technical indicators"),
    indicators: Optional[str] = Query(None, description="Comma-separated list of indicators: vwap,rvwap_7,rvwap_30,rvwap_90,rvwap_200,ema_20,ema_50,ema_100,ema_200,bb,atr")
):
    """Get OHLCV chart data with regime classifications and indicators"""
    with DuckDBStorage() as storage:
        # Get order book data with OHLCV
        # Filter out invalid prices (MNQ typically trades 20000-30000 range)
        # Valid bars should have close price in reasonable range and low/high make sense
        df_ohlcv = storage.conn.execute(f"""
            SELECT
                timestamp,
                open,
                high,
                low,
                close,
                volume
            FROM order_book
            WHERE symbol = 'MNQ' AND timeframe = '{timeframe}'
            AND close BETWEEN 20000 AND 30000
            AND low BETWEEN 20000 AND 30000
            AND high BETWEEN 20000 AND 35000
            AND open BETWEEN 20000 AND 30000
            ORDER BY timestamp DESC
            LIMIT {limit}
        """).pl()

        # Get regime classifications
        df_regimes = storage.conn.execute(f"""
            SELECT timestamp, regime
            FROM regimes
            WHERE symbol = 'MNQ' AND timeframe = '{timeframe}'
            ORDER BY timestamp DESC
            LIMIT {limit}
        """).pl()

        if len(df_ohlcv) == 0:
            raise HTTPException(status_code=404, detail=f"No chart data available for {timeframe}")

        # Calculate indicators if requested
        if include_indicators or indicators:
            # Parse requested indicators
            requested_indicators = []
            if indicators:
                requested_indicators = [ind.strip() for ind in indicators.split(",")]

            # Reverse to chronological order for indicator calculation
            df_ohlcv = df_ohlcv.reverse()

            # Determine which RVWAP and EMA periods to calculate
            rvwap_periods = []
            ema_periods = []
            calc_bb = False
            calc_atr = False
            calc_vwap = False

            if requested_indicators:
                # Calculate only requested indicators
                if "vwap" in requested_indicators:
                    calc_vwap = True
                for ind in requested_indicators:
                    if ind.startswith("rvwap_"):
                        period = int(ind.split("_")[1])
                        if period not in rvwap_periods:
                            rvwap_periods.append(period)
                    elif ind.startswith("ema_"):
                        period = int(ind.split("_")[1])
                        if period not in ema_periods:
                            ema_periods.append(period)
                    elif ind == "bb":
                        calc_bb = True
                    elif ind == "atr":
                        calc_atr = True
            else:
                # Calculate all by default if include_indicators=True
                df_ohlcv = TechnicalIndicators.calculate_all_indicators(df_ohlcv)
                # Reverse back for merging
                df_ohlcv = df_ohlcv.reverse()
                # Skip the custom calculation below
                calc_vwap = False

            # Calculate selected indicators
            if requested_indicators and (calc_vwap or rvwap_periods or ema_periods or calc_bb or calc_atr):
                if calc_vwap:
                    df_ohlcv = TechnicalIndicators.calculate_vwap(df_ohlcv)

                # Calculate selected RVWAPs
                for period in rvwap_periods:
                    df_ohlcv = df_ohlcv.with_columns([
                        ((pl.col("high") + pl.col("low") + pl.col("close")) / 3).alias("typical_price")
                    ])
                    df_ohlcv = df_ohlcv.with_columns([
                        (
                            (pl.col("typical_price") * pl.col("volume")).rolling_sum(period) /
                            pl.col("volume").rolling_sum(period)
                        ).alias(f"rvwap_{period}")
                    ])
                if "typical_price" in df_ohlcv.columns:
                    df_ohlcv = df_ohlcv.drop("typical_price")

                # Calculate selected EMAs
                for period in ema_periods:
                    df_ohlcv = TechnicalIndicators.calculate_ema(df_ohlcv, period=period)

                # Calculate BB if requested
                if calc_bb:
                    df_ohlcv = TechnicalIndicators.calculate_bollinger_bands(df_ohlcv)

                # Calculate ATR if requested
                if calc_atr:
                    df_ohlcv = TechnicalIndicators.calculate_atr(df_ohlcv)

                # Reverse back for merging
                df_ohlcv = df_ohlcv.reverse()

        # Merge regime data
        df_merged = df_ohlcv.join(df_regimes, on="timestamp", how="left")

        # Convert to chart format (reverse order for chronological)
        bars = []
        for row in reversed(list(df_merged.iter_rows(named=True))):
            bar_data = {
                "time": int(row["timestamp"].timestamp()),
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": int(row["volume"]),
                "regime": row.get("regime", "NEUTRAL")
            }

            # Add indicators if available
            if include_indicators or indicators:
                bar_data.update({
                    "vwap": row.get("vwap"),
                    "rvwap_7": row.get("rvwap_7"),
                    "rvwap_30": row.get("rvwap_30"),
                    "rvwap_90": row.get("rvwap_90"),
                    "rvwap_200": row.get("rvwap_200"),
                    "ema_20": row.get("ema_20"),
                    "ema_50": row.get("ema_50"),
                    "ema_100": row.get("ema_100"),
                    "ema_200": row.get("ema_200"),
                    "bb_upper": row.get("bb_upper"),
                    "bb_middle": row.get("bb_middle"),
                    "bb_lower": row.get("bb_lower"),
                    "atr": row.get("atr"),
                })

            bars.append(ChartBar(**bar_data))

        return {"bars": bars}


class SupportResistanceLevel(BaseModel):
    """Support or resistance level"""
    price: float
    touches: int
    type: str  # "support", "resistance", or "volume_node"
    volume: Optional[int] = None


class SupportResistanceLevels(BaseModel):
    """Support and resistance levels for a timeframe"""
    timeframe: str
    support: List[SupportResistanceLevel]
    resistance: List[SupportResistanceLevel]
    volume_nodes: Optional[List[SupportResistanceLevel]] = None
    price_range: dict  # min/max price in the data


@router.get("/support-resistance/{timeframe}", response_model=SupportResistanceLevels)
async def get_support_resistance(
    timeframe: str = Path(..., pattern="^(5M|15M|1H|4H|1D)$"),
    min_touches: int = Query(3, ge=1, le=10, description="Minimum touches for a level"),
    include_volume: bool = Query(False, description="Include volume profile levels"),
    price_tolerance: float = Query(0.01, ge=0.001, le=0.02, description="Price clustering tolerance (1% = 0.01)"),
    max_levels: int = Query(50, ge=1, le=50, description="Maximum number of support/resistance levels to return"),
    price_range_pct: float = Query(None, ge=0, le=100, description="Analyze bars within ±N% of current price (default: auto per timeframe, 0 = all data)")
):
    """
    Get support and resistance levels for a timeframe

    Identifies key price levels using:
    - Swing highs and lows
    - Multiple touches (levels tested repeatedly)
    - Volume profile (high volume nodes)

    Uses price-based lookback: analyzes all historical bars where price
    was within ±price_range_pct of current price.
    """
    with DuckDBStorage() as storage:
        # First, get the current price (most recent close)
        current_price_df = storage.conn.execute(f"""
            SELECT close
            FROM order_book
            WHERE symbol = 'MNQ' AND timeframe = '{timeframe}'
            ORDER BY timestamp DESC
            LIMIT 1
        """).pl()

        if len(current_price_df) == 0:
            raise HTTPException(status_code=404, detail=f"No data available for {timeframe}")

        current_price = float(current_price_df["close"][0])

        # Auto-set price range based on timeframe if not specified (from config/env)
        config = get_config()
        if price_range_pct is None:
            price_range_pct = config.support_resistance.price_range_pct.get(timeframe, 15.0)

        print(f"[S/R] {timeframe}: current price={current_price:.2f}, price_range=±{price_range_pct}%")

        # Calculate price bounds
        price_min = current_price * (1 - price_range_pct / 100)
        price_max = current_price * (1 + price_range_pct / 100)

        # Fetch all bars where price was within the range
        # A bar is included if any part of it (high or low) touched the range
        df = storage.conn.execute(f"""
            SELECT
                timestamp,
                open,
                high,
                low,
                close,
                volume
            FROM order_book
            WHERE symbol = 'MNQ' AND timeframe = '{timeframe}'
              AND high >= {price_min} AND low <= {price_max}
            ORDER BY timestamp ASC
        """).pl()

        print(f"[S/R] Loaded {len(df)} bars, price range: {float(df['low'].min()):.2f} - {float(df['high'].max()):.2f}")

        if len(df) == 0:
            raise HTTPException(status_code=404, detail=f"No data available for {timeframe} within ±{price_range_pct}% of current price")

        # Data is already in chronological order (ORDER BY timestamp ASC)

        # Adjust min_touches based on timeframe for better level detection
        # Use 2 touches for all timeframes to catch more valid levels
        timeframe_min_touches = {
            '5M': 2,
            '15M': 2,
            '1H': 2,
            '4H': 2,
            '1D': 2,
        }
        adjusted_min_touches = timeframe_min_touches.get(timeframe, min_touches)

        # Use timeframe-specific swing windows
        # Smaller window = more sensitive, detects more swing points
        timeframe_swing_windows = {
            '5M': 3,   # Very sensitive for intraday
            '15M': 3,  # Sensitive for intraday
            '1H': 3,   # More sensitive to catch more levels
            '4H': 3,   # More sensitive to catch more levels
            '1D': 5,   # Standard for daily
        }
        swing_window = timeframe_swing_windows.get(timeframe, 5)

        print(f"[S/R] Using: min_touches={adjusted_min_touches}, swing_window={swing_window}, tolerance={price_tolerance:.4f}")

        # Initialize detector
        detector = SupportResistanceDetector(price_tolerance=price_tolerance)

        # Identify levels
        levels = detector.identify_levels(df, min_touches=adjusted_min_touches, swing_window=swing_window)

        # DEBUG: Log raw levels before filtering
        print(f"[S/R DEBUG] Raw support levels from detector: {len(levels.get('support', []))}")
        for level in sorted(levels.get('support', []), key=lambda x: x['price']):
            print(f"  Support: {level['price']:.2f} - {level['touches']} touches - last_seen: {level.get('last_seen', 'N/A')}")

        print(f"[S/R DEBUG] Raw resistance levels from detector: {len(levels.get('resistance', []))}")
        for level in sorted(levels.get('resistance', []), key=lambda x: x['price']):
            print(f"  Resistance: {level['price']:.2f} - {level['touches']} touches - last_seen: {level.get('last_seen', 'N/A')}")

        # Add volume profile if requested
        if include_volume:
            levels = detector.add_volume_profile_levels(df, levels)

        # Calculate price range from data
        price_range = {
            "min": float(df["low"].min()),
            "max": float(df["high"].max()),
        }

        # Combine all levels and reclassify based on current price
        # Levels below current price = support (green)
        # Levels above current price = resistance (red)
        all_levels = levels.get("support", []) + levels.get("resistance", [])

        # Sort by touches and take most significant
        all_sorted = sorted(all_levels, key=lambda x: x["touches"], reverse=True)[:max_levels * 2]

        # Reclassify based on current price
        support_levels = []
        resistance_levels = []

        for level in all_sorted:
            if level["price"] < current_price:
                support_levels.append(level)
            else:
                resistance_levels.append(level)

        print(f"[S/R DEBUG] After reclassification: {len(support_levels)} support, {len(resistance_levels)} resistance")
        print(f"[S/R DEBUG] Support levels before recency filter:")
        for level in sorted(support_levels, key=lambda x: x['price']):
            print(f"  {level['price']:.2f} - {level['touches']} touches")

        # Filter nearby levels by recency (keep most recent when levels are close)
        # Use timeframe-specific recency thresholds from config/env
        recency_threshold = config.support_resistance.recency_threshold_pct.get(timeframe, 1.5) / 100  # Convert to decimal

        def filter_by_recency(levels_list, min_distance_pct=recency_threshold):
            if not levels_list:
                return []

            # Sort by recency first (most recent first)
            sorted_by_recency = sorted(levels_list, key=lambda x: x.get("last_seen", 0), reverse=True)

            filtered = []
            for level in sorted_by_recency:
                # Check if this level is too close to any already accepted level
                too_close = False
                for existing in filtered:
                    distance_pct = abs(level["price"] - existing["price"]) / existing["price"]
                    if distance_pct < min_distance_pct:
                        too_close = True
                        break

                if not too_close:
                    filtered.append(level)

            # Re-sort by touches (most significant first) for final output
            return sorted(filtered, key=lambda x: x["touches"], reverse=True)

        print(f"[S/R] Recency filter threshold: {recency_threshold*100:.1f}%")

        # Apply recency filter
        support_filtered = filter_by_recency(support_levels)
        resistance_filtered = filter_by_recency(resistance_levels)

        print(f"[S/R DEBUG] After recency filter: {len(support_filtered)} support, {len(resistance_filtered)} resistance")
        print(f"[S/R DEBUG] Final support levels:")
        for level in sorted(support_filtered, key=lambda x: x['price']):
            print(f"  {level['price']:.2f} - {level['touches']} touches")

        # No additional price range filter needed - data was already filtered by price range in the query

        # Convert to response model
        support_levels = [
            SupportResistanceLevel(
                price=level["price"],
                touches=level["touches"],
                type="support"
            )
            for level in support_filtered[:max_levels]
        ]

        resistance_levels = [
            SupportResistanceLevel(
                price=level["price"],
                touches=level["touches"],
                type="resistance"
            )
            for level in resistance_filtered[:max_levels]
        ]

        print(f"[S/R] Current price: {current_price:.2f}")
        print(f"[S/R] Returning {len(support_levels)} support and {len(resistance_levels)} resistance levels")

        volume_nodes = None
        if include_volume and "volume_nodes" in levels:
            volume_nodes = [
                SupportResistanceLevel(
                    price=level["price"],
                    touches=0,
                    type="volume_node",
                    volume=int(level["volume"])
                )
                for level in levels["volume_nodes"]
            ]

        return SupportResistanceLevels(
            timeframe=timeframe,
            support=support_levels,
            resistance=resistance_levels,
            volume_nodes=volume_nodes,
            price_range=price_range
        )


class SRSignalResponse(BaseModel):
    """Buy/Sell signal at S/R level"""
    signal: str  # BUY, SELL, or NONE
    price: float
    level_type: str  # support or resistance
    confidence: float
    dom_score: float
    cvd_score: float
    reason: str
    timestamp: datetime


@router.get("/signals/{timeframe}")
async def get_sr_signals(
    timeframe: str = Path(..., pattern="^(5M|15M|1H|4H|1D)$"),
    lookback: int = Query(100, ge=20, le=500, description="Number of bars to analyze"),
    min_touches: int = Query(3, ge=1, le=10, description="Minimum touches for S/R level"),
):
    """
    Get buy/sell signals at S/R levels based on DOM and CVD

    Signal Logic:
    - At Support: BUY if DOM bullish + CVD bullish (50% each)
    - At Resistance: SELL if DOM bearish + CVD bearish (50% each)
    - Ignores VWAP for S/R signals
    """
    with DuckDBStorage() as storage:
        # Get OHLCV + order flow data
        df = storage.conn.execute(f"""
            SELECT
                timestamp,
                open,
                high,
                low,
                close,
                volume,
                dom_imbalance,
                cvd
            FROM order_book
            WHERE symbol = 'MNQ' AND timeframe = '{timeframe}'
            ORDER BY timestamp DESC
            LIMIT {lookback}
        """).pl()

        if len(df) == 0:
            raise HTTPException(status_code=404, detail=f"No data available for {timeframe}")

        # Reverse to chronological order
        df = df.reverse()

        # Get S/R levels
        config = get_config()
        detector = SupportResistanceDetector(price_tolerance=config.support_resistance.proximity_pct)
        levels = detector.identify_levels(df, min_touches=min_touches)

        # Extract price values from levels
        support_prices = [level["price"] for level in levels.get("support", [])]
        resistance_prices = [level["price"] for level in levels.get("resistance", [])]

        # Generate signals
        signal_gen = SRSignalGenerator(
            dom_threshold=config.support_resistance.signal_thresholds.dom_threshold,
            cvd_threshold=config.support_resistance.signal_thresholds.cvd_threshold,
            proximity_pct=config.support_resistance.proximity_pct
        )

        df_with_signals = signal_gen.scan_for_signals(df, support_prices, resistance_prices)

        # Get latest signals (only non-NONE signals)
        signals = []
        for row in df_with_signals.filter(pl.col("signal") != "NONE").iter_rows(named=True):
            signals.append(SRSignalResponse(
                signal=row["signal"],
                price=row["close"],
                level_type="support" if row["signal"] == "BUY" else "resistance",
                confidence=row["signal_confidence"],
                dom_score=0.0,  # Not stored in DF currently
                cvd_score=0.0,  # Not stored in DF currently
                reason=row["signal_reason"],
                timestamp=row["timestamp"]
            ))

        return {
            "timeframe": timeframe,
            "signals": signals,
            "support_levels": support_prices,
            "resistance_levels": resistance_prices,
        }


# Wildcard route - MUST be last or it will catch all other routes
@router.get("/{timeframe}", response_model=RegimeClassification)
async def get_regime_by_timeframe(
    timeframe: str = Path(..., pattern="^(5M|15M|1H|4H|1D)$")
):
    """Get regime classification for a specific timeframe"""
    with DuckDBStorage() as storage:
        df = storage.get_latest_regimes(timeframes=[timeframe])

        if len(df) == 0:
            raise HTTPException(status_code=404, detail=f"No regime data available for {timeframe}")

        row = df.row(0, named=True)
        return RegimeClassification(
            timeframe=row["timeframe"],
            regime=row["regime"],
            confidence=row["confidence"],
            key_signal=row["key_signal"],
            dom_imbalance=row.get("dom_imbalance"),
            delta=row.get("delta"),
            timestamp=row["timestamp"]
        )
