"""
Reset database schema for OHLCV-first architecture
Removes MBP-10 specific columns that are no longer used
"""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import duckdb

DB_PATH = Path("backend/data/market_data.duckdb")


def reset_database():
    """Drop and recreate tables with new OHLCV-focused schema"""
    print("=" * 70)
    print("  Database Schema Reset - OHLCV Architecture")
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
        conn.execute("DROP TABLE IF EXISTS order_book")
        conn.execute("DROP TABLE IF EXISTS regimes")
        print(f"       Tables dropped successfully")

        # Create new simplified schema
        print(f"[3/3] Creating new OHLCV-focused schema...")

        # OHLCV table (simplified, no MBP-10 fields)
        conn.execute("""
            CREATE TABLE order_book (
                timestamp TIMESTAMP,
                symbol VARCHAR,
                timeframe VARCHAR,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume BIGINT,
                dom_imbalance DOUBLE,     -- Will come from real-time MBP-10 only
                cvd DOUBLE,               -- Will calculate from trades data
                vwap DOUBLE,              -- Volume-weighted average price
                PRIMARY KEY (timestamp, symbol, timeframe)
            )
        """)
        print(f"       [OK] Created 'order_book' table (OHLCV + metrics)")

        # Regime classifications (unchanged)
        conn.execute("""
            CREATE TABLE regimes (
                timestamp TIMESTAMP,
                symbol VARCHAR,
                timeframe VARCHAR,
                regime VARCHAR,
                confidence DOUBLE,
                key_signal VARCHAR,
                dom_imbalance DOUBLE,
                cvd DOUBLE,
                vwap DOUBLE,
                price DOUBLE,
                PRIMARY KEY (timestamp, symbol, timeframe)
            )
        """)
        print(f"       [OK] Created 'regimes' table")

        conn.commit()

        print(f"\n{'='*70}")
        print(f"  Database Reset Complete!")
        print(f"{'='*70}")
        print(f"\nNew schema:")
        print(f"  - order_book: OHLCV + cvd + vwap + dom_imbalance")
        print(f"  - regimes: Regime classifications")
        print(f"\nRemoved columns (MBP-10 specific):")
        print(f"  - bid_px_00, bid_sz_00, ask_px_00, ask_sz_00")
        print(f"  - mid_price, total_bid_volume, total_ask_volume, spread")
        print(f"\nNext step:")
        print(f"  Run: python backend/scripts/load_ohlcv.py")

    except Exception as e:
        print(f"\n[ERROR] Failed to reset database: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    reset_database()
