"""
Reset database schema for ohlcv_ticks architecture
Creates the unified ohlcv_ticks table as single source of truth
"""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import duckdb

DB_PATH = Path(__file__).parent.parent.parent / "data" / "market_data.duckdb"


def reset_database():
    """Drop and recreate tables with ohlcv_ticks schema"""
    print("=" * 70)
    print("  Database Schema Reset - ohlcv_ticks Architecture")
    print("=" * 70)

    if not DB_PATH.exists():
        print(f"\n[INFO] No existing database at {DB_PATH}")
        print(f"[INFO] Will create new database on first data load")
        return

    print(f"\n[1/3] Connecting to database: {DB_PATH}")
    conn = duckdb.connect(str(DB_PATH))

    try:
        # Drop existing tables
        print(f"[2/3] Dropping old tables...")
        conn.execute("DROP TABLE IF EXISTS ohlcv_ticks")
        conn.execute("DROP TABLE IF EXISTS mbp_ticks")
        conn.execute("DROP TABLE IF EXISTS order_book")  # Legacy table
        conn.execute("DROP TABLE IF EXISTS regimes")
        print(f"       Tables dropped successfully")

        # Create new schema
        print(f"[3/3] Creating ohlcv_ticks schema...")

        # ohlcv_ticks - single source of truth
        conn.execute("""
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
        print(f"       [OK] Created 'ohlcv_ticks' table")

        # mbp_ticks - raw tick data for aggregation
        conn.execute("""
            CREATE TABLE mbp_ticks (
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
        print(f"       [OK] Created 'mbp_ticks' table")

        # Regime classifications
        conn.execute("""
            CREATE TABLE regimes (
                timestamp TIMESTAMP,
                symbol VARCHAR,
                timeframe VARCHAR,
                regime VARCHAR,
                confidence DOUBLE,
                key_signal VARCHAR,
                PRIMARY KEY (timestamp, symbol, timeframe)
            )
        """)
        print(f"       [OK] Created 'regimes' table")

        # Create index
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ohlcv_ticks_lookup
            ON ohlcv_ticks (symbol, timeframe, timestamp)
        """)
        print(f"       [OK] Created index on ohlcv_ticks")

        conn.commit()

        print(f"\n{'='*70}")
        print(f"  Database Reset Complete!")
        print(f"{'='*70}")
        print(f"\nSchema:")
        print(f"  - ohlcv_ticks: OHLCV + orderflow (single source of truth)")
        print(f"  - mbp_ticks: Raw MBP-1 tick data for aggregation")
        print(f"  - regimes: Regime classifications")
        print(f"\nNext steps:")
        print(f"  # Load OHLCV data")
        print(f"  python scripts/data/load_historical_data.py --ohlcv data/ohlcv.dbn.zst")
        print(f"")
        print(f"  # Load MBP-1 data (adds orderflow metrics)")
        print(f"  python scripts/data/load_historical_data.py --mbp data/mbp1.dbn.zst")

    except Exception as e:
        print(f"\n[ERROR] Failed to reset database: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    reset_database()
