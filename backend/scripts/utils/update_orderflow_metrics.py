"""Update order_book table with DOM imbalance and CVD from tick/trade data

This script supports two data sources:
1. mbp_ticks (MBP-1 or MBP-10) - for DOM imbalance
2. trades - for accurate CVD from trade aggressor side (preferred)

For 5M, 15M, 1H: Uses time_bucket which aligns with order_book timestamps
For 4H, 1D: Joins ticks to existing order_book bar windows (CME session boundaries)

Data priority:
- CVD: Uses trades table if available (more accurate), falls back to mbp_ticks
- DOM: Uses mbp_ticks table (order book imbalance)
"""
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.data.storage import DuckDBStorage
import polars as pl


def update_orderflow_metrics():
    """Calculate and update DOM imbalance and CVD from tick/trade data to order_book"""

    with DuckDBStorage() as storage:
        # Check available data sources
        tick_count = storage.conn.execute("SELECT COUNT(*) FROM mbp_ticks").fetchone()[0]

        # Check if trades table exists and has data
        try:
            trade_count = storage.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        except Exception:
            trade_count = 0

        print(f"Data sources:")
        print(f"  mbp_ticks: {tick_count:,} records")
        print(f"  trades: {trade_count:,} records")

        if tick_count == 0 and trade_count == 0:
            print("\nNo tick or trade data available!")
            return

        # Determine CVD source
        use_trades_for_cvd = trade_count > 0
        print(f"\nUsing {'trades' if use_trades_for_cvd else 'mbp_ticks'} for CVD calculation")

        # Get time ranges
        if tick_count > 0:
            tick_range = storage.conn.execute("""
                SELECT MIN(timestamp), MAX(timestamp) FROM mbp_ticks
            """).fetchone()
            print(f"MBP ticks time range: {tick_range[0]} to {tick_range[1]}")

        if trade_count > 0:
            trade_range = storage.conn.execute("""
                SELECT MIN(timestamp), MAX(timestamp) FROM trades
            """).fetchone()
            print(f"Trades time range: {trade_range[0]} to {trade_range[1]}")

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

            # Build aggregation query based on available data
            if tick_count > 0 and trade_count > 0:
                # Both sources available - use ticks for DOM, trades for CVD
                agg_query = f"""
                    WITH tick_agg AS (
                        SELECT
                            time_bucket(INTERVAL '{interval}', timestamp) as bucket_ts,
                            AVG(dom_imbalance) as avg_dom_imbalance
                        FROM mbp_ticks
                        WHERE symbol = 'MNQ'
                        GROUP BY bucket_ts
                    ),
                    trade_agg AS (
                        SELECT
                            time_bucket(INTERVAL '{interval}', timestamp) as bucket_ts,
                            SUM(signed_size) as total_delta
                        FROM trades
                        WHERE symbol = 'MNQ'
                        GROUP BY bucket_ts
                    )
                    SELECT
                        COALESCE(t.bucket_ts, tr.bucket_ts) as bucket_ts,
                        t.avg_dom_imbalance,
                        tr.total_delta,
                        1 as tick_count
                    FROM tick_agg t
                    FULL OUTER JOIN trade_agg tr ON t.bucket_ts = tr.bucket_ts
                    ORDER BY bucket_ts
                """
            elif trade_count > 0:
                # Only trades available
                agg_query = f"""
                    SELECT
                        time_bucket(INTERVAL '{interval}', timestamp) as bucket_ts,
                        0.5 as avg_dom_imbalance,
                        SUM(signed_size) as total_delta,
                        COUNT(*) as tick_count
                    FROM trades
                    WHERE symbol = 'MNQ'
                    GROUP BY bucket_ts
                    ORDER BY bucket_ts
                """
            else:
                # Only mbp_ticks available
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

            # Fill nulls and cast types (DuckDB returns decimal which doesn't support rolling_sum)
            df_agg = df_agg.with_columns([
                pl.col("avg_dom_imbalance").fill_null(0.5).cast(pl.Float64),
                pl.col("total_delta").fill_null(0).cast(pl.Float64),
            ])

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

        # Determine time range to use
        if tick_count > 0:
            time_range = tick_range
        else:
            time_range = trade_range

        for tf in session_timeframes:
            print(f"\nProcessing {tf} (session-aligned)...")
            interval = session_intervals[tf]

            # Get order_book bar timestamps within the data range
            bars = storage.conn.execute(f"""
                SELECT timestamp
                FROM order_book
                WHERE symbol = 'MNQ' AND timeframe = '{tf}'
                AND timestamp >= '{time_range[0]}'::TIMESTAMP
                AND timestamp <= '{time_range[1]}'::TIMESTAMP + INTERVAL '{interval}'
                ORDER BY timestamp
            """).fetchall()

            print(f"  Found {len(bars)} {tf} bars in data range")

            if len(bars) == 0:
                continue

            # For each bar, aggregate data within [bar_start, bar_start + interval)
            updated = 0
            for i, (bar_ts,) in enumerate(bars):
                # Calculate end of bar window
                bar_end = storage.conn.execute(f"""
                    SELECT '{bar_ts}'::TIMESTAMP + INTERVAL '{interval}'
                """).fetchone()[0]

                # Get DOM from mbp_ticks
                avg_dom = 0.5
                if tick_count > 0:
                    dom_result = storage.conn.execute(f"""
                        SELECT AVG(dom_imbalance)
                        FROM mbp_ticks
                        WHERE symbol = 'MNQ'
                        AND timestamp >= '{bar_ts}'
                        AND timestamp < '{bar_end}'
                    """).fetchone()[0]
                    if dom_result is not None:
                        avg_dom = dom_result

                # Get delta for CVD calculation
                if use_trades_for_cvd:
                    delta_result = storage.conn.execute(f"""
                        SELECT SUM(signed_size)
                        FROM trades
                        WHERE symbol = 'MNQ'
                        AND timestamp >= '{bar_ts}'
                        AND timestamp < '{bar_end}'
                    """).fetchone()[0]
                else:
                    delta_result = storage.conn.execute(f"""
                        SELECT SUM(CASE WHEN delta >= 2147483648 THEN delta - 4294967296 ELSE delta END)
                        FROM mbp_ticks
                        WHERE symbol = 'MNQ'
                        AND timestamp >= '{bar_ts}'
                        AND timestamp < '{bar_end}'
                    """).fetchone()[0]

                total_delta = delta_result if delta_result is not None else 0

                if total_delta != 0 or avg_dom != 0.5:
                    # Calculate rolling CVD
                    window = cvd_windows[tf]
                    window_start_idx = max(0, i - window + 1)
                    window_start_ts = bars[window_start_idx][0]

                    if use_trades_for_cvd:
                        cvd_result = storage.conn.execute(f"""
                            SELECT SUM(signed_size)
                            FROM trades
                            WHERE symbol = 'MNQ'
                            AND timestamp >= '{window_start_ts}'
                            AND timestamp < '{bar_end}'
                        """).fetchone()[0]
                    else:
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
