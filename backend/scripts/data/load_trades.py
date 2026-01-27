#!/usr/bin/env python3
"""
Trades Data Loader - Load Databento Trades schema data

This script loads trade execution data from Databento DBN files into DuckDB.
Trade data provides accurate CVD/delta calculation using actual trade aggressor side.

Trade side indicates aggressor:
- 'A' (Ask): Buy aggressor - buyer lifted the ask (bullish)
- 'B' (Bid): Sell aggressor - seller hit the bid (bearish)
"""
import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import databento as db
import polars as pl
from app.features.trade_flow import TradeFlowCalculator
from app.data.storage import DuckDBStorage

# Configuration
SYMBOL = "MNQ"
DATA_DIR = Path(__file__).parent.parent / "data"
CHUNK_SIZE = 1_000_000  # Process 1M records at a time


def process_chunk(df_chunk: pl.DataFrame, calculator: TradeFlowCalculator, chunk_num: int) -> pl.DataFrame:
    """Process a single chunk of trades data"""
    # Filter to MNQ only if symbol column exists
    if 'symbol' in df_chunk.columns:
        df_chunk = df_chunk.filter(pl.col('symbol').str.starts_with('MNQ'))

    if len(df_chunk) == 0:
        return None

    # Calculate trade flow features
    df_chunk = calculator.calculate_all_features(df_chunk)

    # Prepare columns for storage
    ts_col = "ts_event" if "ts_event" in df_chunk.columns else "timestamp"

    result_cols = [
        pl.col(ts_col).alias("timestamp"),
        pl.col("price").alias("price"),
        pl.col("size").alias("size"),
        pl.col("side").alias("side"),
        pl.col("signed_size").alias("signed_size"),
        pl.col("delta").alias("delta"),
    ]

    # Add optional columns if present
    for col in ["trade_count", "volume", "price_volume"]:
        if col in df_chunk.columns:
            result_cols.append(pl.col(col))

    return df_chunk.select(result_cols)


def process_file_streaming(file_path: Path) -> int:
    """Process trades DBN file using streaming approach"""
    print(f"\n{'='*60}")
    print(f"Processing (streaming): {file_path.name}")
    size_mb = file_path.stat().st_size / 1024 / 1024
    print(f"File size: {size_mb:.1f} MB")
    print(f"{'='*60}")

    calculator = TradeFlowCalculator()
    total_processed = 0

    try:
        print(f"\n[1] Loading DBN file...")
        store = db.DBNStore.from_file(str(file_path))

        print(f"[2] Converting to DataFrame...")
        df = store.to_df()

        print(f"    Raw records: {len(df):,}")

        # Convert to polars
        df = pl.from_pandas(df.reset_index())

        # Process in chunks
        chunk_num = 0
        for start_idx in range(0, len(df), CHUNK_SIZE):
            chunk_num += 1
            end_idx = min(start_idx + CHUNK_SIZE, len(df))
            df_chunk = df.slice(start_idx, end_idx - start_idx)

            df_processed = process_chunk(df_chunk, calculator, chunk_num)

            if df_processed is not None and len(df_processed) > 0:
                with DuckDBStorage() as storage:
                    storage.insert_trades(df_processed, symbol=SYMBOL)
                total_processed += len(df_processed)

            print(f"    Chunk {chunk_num}: Processed {total_processed:,} total")

        print(f"\n[3] Complete! Processed {total_processed:,} MNQ trades")
        return total_processed

    except MemoryError:
        print(f"\n[ERROR] Out of memory! Try iterative mode instead.")
        return 0
    except Exception as e:
        print(f"\n[ERROR] Failed to process file: {e}")
        import traceback
        traceback.print_exc()
        return 0


def process_file_iterative(file_path: Path) -> int:
    """Process trades DBN file by iterating through records"""
    print(f"\n{'='*60}")
    print(f"Processing (iterative): {file_path.name}")
    size_mb = file_path.stat().st_size / 1024 / 1024
    print(f"File size: {size_mb:.1f} MB")
    print(f"{'='*60}")

    calculator = TradeFlowCalculator()
    total_processed = 0
    chunk_num = 0
    buffer = []

    try:
        print(f"\n[1] Opening DBN file for iteration...")
        store = db.DBNStore.from_file(str(file_path))

        # Check metadata
        is_mnq_file = False
        if hasattr(store, 'metadata') and hasattr(store.metadata, 'symbols'):
            symbols = store.metadata.symbols
            print(f"    File symbols: {symbols}")
            is_mnq_file = any('MNQ' in s for s in symbols)
            if is_mnq_file:
                print(f"    Confirmed MNQ data - will process all records")

        print(f"[2] Iterating through records...")

        for i, record in enumerate(store):
            # Convert record to dict
            record_dict = {
                'ts_event': record.ts_event,
                'price': record.price / 1_000_000_000.0 if hasattr(record, 'price') else 0,
                'size': record.size if hasattr(record, 'size') else 0,
                'side': record.side if hasattr(record, 'side') else 'N',
            }

            # Add symbol
            if hasattr(record, 'symbol'):
                record_dict['symbol'] = record.symbol
            elif is_mnq_file:
                record_dict['symbol'] = 'MNQ'

            buffer.append(record_dict)

            # Process buffer when it reaches chunk size
            if len(buffer) >= CHUNK_SIZE:
                chunk_num += 1
                df_chunk = pl.DataFrame(buffer)
                buffer = []

                df_processed = process_chunk(df_chunk, calculator, chunk_num)

                if df_processed is not None and len(df_processed) > 0:
                    with DuckDBStorage() as storage:
                        storage.insert_trades(df_processed, symbol=SYMBOL)
                    total_processed += len(df_processed)

                print(f"    Chunk {chunk_num}: Processed {total_processed:,} total")

                del df_chunk
                if df_processed is not None:
                    del df_processed

        # Process remaining buffer
        if buffer:
            chunk_num += 1
            df_chunk = pl.DataFrame(buffer)
            df_processed = process_chunk(df_chunk, calculator, chunk_num)

            if df_processed is not None and len(df_processed) > 0:
                with DuckDBStorage() as storage:
                    storage.insert_trades(df_processed, symbol=SYMBOL)
                total_processed += len(df_processed)

        print(f"\n[3] Complete! Processed {total_processed:,} MNQ trades")
        return total_processed

    except Exception as e:
        print(f"\n[ERROR] Failed to process file: {e}")
        import traceback
        traceback.print_exc()
        return total_processed


def find_trades_files() -> list[Path]:
    """Find all trades files in data directory"""
    files = []

    # Look for .dbn and .dbn.zst files with 'trades' in name
    for pattern in ["*trades*.dbn", "*trades*.dbn.zst"]:
        files.extend(DATA_DIR.glob(pattern))

    # Sort by filename
    files.sort(key=lambda x: x.name)
    return files


def main():
    print("\n" + "="*70)
    print("  Trades Data Loader - Accurate CVD/Delta Calculation")
    print("="*70)

    print(f"\nSymbol: {SYMBOL}")
    print(f"Data directory: {DATA_DIR}")
    print(f"Chunk size: {CHUNK_SIZE:,} records")

    # Find trades files
    trades_files = find_trades_files()

    if not trades_files:
        print("\nNo trades files found!")
        print("Expected files matching: *trades*.dbn")
        print("\nTo download trades data from Databento:")
        print("  databento-cli get --dataset GLBX.MDP3 --schema trades \\")
        print("    --symbols MNQ.FUT --start 2024-01-01 --end 2024-01-02")
        return

    # Check if trades table exists, create if not
    with DuckDBStorage() as storage:
        # Create trades table if it doesn't exist
        storage.conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                timestamp TIMESTAMP,
                symbol VARCHAR,
                price DOUBLE,
                size INTEGER,
                side VARCHAR,
                signed_size INTEGER,
                delta BIGINT,
                PRIMARY KEY (timestamp, symbol, price, size)
            )
        """)

        current_count = storage.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    print(f"\nFound {len(trades_files)} trades file(s):")
    for f in trades_files:
        size_mb = f.stat().st_size / 1024 / 1024
        print(f"  - {f.name} ({size_mb:.1f} MB)")

    print(f"\nCurrent trades in database: {current_count:,}")

    # Menu
    print("\n" + "="*70)
    print("  Select files to process:")
    print("="*70)

    for i, f in enumerate(trades_files, 1):
        size_mb = f.stat().st_size / 1024 / 1024
        print(f"  [{i}] {f.name} ({size_mb:.1f} MB)")

    print(f"  [A] Process ALL files")
    print(f"  [S] Process SMALLEST file (quick test)")
    print(f"  [Q] Quit")

    choice = input("\nEnter choice: ").strip().upper()

    if choice == 'Q':
        print("Exiting.")
        return

    # Determine files to process
    files_to_process = []

    if choice == 'A':
        files_to_process = trades_files
    elif choice == 'S':
        smallest = min(trades_files, key=lambda x: x.stat().st_size)
        files_to_process = [smallest]
    elif choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(trades_files):
            files_to_process = [trades_files[idx]]

    if not files_to_process:
        print("Invalid choice.")
        return

    # Select processing method
    print("\n" + "="*70)
    print("  Select processing method:")
    print("="*70)
    print("  [1] Streaming (faster, needs more RAM)")
    print("  [2] Iterative (slower, uses less RAM)")

    method = input("\nEnter choice [1]: ").strip() or "1"

    # Process files
    print("\n" + "="*70)
    print(f"  Processing {len(files_to_process)} file(s)")
    print("="*70)

    total_added = 0
    for file_path in files_to_process:
        if method == "1":
            added = process_file_streaming(file_path)
        else:
            added = process_file_iterative(file_path)
        total_added += added

    # Final summary
    with DuckDBStorage() as storage:
        final_count = storage.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    print("\n" + "="*70)
    print("  Processing Complete!")
    print("="*70)

    print(f"\nSummary:")
    print(f"  Files processed: {len(files_to_process)}")
    print(f"  Records added: {total_added:,}")
    print(f"  Total trades in DB: {final_count:,}")

    print("\nNext steps:")
    print("1. Run update_orderflow_metrics.py to update OHLCV bars with CVD from trades")
    print("2. Check data: SELECT * FROM trades LIMIT 10;")


if __name__ == "__main__":
    main()
