"""
Technical Indicators

Implements common technical indicators:
- VWAP (Volume-Weighted Average Price)
- RVWAP (Rolling VWAP)
- EMA (Exponential Moving Average)
- Bollinger Bands
- ATR (Average True Range)
"""

import polars as pl
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class TechnicalIndicators:
    """Calculate technical indicators on OHLCV data"""

    @staticmethod
    def calculate_vwap(df: pl.DataFrame, session_reset: bool = True) -> pl.DataFrame:
        """
        Calculate Volume-Weighted Average Price

        VWAP = Cumulative(Typical Price × Volume) / Cumulative(Volume)
        where Typical Price = (High + Low + Close) / 3

        Args:
            df: DataFrame with OHLCV data
            session_reset: Whether to reset VWAP at session start (not implemented yet)

        Returns:
            DataFrame with vwap column
        """
        logger.info("Calculating VWAP")

        df = df.with_columns([
            # Typical price
            ((pl.col("high") + pl.col("low") + pl.col("close")) / 3).alias("typical_price")
        ])

        df = df.with_columns([
            # VWAP = cumsum(typical_price * volume) / cumsum(volume)
            (
                (pl.col("typical_price") * pl.col("volume")).cum_sum() /
                pl.col("volume").cum_sum()
            ).alias("vwap")
        ])

        return df

    @staticmethod
    def calculate_rvwap(df: pl.DataFrame, period: int = 20) -> pl.DataFrame:
        """
        Calculate Rolling Volume-Weighted Average Price

        Rolling version of VWAP over a fixed window

        Args:
            df: DataFrame with OHLCV data
            period: Rolling window size

        Returns:
            DataFrame with rvwap column
        """
        logger.info(f"Calculating RVWAP with period={period}")

        df = df.with_columns([
            # Typical price
            ((pl.col("high") + pl.col("low") + pl.col("close")) / 3).alias("typical_price")
        ])

        df = df.with_columns([
            # RVWAP = rolling_sum(typical_price * volume) / rolling_sum(volume)
            (
                (pl.col("typical_price") * pl.col("volume")).rolling_sum(period) /
                pl.col("volume").rolling_sum(period)
            ).alias("rvwap")
        ])

        return df

    @staticmethod
    def calculate_ema(df: pl.DataFrame, period: int = 20, column: str = "close") -> pl.DataFrame:
        """
        Calculate Exponential Moving Average

        EMA = (Close - EMA_prev) × multiplier + EMA_prev
        where multiplier = 2 / (period + 1)

        Args:
            df: DataFrame with price data
            period: EMA period
            column: Column to calculate EMA on (default: close)

        Returns:
            DataFrame with ema_{period} column
        """
        logger.info(f"Calculating EMA-{period} on {column}")

        # Polars has built-in EMA
        df = df.with_columns([
            pl.col(column)
              .ewm_mean(span=period, adjust=False)
              .alias(f"ema_{period}")
        ])

        return df

    @staticmethod
    def calculate_bollinger_bands(
        df: pl.DataFrame,
        period: int = 20,
        std_dev: float = 2.0,
        column: str = "close"
    ) -> pl.DataFrame:
        """
        Calculate Bollinger Bands

        Middle Band = SMA(period)
        Upper Band = Middle Band + (std_dev × standard deviation)
        Lower Band = Middle Band - (std_dev × standard deviation)

        Args:
            df: DataFrame with price data
            period: Moving average period
            std_dev: Number of standard deviations
            column: Column to calculate on (default: close)

        Returns:
            DataFrame with bb_middle, bb_upper, bb_lower columns
        """
        logger.info(f"Calculating Bollinger Bands ({period}, {std_dev}σ)")

        df = df.with_columns([
            # Middle band (SMA)
            pl.col(column).rolling_mean(period).alias("bb_middle"),
            # Standard deviation
            pl.col(column).rolling_std(period).alias("bb_std"),
        ])

        df = df.with_columns([
            # Upper band
            (pl.col("bb_middle") + (pl.col("bb_std") * std_dev)).alias("bb_upper"),
            # Lower band
            (pl.col("bb_middle") - (pl.col("bb_std") * std_dev)).alias("bb_lower"),
        ])

        # Drop intermediate std column
        df = df.drop("bb_std")

        return df

    @staticmethod
    def calculate_atr(df: pl.DataFrame, period: int = 14) -> pl.DataFrame:
        """
        Calculate Average True Range

        True Range = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        ATR = EMA of True Range

        Args:
            df: DataFrame with OHLC data
            period: ATR period

        Returns:
            DataFrame with atr column
        """
        logger.info(f"Calculating ATR-{period}")

        df = df.with_columns([
            # True Range components
            (pl.col("high") - pl.col("low")).alias("hl_diff"),
            (pl.col("high") - pl.col("close").shift(1)).abs().alias("hc_diff"),
            (pl.col("low") - pl.col("close").shift(1)).abs().alias("lc_diff"),
        ])

        df = df.with_columns([
            # True Range = max of the three
            pl.max_horizontal(["hl_diff", "hc_diff", "lc_diff"]).alias("true_range")
        ])

        # ATR = EMA of True Range
        df = df.with_columns([
            pl.col("true_range")
              .ewm_mean(span=period, adjust=False)
              .alias("atr")
        ])

        # Clean up intermediate columns
        df = df.drop(["hl_diff", "hc_diff", "lc_diff", "true_range"])

        return df

    @staticmethod
    def calculate_trend_ema(
        df: pl.DataFrame,
        fast_period: int = 12,
        slow_period: int = 25,
        column: str = "close"
    ) -> pl.DataFrame:
        """
        Calculate trend indicator based on fast/slow EMA crossover

        Trend Logic:
        - BULLISH: Fast EMA > Slow EMA (golden cross)
        - BEARISH: Fast EMA < Slow EMA (death cross)
        - Strength measured by distance between EMAs

        Args:
            df: DataFrame with price data
            fast_period: Fast EMA period (default: 12)
            slow_period: Slow EMA period (default: 25)
            column: Column to calculate on (default: close)

        Returns:
            DataFrame with ema_12, ema_25, trend, trend_strength columns
        """
        logger.info(f"Calculating Trend EMA ({fast_period}/{slow_period})")

        # Calculate fast and slow EMAs
        df = df.with_columns([
            pl.col(column)
              .ewm_mean(span=fast_period, adjust=False)
              .alias(f"ema_{fast_period}"),
            pl.col(column)
              .ewm_mean(span=slow_period, adjust=False)
              .alias(f"ema_{slow_period}"),
        ])

        # Calculate trend direction
        df = df.with_columns([
            pl.when(pl.col(f"ema_{fast_period}") > pl.col(f"ema_{slow_period}"))
              .then(pl.lit("BULLISH"))
              .when(pl.col(f"ema_{fast_period}") < pl.col(f"ema_{slow_period}"))
              .then(pl.lit("BEARISH"))
              .otherwise(pl.lit("NEUTRAL"))
              .alias("trend")
        ])

        # Calculate trend strength as percentage distance between EMAs
        # Normalized by price to make it comparable across different price levels
        df = df.with_columns([
            (
                (pl.col(f"ema_{fast_period}") - pl.col(f"ema_{slow_period}")).abs() /
                pl.col(column)
            ).alias("trend_strength")
        ])

        logger.info("Trend EMA calculation completed")
        return df

    @staticmethod
    def calculate_all_indicators(
        df: pl.DataFrame,
        rvwap_periods: list = [7, 30, 90, 200],
        ema_periods: list = [20, 50, 100, 200],
        bb_period: int = 20,
        bb_std: float = 2.0,
        atr_period: int = 14,
    ) -> pl.DataFrame:
        """
        Calculate all indicators at once

        Args:
            df: DataFrame with OHLCV data
            rvwap_periods: List of Rolling VWAP periods (default: 7, 30, 90, 200 days)
            ema_periods: List of EMA periods to calculate (default: 20, 50, 100, 200)
            bb_period: Bollinger Bands period
            bb_std: Bollinger Bands standard deviation
            atr_period: ATR period

        Returns:
            DataFrame with all indicator columns
        """
        logger.info("Calculating all technical indicators")

        # VWAP
        df = TechnicalIndicators.calculate_vwap(df)

        # Multiple RVWAPs
        for period in rvwap_periods:
            df = df.with_columns([
                # Typical price
                ((pl.col("high") + pl.col("low") + pl.col("close")) / 3).alias("typical_price")
            ])
            df = df.with_columns([
                # RVWAP for this period
                (
                    (pl.col("typical_price") * pl.col("volume")).rolling_sum(period) /
                    pl.col("volume").rolling_sum(period)
                ).alias(f"rvwap_{period}")
            ])

        # Remove temporary typical_price column
        if "typical_price" in df.columns:
            df = df.drop("typical_price")

        # Multiple EMAs
        for period in ema_periods:
            df = TechnicalIndicators.calculate_ema(df, period=period)

        # Bollinger Bands
        df = TechnicalIndicators.calculate_bollinger_bands(
            df, period=bb_period, std_dev=bb_std
        )

        # ATR
        df = TechnicalIndicators.calculate_atr(df, period=atr_period)

        logger.info("All indicators calculated successfully")
        return df
