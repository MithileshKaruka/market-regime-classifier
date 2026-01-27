"""
Support and Resistance Level Detection

This module identifies key support and resistance levels using:
1. Swing highs and lows
2. Volume profile (high volume nodes)
3. Multiple touches (levels tested repeatedly)
"""

import polars as pl
import logging
from typing import List, Dict, Optional

from config import get_config

logger = logging.getLogger(__name__)


class SupportResistanceDetector:
    """Detect support and resistance levels from OHLC data

    All parameters can be overridden, but defaults are loaded from config.
    """

    def __init__(self, price_tolerance: Optional[float] = None):
        """
        Initialize the detector

        Args:
            price_tolerance: Percentage tolerance for clustering levels
                            (default from config)
        """
        # Load defaults from config
        config = get_config()
        sr_config = config.support_resistance

        self.price_tolerance = price_tolerance or sr_config.proximity_pct
        self._min_touches = sr_config.min_touches
        self._swing_window = sr_config.swing_window
        self._volume_bins = sr_config.volume_profile_bins
        self._volume_top_n = sr_config.volume_profile_top_n

    def find_swing_points(self, df: pl.DataFrame, window: int = 5) -> pl.DataFrame:
        """
        Find swing highs and swing lows

        A swing high is a high that is higher than N bars before and after
        A swing low is a low that is lower than N bars before and after

        Args:
            df: DataFrame with OHLC data
            window: Number of bars to look before/after for comparison

        Returns:
            DataFrame with swing_high and swing_low boolean columns
        """
        logger.info(f"Finding swing points with window={window}")

        # Find local maxima (swing highs)
        df = df.with_columns([
            (
                (pl.col("high") == pl.col("high").rolling_max(window * 2 + 1, center=True)) &
                (pl.col("high") > pl.col("high").shift(window)) &
                (pl.col("high") > pl.col("high").shift(-window))
            ).alias("swing_high"),
        ])

        # Find local minima (swing lows)
        df = df.with_columns([
            (
                (pl.col("low") == pl.col("low").rolling_min(window * 2 + 1, center=True)) &
                (pl.col("low") < pl.col("low").shift(window)) &
                (pl.col("low") < pl.col("low").shift(-window))
            ).alias("swing_low"),
        ])

        swing_high_count = df.filter(pl.col("swing_high")).shape[0]
        swing_low_count = df.filter(pl.col("swing_low")).shape[0]
        logger.info(f"Found {swing_high_count} swing highs and {swing_low_count} swing lows")

        return df

    def cluster_levels(self, levels: List[float], tolerance: float) -> List[Dict]:
        """
        Cluster nearby price levels together

        Args:
            levels: List of price levels
            tolerance: Percentage tolerance for clustering

        Returns:
            List of clustered levels with touch counts
        """
        if not levels:
            return []

        sorted_levels = sorted(levels)
        clusters = []
        current_cluster = [sorted_levels[0]]

        for level in sorted_levels[1:]:
            # Check if this level is within tolerance of the cluster average
            cluster_avg = sum(current_cluster) / len(current_cluster)
            if abs(level - cluster_avg) / cluster_avg <= tolerance:
                current_cluster.append(level)
            else:
                # Start new cluster
                clusters.append({
                    "price": sum(current_cluster) / len(current_cluster),
                    "touches": len(current_cluster),
                })
                current_cluster = [level]

        # Add last cluster
        if current_cluster:
            clusters.append({
                "price": sum(current_cluster) / len(current_cluster),
                "touches": len(current_cluster),
            })

        return clusters

    def cluster_levels_with_timestamps(
        self, levels: List[float], timestamps: List, tolerance: float
    ) -> List[Dict]:
        """
        Cluster nearby price levels together, tracking the most recent timestamp

        Args:
            levels: List of price levels
            timestamps: List of timestamps corresponding to each level
            tolerance: Percentage tolerance for clustering

        Returns:
            List of clustered levels with touch counts and last_seen timestamp
        """
        if not levels:
            return []

        # Sort by price, keeping timestamps aligned
        sorted_pairs = sorted(zip(levels, timestamps), key=lambda x: x[0])
        sorted_levels = [p[0] for p in sorted_pairs]
        sorted_timestamps = [p[1] for p in sorted_pairs]

        clusters = []
        current_cluster_prices = [sorted_levels[0]]
        current_cluster_timestamps = [sorted_timestamps[0]]

        for i in range(1, len(sorted_levels)):
            level = sorted_levels[i]
            timestamp = sorted_timestamps[i]

            # Check if this level is within tolerance of the cluster average
            cluster_avg = sum(current_cluster_prices) / len(current_cluster_prices)
            if abs(level - cluster_avg) / cluster_avg <= tolerance:
                current_cluster_prices.append(level)
                current_cluster_timestamps.append(timestamp)
            else:
                # Start new cluster - save current one with most recent timestamp
                clusters.append({
                    "price": sum(current_cluster_prices) / len(current_cluster_prices),
                    "touches": len(current_cluster_prices),
                    "last_seen": max(current_cluster_timestamps),
                })
                current_cluster_prices = [level]
                current_cluster_timestamps = [timestamp]

        # Add last cluster
        if current_cluster_prices:
            clusters.append({
                "price": sum(current_cluster_prices) / len(current_cluster_prices),
                "touches": len(current_cluster_prices),
                "last_seen": max(current_cluster_timestamps),
            })

        return clusters

    def identify_levels(
        self,
        df: pl.DataFrame,
        min_touches: Optional[int] = None,
        swing_window: Optional[int] = None,
    ) -> Dict:
        """
        Identify support and resistance levels

        Args:
            df: DataFrame with OHLC data (must have timestamp, open, high, low, close, volume)
            min_touches: Minimum number of touches for a level to be significant
            swing_window: Window size for swing point detection

        Returns:
            Dictionary with resistance and support levels (includes last_seen timestamp)
        """
        # Use config defaults if not provided
        min_touches = min_touches or self._min_touches
        swing_window = swing_window or self._swing_window

        logger.info(f"Identifying S/R levels from {len(df)} bars")

        # Find swing points
        df = self.find_swing_points(df, window=swing_window)

        # Extract swing high prices with timestamps (resistance candidates)
        resistance_df = df.filter(pl.col("swing_high")).select(["high", "timestamp"])
        resistance_levels = resistance_df["high"].to_list()
        resistance_timestamps = resistance_df["timestamp"].to_list()

        # Extract swing low prices with timestamps (support candidates)
        support_df = df.filter(pl.col("swing_low")).select(["low", "timestamp"])
        support_levels = support_df["low"].to_list()
        support_timestamps = support_df["timestamp"].to_list()

        # Cluster levels with timestamps
        resistance_clusters = self.cluster_levels_with_timestamps(
            resistance_levels, resistance_timestamps, self.price_tolerance
        )
        support_clusters = self.cluster_levels_with_timestamps(
            support_levels, support_timestamps, self.price_tolerance
        )

        # Filter by minimum touches
        significant_resistance = [
            level for level in resistance_clusters
            if level["touches"] >= min_touches
        ]
        significant_support = [
            level for level in support_clusters
            if level["touches"] >= min_touches
        ]

        logger.info(
            f"Found {len(significant_resistance)} resistance levels "
            f"and {len(significant_support)} support levels"
        )

        return {
            "resistance": sorted(significant_resistance, key=lambda x: x["price"], reverse=True),
            "support": sorted(significant_support, key=lambda x: x["price"]),
        }

    def add_volume_profile_levels(
        self,
        df: pl.DataFrame,
        levels: Dict,
        num_bins: Optional[int] = None,
        top_n: Optional[int] = None,
    ) -> Dict:
        """
        Add high volume nodes as additional S/R levels

        Args:
            df: DataFrame with OHLC and volume data
            levels: Existing S/R levels dictionary
            num_bins: Number of price bins for volume profile (default from config)
            top_n: Number of top volume nodes to include (default from config)

        Returns:
            Updated levels dictionary with volume profile levels
        """
        # Use config defaults if not provided
        num_bins = num_bins or self._volume_bins
        top_n = top_n or self._volume_top_n

        logger.info("Adding volume profile levels")

        # Calculate price range
        min_price = df["low"].min()
        max_price = df["high"].max()
        bin_size = (max_price - min_price) / num_bins

        # Create bins and aggregate volume
        df = df.with_columns([
            ((pl.col("close") - min_price) / bin_size).cast(pl.Int32).alias("price_bin")
        ])

        volume_profile = (
            df.group_by("price_bin")
            .agg([
                pl.col("volume").sum().alias("total_volume"),
                pl.col("close").mean().alias("avg_price"),
            ])
            .sort("total_volume", descending=True)
            .head(top_n)
        )

        # Add as "volume" type levels
        volume_levels = []
        for row in volume_profile.iter_rows(named=True):
            volume_levels.append({
                "price": row["avg_price"],
                "touches": 0,  # Volume-based, not touch-based
                "volume": row["total_volume"],
                "type": "volume_node",
            })

        levels["volume_nodes"] = volume_levels
        logger.info(f"Added {len(volume_levels)} volume profile levels")

        return levels
