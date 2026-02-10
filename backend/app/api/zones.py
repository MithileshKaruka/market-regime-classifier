"""Supply/Demand Zones API endpoint"""
from fastapi import APIRouter, Path, Query, HTTPException
from typing import List, Optional, Tuple
from pydantic import BaseModel
from datetime import datetime
import polars as pl

from app.data.storage import DuckDBStorage
from app.features.zone_bias import ZoneBiasScorer, ZoneType, ActiveZone

router = APIRouter()


def check_zone_status(zone: ActiveZone, df: pl.DataFrame, break_pct: float = 0.002) -> Tuple[str, int]:
    """Check if a zone has been tested and whether it held or broke.

    Zone breaking rules:
    - Demand zone: broken when price CLOSES below zone low by break_pct
    - Supply zone: broken when price CLOSES above zone high by break_pct
    - Uses percentage-based threshold (default 0.2%) to filter noise

    Args:
        zone: The zone to check
        df: Price data DataFrame
        break_pct: Percentage threshold for zone break (default 0.2%)

    Returns:
        Tuple of (status, times_tested)
        status: "UNTESTED", "HELD", or "BROKEN"
    """
    rows = df.to_dicts()
    formed_idx = zone.formed_bar_idx

    # Break threshold: use percentage of zone boundary price
    # e.g., 0.2% of 25500 = 51 points - price must close 51+ points beyond
    if zone.zone_type == ZoneType.SUPPLY:
        break_threshold = zone.price_high * break_pct
    else:
        break_threshold = zone.price_low * break_pct

    # Scan all bars after zone formation
    times_tested = 0
    was_tested = False
    is_broken = False

    for i in range(formed_idx + 1, len(rows)):
        bar = rows[i]
        bar_low = bar["low"]
        bar_high = bar["high"]
        bar_close = bar["close"]

        # Check if price entered/touched the zone
        zone_touched = (bar_low <= zone.price_high and bar_high >= zone.price_low)

        if zone_touched:
            was_tested = True
            times_tested += 1

        # Check if zone is broken - by CLOSE beyond threshold
        if not is_broken:
            if zone.zone_type == ZoneType.DEMAND:
                # Demand broken if close is break_pct below zone low
                if bar_close < zone.price_low - break_threshold:
                    is_broken = True
            else:  # SUPPLY
                # Supply broken if close is break_pct above zone high
                if bar_close > zone.price_high + break_threshold:
                    is_broken = True

    if is_broken:
        return "BROKEN", times_tested
    elif was_tested:
        return "HELD", times_tested
    else:
        return "UNTESTED", 0


class ZoneResponse(BaseModel):
    """Single zone response"""
    zone_type: str  # "DEMAND" or "SUPPLY"
    price_low: float
    price_high: float
    formed_at: int  # Unix timestamp
    quality: float  # 0-100
    timeframe: str
    status: str = "UNTESTED"  # "UNTESTED", "HELD", or "BROKEN"
    times_tested: int = 0


class ZonesResponse(BaseModel):
    """Response for zones endpoint"""
    zones: List[ZoneResponse]
    demand_count: int
    supply_count: int
    timeframe: str


# Price range scales by timeframe - HTF zones can be further from current price
PRICE_RANGE_BY_TIMEFRAME = {
    "5M": 5.0,    # 5% range
    "15M": 10.0,  # 10% range
    "1H": 15.0,   # 15% range
    "4H": 15.0,   # 15% range (same as 1H - focus on relevant zones)
    "1D": 30.0,   # 30% range
}

# Chart bars filter by timeframe - ensures zones from longer historical context are included
# All intraday timeframes now cover ~200 days to ensure fair comparison
CHART_BARS_BY_TIMEFRAME = {
    "5M": 5000,   # ~17 days (limited by data/performance)
    "15M": 5000,  # ~52 days
    "1H": 4800,   # ~200 days (matches 4H coverage)
    "4H": 1200,   # ~200 days
    "1D": 500,    # ~500 days
}


@router.get("/zones/{timeframe}", response_model=ZonesResponse)
async def get_zones(
    timeframe: str = Path(..., pattern="^(5M|15M|1H|4H|1D)$"),
    symbol: str = Query("MNQ", description="Trading symbol"),
    limit: int = Query(50, ge=1, le=100, description="Max zones to return"),
    min_quality: float = Query(40, ge=0, le=100, description="Minimum quality score"),
    price_range_pct: Optional[float] = Query(None, ge=0.5, le=100.0, description="% range from current price (auto-scales by TF if not set)"),
    chart_bars: Optional[int] = Query(None, ge=100, le=10000, description="Number of bars for time filter (auto-scales by TF if not set)"),
):
    """Get active supply and demand zones for a timeframe.

    Returns zones detected using DBR/RBD pattern with N-bar trend analysis.
    Only returns zones that formed within the chart's visible range.

    Args:
        timeframe: Bar timeframe (5M, 15M, 1H, 4H, 1D)
        symbol: Trading symbol (default MNQ)
        limit: Maximum number of zones to return (default 50)
        min_quality: Minimum quality score filter (default 0)
        chart_bars: Number of bars shown in chart (zones outside this range are filtered)

    Returns:
        List of zones with their boundaries and quality scores
    """
    try:
        # Load data - get the MOST RECENT bars for detection (matching chart display)
        # Use subquery to get newest first, then re-order ASC for proper zone detection
        with DuckDBStorage() as storage:
            df = storage.conn.execute(f"""
                SELECT * FROM (
                    SELECT timestamp, open, high, low, close, volume,
                           dom_imbalance, cvd, instant_delta, trade_flow_ratio
                    FROM ohlcv_ticks
                    WHERE symbol = '{symbol}' AND timeframe = '{timeframe}'
                    ORDER BY timestamp DESC
                    LIMIT 10000
                ) ORDER BY timestamp ASC
            """).pl()

        if len(df) == 0:
            raise HTTPException(status_code=404, detail=f"No data for {timeframe}")

        # Get current price for filtering
        current_price = float(df["close"][-1])

        # Use timeframe-specific price range if not explicitly provided
        effective_range_pct = price_range_pct if price_range_pct is not None else PRICE_RANGE_BY_TIMEFRAME.get(timeframe, 15.0)
        price_range = current_price * (effective_range_pct / 100.0)
        price_min = current_price - price_range
        price_max = current_price + price_range

        # Detect zones
        zone_scorer = ZoneBiasScorer()
        zones = zone_scorer.detect_active_zones(df, timeframe, current_bar_idx=len(df) - 1)

        # Filter by time - only zones that formed within the chart's visible range
        # Use timeframe-specific default if not explicitly provided
        effective_chart_bars = chart_bars if chart_bars is not None else CHART_BARS_BY_TIMEFRAME.get(timeframe, 1000)
        min_bar_idx = max(0, len(df) - effective_chart_bars)
        zones = [z for z in zones if z.formed_bar_idx >= min_bar_idx]

        # Filter by quality
        if min_quality > 0:
            zones = [z for z in zones if z.base_quality >= min_quality]

        # Filter by price range - only zones within X% of current price
        zones = [z for z in zones if (z.price_low <= price_max and z.price_high >= price_min)]

        # Check zone status (UNTESTED, HELD, or BROKEN)
        # Include all zones - frontend can filter by status if needed
        all_zones = []
        for zone in zones:
            status, times_tested = check_zone_status(zone, df)
            all_zones.append((zone, status, times_tested))

        # Sort by quality (highest first) and limit
        all_zones = sorted(all_zones, key=lambda x: x[0].base_quality, reverse=True)[:limit]

        # Convert to response format
        zone_responses = []
        for zone, status, times_tested in all_zones:
            ts = zone.formed_at
            if hasattr(ts, "timestamp"):
                ts = int(ts.timestamp())
            elif isinstance(ts, datetime):
                ts = int(ts.timestamp())

            zone_responses.append(ZoneResponse(
                zone_type=zone.zone_type.value,
                price_low=zone.price_low,
                price_high=zone.price_high,
                formed_at=ts,
                quality=zone.base_quality,
                timeframe=timeframe,
                status=status,
                times_tested=times_tested,
            ))

        demand_count = sum(1 for z in zone_responses if z.zone_type == "DEMAND")
        supply_count = sum(1 for z in zone_responses if z.zone_type == "SUPPLY")

        return ZonesResponse(
            zones=zone_responses,
            demand_count=demand_count,
            supply_count=supply_count,
            timeframe=timeframe,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
