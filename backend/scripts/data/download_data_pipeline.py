#!/usr/bin/env python
"""
Unified Data Pipeline - Downloads MBP-1 and trades, then reaggregates all timeframes.

This script ensures data consistency by:
1. Downloading MBP-1 data -> aggregating to 5M only
2. Downloading trades data -> updating 5M orderflow metrics only
3. Reaggregating higher timeframes (15M, 1H, 4H, 1D) from clean 5M data

Usage:
    # Download full pipeline for a date range
    python scripts/data/download_data_pipeline.py --start 2025-08-01 --end 2025-12-01

    # Download MBP-1 only (skip trades)
    python scripts/data/download_data_pipeline.py --start 2025-08-01 --end 2025-12-01 --mbp-only

    # Download trades only (skip MBP-1)
    python scripts/data/download_data_pipeline.py --start 2025-08-01 --end 2025-12-01 --trades-only

    # Skip reaggregation (just download)
    python scripts/data/download_data_pipeline.py --start 2025-08-01 --end 2025-12-01 --skip-reaggregate

    # Dry run - show what would be done
    python scripts/data/download_data_pipeline.py --start 2025-08-01 --end 2025-12-01 --dry-run
"""
import sys
import argparse
import gc
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import databento as db
import polars as pl
from app.data.storage import DuckDBStorage
from config import get_secrets
from scripts.data.load_historical_data import ensure_ohlcv_table

# Databento settings
DATASET = "GLBX.MDP3"
SYMBOL = "MNQ.c.0"
STYPE_IN = "continuous"
LARGE_TRADE_THRESHOLD = 50


def _process_mbp_chunk(df_pl: pl.DataFrame) -> pl.DataFrame:
    """Process MBP-1 data: extract prices and orderflow metrics."""
    # Note: Databento returns prices already in dollars (e.g., 24760.0), no conversion needed
    # Cast size columns to Int64 to avoid UInt32 underflow when bid < ask
    return df_pl.with_columns([
        pl.col("ts_event").alias("timestamp"),
        ((pl.col("bid_px_00") + pl.col("ask_px_00")) / 2).alias("mid_price"),
        (pl.col("bid_sz_00").cast(pl.Int64) - pl.col("ask_sz_00").cast(pl.Int64)).alias("delta"),
        # DOM imbalance: 0-1 range where 0.5 = balanced, >0.5 = bid heavy (bullish), <0.5 = ask heavy (bearish)
        (pl.col("bid_sz_00").cast(pl.Float64) /
         (pl.col("bid_sz_00").cast(pl.Float64) + pl.col("ask_sz_00").cast(pl.Float64) + 1)).alias("dom_imbalance"),
        pl.col("bid_sz_00").alias("bid_size"),
        pl.col("ask_sz_00").alias("ask_size"),
    ])


def download_mbp_to_5m(
    api_key: str,
    start_date: str,
    end_date: str,
    hours_per_chunk: int = 4,
    dry_run: bool = False
) -> int:
    """Download MBP-1 data and aggregate to 5M bars only.

    Returns:
        Number of 5M bars created
    """
    print(f"\n{'=' * 60}")
    print("Step 1: Downloading MBP-1 -> 5M bars")
    print(f"{'=' * 60}")
    print(f"  Range: {start_date} to {end_date}")
    print(f"  Chunk size: {hours_per_chunk} hours")

    if dry_run:
        print("  [DRY RUN] Would download MBP-1 and aggregate to 5M")
        return 0

    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')

    with DuckDBStorage() as storage:
        ensure_ohlcv_table(storage)

        client = db.Historical(api_key)
        total_bars = 0
        chunk_num = 0
        current = start_dt

        while current < end_dt:
            chunk_num += 1
            chunk_end = min(current + timedelta(hours=hours_per_chunk), end_dt)

            print(f"\n  Chunk {chunk_num}: {current.strftime('%Y-%m-%d %H:%M')} -> {chunk_end.strftime('%Y-%m-%d %H:%M')}")

            try:
                data = client.timeseries.get_range(
                    dataset=DATASET,
                    symbols=[SYMBOL],
                    stype_in=STYPE_IN,
                    schema="mbp-1",
                    start=current.strftime('%Y-%m-%dT%H:%M:%S'),
                    end=chunk_end.strftime('%Y-%m-%dT%H:%M:%S'),
                )

                df = data.to_df()
                if len(df) == 0:
                    print(f"    No data")
                    current = chunk_end
                    continue

                if hasattr(df, 'index'):
                    df = df.reset_index()
                    if 'index' in df.columns:
                        df = df.rename(columns={'index': 'ts_event'})

                print(f"    Downloaded: {len(df):,} records")

                df_pl = pl.from_pandas(df)
                df_processed = _process_mbp_chunk(df_pl)

                del df, df_pl, data

                # Aggregate to 5M only with median-based outlier filtering
                df_with_bucket = df_processed.with_columns([
                    pl.col("timestamp").dt.truncate("5m").alias("bucket")
                ])

                medians = df_with_bucket.group_by("bucket").agg([
                    pl.col("mid_price").median().alias("median_price")
                ])

                df_filtered = df_with_bucket.join(medians, on="bucket", how="left").filter(
                    (pl.col("mid_price") - pl.col("median_price")).abs() / pl.col("median_price") < 0.005
                )

                df_agg = df_filtered.group_by_dynamic(
                    "timestamp", every="5m", closed="left", label="left"
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
                    pl.lit("5M").alias("timeframe"),
                    pl.lit(0).cast(pl.Int64).alias("cvd"),
                ])

                # Filter extreme bars (>3% range)
                df_agg = df_agg.filter(
                    ((pl.col("high") - pl.col("low")) / pl.col("close") < 0.03)
                )

                df_insert = df_agg.select([
                    "timestamp", "symbol", "timeframe", "open", "high", "low", "close",
                    "volume", "instant_delta", "dom_imbalance", "total_bid_depth",
                    "total_ask_depth", "cvd",
                    pl.lit(None).cast(pl.Float64).alias("trade_flow_ratio"),
                    pl.lit(None).cast(pl.Int32).alias("buy_trades"),
                    pl.lit(None).cast(pl.Int32).alias("sell_trades"),
                    pl.lit(None).cast(pl.Int32).alias("large_trade_count"),
                ])

                if len(df_insert) > 0:
                    storage.conn.execute("INSERT OR REPLACE INTO ohlcv_ticks SELECT * FROM df_insert")
                    total_bars += len(df_insert)
                    print(f"    Aggregated: {len(df_insert)} bars (total: {total_bars})")

                storage.conn.commit()

                del df_processed
                gc.collect()

            except Exception as e:
                print(f"    Error: {e}")

            current = chunk_end

        print(f"\n  Total 5M bars: {total_bars:,}")
        return total_bars


def download_trades_to_5m(
    api_key: str,
    start_date: str,
    end_date: str,
    hours_per_chunk: int = 2,
    dry_run: bool = False
) -> int:
    """Download trades data and update 5M bars with trade flow metrics.

    Returns:
        Number of 5M bars updated
    """
    print(f"\n{'=' * 60}")
    print("Step 2: Downloading Trades -> Update 5M orderflow")
    print(f"{'=' * 60}")
    print(f"  Range: {start_date} to {end_date}")
    print(f"  Chunk size: {hours_per_chunk} hours")

    if dry_run:
        print("  [DRY RUN] Would download trades and update 5M orderflow metrics")
        return 0

    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')

    with DuckDBStorage() as storage:
        client = db.Historical(api_key)
        total_trades = 0
        total_bars_updated = 0
        chunk_num = 0
        current = start_dt

        while current < end_dt:
            chunk_num += 1
            chunk_end = min(current + timedelta(hours=hours_per_chunk), end_dt)

            print(f"\n  Chunk {chunk_num}: {current.strftime('%Y-%m-%d %H:%M')} -> {chunk_end.strftime('%Y-%m-%d %H:%M')}")

            try:
                data = client.timeseries.get_range(
                    dataset=DATASET,
                    symbols=[SYMBOL],
                    stype_in=STYPE_IN,
                    schema="trades",
                    start=current.strftime('%Y-%m-%dT%H:%M:%S'),
                    end=chunk_end.strftime('%Y-%m-%dT%H:%M:%S'),
                )

                df = data.to_df()
                if len(df) == 0:
                    print(f"    No trades")
                    current = chunk_end
                    continue

                if hasattr(df, 'index'):
                    df = df.reset_index()
                    if 'index' in df.columns:
                        df = df.rename(columns={'index': 'ts_event'})

                total_trades += len(df)
                print(f"    Downloaded: {len(df):,} trades")

                df_pl = pl.from_pandas(df)

                df_trades = df_pl.with_columns([
                    pl.col("ts_event").alias("timestamp"),
                    pl.when(pl.col("side") == "A").then(1).otherwise(0).alias("is_buy"),
                    pl.when(pl.col("side") == "B").then(1).otherwise(0).alias("is_sell"),
                    pl.when(pl.col("size") >= LARGE_TRADE_THRESHOLD).then(1).otherwise(0).alias("is_large"),
                ])

                del df, data
                gc.collect()

                # Aggregate to 5M only
                df_agg = df_trades.group_by_dynamic(
                    "timestamp", every="5m", closed="left", label="left"
                ).agg([
                    pl.col("size").filter(pl.col("is_buy") == 1).sum().alias("buy_volume"),
                    pl.col("size").filter(pl.col("is_sell") == 1).sum().alias("sell_volume"),
                    pl.col("is_buy").sum().alias("buy_trades"),
                    pl.col("is_sell").sum().alias("sell_trades"),
                    pl.col("is_large").sum().alias("large_trade_count"),
                ]).with_columns([
                    (pl.col("buy_volume") / (pl.col("buy_volume") + pl.col("sell_volume") + 1)).alias("trade_flow_ratio")
                ])

                # Update existing 5M bars
                chunk_bars = 0
                for row in df_agg.to_dicts():
                    storage.conn.execute("""
                        UPDATE ohlcv_ticks
                        SET trade_flow_ratio = ?,
                            buy_trades = ?,
                            sell_trades = ?,
                            large_trade_count = ?
                        WHERE timestamp = ?
                          AND symbol = 'MNQ'
                          AND timeframe = '5M'
                    """, [
                        row['trade_flow_ratio'],
                        row['buy_trades'],
                        row['sell_trades'],
                        row['large_trade_count'],
                        row['timestamp'],
                    ])
                    chunk_bars += 1

                storage.conn.commit()
                total_bars_updated += chunk_bars
                print(f"    Updated: {chunk_bars} bars (total: {total_bars_updated})")

                del df_trades, df_agg
                gc.collect()

            except Exception as e:
                print(f"    Error: {e}")

            current = chunk_end

        print(f"\n  Total trades processed: {total_trades:,}")
        print(f"  Total 5M bars updated: {total_bars_updated:,}")
        return total_bars_updated


def reaggregate_higher_timeframes(dry_run: bool = False):
    """Reaggregate 15M, 1H, 4H, 1D from 5M data."""
    from scripts.maintenance.reaggregate_timeframes import reaggregate_timeframe

    print(f"\n{'=' * 60}")
    print("Step 3: Reaggregating higher timeframes from 5M")
    print(f"{'=' * 60}")

    if dry_run:
        print("  [DRY RUN] Would reaggregate 15M, 1H, 4H, 1D from 5M")
        return

    for tf in ['15M', '1H', '4H', '1D']:
        reaggregate_timeframe('5M', tf, dry_run=False)

    print("\n  All timeframes reaggregated!")


def run_pipeline(
    start_date: str,
    end_date: str,
    mbp_only: bool = False,
    trades_only: bool = False,
    skip_reaggregate: bool = False,
    hours_per_chunk: int = 4,
    dry_run: bool = False
):
    """Run the full data pipeline."""
    print("=" * 60)
    print("DATA DOWNLOAD PIPELINE")
    print("=" * 60)
    print(f"Date range: {start_date} to {end_date}")
    print(f"Options: mbp_only={mbp_only}, trades_only={trades_only}, skip_reaggregate={skip_reaggregate}")

    secrets = get_secrets()
    api_key = secrets.api_key

    # Step 1: Download MBP-1 to 5M
    if not trades_only:
        download_mbp_to_5m(
            api_key=api_key,
            start_date=start_date,
            end_date=end_date,
            hours_per_chunk=hours_per_chunk,
            dry_run=dry_run
        )

    # Step 2: Download trades to update 5M
    if not mbp_only:
        download_trades_to_5m(
            api_key=api_key,
            start_date=start_date,
            end_date=end_date,
            hours_per_chunk=max(2, hours_per_chunk // 2),  # Trades need smaller chunks
            dry_run=dry_run
        )

    # Step 3: Reaggregate higher timeframes
    if not skip_reaggregate:
        reaggregate_higher_timeframes(dry_run=dry_run)

    print(f"\n{'=' * 60}")
    print("PIPELINE COMPLETE")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description='Unified data download pipeline',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full pipeline for a date range
  python download_data_pipeline.py --start 2025-08-01 --end 2025-12-01

  # MBP-1 only (skip trades)
  python download_data_pipeline.py --start 2025-08-01 --end 2025-12-01 --mbp-only

  # Trades only (skip MBP-1)
  python download_data_pipeline.py --start 2025-08-01 --end 2025-12-01 --trades-only

  # Just reaggregate (no download)
  python download_data_pipeline.py --reaggregate-only
        """
    )

    parser.add_argument('--start', '-s', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', '-e', type=str, help='End date (YYYY-MM-DD)')
    parser.add_argument('--mbp-only', action='store_true', help='Download MBP-1 only (skip trades)')
    parser.add_argument('--trades-only', action='store_true', help='Download trades only (skip MBP-1)')
    parser.add_argument('--skip-reaggregate', action='store_true', help='Skip reaggregation step')
    parser.add_argument('--reaggregate-only', action='store_true', help='Only run reaggregation (no download)')
    parser.add_argument('--chunk-hours', type=int, default=4, help='Hours per download chunk (default: 4)')
    parser.add_argument('--dry-run', '-n', action='store_true', help='Show what would be done')

    args = parser.parse_args()

    if args.reaggregate_only:
        reaggregate_higher_timeframes(dry_run=args.dry_run)
        return

    if not args.start or not args.end:
        parser.error("--start and --end are required (or use --reaggregate-only)")

    run_pipeline(
        start_date=args.start,
        end_date=args.end,
        mbp_only=args.mbp_only,
        trades_only=args.trades_only,
        skip_reaggregate=args.skip_reaggregate,
        hours_per_chunk=args.chunk_hours,
        dry_run=args.dry_run
    )


if __name__ == '__main__':
    main()
