"""Update order_book table with real DOM imbalance and CVD from mbp_ticks data

For 5M, 15M, 1H: Uses time_bucket which aligns with order_book timestamps
For 4H, 1D: Joins ticks to existing order_book bar windows (CME session boundaries)

Note: Delta values in mbp_ticks are stored as uint32, where negative values
wrap around (e.g., -1 becomes 4294967295). This script converts them to
signed int32 before summing for CVD calculation.
"""
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.data.storage import DuckDBStorage
import polars as pl

# Threshold for detecting wrapped negative values (2^31)
UINT32_WRAP_THRESHOLD = 2147483648


def convert_delta_to_signed(delta_value):
    """Convert uint32 delta to signed int32

    Values >= 2^31 are actually negative (wrapped around)
    e.g., 4294967295 (-1) -> -1
          4294967288 (-8) -> -8
    """
    if delta_value >= UINT32_WRAP_THRESHOLD:
        return delta_value - 4294967296  # 2^32
    return delta_value


def update_orderflow_metrics():
    """Calculate and update DOM imbalance and CVD from mbp_ticks to order_book"""

    with DuckDBStorage() as storage:
        # Check mbp_ticks data
        tick_count = storage.conn.execute("SELECT COUNT(*) FROM mbp_ticks").fetchone()[0]
        print(f"Found {tick_count:,} mbp_ticks records")

        if tick_count == 0:
            print("No mbp_ticks data available!")
            return

        # Get time range of mbp_ticks
        time_range = storage.conn.execute("""
            SELECT MIN(timestamp), MAX(timestamp) FROM mbp_ticks
        """).fetchone()
        print(f"MBP ticks time range: {time_range[0]} to {time_range[1]}")

        # CVD rolling windows
        cvd_windows = {
            '5M': 288,   # 24 hours
            '15M': 96,   # 24 hours
            '1H': 24,    # 24 hours
            '4H': 30,    # 5 days
            '1D': 5,     # 5 days
        }

        # Process timeframes that align with time_bucket (5M, 15M, 1H)
        aligned_timeframes = ['5M', '15M', '1H']
        timeframe_intervals = {
            '5M': '5 minutes',
            '15M': '15 minutes',
            '1H': '1 hour',
        }

        for tf in aligned_timeframes:
            print(f"\nProcessing {tf}...")
            interval = timeframe_intervals[tf]

            # Aggregate mbp_ticks by timeframe to get DOM imbalance and delta
            # Convert uint32 delta to signed: if delta >= 2^31, it's negative (delta - 2^32)
            agg_query = f"""
                SELECT
                    time_bucket(INTERVAL '{interval}', timestamp) as bucket_ts,
                    AVG(dom_imbalance) as avg_dom_imbalance,
                    SUM(CASE WHEN delta >= 2147483648 THEN delta - 4294967296 ELSE delta END) as total_delta,
                    COUNT(*) as tick_count
                FROM mbp_ticks
                WHERE symbol = 'MNQ'
                GROUP BY bucket_ts
                ORDER BY bucket_ts
            """

            df_agg = storage.conn.execute(agg_query).pl()
            print(f"  Aggregated to {len(df_agg)} {tf} bars")

            if len(df_agg) == 0:
                continue

            # Calculate rolling CVD
            window = cvd_windows[tf]
            df_agg = df_agg.with_columns([
                pl.col("total_delta").rolling_sum(window_size=window).alias("cvd")
            ])

            # Update order_book
            match_count = storage.conn.execute(f"""
                SELECT COUNT(*)
                FROM order_book ob
                WHERE ob.symbol = 'MNQ' AND ob.timeframe = '{tf}'
                AND ob.timestamp IN (SELECT bucket_ts FROM df_agg)
            """).fetchone()[0]
            print(f"  Can update {match_count} bars in order_book")

            if match_count > 0:
                storage.conn.execute(f"""
                    UPDATE order_book
                    SET
                        dom_imbalance = agg.avg_dom_imbalance,
                        cvd = agg.cvd
                    FROM df_agg agg
                    WHERE order_book.symbol = 'MNQ'
                    AND order_book.timeframe = '{tf}'
                    AND order_book.timestamp = agg.bucket_ts
                """)
                storage.conn.commit()
                print(f"  Updated {match_count} bars with DOM and CVD")

        # Process 4H and 1D using order_book bar windows (CME session boundaries)
        session_timeframes = ['4H', '1D']
        session_intervals = {
            '4H': '4 hours',
            '1D': '1 day'
        }

        for tf in session_timeframes:
            print(f"\nProcessing {tf} (session-aligned)...")
            interval = session_intervals[tf]

            # Get order_book bar timestamps within the mbp_ticks range
            bars = storage.conn.execute(f"""
                SELECT timestamp
                FROM order_book
                WHERE symbol = 'MNQ' AND timeframe = '{tf}'
                AND timestamp >= '{time_range[0]}'::TIMESTAMP
                AND timestamp <= '{time_range[1]}'::TIMESTAMP + INTERVAL '{interval}'
                ORDER BY timestamp
            """).fetchall()

            print(f"  Found {len(bars)} {tf} bars in tick data range")

            if len(bars) == 0:
                continue

            # For each bar, aggregate ticks within [bar_start, bar_start + interval)
            updated = 0
            for i, (bar_ts,) in enumerate(bars):
                # Calculate end of bar window
                bar_end = storage.conn.execute(f"""
                    SELECT '{bar_ts}'::TIMESTAMP + INTERVAL '{interval}'
                """).fetchone()[0]

                # Aggregate ticks within this bar's window (convert uint32 delta to signed)
                agg = storage.conn.execute(f"""
                    SELECT
                        AVG(dom_imbalance) as avg_dom,
                        SUM(CASE WHEN delta >= 2147483648 THEN delta - 4294967296 ELSE delta END) as total_delta,
                        COUNT(*) as tick_count
                    FROM mbp_ticks
                    WHERE symbol = 'MNQ'
                    AND timestamp >= '{bar_ts}'
                    AND timestamp < '{bar_end}'
                """).fetchone()

                if agg[2] > 0:  # Has ticks
                    avg_dom, total_delta, tick_count = agg

                    # For CVD, we need to sum delta from previous bars too
                    # Get cumulative delta up to and including this bar
                    window = cvd_windows[tf]
                    window_start_idx = max(0, i - window + 1)

                    if window_start_idx == 0:
                        # Get earliest bar timestamp for window
                        window_start_ts = bars[0][0]
                    else:
                        window_start_ts = bars[window_start_idx][0]

                    cvd_result = storage.conn.execute(f"""
                        SELECT SUM(CASE WHEN delta >= 2147483648 THEN delta - 4294967296 ELSE delta END)
                        FROM mbp_ticks
                        WHERE symbol = 'MNQ'
                        AND timestamp >= '{window_start_ts}'
                        AND timestamp < '{bar_end}'
                    """).fetchone()[0]

                    cvd = cvd_result if cvd_result is not None else 0

                    # Update the bar
                    storage.conn.execute(f"""
                        UPDATE order_book
                        SET dom_imbalance = {avg_dom}, cvd = {cvd}
                        WHERE symbol = 'MNQ' AND timeframe = '{tf}'
                        AND timestamp = '{bar_ts}'
                    """)
                    updated += 1

            storage.conn.commit()
            print(f"  Updated {updated} bars with DOM and CVD")

            # Show sample of updated data
            sample = storage.conn.execute(f"""
                SELECT timestamp, dom_imbalance, cvd
                FROM order_book
                WHERE symbol = 'MNQ' AND timeframe = '{tf}'
                AND dom_imbalance != 0.5
                ORDER BY timestamp DESC
                LIMIT 3
            """).fetchall()
            if sample:
                print(f"  Sample updated data: {sample[0]}")

        # Verify updates
        print("\n=== Verification ===")
        all_timeframes = ['5M', '15M', '1H', '4H', '1D']
        for tf in all_timeframes:
            stats = storage.conn.execute(f"""
                SELECT
                    COUNT(*) as total,
                    COUNT(CASE WHEN dom_imbalance != 0.5 THEN 1 END) as updated,
                    AVG(dom_imbalance) as avg_dom,
                    MIN(dom_imbalance) as min_dom,
                    MAX(dom_imbalance) as max_dom
                FROM order_book
                WHERE symbol = 'MNQ' AND timeframe = '{tf}'
            """).fetchone()
            print(f"{tf}: {stats[1]}/{stats[0]} updated, DOM range: {stats[3]:.3f} - {stats[4]:.3f}")


if __name__ == "__main__":
    update_orderflow_metrics()
