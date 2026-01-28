"""
Verify OHLCV data is loaded correctly
"""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.data.storage import DuckDBStorage


def verify_data():
    """Verify data is loaded for all timeframes"""
    print("=" * 70)
    print("  Data Verification - ohlcv_ticks")
    print("=" * 70)

    with DuckDBStorage() as storage:
        timeframes = ["5M", "15M", "1H", "4H", "1D"]

        for tf in timeframes:
            df = storage.conn.execute(f"""
                SELECT
                    COUNT(*) as total_bars,
                    SUM(CASE WHEN instant_delta IS NOT NULL THEN 1 ELSE 0 END) as orderflow_bars,
                    MIN(timestamp) as first_date,
                    MAX(timestamp) as last_date,
                    MIN(close) as min_price,
                    MAX(close) as max_price
                FROM ohlcv_ticks
                WHERE symbol = 'MNQ' AND timeframe = '{tf}'
            """).pl()

            if len(df) > 0 and df["total_bars"][0] > 0:
                total = df['total_bars'][0]
                orderflow = df['orderflow_bars'][0]
                print(f"\n{tf:4s}: {total:>10,} bars ({orderflow:,} with orderflow)")
                print(f"      Date range: {df['first_date'][0]} to {df['last_date'][0]}")
                print(f"      Price range: ${df['min_price'][0]:,.2f} - ${df['max_price'][0]:,.2f}")
            else:
                print(f"\n{tf:4s}: NO DATA")

        # Sample recent bars from 1H
        print("\n" + "=" * 70)
        print("  Sample 1H bars (last 5)")
        print("=" * 70)

        df_sample = storage.conn.execute("""
            SELECT timestamp, open, high, low, close, volume, instant_delta, dom_imbalance, cvd
            FROM ohlcv_ticks
            WHERE symbol = 'MNQ' AND timeframe = '1H'
            ORDER BY timestamp DESC
            LIMIT 5
        """).pl()

        print(df_sample)

        # Check mbp_ticks if exists
        try:
            mbp_count = storage.conn.execute("SELECT COUNT(*) FROM mbp_ticks").fetchone()[0]
            if mbp_count > 0:
                mbp_range = storage.conn.execute("""
                    SELECT MIN(timestamp), MAX(timestamp) FROM mbp_ticks
                """).fetchone()
                print(f"\n" + "=" * 70)
                print(f"  mbp_ticks: {mbp_count:,} rows")
                print(f"  Range: {mbp_range[0]} to {mbp_range[1]}")
        except Exception:
            pass


if __name__ == "__main__":
    verify_data()
