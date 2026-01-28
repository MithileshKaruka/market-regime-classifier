"""Create pre-aggregated OHLCV table from mbp_ticks for faster signal detection"""
import sys
import time
sys.path.insert(0, '../..')
from app.data.storage import DuckDBStorage

def create_ohlcv_table():
    """Create ohlcv_ticks table with pre-aggregated data from mbp_ticks"""

    storage = DuckDBStorage()

    # Drop existing table if exists
    print("Dropping existing ohlcv_ticks table if exists...")
    storage.conn.execute("DROP TABLE IF EXISTS ohlcv_ticks")

    timeframes = [
        ("5M", "5 minutes"),
        ("15M", "15 minutes"),
        ("1H", "1 hour"),
        ("4H", "4 hours"),
        ("1D", "1 day"),
    ]

    print("Creating ohlcv_ticks table...")
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
            PRIMARY KEY (symbol, timeframe, timestamp)
        )
    """)

    for tf_name, interval in timeframes:
        print(f"\nAggregating {tf_name} bars...")
        start = time.time()

        storage.conn.execute(f"""
            INSERT INTO ohlcv_ticks
            SELECT
                time_bucket(INTERVAL '{interval}', timestamp) as timestamp,
                'MNQ' as symbol,
                '{tf_name}' as timeframe,
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
        """)

        count = storage.conn.execute(f"""
            SELECT COUNT(*) FROM ohlcv_ticks WHERE timeframe = '{tf_name}'
        """).fetchone()[0]

        elapsed = time.time() - start
        print(f"  Created {count:,} bars in {elapsed:.1f}s")

    # Create index for fast lookups
    print("\nCreating index...")
    storage.conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_ohlcv_ticks_lookup
        ON ohlcv_ticks (symbol, timeframe, timestamp)
    """)

    # Show summary
    print("\n=== Summary ===")
    result = storage.conn.execute("""
        SELECT timeframe, COUNT(*) as bars, MIN(timestamp) as first, MAX(timestamp) as last
        FROM ohlcv_ticks
        GROUP BY timeframe
        ORDER BY timeframe
    """).fetchall()
    for row in result:
        print(f"{row[0]}: {row[1]:,} bars from {row[2]} to {row[3]}")

    print("\nDone!")

if __name__ == "__main__":
    create_ohlcv_table()
