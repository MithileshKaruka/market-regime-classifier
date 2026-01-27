#!/usr/bin/env python3
"""
MBP Data Loader - Supports both MBP-1 and MBP-10 schemas

This script loads Market-By-Price data from Databento DBN files into DuckDB.
It automatically detects the schema (MBP-1 or MBP-10) from the file.

For live streaming, only MBP-1 is available on personal plans.
For historical data, both MBP-1 and MBP-10 may be available.
"""
import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import databento as db
import polars as pl
from app.features.order_flow import OrderFlowCalculator
from app.data.storage import DuckDBStorage

# Configuration
SYMBOL = "MNQ"
DATA_DIR = Path(__file__).parent.parent / "data"
CHUNK_SIZE = 1_000_000  # Process 1M records at a time


def detect_schema(file_path: Path) -> tuple[str, int]:
    """Detect MBP schema from file

    Returns:
        Tuple of (schema_name, levels)
        e.g., ("mbp-1", 1) or ("mbp-10", 10)
    """
    filename = file_path.name.lower()

    if "mbp-1" in filename or "mbp1" in filename:
        return "mbp-1", 1
    elif "mbp-10" in filename or "mbp10" in filename:
        return "mbp-10", 10
    else:
        # Try to detect from file content
        try:
            store = db.DBNStore.from_file(str(file_path))
            for record in store:
                if hasattr(record, 'levels'):
                    num_levels = len(record.levels) if record.levels else 1
                    schema = f"mbp-{num_levels}"
                    return schema, num_levels
                break
        except Exception:
            pass

        # Default to MBP-1
        return "mbp-1", 1


def process_chunk(df_chunk: pl.DataFrame, calculator: OrderFlowCalculator, chunk_num: int) -> pl.DataFrame:
    """Process a single chunk of MBP data"""
    # Filter to MNQ only if symbol column exists
    if 'symbol' in df_chunk.columns:
        df_chunk = df_chunk.filter(pl.col('symbol').str.starts_with('MNQ'))

    if len(df_chunk) == 0:
        return None

    # Calculate order flow features
    df_chunk = calculator.calculate_all_features(df_chunk)

    # Prepare columns for storage
    ts_col = "ts_event" if "ts_event" in df_chunk.columns else "timestamp"

    # Build result with available columns
    result_cols = [pl.col(ts_col).alias("timestamp")]

    col_mapping = {
        "mid_price": "mid_price",
        "bid_px_00": "bid_price",
        "ask_px_00": "ask_price",
        "spread": "spread",
        "bid_sz_00": "bid_size",
        "ask_sz_00": "ask_size",
        "total_bid_volume": "total_bid_depth",
        "total_ask_volume": "total_ask_depth",
        "dom_imbalance": "dom_imbalance",
        "instant_delta": "delta",
        "delta": "cvd",
    }

    for src_col, dst_col in col_mapping.items():
        if src_col in df_chunk.columns:
            result_cols.append(pl.col(src_col).alias(dst_col))

    return df_chunk.select(result_cols)


def process_file_streaming(file_path: Path, levels: int = 1) -> int:
    """Process DBN file using streaming approach (faster but needs more RAM)"""
    print(f"\n{'='*60}")
    print(f"Processing (streaming): {file_path.name}")
    size_mb = file_path.stat().st_size / 1024 / 1024
    print(f"File size: {size_mb:.1f} MB")
    print(f"Schema: MBP-{levels}")
    print(f"{'='*60}")

    calculator = OrderFlowCalculator(levels=levels)
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
                    storage.insert_mbp_ticks(df_processed, symbol=SYMBOL)
                total_processed += len(df_processed)

            print(f"    Chunk {chunk_num}: Processed {total_processed:,} total")

        print(f"\n[3] Complete! Processed {total_processed:,} MNQ records")
        return total_processed

    except MemoryError:
        print(f"\n[ERROR] Out of memory! Try iterative mode instead.")
        return 0
    except Exception as e:
        print(f"\n[ERROR] Failed to process file: {e}")
        import traceback
        traceback.print_exc()
        return 0


def process_file_iterative(file_path: Path, levels: int = 1) -> int:
    """Process DBN file by iterating through records (slower but uses less RAM)"""
    print(f"\n{'='*60}")
    print(f"Processing (iterative): {file_path.name}")
    size_mb = file_path.stat().st_size / 1024 / 1024
    print(f"File size: {size_mb:.1f} MB")
    print(f"Schema: MBP-{levels}")
    print(f"{'='*60}")

    calculator = OrderFlowCalculator(levels=levels)
    total_processed = 0
    chunk_num = 0
    buffer = []

    try:
        print(f"\n[1] Opening DBN file for iteration...")
        store = db.DBNStore.from_file(str(file_path))

        # Check metadata to see if this is MNQ data
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
            }

            # Extract levels data
            if hasattr(record, 'levels') and record.levels:
                for level_idx, level in enumerate(record.levels):
                    if level_idx >= levels:
                        break
                    record_dict[f'bid_px_{level_idx:02d}'] = level.bid_px
                    record_dict[f'ask_px_{level_idx:02d}'] = level.ask_px
                    record_dict[f'bid_sz_{level_idx:02d}'] = level.bid_sz
                    record_dict[f'ask_sz_{level_idx:02d}'] = level.ask_sz
                    record_dict[f'bid_ct_{level_idx:02d}'] = level.bid_ct
                    record_dict[f'ask_ct_{level_idx:02d}'] = level.ask_ct

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
                        storage.insert_mbp_ticks(df_processed, symbol=SYMBOL)
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
                    storage.insert_mbp_ticks(df_processed, symbol=SYMBOL)
                total_processed += len(df_processed)

        print(f"\n[3] Complete! Processed {total_processed:,} MNQ records")
        return total_processed

    except Exception as e:
        print(f"\n[ERROR] Failed to process file: {e}")
        import traceback
        traceback.print_exc()
        return total_processed


def find_mbp_files() -> list[tuple[Path, str, int]]:
    """Find all MBP files in data directory

    Returns:
        List of tuples: (file_path, schema_name, levels)
    """
    files = []

    # Look for .dbn and .dbn.zst files
    for pattern in ["*.mbp-1*.dbn", "*.mbp-10*.dbn", "*.mbp-1*.dbn.zst", "*.mbp-10*.dbn.zst"]:
        for f in DATA_DIR.glob(pattern):
            schema, levels = detect_schema(f)
            files.append((f, schema, levels))

    # Sort by filename
    files.sort(key=lambda x: x[0].name)
    return files


def main():
    print("\n" + "="*70)
    print("  MBP Data Loader - Order Flow Analysis")
    print("  Supports MBP-1 (top of book) and MBP-10 (10 levels)")
    print("="*70)

    print(f"\nSymbol: {SYMBOL}")
    print(f"Data directory: {DATA_DIR}")
    print(f"Chunk size: {CHUNK_SIZE:,} records")

    # Find MBP files
    mbp_files = find_mbp_files()

    if not mbp_files:
        print("\nNo MBP files found!")
        print("Expected files matching: *.mbp-1*.dbn or *.mbp-10*.dbn")
        return

    # Get current count
    with DuckDBStorage() as storage:
        current_count = storage.conn.execute("SELECT COUNT(*) FROM mbp_ticks").fetchone()[0]

    print(f"\nFound {len(mbp_files)} MBP file(s):")
    for f, schema, levels in mbp_files:
        size_mb = f.stat().st_size / 1024 / 1024
        print(f"  - {f.name} ({size_mb:.1f} MB) [{schema}]")

    print(f"\nCurrent MBP ticks in database: {current_count:,}")

    # Menu
    print("\n" + "="*70)
    print("  Select files to process:")
    print("="*70)

    for i, (f, schema, levels) in enumerate(mbp_files, 1):
        size_mb = f.stat().st_size / 1024 / 1024
        print(f"  [{i}] {f.name} ({size_mb:.1f} MB) [{schema}]")

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
        files_to_process = mbp_files
    elif choice == 'S':
        smallest = min(mbp_files, key=lambda x: x[0].stat().st_size)
        files_to_process = [smallest]
    elif choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(mbp_files):
            files_to_process = [mbp_files[idx]]

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
    for file_path, schema, levels in files_to_process:
        if method == "1":
            added = process_file_streaming(file_path, levels)
        else:
            added = process_file_iterative(file_path, levels)
        total_added += added

    # Final summary
    with DuckDBStorage() as storage:
        final_count = storage.conn.execute("SELECT COUNT(*) FROM mbp_ticks").fetchone()[0]

    print("\n" + "="*70)
    print("  Processing Complete!")
    print("="*70)

    print(f"\nSummary:")
    print(f"  Files processed: {len(files_to_process)}")
    print(f"  Records added: {total_added:,}")
    print(f"  Total MBP ticks in DB: {final_count:,}")

    print("\nNext steps:")
    print("1. Run update_orderflow_metrics.py to update OHLCV bars with DOM/CVD")
    print("2. Check data: SELECT * FROM mbp_ticks LIMIT 10;")


if __name__ == "__main__":
    main()
