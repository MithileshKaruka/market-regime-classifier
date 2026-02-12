"""Dynamic Stop Loss and Take Profit Calculator

Calculates SL/TP based on market structure rather than static percentages:
- Stop Loss: Recent swing high/low or zone boundary
- Take Profit: Next S/R level or zone boundary

Uses ATR-based buffers to ensure stops aren't placed at exact levels.
"""
import logging
from dataclasses import dataclass
from typing import Optional, Tuple, List
import polars as pl

from app.data.storage import DuckDBStorage
from config import get_config

logger = logging.getLogger(__name__)


@dataclass
class DynamicLevels:
    """Result of dynamic SL/TP calculation."""
    stop_loss: float
    take_profit: float
    sl_reason: str  # What the SL is based on
    tp_reason: str  # What the TP is based on
    risk_reward: float  # TP distance / SL distance


class DynamicLevelCalculator:
    """Calculate SL/TP based on market structure.

    For LONG entries:
        - SL: Below recent swing low (with ATR buffer)
        - TP: At next resistance/supply zone

    For SHORT entries:
        - SL: Above recent swing high (with ATR buffer)
        - TP: At next support/demand zone
    """

    def __init__(
        self,
        swing_window: int = 5,
        atr_period: int = 14,
        sl_atr_buffer: float = 0.3,  # Add 0.3 ATR beyond swing point
        min_rr_ratio: float = 1.5,   # Minimum risk:reward ratio
        max_rr_ratio: float = 3.0,   # Maximum risk:reward ratio (cap)
        max_sl_pct: float = 0.015,   # Max SL = 1.5% (fallback cap)
        min_sl_pct: float = 0.003,   # Min SL = 0.3% (floor)
    ):
        self.swing_window = swing_window
        self.atr_period = atr_period
        self.sl_atr_buffer = sl_atr_buffer
        self.min_rr_ratio = min_rr_ratio
        self.max_rr_ratio = max_rr_ratio
        self.max_sl_pct = max_sl_pct
        self.min_sl_pct = min_sl_pct

    def load_market_data(
        self,
        timeframe: str,
        symbol: str = "MNQ",
        limit: int = 200,
    ) -> pl.DataFrame:
        """Load recent OHLCV data for market structure analysis."""
        with DuckDBStorage() as db:
            query = f"""
                SELECT timestamp, open, high, low, close, volume
                FROM ohlcv_ticks
                WHERE symbol = '{symbol}' AND timeframe = '{timeframe}'
                ORDER BY timestamp DESC
                LIMIT {limit}
            """
            df = db.conn.execute(query).pl()
            return df.reverse() if len(df) > 0 else df

    def calculate_atr(self, df: pl.DataFrame, period: int = 14) -> float:
        """Calculate current ATR value."""
        if len(df) < period + 1:
            return 0.0

        # True Range calculation
        df = df.with_columns([
            pl.max_horizontal(
                pl.col("high") - pl.col("low"),
                (pl.col("high") - pl.col("close").shift(1)).abs(),
                (pl.col("low") - pl.col("close").shift(1)).abs(),
            ).alias("tr")
        ])

        # EMA of True Range
        atr = df["tr"].ewm_mean(span=period).to_list()[-1]
        return float(atr) if atr else 0.0

    def find_recent_swing_low(
        self,
        df: pl.DataFrame,
        current_price: float,
        lookback: int = 50,
    ) -> Optional[Tuple[float, int]]:
        """Find the most recent swing low below current price.

        Returns:
            Tuple of (price, bars_ago) or None if not found
        """
        if len(df) < self.swing_window * 2 + 1:
            return None

        # Only look at recent bars
        recent_df = df.tail(lookback)

        # Find swing lows (local minima)
        swing_lows = []
        lows = recent_df["low"].to_list()

        for i in range(self.swing_window, len(lows) - self.swing_window):
            is_swing = True
            for j in range(1, self.swing_window + 1):
                if lows[i] >= lows[i - j] or lows[i] >= lows[i + j]:
                    is_swing = False
                    break
            if is_swing and lows[i] < current_price:
                bars_ago = len(lows) - 1 - i
                swing_lows.append((lows[i], bars_ago))

        # Return most recent swing low below price
        if swing_lows:
            # Sort by recency (smallest bars_ago first)
            swing_lows.sort(key=lambda x: x[1])
            return swing_lows[0]
        return None

    def find_recent_swing_high(
        self,
        df: pl.DataFrame,
        current_price: float,
        lookback: int = 50,
    ) -> Optional[Tuple[float, int]]:
        """Find the most recent swing high above current price.

        Returns:
            Tuple of (price, bars_ago) or None if not found
        """
        if len(df) < self.swing_window * 2 + 1:
            return None

        # Only look at recent bars
        recent_df = df.tail(lookback)

        # Find swing highs (local maxima)
        swing_highs = []
        highs = recent_df["high"].to_list()

        for i in range(self.swing_window, len(highs) - self.swing_window):
            is_swing = True
            for j in range(1, self.swing_window + 1):
                if highs[i] <= highs[i - j] or highs[i] <= highs[i + j]:
                    is_swing = False
                    break
            if is_swing and highs[i] > current_price:
                bars_ago = len(highs) - 1 - i
                swing_highs.append((highs[i], bars_ago))

        # Return most recent swing high above price
        if swing_highs:
            # Sort by recency (smallest bars_ago first)
            swing_highs.sort(key=lambda x: x[1])
            return swing_highs[0]
        return None

    def find_next_resistance(
        self,
        df: pl.DataFrame,
        current_price: float,
        lookback: int = 100,
    ) -> Optional[float]:
        """Find the next significant resistance level above current price."""
        swing_high = self.find_recent_swing_high(df, current_price, lookback)
        if swing_high:
            return swing_high[0]
        return None

    def find_next_support(
        self,
        df: pl.DataFrame,
        current_price: float,
        lookback: int = 100,
    ) -> Optional[float]:
        """Find the next significant support level below current price."""
        swing_low = self.find_recent_swing_low(df, current_price, lookback)
        if swing_low:
            return swing_low[0]
        return None

    def calculate_long_levels(
        self,
        timeframe: str,
        current_price: float,
        symbol: str = "MNQ",
        zone_low: Optional[float] = None,
        zone_high: Optional[float] = None,
    ) -> Optional[DynamicLevels]:
        """Calculate SL/TP for a LONG entry.

        Args:
            timeframe: Chart timeframe
            current_price: Current/entry price
            symbol: Trading symbol
            zone_low: If in a demand zone, the zone's lower boundary
            zone_high: If in a demand zone, the zone's upper boundary

        Returns:
            DynamicLevels with calculated SL and TP
        """
        df = self.load_market_data(timeframe, symbol)
        if len(df) < 20:
            return None

        atr = self.calculate_atr(df, self.atr_period)
        if atr <= 0:
            return None

        # Calculate Stop Loss
        sl_price = None
        sl_reason = ""

        # Option 1: Zone boundary (if we entered from a demand zone)
        if zone_low is not None:
            sl_price = zone_low - (atr * self.sl_atr_buffer)
            sl_reason = f"Zone bottom - {self.sl_atr_buffer}x ATR buffer"

        # Option 2: Recent swing low
        swing_low = self.find_recent_swing_low(df, current_price)
        if swing_low:
            swing_sl = swing_low[0] - (atr * self.sl_atr_buffer)
            # Use swing low if tighter than zone but still valid
            if sl_price is None or (swing_sl > sl_price and swing_sl < current_price):
                sl_price = swing_sl
                sl_reason = f"Swing low ({swing_low[1]} bars ago) - {self.sl_atr_buffer}x ATR buffer"

        # Fallback: ATR-based stop
        if sl_price is None or sl_price >= current_price:
            sl_price = current_price - (atr * 1.5)
            sl_reason = "1.5x ATR (fallback)"

        # Apply min/max caps
        sl_distance = current_price - sl_price
        min_sl = current_price * self.min_sl_pct
        max_sl = current_price * self.max_sl_pct

        if sl_distance < min_sl:
            sl_price = current_price - min_sl
            sl_reason += f" (capped to min {self.min_sl_pct*100:.1f}%)"
        elif sl_distance > max_sl:
            sl_price = current_price - max_sl
            sl_reason += f" (capped to max {self.max_sl_pct*100:.1f}%)"

        # Calculate Take Profit
        tp_price = None
        tp_reason = ""
        sl_distance = current_price - sl_price

        # Option 1: Next swing high/resistance
        resistance = self.find_next_resistance(df, current_price)
        if resistance:
            tp_price = resistance
            tp_reason = "Next resistance level"

        # Ensure minimum R:R ratio
        min_tp_distance = sl_distance * self.min_rr_ratio
        min_tp = current_price + min_tp_distance

        if tp_price is None or tp_price < min_tp:
            tp_price = min_tp
            tp_reason = f"Min {self.min_rr_ratio}:1 R:R target"

        # Cap at maximum R:R ratio to keep targets realistic
        max_tp_distance = sl_distance * self.max_rr_ratio
        max_tp = current_price + max_tp_distance

        if tp_price > max_tp:
            tp_price = max_tp
            tp_reason = f"Max {self.max_rr_ratio}:1 R:R cap"

        risk_reward = (tp_price - current_price) / sl_distance if sl_distance > 0 else 0

        return DynamicLevels(
            stop_loss=sl_price,
            take_profit=tp_price,
            sl_reason=sl_reason,
            tp_reason=tp_reason,
            risk_reward=risk_reward,
        )

    def calculate_short_levels(
        self,
        timeframe: str,
        current_price: float,
        symbol: str = "MNQ",
        zone_low: Optional[float] = None,
        zone_high: Optional[float] = None,
    ) -> Optional[DynamicLevels]:
        """Calculate SL/TP for a SHORT entry.

        Args:
            timeframe: Chart timeframe
            current_price: Current/entry price
            symbol: Trading symbol
            zone_low: If in a supply zone, the zone's lower boundary
            zone_high: If in a supply zone, the zone's upper boundary

        Returns:
            DynamicLevels with calculated SL and TP
        """
        df = self.load_market_data(timeframe, symbol)
        if len(df) < 20:
            return None

        atr = self.calculate_atr(df, self.atr_period)
        if atr <= 0:
            return None

        # Calculate Stop Loss
        sl_price = None
        sl_reason = ""

        # Option 1: Zone boundary (if we entered from a supply zone)
        if zone_high is not None:
            sl_price = zone_high + (atr * self.sl_atr_buffer)
            sl_reason = f"Zone top + {self.sl_atr_buffer}x ATR buffer"

        # Option 2: Recent swing high
        swing_high = self.find_recent_swing_high(df, current_price)
        if swing_high:
            swing_sl = swing_high[0] + (atr * self.sl_atr_buffer)
            # Use swing high if tighter than zone but still valid
            if sl_price is None or (swing_sl < sl_price and swing_sl > current_price):
                sl_price = swing_sl
                sl_reason = f"Swing high ({swing_high[1]} bars ago) + {self.sl_atr_buffer}x ATR buffer"

        # Fallback: ATR-based stop
        if sl_price is None or sl_price <= current_price:
            sl_price = current_price + (atr * 1.5)
            sl_reason = "1.5x ATR (fallback)"

        # Apply min/max caps
        sl_distance = sl_price - current_price
        min_sl = current_price * self.min_sl_pct
        max_sl = current_price * self.max_sl_pct

        if sl_distance < min_sl:
            sl_price = current_price + min_sl
            sl_reason += f" (capped to min {self.min_sl_pct*100:.1f}%)"
        elif sl_distance > max_sl:
            sl_price = current_price + max_sl
            sl_reason += f" (capped to max {self.max_sl_pct*100:.1f}%)"

        # Calculate Take Profit
        tp_price = None
        tp_reason = ""
        sl_distance = sl_price - current_price

        # Option 1: Next swing low/support
        support = self.find_next_support(df, current_price)
        if support:
            tp_price = support
            tp_reason = "Next support level"

        # Ensure minimum R:R ratio
        min_tp_distance = sl_distance * self.min_rr_ratio
        min_tp = current_price - min_tp_distance

        if tp_price is None or tp_price > min_tp:
            tp_price = min_tp
            tp_reason = f"Min {self.min_rr_ratio}:1 R:R target"

        # Cap at maximum R:R ratio to keep targets realistic
        max_tp_distance = sl_distance * self.max_rr_ratio
        max_tp = current_price - max_tp_distance

        if tp_price < max_tp:
            tp_price = max_tp
            tp_reason = f"Max {self.max_rr_ratio}:1 R:R cap"

        risk_reward = (current_price - tp_price) / sl_distance if sl_distance > 0 else 0

        return DynamicLevels(
            stop_loss=sl_price,
            take_profit=tp_price,
            sl_reason=sl_reason,
            tp_reason=tp_reason,
            risk_reward=risk_reward,
        )

    def calculate_levels(
        self,
        direction: str,  # "LONG" or "SHORT"
        timeframe: str,
        current_price: float,
        symbol: str = "MNQ",
        zone_low: Optional[float] = None,
        zone_high: Optional[float] = None,
    ) -> Optional[DynamicLevels]:
        """Calculate SL/TP based on direction.

        Args:
            direction: "LONG" or "SHORT"
            timeframe: Chart timeframe
            current_price: Current/entry price
            symbol: Trading symbol
            zone_low: Zone lower boundary (if applicable)
            zone_high: Zone upper boundary (if applicable)

        Returns:
            DynamicLevels with calculated SL and TP
        """
        if direction == "LONG":
            return self.calculate_long_levels(
                timeframe, current_price, symbol, zone_low, zone_high
            )
        elif direction == "SHORT":
            return self.calculate_short_levels(
                timeframe, current_price, symbol, zone_low, zone_high
            )
        return None
