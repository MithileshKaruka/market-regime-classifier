"""Order flow calculations from MBP-1 and Trades data"""
import logging
from typing import Dict, Tuple
import polars as pl
import numpy as np

logger = logging.getLogger(__name__)


class OrderFlowCalculator:
    """Calculate order flow metrics from MBP-1 order book data

    Supports both MBP-1 (top of book) and MBP-10 (10 levels) for backwards compatibility.
    For live streaming, MBP-1 is used. For historical analysis, MBP-10 may be available.
    """

    def __init__(
        self,
        cvd_window_config: dict = None,
        levels: int = 1
    ):
        """Initialize calculator

        Args:
            cvd_window_config: Dict mapping timeframe to CVD rolling window size
                              Default: {'5M': 288, '15M': 96, '1H': 24, '4H': 30, '1D': 5}
            levels: Number of order book levels (1 for MBP-1, 10 for MBP-10)
        """
        self.levels = levels
        # Default CVD rolling window sizes (roughly 24 hours for intraday, 5 days for daily)
        self.cvd_window_config = cvd_window_config or {
            '5M': 288,   # 24 hours (288 * 5min = 1440min = 24h)
            '15M': 96,   # 24 hours (96 * 15min = 1440min = 24h)
            '1H': 24,    # 24 hours (24 * 1h = 24h)
            '4H': 30,    # 5 days (30 * 4h = 120h = 5 days)
            '1D': 5,     # 5 days
        }
        logger.info(f"OrderFlowCalculator initialized with {levels} levels, CVD windows: {self.cvd_window_config}")

    def calculate_dom_imbalance(
        self,
        df: pl.DataFrame,
        levels: int = None
    ) -> pl.DataFrame:
        """Calculate DOM (Depth of Market) imbalance

        Imbalance = bid_volume / (bid_volume + ask_volume)
        > 0.5: Bid heavy (bullish)
        < 0.5: Ask heavy (bearish)
        = 0.5: Balanced

        For MBP-1: Uses only top-of-book (level 0)
        For MBP-10: Uses all 10 levels

        Args:
            df: DataFrame with bid/ask levels
            levels: Number of levels to consider (default: use self.levels)

        Returns:
            DataFrame with dom_imbalance column
        """
        levels = levels or self.levels
        logger.info(f"Calculating DOM imbalance for {len(df)} records using {levels} level(s)")

        # Sum bid volumes across available levels
        bid_cols = [f"bid_sz_{i:02d}" for i in range(levels) if f"bid_sz_{i:02d}" in df.columns]
        ask_cols = [f"ask_sz_{i:02d}" for i in range(levels) if f"ask_sz_{i:02d}" in df.columns]

        # If no level columns found, check for simple bid_sz/ask_sz columns
        if not bid_cols and "bid_sz" in df.columns:
            bid_cols = ["bid_sz"]
            ask_cols = ["ask_sz"]

        if not bid_cols:
            logger.warning("No bid/ask size columns found, using default imbalance of 0.5")
            return df.with_columns([
                pl.lit(0).alias("total_bid_volume"),
                pl.lit(0).alias("total_ask_volume"),
                pl.lit(0.5).alias("dom_imbalance")
            ])

        df = df.with_columns([
            pl.sum_horizontal([pl.col(c) for c in bid_cols]).alias("total_bid_volume"),
            pl.sum_horizontal([pl.col(c) for c in ask_cols]).alias("total_ask_volume"),
        ])

        # Calculate imbalance ratio (avoid division by zero)
        df = df.with_columns([
            pl.when((pl.col("total_bid_volume") + pl.col("total_ask_volume")) > 0)
              .then(pl.col("total_bid_volume") / (pl.col("total_bid_volume") + pl.col("total_ask_volume")))
              .otherwise(0.5)
              .alias("dom_imbalance")
        ])

        return df

    def calculate_delta(self, df: pl.DataFrame) -> pl.DataFrame:
        """Calculate cumulative delta (buying pressure - selling pressure)

        Delta = cumulative(trades at ask - trades at bid)
        Positive delta: Net buying
        Negative delta: Net selling

        Args:
            df: DataFrame with trade data

        Returns:
            DataFrame with delta column
        """
        logger.info(f"Calculating delta for {len(df)} records")

        # For MBP-10, we approximate delta using bid/ask volume changes
        # Real implementation would use trade data
        # TODO: Implement proper delta calculation with trade-level data

        df = df.with_columns([
            (pl.col("total_bid_volume") - pl.col("total_ask_volume"))
            .alias("instant_delta")
        ])

        df = df.with_columns([
            pl.col("instant_delta").cum_sum().alias("delta")
        ])

        return df

    def calculate_vwap(self, df: pl.DataFrame) -> pl.DataFrame:
        """Calculate Volume-Weighted Average Price

        VWAP = sum(price * volume) / sum(volume)

        Args:
            df: DataFrame with price and volume data

        Returns:
            DataFrame with vwap column
        """
        logger.info(f"Calculating VWAP for {len(df)} records")

        # Calculate mid price from best bid/ask
        # Filter out invalid prices (must be > 0 for both bid and ask)
        df = df.with_columns([
            pl.when((pl.col("bid_px_00") > 0) & (pl.col("ask_px_00") > 0))
              .then((pl.col("bid_px_00") + pl.col("ask_px_00")) / 2)
              .otherwise(None)
              .alias("mid_price")
        ])

        # Filter out records with null mid_price OR prices outside reasonable instrument range
        # For MNQ: typical range is 18,000-32,000 (allows for market moves)
        df = df.filter(
            pl.col("mid_price").is_not_null() &
            (pl.col("mid_price") >= 18000) &
            (pl.col("mid_price") <= 32000)
        )

        logger.info(f"After filtering invalid prices: {len(df)} records remain")

        df = df.with_columns([
            (pl.col("mid_price") * pl.col("total_bid_volume")).alias("price_volume")
        ])

        # Calculate cumulative VWAP
        df = df.with_columns([
            (pl.col("price_volume").cum_sum() / pl.col("total_bid_volume").cum_sum())
            .alias("vwap")
        ])

        return df

    def calculate_liquidity_metrics(self, df: pl.DataFrame) -> pl.DataFrame:
        """Calculate liquidity metrics

        - Spread
        - Depth at best levels
        - Average depth across levels (if available)

        Args:
            df: DataFrame with order book data

        Returns:
            DataFrame with liquidity metrics
        """
        logger.info(f"Calculating liquidity metrics for {len(df)} records")

        result_cols = []

        # Spread - works with both MBP-1 and MBP-10
        if "ask_px_00" in df.columns and "bid_px_00" in df.columns:
            result_cols.append((pl.col("ask_px_00") - pl.col("bid_px_00")).alias("spread"))
            result_cols.append(pl.col("bid_sz_00").alias("best_bid_size"))
            result_cols.append(pl.col("ask_sz_00").alias("best_ask_size"))
        elif "ask_px" in df.columns and "bid_px" in df.columns:
            result_cols.append((pl.col("ask_px") - pl.col("bid_px")).alias("spread"))
            result_cols.append(pl.col("bid_sz").alias("best_bid_size"))
            result_cols.append(pl.col("ask_sz").alias("best_ask_size"))

        # Average depth across levels (only for MBP-10)
        if self.levels > 1:
            bid_depth_cols = [f"bid_sz_{i:02d}" for i in range(min(5, self.levels)) if f"bid_sz_{i:02d}" in df.columns]
            ask_depth_cols = [f"ask_sz_{i:02d}" for i in range(min(5, self.levels)) if f"ask_sz_{i:02d}" in df.columns]

            if bid_depth_cols:
                result_cols.append(pl.mean_horizontal([pl.col(c) for c in bid_depth_cols]).alias("avg_bid_depth_5"))
            if ask_depth_cols:
                result_cols.append(pl.mean_horizontal([pl.col(c) for c in ask_depth_cols]).alias("avg_ask_depth_5"))
        else:
            # For MBP-1, avg depth equals best level depth
            if "bid_sz_00" in df.columns:
                result_cols.append(pl.col("bid_sz_00").alias("avg_bid_depth_5"))
                result_cols.append(pl.col("ask_sz_00").alias("avg_ask_depth_5"))

        if result_cols:
            df = df.with_columns(result_cols)

        return df

    def calculate_all_features(self, df: pl.DataFrame) -> pl.DataFrame:
        """Calculate all order flow features

        Args:
            df: Raw MBP-10 DataFrame

        Returns:
            DataFrame with all calculated features
        """
        logger.info("Calculating all order flow features")

        df = self.calculate_dom_imbalance(df)
        df = self.calculate_delta(df)
        df = self.calculate_vwap(df)
        df = self.calculate_liquidity_metrics(df)

        logger.info("All features calculated successfully")
        return df

    def resample_to_timeframe(
        self,
        df: pl.DataFrame,
        timeframe: str
    ) -> pl.DataFrame:
        """Resample data to specific timeframe (5M, 15M, 1H, etc.)

        Args:
            df: DataFrame with timestamp column
            timeframe: Target timeframe ('5M', '15M', '1H', '4H', '1D')

        Returns:
            Resampled DataFrame
        """
        logger.info(f"Resampling to {timeframe} timeframe")

        # Map timeframe strings to polars duration
        timeframe_map = {
            "5M": "5m",
            "15M": "15m",
            "1H": "1h",
            "4H": "4h",
            "1D": "1d",
        }

        if timeframe not in timeframe_map:
            raise ValueError(f"Invalid timeframe: {timeframe}")

        duration = timeframe_map[timeframe]

        # Ensure timestamp column is datetime type
        if "ts_event" in df.columns:
            ts_col = "ts_event"
        elif "timestamp" in df.columns:
            ts_col = "timestamp"
        else:
            raise ValueError("No timestamp column found")

        # Convert timestamp to datetime if it's an integer (nanoseconds)
        if df[ts_col].dtype == pl.Int64 or df[ts_col].dtype == pl.UInt64:
            df = df.with_columns([
                pl.from_epoch(pl.col(ts_col), time_unit="ns").alias(ts_col)
            ])

        # Sort by timestamp (required for group_by_dynamic)
        df = df.sort(ts_col)

        # Group by timeframe and aggregate
        # Since we filter by instrument_id, we should have clean single-instrument data
        df_resampled = df.group_by_dynamic(
            ts_col,
            every=duration,
        ).agg([
            pl.col("mid_price").first().alias("open"),
            pl.col("mid_price").max().alias("high"),
            pl.col("mid_price").min().alias("low"),
            pl.col("mid_price").last().alias("close"),
            pl.col("total_bid_volume").sum().alias("volume"),
            pl.col("dom_imbalance").mean().alias("dom_imbalance"),
            pl.col("instant_delta").sum().alias("instant_delta"),  # Sum instant delta within candle
            # Calculate VWAP for this candle (not cumulative)
            ((pl.col("mid_price") * pl.col("total_bid_volume")).sum() /
             pl.col("total_bid_volume").sum()).alias("vwap"),
        ])

        # Fix price gaps: Make open price equal to previous close for continuity
        # This ensures candlesticks connect properly without visual gaps
        # During active trading, close[n] must equal open[n+1]
        df_resampled = df_resampled.with_columns([
            # Use previous close as open, except for first bar
            pl.when(pl.col(ts_col) == df_resampled[ts_col][0])
              .then(pl.col("open"))  # Keep first bar's open as-is
              .otherwise(pl.col("close").shift(1))  # Use previous close
              .alias("open")
        ])

        # Recalculate high/low to include the adjusted open price
        df_resampled = df_resampled.with_columns([
            pl.max_horizontal([pl.col("open"), pl.col("high"), pl.col("close")]).alias("high"),
            pl.min_horizontal([pl.col("open"), pl.col("low"), pl.col("close")]).alias("low"),
        ])

        # Calculate rolling CVD (Cumulative Volume Delta) using configured window size
        cvd_window = self.cvd_window_config.get(timeframe, 24)  # Default to 24 if timeframe not configured
        df_resampled = df_resampled.with_columns([
            pl.col("instant_delta")
              .rolling_sum(window_size=cvd_window)
              .alias("cvd")
        ])

        logger.info(f"Resampled to {len(df_resampled)} {timeframe} bars (CVD window: {cvd_window})")
        return df_resampled
