"""
Verify OHLCV data is loaded correctly
"""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.data.storage import DuckDBStorage


def verify_data():
    """Verify data is loaded for all timeframes"""
    print("=" * 70)
    print("  Data Verification - OHLCV")
    print("=" * 70)

    with DuckDBStorage() as storage:
        timeframes = ["1M", "5M", "15M", "1H", "4H", "1D"]

        for tf in timeframes:
            df = storage.conn.execute(f"""
                SELECT
                    COUNT(*) as count,
                    MIN(timestamp) as first_date,
                    MAX(timestamp) as last_date,
                    MIN(close) as min_price,
                    MAX(close) as max_price
                FROM order_book
                WHERE symbol = 'MNQ' AND timeframe = '{tf}'
            """).pl()

            if len(df) > 0 and df["count"][0] > 0:
                print(f"\n{tf:4s}: {df['count'][0]:>10,} bars")
                print(f"      Date range: {df['first_date'][0]} to {df['last_date'][0]}")
                print(f"      Price range: ${df['min_price'][0]:,.2f} - ${df['max_price'][0]:,.2f}")
            else:
                print(f"\n{tf:4s}: NO DATA")

        # Sample recent bars from 1H
        print("\n" + "=" * 70)
        print("  Sample 1H bars (last 5)")
        print("=" * 70)

        df_sample = storage.conn.execute("""
            SELECT timestamp, open, high, low, close, volume
            FROM order_book
            WHERE symbol = 'MNQ' AND timeframe = '1H'
            ORDER BY timestamp DESC
            LIMIT 5
        """).pl()

        print(df_sample)


if __name__ == "__main__":
    verify_data()
