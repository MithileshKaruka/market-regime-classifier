"""Chart endpoint v2 with working indicators"""
from fastapi import APIRouter, Query, Path, HTTPException
from typing import Optional
from pydantic import BaseModel
import polars as pl
from app.data.storage import DuckDBStorage
from app.features.indicators import TechnicalIndicators

router = APIRouter()


class ChartBar(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    regime: str
    rvwap_7: Optional[float] = None
    rvwap_30: Optional[float] = None
    rvwap_90: Optional[float] = None
    rvwap_200: Optional[float] = None
    ema_12: Optional[float] = None
    ema_25: Optional[float] = None
    ema_20: Optional[float] = None
    ema_50: Optional[float] = None
    ema_100: Optional[float] = None
    ema_200: Optional[float] = None


@router.get("/chart/{timeframe}")
async def get_chart_with_indicators(
    timeframe: str = Path(..., pattern="^(5M|15M|1H|4H|1D)$"),
    limit: int = Query(5000, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    indicators: str = Query("", description="Comma-separated: rvwap_7,rvwap_30,rvwap_90,rvwap_200,ema_20,ema_50,ema_100,ema_200")
):
    """Get chart data with indicators - V2 endpoint with pagination support

    Args:
        timeframe: Chart timeframe (5M, 15M, 1H, 4H, 1D)
        limit: Number of bars to return (default 5000, max 10000)
        offset: Number of bars to skip from most recent (for pagination)
        indicators: Comma-separated list of indicators to calculate

    Returns:
        JSON with bars array and total_count for pagination
    """

    with DuckDBStorage() as storage:
        # Get total count for pagination info
        # Filter out spurious low-volume bars (V=1 weekend/holiday ticks)
        count_result = storage.conn.execute(f"""
            SELECT COUNT(*) as total
            FROM ohlcv_ticks
            WHERE symbol = 'MNQ' AND timeframe = '{timeframe}'
              AND volume > 1
        """).fetchone()
        total_count = count_result[0] if count_result else 0

        # Get OHLCV data with pagination
        # Filter out spurious low-volume bars (V=1 weekend/holiday single ticks)
        df_ohlcv = storage.conn.execute(f"""
            SELECT timestamp, open, high, low, close, volume
            FROM ohlcv_ticks
            WHERE symbol = 'MNQ' AND timeframe = '{timeframe}'
              AND volume > 1
            ORDER BY timestamp DESC
            LIMIT {limit} OFFSET {offset}
        """).pl()

        # Get regimes
        df_regimes = storage.conn.execute(f"""
            SELECT timestamp, regime
            FROM regimes
            WHERE symbol = 'MNQ' AND timeframe = '{timeframe}'
            ORDER BY timestamp DESC
            LIMIT {limit}
        """).pl()

        if len(df_ohlcv) == 0:
            raise HTTPException(status_code=404, detail=f"No data for {timeframe}")

        # Calculate indicators if requested
        if indicators:
            requested = [ind.strip() for ind in indicators.split(",") if ind.strip()]

            if requested:
                # Reverse for chronological calculation
                df_ohlcv = df_ohlcv.reverse()

                # Parse indicators
                rvwap_periods = []
                ema_periods = []

                for ind in requested:
                    if ind.startswith("rvwap_"):
                        period = int(ind.split("_")[1])
                        rvwap_periods.append(period)
                    elif ind.startswith("ema_"):
                        period = int(ind.split("_")[1])
                        ema_periods.append(period)

                # Calculate RVWAPs
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

                # Calculate EMAs
                for period in ema_periods:
                    df_ohlcv = TechnicalIndicators.calculate_ema(df_ohlcv, period=period)

                # Reverse back
                df_ohlcv = df_ohlcv.reverse()

        # Merge with regimes
        df_merged = df_ohlcv.join(df_regimes, on="timestamp", how="left")

        # Build response
        bars = []
        for row in reversed(list(df_merged.iter_rows(named=True))):
            bars.append(ChartBar(
                time=int(row["timestamp"].timestamp()),
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=int(row["volume"]),
                regime=row.get("regime") or "NEUTRAL",
                rvwap_7=row.get("rvwap_7"),
                rvwap_30=row.get("rvwap_30"),
                rvwap_90=row.get("rvwap_90"),
                rvwap_200=row.get("rvwap_200"),
                ema_12=row.get("ema_12"),
                ema_25=row.get("ema_25"),
                ema_20=row.get("ema_20"),
                ema_50=row.get("ema_50"),
                ema_100=row.get("ema_100"),
                ema_200=row.get("ema_200"),
            ))

        return {
            "bars": bars,
            "total_count": total_count,
            "returned_count": len(bars),
            "offset": offset
        }
