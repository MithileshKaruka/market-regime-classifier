"""Key Levels API endpoint"""
from fastapi import APIRouter, Query, HTTPException
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from app.data.storage import DuckDBStorage
from app.features.key_levels import KeyLevelsCalculator

router = APIRouter()


class KeyLevelResponse(BaseModel):
    """Single key level response"""
    name: str
    short_name: str
    price: float
    timestamp: int  # Unix timestamp
    color: str


class KeyLevelsResponse(BaseModel):
    """Response for key levels endpoint"""
    levels: List[KeyLevelResponse]
    symbol: str
    generated_at: int  # Unix timestamp


@router.get("/key-levels", response_model=KeyLevelsResponse)
async def get_key_levels(
    symbol: str = Query("MNQ", description="Trading symbol"),
    timeframe: str = Query("1H", description="Timeframe for data lookup (1H recommended for accuracy)"),
):
    """Get key reference price levels.

    Returns:
    - Monthly Open (MO)
    - Weekly Open (WO)
    - Yearly Open (YO)
    - Monday High (MDAY-H)
    - Monday Low (MDAY-L)
    - Previous Week High (PWH)
    - Previous Week Low (PWL)

    All levels use CME session boundaries (23:00 UTC = 18:00 ET).
    """
    try:
        with DuckDBStorage() as storage:
            # Get sufficient historical data (1 year + buffer)
            df = storage.conn.execute(f"""
                SELECT timestamp, open, high, low, close
                FROM ohlcv_ticks
                WHERE symbol = '{symbol}' AND timeframe = '{timeframe}'
                ORDER BY timestamp ASC
            """).pl()

        if len(df) == 0:
            raise HTTPException(status_code=404, detail=f"No data for {symbol}")

        # Calculate key levels
        calculator = KeyLevelsCalculator()
        levels = calculator.calculate(df)
        levels_dict = calculator.to_dict(levels)

        # Convert to response format
        level_responses = []
        for key, level_data in levels_dict.items():
            level_responses.append(KeyLevelResponse(
                name=level_data["name"],
                short_name=level_data["short_name"],
                price=level_data["price"],
                timestamp=level_data["timestamp"],
                color=level_data["color"],
            ))

        # Sort by price descending for display
        level_responses.sort(key=lambda x: x.price, reverse=True)

        return KeyLevelsResponse(
            levels=level_responses,
            symbol=symbol,
            generated_at=int(datetime.utcnow().timestamp()),
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
