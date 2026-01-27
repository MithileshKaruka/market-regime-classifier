"""Trade flow calculations from Databento Trades schema

This module calculates CVD (Cumulative Volume Delta) from actual trade executions,
which is more accurate than approximating from order book changes.

Databento Trades schema provides:
- ts_event: Timestamp of the trade
- price: Trade price
- size: Trade size (volume)
- side: Trade aggressor side ('A' = ask/buy aggressor, 'B' = bid/sell aggressor)
"""
import logging
from typing import Optional, Dict
import polars as pl

from config import get_config

logger = logging.getLogger(__name__)


class TradeFlowCalculator:
    """Calculate trade flow metrics from Databento Trades schema

    Uses actual trade executions to calculate accurate CVD/delta.
    Trade side indicates aggressor:
    - 'A' (Ask): Buy aggressor - buyer lifted the ask (bullish)
    - 'B' (Bid): Sell aggressor - seller hit the bid (bearish)

    All parameters can be overridden, but defaults are loaded from config.
    """

    def __init__(self, cvd_window_config: Optional[Dict[str, int]] = None):
        """Initialize calculator

        Args:
            cvd_window_config: Dict mapping timeframe to CVD rolling window size
                              (defaults loaded from config)
        """
        # Load defaults from config if not provided
        config = get_config()
        self.cvd_window_config = cvd_window_config or config.regime.cvd_windows

        logger.info(f"TradeFlowCalculator initialized with CVD windows: {self.cvd_window_config}")

    def calculate_delta(self, df: pl.DataFrame) -> pl.DataFrame:
        """Calculate delta from trade side

        Delta = buy_volume - sell_volume
        - Positive: More aggressive buying (bullish)
        - Negative: More aggressive selling (bearish)

        Args:
            df: DataFrame with 'side' and 'size' columns

        Returns:
            DataFrame with 'delta' and 'signed_size' columns
        """
        logger.info(f"Calculating trade delta for {len(df)} trades")

        # Determine side column format
        # Databento uses 'A' for ask (buy aggressor) and 'B' for bid (sell aggressor)
        if "side" not in df.columns:
            logger.warning("No 'side' column found, cannot calculate delta")
            return df.with_columns([
                pl.lit(0).alias("signed_size"),
                pl.lit(0).alias("delta")
            ])

        # Convert side to signed size
        # 'A' = Ask side = Buy aggressor = positive delta
        # 'B' = Bid side = Sell aggressor = negative delta
        # Note: size is u32, must cast to i64 before negation
        df = df.with_columns([
            pl.when(pl.col("side") == "A")
              .then(pl.col("size").cast(pl.Int64))
              .when(pl.col("side") == "B")
              .then(-pl.col("size").cast(pl.Int64))
              .otherwise(0)
              .alias("signed_size")
        ])

        # Cumulative delta
        df = df.with_columns([
            pl.col("signed_size").cum_sum().alias("delta")
        ])

        return df

    def calculate_trade_metrics(self, df: pl.DataFrame) -> pl.DataFrame:
        """Calculate trade-based metrics

        Args:
            df: DataFrame with trade data

        Returns:
            DataFrame with trade metrics
        """
        logger.info(f"Calculating trade metrics for {len(df)} trades")

        # Ensure we have the delta calculated
        if "signed_size" not in df.columns:
            df = self.calculate_delta(df)

        # Calculate additional metrics
        result_cols = []

        # Trade count
        result_cols.append(pl.lit(1).alias("trade_count"))

        # Volume
        if "size" in df.columns:
            result_cols.append(pl.col("size").alias("volume"))

        # Price (for VWAP calculation)
        if "price" in df.columns:
            result_cols.append(pl.col("price").alias("trade_price"))
            if "size" in df.columns:
                result_cols.append((pl.col("price") * pl.col("size")).alias("price_volume"))

        if result_cols:
            df = df.with_columns(result_cols)

        return df

    def calculate_all_features(self, df: pl.DataFrame) -> pl.DataFrame:
        """Calculate all trade flow features

        Args:
            df: Raw trades DataFrame

        Returns:
            DataFrame with all calculated features
        """
        logger.info("Calculating all trade flow features")

        df = self.calculate_delta(df)
        df = self.calculate_trade_metrics(df)

        logger.info("All trade features calculated successfully")
        return df

    def resample_to_timeframe(
        self,
        df: pl.DataFrame,
        timeframe: str
    ) -> pl.DataFrame:
        """Resample trade data to specific timeframe

        Args:
            df: DataFrame with timestamp column
            timeframe: Target timeframe ('5M', '15M', '1H', '4H', '1D')

        Returns:
            Resampled DataFrame with OHLCV and delta
        """
        logger.info(f"Resampling trades to {timeframe} timeframe")

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

        # Determine timestamp column
        if "ts_event" in df.columns:
            ts_col = "ts_event"
        elif "timestamp" in df.columns:
            ts_col = "timestamp"
        else:
            raise ValueError("No timestamp column found")

        # Convert timestamp to datetime if needed
        if df[ts_col].dtype == pl.Int64 or df[ts_col].dtype == pl.UInt64:
            df = df.with_columns([
                pl.from_epoch(pl.col(ts_col), time_unit="ns").alias(ts_col)
            ])

        # Sort by timestamp
        df = df.sort(ts_col)

        # Build aggregation expressions
        agg_exprs = [
            pl.col("trade_price").first().alias("open"),
            pl.col("trade_price").max().alias("high"),
            pl.col("trade_price").min().alias("low"),
            pl.col("trade_price").last().alias("close"),
            pl.col("size").sum().alias("volume"),
            pl.col("signed_size").sum().alias("instant_delta"),
            pl.col("trade_count").sum().alias("trade_count"),
        ]

        # VWAP calculation
        if "price_volume" in df.columns:
            agg_exprs.append(
                (pl.col("price_volume").sum() / pl.col("size").sum()).alias("vwap")
            )

        # Group by timeframe
        df_resampled = df.group_by_dynamic(
            ts_col,
            every=duration,
        ).agg(agg_exprs)

        # Calculate rolling CVD
        cvd_window = self.cvd_window_config.get(timeframe, 24)
        df_resampled = df_resampled.with_columns([
            pl.col("instant_delta")
              .rolling_sum(window_size=cvd_window)
              .alias("cvd")
        ])

        logger.info(f"Resampled to {len(df_resampled)} {timeframe} bars (CVD window: {cvd_window})")
        return df_resampled


def merge_quotes_and_trades(
    quotes_df: pl.DataFrame,
    trades_df: pl.DataFrame,
    timeframe: str
) -> pl.DataFrame:
    """Merge MBP-1 quotes and trades data into unified OHLCV bars

    This combines:
    - OHLCV from trades (accurate prices and volume)
    - DOM imbalance from quotes (order book state)
    - CVD/delta from trades (true buying/selling pressure)

    Args:
        quotes_df: Resampled MBP-1 data with dom_imbalance
        trades_df: Resampled trades data with delta/cvd
        timeframe: Timeframe string for logging

    Returns:
        Merged DataFrame with all metrics
    """
    logger.info(f"Merging quotes and trades for {timeframe}")

    # Determine timestamp columns
    quotes_ts = "ts_event" if "ts_event" in quotes_df.columns else "timestamp"
    trades_ts = "ts_event" if "ts_event" in trades_df.columns else "timestamp"

    # Rename timestamp columns for join
    quotes_df = quotes_df.rename({quotes_ts: "timestamp"})
    trades_df = trades_df.rename({trades_ts: "timestamp"})

    # Select columns from each source
    # From trades: OHLCV, delta, cvd, vwap
    trades_cols = ["timestamp", "open", "high", "low", "close", "volume",
                   "instant_delta", "cvd", "vwap", "trade_count"]
    trades_select = [c for c in trades_cols if c in trades_df.columns]

    # From quotes: dom_imbalance, spread
    quotes_cols = ["timestamp", "dom_imbalance", "spread", "best_bid_size", "best_ask_size"]
    quotes_select = [c for c in quotes_cols if c in quotes_df.columns]

    # Join on timestamp
    merged = trades_df.select(trades_select).join(
        quotes_df.select(quotes_select),
        on="timestamp",
        how="left"
    )

    # Fill missing DOM imbalance with neutral value
    if "dom_imbalance" in merged.columns:
        merged = merged.with_columns([
            pl.col("dom_imbalance").fill_null(0.5)
        ])

    logger.info(f"Merged {len(merged)} bars for {timeframe}")
    return merged
