"""
Unified Historical Data Loader

Loads historical data from Databento DBN files into the database:
1. OHLCV data from ohlcv-1m.dbn.zst files
2. MBP-1 data from mbp-1.dbn.zst files (for orderflow metrics)

Usage:
    python scripts/data/load_historical_data.py --ohlcv path/to/ohlcv.dbn.zst
    python scripts/data/load_historical_data.py --mbp path/to/mbp1.dbn.zst
    python scripts/data/load_historical_data.py --ohlcv ohlcv.dbn.zst --mbp mbp1.dbn.zst
"""
import sys
import argparse
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import databento as db
import polars as pl
from app.data.storage import DuckDBStorage
from config import get_config

SYMBOL = "MNQ"
TIMEFRAMES = ["5M", "15M", "1H", "4H", "1D"]

# CVD rolling window sizes (bars) - from config or defaults
CVD_WINDOWS = {
    "5M": 288,   # 24 hours
    "15M": 96,   # 24 hours
    "1H": 24,    # 24 hours
    "4H": 30,    # 5 days
    "1D": 5,     # 5 days
}

# CME session start offset (18:00 ET = 23:00 UTC, we use 6 hour offset for simplicity)
CME_SESSION_OFFSET_HOURS = 6


def load_cvd_windows():
    """Load CVD window sizes from config"""
    global CVD_WINDOWS
    try:
        config = get_config()
        if hasattr(config, 'regime') and hasattr(config.regime, 'cvd_windows'):
            CVD_WINDOWS = config.regime.cvd_windows
    except Exception:
        pass  # Use defaults if config unavailable


def load_ohlcv_from_dbn(file_path: Path) -> pl.DataFrame:
    """Load OHLCV data from DBN file and build continuous contract"""
    print(f"\nLoading OHLCV from {file_path.name}...")
    print(f"  File size: {file_path.stat().st_size / 1024 / 1024 / 1024:.2f} GB")

    store = db.DBNStore.from_file(str(file_path))
    df = store.to_df()
    print(f"  Records loaded: {len(df):,}")

    # Reset index to make timestamp a column
    if hasattr(df, 'index'):
        df = df.reset_index()
        if 'index' in df.columns:
            df = df.rename(columns={'index': 'ts_event'})

    # Convert to Polars
    if not isinstance(df, pl.DataFrame):
        df = pl.from_pandas(df)

    # Filter out spread contracts
    df = df.filter(~pl.col("symbol").str.contains("-"))
    print(f"  Outright contracts: {len(df):,}")

    # Build continuous contract using daily volume leader
    df = df.with_columns([
        pl.col("ts_event").dt.truncate("1d").alias("date")
    ])

    daily_volume = df.group_by(["date", "symbol"]).agg([
        pl.col("volume").sum().alias("daily_volume")
    ])

    front_month = daily_volume.group_by("date").agg([
        pl.all().sort_by("daily_volume", descending=True).first()
    ]).select(["date", "symbol"]).rename({"symbol": "front_symbol"})

    df = df.join(front_month, on="date", how="left")
    df = df.filter(pl.col("symbol") == pl.col("front_symbol"))

    df_result = df.select(["ts_event", "open", "high", "low", "close", "volume"])
    df_result = df_result.sort("ts_event")

    print(f"  Continuous contract: {len(df_result):,} bars")
    return df_result


def filter_ohlcv_data(df: pl.DataFrame) -> pl.DataFrame:
    """Filter out settlement artifacts and invalid data"""
    print("Filtering invalid data...")
    bars_before = len(df)

    df = df.filter(
        (pl.col("open") >= 10000) & (pl.col("open") <= 50000) &
        (pl.col("high") >= 10000) & (pl.col("high") <= 50000) &
        (pl.col("low") >= 10000) & (pl.col("low") <= 50000) &
        (pl.col("close") >= 10000) & (pl.col("close") <= 50000) &
        (((pl.col("high") - pl.col("low")) / pl.col("close")) < 0.01) &  # 1% max for 1-min bars
        (pl.col("low") <= pl.col("open")) &
        (pl.col("low") <= pl.col("close")) &
        (pl.col("high") >= pl.col("open")) &
        (pl.col("high") >= pl.col("close"))
    )

    bars_filtered = bars_before - len(df)
    print(f"  Filtered {bars_filtered:,} invalid bars")
    print(f"  Clean bars: {len(df):,}")
    return df


def resample_to_timeframe(df: pl.DataFrame, timeframe: str) -> pl.DataFrame:
    """Resample 1-minute OHLCV to higher timeframe"""
    timeframe_map = {"5M": "5m", "15M": "15m", "1H": "1h", "4H": "4h", "1D": "1d"}
    duration = timeframe_map[timeframe]

    df_resampled = df.group_by_dynamic(
        "ts_event", every=duration, closed="left", label="left"
    ).agg([
        pl.col("open").first(),
        pl.col("high").max(),
        pl.col("low").min(),
        pl.col("close").last(),
        pl.col("volume").sum(),
    ])

    df_resampled = df_resampled.drop_nulls()

    # Post-resample filter: only remove extreme outliers (>3%)
    # OHLCV data from DBN is already validated, don't filter legitimate volatility
    df_resampled = df_resampled.filter(
        ((pl.col("high") - pl.col("low")) / pl.col("close") < 0.03)
    )

    return df_resampled


def load_mbp1_from_dbn(file_path: Path) -> pl.DataFrame:
    """Load MBP-1 data from DBN file"""
    print(f"\nLoading MBP-1 from {file_path.name}...")
    print(f"  File size: {file_path.stat().st_size / 1024 / 1024:.1f} MB")

    store = db.DBNStore.from_file(str(file_path))
    df = store.to_df()
    print(f"  Records loaded: {len(df):,}")

    # Reset index
    if hasattr(df, 'index'):
        df = df.reset_index()
        if 'index' in df.columns:
            df = df.rename(columns={'index': 'ts_event'})

    # Convert to Polars
    if not isinstance(df, pl.DataFrame):
        df = pl.from_pandas(df)

    return df


def process_mbp1_to_ticks(df: pl.DataFrame) -> pl.DataFrame:
    """Process MBP-1 DataFrame to mbp_ticks format"""
    print("Processing MBP-1 data...")

    # Calculate mid price
    df = df.with_columns([
        ((pl.col("bid_px_00") + pl.col("ask_px_00")) / 2).alias("mid_price"),
        (pl.col("ask_px_00") - pl.col("bid_px_00")).alias("spread"),
    ])

    # Calculate delta from size changes
    df = df.with_columns([
        (pl.col("bid_sz_00") - pl.col("bid_sz_00").shift(1)).fill_null(0).alias("bid_change"),
        (pl.col("ask_sz_00") - pl.col("ask_sz_00").shift(1)).fill_null(0).alias("ask_change"),
    ])

    # Delta: negative ask change = buy, negative bid change = sell
    df = df.with_columns([
        (
            pl.when(pl.col("ask_change") < 0).then(-pl.col("ask_change")).otherwise(0) -
            pl.when(pl.col("bid_change") < 0).then(-pl.col("bid_change")).otherwise(0)
        ).cast(pl.Int64).alias("delta")
    ])

    # Calculate DOM imbalance
    df = df.with_columns([
        (pl.col("bid_sz_00") / (pl.col("bid_sz_00") + pl.col("ask_sz_00"))).alias("dom_imbalance")
    ])

    # CVD (cumulative delta)
    df = df.with_columns([
        pl.col("delta").cum_sum().alias("cvd")
    ])

    # Select final columns for mbp_ticks table
    df_ticks = df.select([
        pl.col("ts_event").alias("timestamp"),
        pl.lit(SYMBOL).alias("symbol"),
        pl.col("mid_price"),
        pl.col("bid_px_00").alias("bid_price"),
        pl.col("ask_px_00").alias("ask_price"),
        pl.col("spread"),
        pl.col("bid_sz_00").alias("bid_size"),
        pl.col("ask_sz_00").alias("ask_size"),
        pl.col("bid_sz_00").alias("total_bid_depth"),
        pl.col("ask_sz_00").alias("total_ask_depth"),
        pl.col("dom_imbalance"),
        pl.col("delta"),
        pl.col("cvd"),
    ])

    print(f"  Processed {len(df_ticks):,} ticks")
    return df_ticks


def ensure_ohlcv_table(storage):
    """Ensure ohlcv_ticks table exists with proper schema"""
    storage.conn.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv_ticks (
            timestamp TIMESTAMP,
            symbol VARCHAR,
            timeframe VARCHAR,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume BIGINT,
            instant_delta BIGINT,
            dom_imbalance DOUBLE,
            total_bid_depth DOUBLE,
            total_ask_depth DOUBLE,
            cvd BIGINT,
            PRIMARY KEY (symbol, timeframe, timestamp)
        )
    """)


def ensure_mbp_table(storage):
    """Ensure mbp_ticks table exists with proper schema"""
    storage.conn.execute("""
        CREATE TABLE IF NOT EXISTS mbp_ticks (
            timestamp TIMESTAMP,
            symbol VARCHAR,
            mid_price DOUBLE,
            bid_price DOUBLE,
            ask_price DOUBLE,
            spread DOUBLE,
            bid_size INTEGER,
            ask_size INTEGER,
            total_bid_depth BIGINT,
            total_ask_depth BIGINT,
            dom_imbalance DOUBLE,
            delta BIGINT,
            cvd BIGINT
        )
    """)


def insert_ohlcv_data(storage, df: pl.DataFrame, timeframe: str, with_orderflow: bool = False):
    """Insert OHLCV data into ohlcv_ticks table"""
    if with_orderflow:
        df_insert = df
    else:
        df_insert = df.select([
            pl.col("ts_event").alias("timestamp"),
            pl.lit(SYMBOL).alias("symbol"),
            pl.lit(timeframe).alias("timeframe"),
            pl.col("open"),
            pl.col("high"),
            pl.col("low"),
            pl.col("close"),
            pl.col("volume"),
            pl.lit(None).cast(pl.Int64).alias("instant_delta"),
            pl.lit(None).cast(pl.Float64).alias("dom_imbalance"),
            pl.lit(None).cast(pl.Float64).alias("total_bid_depth"),
            pl.lit(None).cast(pl.Float64).alias("total_ask_depth"),
            pl.lit(None).cast(pl.Int64).alias("cvd"),
        ])

    storage.conn.execute("INSERT OR REPLACE INTO ohlcv_ticks SELECT * FROM df_insert")


def insert_mbp_ticks(storage, df: pl.DataFrame):
    """Insert MBP ticks into mbp_ticks table"""
    storage.conn.execute("INSERT INTO mbp_ticks SELECT * FROM df")


def aggregate_mbp_to_ohlcv(storage):
    """Aggregate mbp_ticks to ohlcv_ticks with orderflow metrics"""
    print("\nAggregating MBP data to OHLCV bars...")

    # Load CVD windows from config
    load_cvd_windows()

    timeframe_intervals = {
        "5M": "5 minutes",
        "15M": "15 minutes",
        "1H": "1 hour",
        "4H": "4 hours",
        "1D": "1 day",
    }

    # Get MBP date range
    date_range = storage.conn.execute("""
        SELECT MIN(timestamp), MAX(timestamp) FROM mbp_ticks WHERE symbol = 'MNQ'
    """).fetchone()

    if not date_range[0]:
        print("  No MBP data to aggregate")
        return

    print(f"  MBP data range: {date_range[0]} to {date_range[1]}")

    for tf, interval in timeframe_intervals.items():
        cvd_window = CVD_WINDOWS.get(tf, 100)
        print(f"  Processing {tf} (CVD window: {cvd_window} bars)...")

        # Delete existing bars in MBP date range
        storage.conn.execute(f"""
            DELETE FROM ohlcv_ticks
            WHERE symbol = 'MNQ' AND timeframe = '{tf}'
            AND timestamp >= (
                SELECT time_bucket(INTERVAL '{interval}', MIN(timestamp))
                FROM mbp_ticks WHERE symbol = 'MNQ'
            )
        """)

        # For 4H and 1D, use CME session boundaries (18:00 ET)
        # Shift timestamps by 6 hours, bucket, then shift back
        if tf in ("4H", "1D"):
            # CME session boundary alignment
            # Two-pass approach: first compute median, then filter outliers and aggregate
            storage.conn.execute(f"""
                INSERT OR REPLACE INTO ohlcv_ticks
                WITH shifted AS (
                    SELECT
                        timestamp + INTERVAL '{CME_SESSION_OFFSET_HOURS} hours' as shifted_ts,
                        mid_price, delta, dom_imbalance, total_bid_depth, total_ask_depth
                    FROM mbp_ticks
                    WHERE symbol = 'MNQ'
                    AND spread / mid_price < 0.005
                    AND mid_price > 10000 AND mid_price < 50000
                ),
                medians AS (
                    SELECT
                        time_bucket(INTERVAL '{interval}', shifted_ts) as bucket,
                        MEDIAN(mid_price) as median_price
                    FROM shifted
                    GROUP BY time_bucket(INTERVAL '{interval}', shifted_ts)
                ),
                filtered AS (
                    SELECT s.*, med.median_price
                    FROM shifted s
                    JOIN medians med ON time_bucket(INTERVAL '{interval}', s.shifted_ts) = med.bucket
                    WHERE ABS(s.mid_price - med.median_price) / med.median_price < 0.005
                ),
                bars AS (
                    SELECT
                        time_bucket(INTERVAL '{interval}', shifted_ts) - INTERVAL '{CME_SESSION_OFFSET_HOURS} hours' as timestamp,
                        'MNQ' as symbol,
                        '{tf}' as timeframe,
                        FIRST(mid_price) as open,
                        MAX(mid_price) as high,
                        MIN(mid_price) as low,
                        LAST(mid_price) as close,
                        COUNT(*) as volume,
                        SUM(delta) as instant_delta,
                        AVG(dom_imbalance) as dom_imbalance,
                        AVG(total_bid_depth) as total_bid_depth,
                        AVG(total_ask_depth) as total_ask_depth
                    FROM filtered
                    GROUP BY time_bucket(INTERVAL '{interval}', shifted_ts)
                    HAVING FIRST(mid_price) IS NOT NULL
                )
                SELECT
                    timestamp, symbol, timeframe, open, high, low, close, volume,
                    instant_delta, dom_imbalance, total_bid_depth, total_ask_depth,
                    SUM(instant_delta) OVER (
                        ORDER BY timestamp
                        ROWS BETWEEN {cvd_window - 1} PRECEDING AND CURRENT ROW
                    ) as cvd
                FROM bars
                ORDER BY timestamp
            """)
        else:
            # Standard time bucketing for intraday timeframes
            # Two-pass approach: first compute median, then filter outliers and aggregate
            storage.conn.execute(f"""
                INSERT OR REPLACE INTO ohlcv_ticks
                WITH medians AS (
                    -- First pass: compute median for each time bucket
                    SELECT
                        time_bucket(INTERVAL '{interval}', timestamp) as bucket,
                        MEDIAN(mid_price) as median_price
                    FROM mbp_ticks
                    WHERE symbol = 'MNQ'
                    AND spread / mid_price < 0.005
                    AND mid_price > 10000 AND mid_price < 50000
                    GROUP BY time_bucket(INTERVAL '{interval}', timestamp)
                ),
                filtered AS (
                    -- Second pass: filter quotes within 0.5% of median (removes back-month)
                    SELECT m.*, med.median_price
                    FROM mbp_ticks m
                    JOIN medians med ON time_bucket(INTERVAL '{interval}', m.timestamp) = med.bucket
                    WHERE m.symbol = 'MNQ'
                    AND m.spread / m.mid_price < 0.005
                    AND m.mid_price > 10000 AND m.mid_price < 50000
                    AND ABS(m.mid_price - med.median_price) / med.median_price < 0.005
                ),
                bars AS (
                    SELECT
                        time_bucket(INTERVAL '{interval}', timestamp) as timestamp,
                        'MNQ' as symbol,
                        '{tf}' as timeframe,
                        FIRST(mid_price) as open,
                        MAX(mid_price) as high,
                        MIN(mid_price) as low,
                        LAST(mid_price) as close,
                        COUNT(*) as volume,
                        SUM(delta) as instant_delta,
                        AVG(dom_imbalance) as dom_imbalance,
                        AVG(total_bid_depth) as total_bid_depth,
                        AVG(total_ask_depth) as total_ask_depth
                    FROM filtered
                    GROUP BY time_bucket(INTERVAL '{interval}', timestamp)
                    HAVING FIRST(mid_price) IS NOT NULL
                )
                SELECT
                    timestamp, symbol, timeframe, open, high, low, close, volume,
                    instant_delta, dom_imbalance, total_bid_depth, total_ask_depth,
                    SUM(instant_delta) OVER (
                        ORDER BY timestamp
                        ROWS BETWEEN {cvd_window - 1} PRECEDING AND CURRENT ROW
                    ) as cvd
                FROM bars
                ORDER BY timestamp
            """)

        count = storage.conn.execute(f"""
            SELECT COUNT(*) FROM ohlcv_ticks
            WHERE symbol = 'MNQ' AND timeframe = '{tf}' AND instant_delta IS NOT NULL
        """).fetchone()[0]
        print(f"    {count:,} bars with orderflow")


def print_summary(storage):
    """Print database summary"""
    print("\n" + "=" * 60)
    print("  Database Summary")
    print("=" * 60)

    try:
        result = storage.conn.execute("""
            SELECT
                timeframe,
                COUNT(*) as total_bars,
                SUM(CASE WHEN instant_delta IS NOT NULL THEN 1 ELSE 0 END) as orderflow_bars,
                MIN(timestamp) as first_bar,
                MAX(timestamp) as last_bar
            FROM ohlcv_ticks
            WHERE symbol = 'MNQ'
            GROUP BY timeframe
            ORDER BY timeframe
        """).fetchall()

        print("\nohlcv_ticks:")
        for row in result:
            tf, total, orderflow, first, last = row
            print(f"  {tf:4}: {total:,} bars ({orderflow:,} with orderflow) | {first.date()} to {last.date()}")
    except Exception as e:
        print(f"  Error reading ohlcv_ticks: {e}")

    try:
        mbp_count = storage.conn.execute("SELECT COUNT(*) FROM mbp_ticks").fetchone()[0]
        if mbp_count > 0:
            mbp_range = storage.conn.execute("""
                SELECT MIN(timestamp), MAX(timestamp) FROM mbp_ticks
            """).fetchone()
            print(f"\nmbp_ticks: {mbp_count:,} rows | {mbp_range[0]} to {mbp_range[1]}")
        else:
            print("\nmbp_ticks: empty")
    except Exception:
        print("\nmbp_ticks: table not found")


def main():
    parser = argparse.ArgumentParser(description='Load historical data from Databento DBN files')
    parser.add_argument('--ohlcv', type=str, help='Path to OHLCV DBN file (ohlcv-1m.dbn.zst)')
    parser.add_argument('--mbp', type=str, help='Path to MBP-1 DBN file (mbp-1.dbn.zst)')
    parser.add_argument('--aggregate', action='store_true', help='Aggregate mbp_ticks to ohlcv_ticks')
    parser.add_argument('--status', action='store_true', help='Show database status')

    args = parser.parse_args()

    storage = DuckDBStorage()

    try:
        # Status only
        if args.status:
            print_summary(storage)
            return

        # Aggregate only
        if args.aggregate:
            aggregate_mbp_to_ohlcv(storage)
            storage.conn.commit()
            print_summary(storage)
            return

        # Load OHLCV
        if args.ohlcv:
            ohlcv_path = Path(args.ohlcv)
            if not ohlcv_path.exists():
                print(f"[ERROR] File not found: {ohlcv_path}")
                return

            ensure_ohlcv_table(storage)

            df_1m = load_ohlcv_from_dbn(ohlcv_path)
            df_1m = filter_ohlcv_data(df_1m)

            print("\nInserting OHLCV data...")
            for tf in TIMEFRAMES:
                df_tf = resample_to_timeframe(df_1m, tf)
                insert_ohlcv_data(storage, df_tf, tf)
                print(f"  {tf}: {len(df_tf):,} bars")

            storage.conn.commit()

        # Load MBP-1
        if args.mbp:
            mbp_path = Path(args.mbp)
            if not mbp_path.exists():
                print(f"[ERROR] File not found: {mbp_path}")
                return

            ensure_mbp_table(storage)
            ensure_ohlcv_table(storage)

            df_mbp = load_mbp1_from_dbn(mbp_path)
            df_ticks = process_mbp1_to_ticks(df_mbp)

            print("\nInserting MBP ticks...")
            insert_mbp_ticks(storage, df_ticks)
            storage.conn.commit()

            # Aggregate to OHLCV
            aggregate_mbp_to_ohlcv(storage)
            storage.conn.commit()

        # Create index
        print("\nCreating index...")
        storage.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ohlcv_ticks_lookup
            ON ohlcv_ticks (symbol, timeframe, timestamp)
        """)
        storage.conn.commit()

        print_summary(storage)

    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
    finally:
        storage.close()


if __name__ == "__main__":
    main()
