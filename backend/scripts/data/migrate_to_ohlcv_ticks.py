"""
Migration script: Consolidate to ohlcv_ticks as single source of truth

Steps:
1. Recreate ohlcv_ticks table with proper schema
2. Load 5yr OHLCV from Databento (NULL orderflow columns)
3. Overlay recent data from mbp_ticks (full orderflow + cvd)
4. Drop order_book table
5. Truncate trades table
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import databento as db
import polars as pl
from datetime import datetime
from app.data.storage import DuckDBStorage

# Configuration
OHLCV_FILE = Path("C:/Users/jthlbg2/market-regime-classifier/backend/data/glbx-mdp3-20210116-20260115.ohlcv-1m.dbn.zst")
SYMBOL = "MNQ"
TIMEFRAMES = ["5M", "15M", "1H", "4H", "1D"]


def load_ohlcv_from_dbn(file_path: Path) -> pl.DataFrame:
    """Load OHLCV data from DBN file and build continuous contract"""
    print(f"\n[1/6] Loading OHLCV data from {file_path.name}...")
    print(f"      File size: {file_path.stat().st_size / 1024 / 1024 / 1024:.2f} GB")

    store = db.DBNStore.from_file(str(file_path))
    df = store.to_df()
    print(f"      Records loaded: {len(df):,}")

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
    print(f"      Outright contracts: {len(df):,}")

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

    print(f"      Continuous contract: {len(df_result):,} bars")
    return df_result


def filter_ohlcv_data(df: pl.DataFrame) -> pl.DataFrame:
    """Filter out settlement artifacts and invalid data"""
    print(f"\n[2/6] Filtering invalid data...")
    bars_before = len(df)

    df = df.filter(
        (pl.col("open") >= 10000) & (pl.col("open") <= 30000) &
        (pl.col("high") >= 10000) & (pl.col("high") <= 30000) &
        (pl.col("low") >= 10000) & (pl.col("low") <= 30000) &
        (pl.col("close") >= 10000) & (pl.col("close") <= 30000) &
        (((pl.col("high") - pl.col("low")) / pl.col("close")) < 0.02) &
        (pl.col("low") <= pl.col("open")) &
        (pl.col("low") <= pl.col("close")) &
        (pl.col("high") >= pl.col("open")) &
        (pl.col("high") >= pl.col("close"))
    )

    bars_filtered = bars_before - len(df)
    print(f"      Filtered {bars_filtered:,} invalid bars")
    print(f"      Clean bars: {len(df):,}")
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

    return df_resampled.drop_nulls()


def recreate_ohlcv_ticks_table(storage):
    """Recreate ohlcv_ticks table with proper schema"""
    print(f"\n[3/6] Recreating ohlcv_ticks table...")

    storage.conn.execute("DROP TABLE IF EXISTS ohlcv_ticks")

    storage.conn.execute("""
        CREATE TABLE ohlcv_ticks (
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
    print("      Table created")


def insert_ohlcv_data(storage, df: pl.DataFrame, timeframe: str):
    """Insert OHLCV data with NULL orderflow columns"""
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

    storage.conn.execute("INSERT INTO ohlcv_ticks SELECT * FROM df_insert")


def overlay_mbp_data(storage):
    """Overlay recent mbp_ticks data with full orderflow metrics"""
    print(f"\n[5/6] Overlaying MBP-1 data for recent bars...")

    # Check mbp_ticks exists and has data
    try:
        count = storage.conn.execute("SELECT COUNT(*) FROM mbp_ticks").fetchone()[0]
        if count == 0:
            print("      No mbp_ticks data, skipping overlay")
            return
        print(f"      mbp_ticks has {count:,} rows")
    except Exception:
        print("      mbp_ticks table not found, skipping overlay")
        return

    # Get date range of mbp_ticks
    date_range = storage.conn.execute("""
        SELECT MIN(timestamp), MAX(timestamp) FROM mbp_ticks WHERE symbol = 'MNQ'
    """).fetchone()
    print(f"      MBP data range: {date_range[0]} to {date_range[1]}")

    timeframe_intervals = {
        "5M": "5 minutes",
        "15M": "15 minutes",
        "1H": "1 hour",
        "4H": "4 hours",
        "1D": "1 day",
    }

    for tf, interval in timeframe_intervals.items():
        print(f"      Processing {tf}...")

        # Delete existing bars in mbp_ticks date range
        # Use time_bucket on min timestamp to get the correct bar boundary
        storage.conn.execute(f"""
            DELETE FROM ohlcv_ticks
            WHERE symbol = 'MNQ' AND timeframe = '{tf}'
            AND timestamp >= (
                SELECT time_bucket(INTERVAL '{interval}', MIN(timestamp))
                FROM mbp_ticks WHERE symbol = 'MNQ'
            )
        """)

        # Aggregate mbp_ticks with CVD calculation
        storage.conn.execute(f"""
            INSERT INTO ohlcv_ticks
            WITH bars AS (
                SELECT
                    time_bucket(INTERVAL '{interval}', timestamp) as timestamp,
                    'MNQ' as symbol,
                    '{tf}' as timeframe,
                    FIRST(mid_price) as open,
                    MAX(mid_price) as high,
                    MIN(mid_price) as low,
                    LAST(mid_price) as close,
                    COUNT(*) as volume,
                    SUM(CASE WHEN delta > 2147483647 THEN CAST(delta AS BIGINT) - 4294967296 ELSE delta END) as instant_delta,
                    AVG(dom_imbalance) as dom_imbalance,
                    AVG(total_bid_depth) as total_bid_depth,
                    AVG(total_ask_depth) as total_ask_depth
                FROM mbp_ticks
                WHERE symbol = 'MNQ'
                GROUP BY time_bucket(INTERVAL '{interval}', timestamp)
                HAVING FIRST(mid_price) IS NOT NULL
            )
            SELECT
                timestamp, symbol, timeframe, open, high, low, close, volume,
                instant_delta, dom_imbalance, total_bid_depth, total_ask_depth,
                SUM(instant_delta) OVER (ORDER BY timestamp) as cvd
            FROM bars
            ORDER BY timestamp
        """)

        count = storage.conn.execute(f"""
            SELECT COUNT(*) FROM ohlcv_ticks
            WHERE symbol = 'MNQ' AND timeframe = '{tf}' AND instant_delta IS NOT NULL
        """).fetchone()[0]
        print(f"        Inserted {count} bars with orderflow")


def cleanup_tables(storage):
    """Drop order_book and truncate trades"""
    print(f"\n[6/6] Cleaning up old tables...")

    # Drop order_book
    try:
        storage.conn.execute("DROP TABLE IF EXISTS order_book")
        print("      Dropped order_book table")
    except Exception as e:
        print(f"      Error dropping order_book: {e}")

    # Truncate trades (keep structure)
    try:
        storage.conn.execute("DELETE FROM trades")
        print("      Truncated trades table")
    except Exception as e:
        print(f"      Error truncating trades: {e}")


def print_summary(storage):
    """Print summary of migration"""
    print(f"\n{'='*70}")
    print("  Migration Complete!")
    print(f"{'='*70}")

    print("\nohlcv_ticks summary:")
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

    for row in result:
        tf, total, orderflow, first, last = row
        print(f"  {tf:4}: {total:,} bars ({orderflow:,} with orderflow) | {first.date()} to {last.date()}")

    print("\nRemaining tables:")
    tables = storage.conn.execute("SHOW TABLES").fetchall()
    for t in tables:
        count = storage.conn.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
        print(f"  {t[0]}: {count:,} rows")


def main():
    print("=" * 70)
    print("  Migration: Consolidate to ohlcv_ticks")
    print("=" * 70)

    if not OHLCV_FILE.exists():
        print(f"\n[ERROR] OHLCV file not found: {OHLCV_FILE}")
        return

    storage = DuckDBStorage()

    try:
        # Step 1-2: Load and filter OHLCV
        df_1m = load_ohlcv_from_dbn(OHLCV_FILE)
        df_1m = filter_ohlcv_data(df_1m)

        # Step 3: Recreate table
        recreate_ohlcv_ticks_table(storage)

        # Step 4: Load all timeframes
        print(f"\n[4/6] Loading OHLCV data into ohlcv_ticks...")
        for tf in TIMEFRAMES:
            df_tf = resample_to_timeframe(df_1m, tf)
            insert_ohlcv_data(storage, df_tf, tf)
            print(f"      {tf}: {len(df_tf):,} bars")

        storage.conn.commit()

        # Step 5: Overlay mbp_ticks data
        overlay_mbp_data(storage)
        storage.conn.commit()

        # Step 6: Cleanup
        cleanup_tables(storage)
        storage.conn.commit()

        # Summary
        print_summary(storage)

        # Create index
        print("\nCreating index...")
        storage.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ohlcv_ticks_lookup
            ON ohlcv_ticks (symbol, timeframe, timestamp)
        """)
        storage.conn.commit()
        print("Index created")

    except Exception as e:
        print(f"\n[ERROR] Migration failed: {e}")
        import traceback
        traceback.print_exc()
        raise
    finally:
        storage.close()


if __name__ == "__main__":
    main()
