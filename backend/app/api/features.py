"""Order flow features endpoints"""
from fastapi import APIRouter, Query, Path, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from app.data.storage import DuckDBStorage
from config import get_config

router = APIRouter()


class OrderFlowMetrics(BaseModel):
    """Real-time order flow metrics"""
    dom_imbalance: float
    delta: float
    vwap: float
    price: float
    liquidity_description: str
    timestamp: datetime


@router.get("/{timeframe}", response_model=OrderFlowMetrics)
async def get_features(
    timeframe: str = Path(..., pattern="^(5M|15M|1H|4H|1D)$")
):
    """Get order flow features for a specific timeframe"""
    with DuckDBStorage() as storage:
        df = storage.get_order_flow_metrics(timeframe=timeframe, limit=1)

        if len(df) == 0:
            raise HTTPException(status_code=404, detail=f"No order flow data available for {timeframe}")

        row = df.row(0, named=True)

        # Determine liquidity description based on DOM imbalance
        config = get_config()
        dom_threshold = config.regime.thresholds.dom_threshold
        dom = row.get("dom_imbalance", 0.5)
        if dom > dom_threshold:
            liquidity_desc = "Heavy Bid"
        elif dom < (1 - dom_threshold):
            liquidity_desc = "Heavy Ask"
        else:
            liquidity_desc = "Balanced"

        return OrderFlowMetrics(
            dom_imbalance=row.get("dom_imbalance", 0.0),
            delta=row.get("delta", 0.0),
            vwap=row.get("vwap", 0.0),
            price=row.get("mid_price", 0.0),
            liquidity_description=liquidity_desc,
            timestamp=row["timestamp"]
        )


class OrderBookSnapshot(BaseModel):
    """Order book snapshot (10 levels)"""
    bids: list[tuple[float, int, int]]  # [(price, size, count), ...]
    asks: list[tuple[float, int, int]]
    timestamp: datetime


@router.get("/orderbook/snapshot", response_model=OrderBookSnapshot)
async def get_orderbook_snapshot():
    """Get current order book snapshot (MBP-10)"""
    # TODO: Implement actual order book data
    # Placeholder data
    return OrderBookSnapshot(
        bids=[
            (20125.0, 150, 12),
            (20124.0, 120, 10),
            (20123.0, 100, 8),
            (20122.0, 80, 6),
            (20121.0, 60, 5),
            (20120.0, 50, 4),
            (20119.0, 40, 3),
            (20118.0, 30, 2),
            (20117.0, 20, 2),
            (20116.0, 10, 1),
        ],
        asks=[
            (20126.0, 80, 8),
            (20127.0, 95, 9),
            (20128.0, 110, 11),
            (20129.0, 130, 13),
            (20130.0, 150, 15),
            (20131.0, 170, 17),
            (20132.0, 190, 19),
            (20133.0, 210, 21),
            (20134.0, 230, 23),
            (20135.0, 250, 25),
        ],
        timestamp=datetime.utcnow()
    )
