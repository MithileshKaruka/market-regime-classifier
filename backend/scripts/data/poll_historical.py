"""
Historical Data Polling Service

Periodically fetches recent data from Databento Historical API (HTTPS)
to keep the database current when live streaming is unavailable.

Usage:
    # Poll once (fetch last 2 hours)
    python scripts/data/poll_historical.py

    # Run continuous polling every 5 minutes
    python scripts/data/poll_historical.py --loop --interval 300

    # Custom lookback
    python scripts/data/poll_historical.py --hours 4
"""
import sys
import argparse
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import databento as db
import polars as pl
from app.data.storage import DuckDBStorage
from config import get_secrets, get_config

DATASET = "GLBX.MDP3"
SYMBOL = "MNQ.c.0"
STYPE_IN = "continuous"


def fetch_and_update(api_key: str, hours_back: int = 2):
    """Fetch recent MBP-1 data and update OHLCV bars

    Args:
        api_key: Databento API key
        hours_back: Hours of data to fetch (default 2)
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_back)

    print(f"[{now.strftime('%H:%M:%S')}] Fetching {hours_back}h of data...")
    print(f"  Range: {start.strftime('%Y-%m-%d %H:%M')} to {now.strftime('%Y-%m-%d %H:%M')}")

    try:
        client = db.Historical(api_key)

        # Download MBP-1 data
        data = client.timeseries.get_range(
            dataset=DATASET,
            symbols=[SYMBOL],
            stype_in=STYPE_IN,
            schema="mbp-1",
            start=start.strftime('%Y-%m-%dT%H:%M:%S'),
            end=now.strftime('%Y-%m-%dT%H:%M:%S'),
        )

        df = data.to_df()
        if len(df) == 0:
            print("  No data available (market may be closed)")
            return 0

        # Reset index
        if hasattr(df, 'index'):
            df = df.reset_index()
            if 'index' in df.columns:
                df = df.rename(columns={'index': 'ts_event'})

        print(f"  Downloaded: {len(df):,} records")

        # Convert to polars and process
        df_pl = pl.from_pandas(df)
        df_processed = process_mbp_data(df_pl)

        # Aggregate to OHLCV bars and insert
        bars_inserted = aggregate_and_insert(df_processed)

        print(f"  Updated: {bars_inserted} bars")
        return bars_inserted

    except Exception as e:
        print(f"  Error: {e}")
        return 0


def process_mbp_data(df: pl.DataFrame) -> pl.DataFrame:
    """Process MBP-1 data to calculate metrics"""

    # Cast size columns
    df = df.with_columns([
        pl.col("bid_sz_00").cast(pl.Int64),
        pl.col("ask_sz_00").cast(pl.Int64),
    ])

    # Calculate mid price and spread
    df = df.with_columns([
        ((pl.col("bid_px_00") + pl.col("ask_px_00")) / 2).alias("mid_price"),
        (pl.col("ask_px_00") - pl.col("bid_px_00")).alias("spread"),
    ])

    # Filter bad quotes: wide spreads or prices out of range
    df = df.filter(
        (pl.col("spread") / pl.col("mid_price") < 0.005) &  # Spread < 0.5%
        (pl.col("mid_price") > 10000) &
        (pl.col("mid_price") < 50000) &
        (pl.col("bid_px_00") > 0) &
        (pl.col("ask_px_00") > 0)
    )

    # Calculate delta from size changes
    df = df.with_columns([
        (pl.col("bid_sz_00") - pl.col("bid_sz_00").shift(1)).fill_null(0).alias("bid_change"),
        (pl.col("ask_sz_00") - pl.col("ask_sz_00").shift(1)).fill_null(0).alias("ask_change"),
    ])

    df = df.with_columns([
        (
            pl.when(pl.col("ask_change") < 0).then(-pl.col("ask_change")).otherwise(0) -
            pl.when(pl.col("bid_change") < 0).then(-pl.col("bid_change")).otherwise(0)
        ).alias("delta")
    ])

    # Calculate DOM imbalance
    df = df.with_columns([
        (pl.col("bid_sz_00") / (pl.col("bid_sz_00") + pl.col("ask_sz_00"))).alias("dom_imbalance")
    ])

    return df.select([
        pl.col("ts_event").alias("timestamp"),
        "mid_price",
        "delta",
        "dom_imbalance",
        pl.col("bid_sz_00").alias("bid_size"),
        pl.col("ask_sz_00").alias("ask_size"),
    ])


def aggregate_and_insert(df: pl.DataFrame) -> int:
    """Aggregate to OHLCV bars and insert into database"""

    timeframes = {
        "5M": "5m",
        "15M": "15m",
        "1H": "1h",
        "4H": "4h",
    }

    total_bars = 0

    with DuckDBStorage() as storage:
        for tf, duration in timeframes.items():
            # Two-pass approach: compute median per bar, filter outliers, then aggregate
            # First, compute median for each time bucket
            df_with_bucket = df.with_columns([
                pl.col("timestamp").dt.truncate(duration).alias("bucket")
            ])

            medians = df_with_bucket.group_by("bucket").agg([
                pl.col("mid_price").median().alias("median_price")
            ])

            # Join and filter quotes within 0.5% of median (removes back-month quotes)
            df_filtered = df_with_bucket.join(medians, on="bucket", how="left").filter(
                (pl.col("mid_price") - pl.col("median_price")).abs() / pl.col("median_price") < 0.005
            )

            # Now aggregate the filtered data
            df_agg = df_filtered.group_by_dynamic(
                "timestamp", every=duration, closed="left", label="left"
            ).agg([
                pl.col("mid_price").first().alias("open"),
                pl.col("mid_price").max().alias("high"),
                pl.col("mid_price").min().alias("low"),
                pl.col("mid_price").last().alias("close"),
                pl.len().alias("volume"),
                pl.col("delta").sum().alias("instant_delta"),
                pl.col("dom_imbalance").mean().alias("dom_imbalance"),
                pl.col("bid_size").mean().cast(pl.Float64).alias("total_bid_depth"),
                pl.col("ask_size").mean().cast(pl.Float64).alias("total_ask_depth"),
            ]).with_columns([
                pl.lit("MNQ").alias("symbol"),
                pl.lit(tf).alias("timeframe"),
                pl.lit(0).cast(pl.Int64).alias("cvd"),
            ])

            # Post-aggregation filter: remove bars with >2% range
            df_agg = df_agg.filter(
                ((pl.col("high") - pl.col("low")) / pl.col("close") < 0.015)
            )

            # Reorder columns
            df_insert = df_agg.select([
                "timestamp", "symbol", "timeframe", "open", "high", "low", "close",
                "volume", "instant_delta", "dom_imbalance", "total_bid_depth",
                "total_ask_depth", "cvd"
            ])

            if len(df_insert) > 0:
                storage.conn.execute("INSERT OR REPLACE INTO ohlcv_ticks SELECT * FROM df_insert")
                total_bars += len(df_insert)

        storage.conn.commit()

        # Update rolling CVD
        update_rolling_cvd(storage)

    return total_bars


def update_rolling_cvd(storage):
    """Update rolling CVD for recent bars"""
    config = get_config()
    cvd_windows = config.regime.cvd_windows

    for tf, window in cvd_windows.items():
        storage.conn.execute(f"""
            UPDATE ohlcv_ticks AS t
            SET cvd = (
                SELECT SUM(instant_delta)
                FROM (
                    SELECT instant_delta
                    FROM ohlcv_ticks
                    WHERE symbol = t.symbol AND timeframe = t.timeframe
                    AND timestamp <= t.timestamp
                    ORDER BY timestamp DESC
                    LIMIT {window}
                )
            )
            WHERE symbol = 'MNQ' AND timeframe = '{tf}'
            AND timestamp >= (
                SELECT MAX(timestamp) - INTERVAL '1 day'
                FROM ohlcv_ticks
                WHERE symbol = 'MNQ' AND timeframe = '{tf}'
            )
        """)


def main():
    parser = argparse.ArgumentParser(description='Poll historical data to keep database current')
    parser.add_argument('--hours', type=int, default=2, help='Hours of data to fetch (default: 2)')
    parser.add_argument('--loop', action='store_true', help='Run continuously')
    parser.add_argument('--interval', type=int, default=300, help='Seconds between polls (default: 300)')

    args = parser.parse_args()

    # Get API key
    try:
        secrets = get_secrets()
        api_key = secrets.api_key
        if not api_key:
            raise ValueError("API key is empty")
    except Exception as e:
        print(f"[ERROR] Could not load API key: {e}")
        return

    print("=" * 50)
    print("  Historical Data Polling Service")
    print("=" * 50)
    print(f"  Lookback: {args.hours} hours")
    print(f"  Mode: {'Continuous' if args.loop else 'Single poll'}")
    if args.loop:
        print(f"  Interval: {args.interval} seconds")
    print("=" * 50)

    if args.loop:
        print("\nStarting continuous polling (Ctrl+C to stop)...\n")
        try:
            while True:
                fetch_and_update(api_key, args.hours)
                print(f"  Next poll in {args.interval}s...\n")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        fetch_and_update(api_key, args.hours)


if __name__ == "__main__":
    main()
