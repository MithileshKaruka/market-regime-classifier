"""
Load MBP-10 tick data from DBN files and store in DuckDB
This is for order flow analysis - separate from OHLCV candlestick data

Uses chunked/streaming processing to handle large files (8+ GB)
"""
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import databento as db
import polars as pl
import pandas as pd
from datetime import datetime
from app.data.storage import DuckDBStorage
from app.features.order_flow import OrderFlowCalculator

# Configuration
DATA_DIR = Path("C:/Users/jthlbg2/market-regime-classifier/backend/data")
SYMBOL = "MNQ"
CHUNK_SIZE = 1_000_000  # Process 1M records at a time


def find_mbp10_files(data_dir: Path) -> list[Path]:
    """Find all MBP-10 DBN files in the data directory"""
    # Look for both compressed and uncompressed files
    files = list(data_dir.glob("*.mbp-10.dbn")) + list(data_dir.glob("*.mbp-10.dbn.zst"))

    # Remove duplicates (prefer uncompressed if both exist)
    file_stems = {}
    for f in files:
        stem = f.name.replace('.zst', '')
        if stem not in file_stems or not f.name.endswith('.zst'):
            file_stems[stem] = f

    files = sorted(file_stems.values(), key=lambda f: f.name)

    print(f"Found {len(files)} MBP-10 files:")
    for f in files:
        size_mb = f.stat().st_size / 1024 / 1024
        print(f"  - {f.name} ({size_mb:.1f} MB)")

    return files


def process_chunk(df_chunk: pl.DataFrame, calculator: OrderFlowCalculator, chunk_num: int) -> pl.DataFrame:
    """
    Process a single chunk of MBP-10 data

    Args:
        df_chunk: Chunk of MBP-10 data
        calculator: OrderFlowCalculator instance
        chunk_num: Chunk number for logging

    Returns:
        Processed DataFrame ready for storage
    """
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


def process_file_streaming(file_path: Path) -> int:
    """
    Process a DBN file using streaming/chunked approach

    Args:
        file_path: Path to DBN file

    Returns:
        Number of records processed
    """
    print(f"\n{'='*60}")
    print(f"Processing: {file_path.name}")
    size_mb = file_path.stat().st_size / 1024 / 1024
    print(f"File size: {size_mb:.1f} MB")
    print(f"Chunk size: {CHUNK_SIZE:,} records")
    print(f"{'='*60}")

    calculator = OrderFlowCalculator()
    total_processed = 0
    chunk_num = 0

    try:
        # Load DBN store
        print(f"\n[1] Opening DBN file...")
        store = db.DBNStore.from_file(str(file_path))

        # Get total record count if available
        try:
            # Try to get metadata about record count
            print(f"[2] Reading data in chunks...")
        except:
            pass

        # Convert to pandas DataFrame (databento's native output)
        # Then process in chunks
        print(f"[3] Converting to DataFrame (this may take a while for large files)...")

        # For very large files, we need to iterate through the store
        # Unfortunately databento doesn't have a direct chunk iterator
        # So we load to pandas and then chunk it
        df_pandas = store.to_df()
        total_records = len(df_pandas)

        print(f"    Total records in file: {total_records:,}")

        # Reset index to make timestamp a column
        df_pandas = df_pandas.reset_index()
        if 'index' in df_pandas.columns:
            df_pandas = df_pandas.rename(columns={'index': 'ts_event'})

        # Process in chunks
        print(f"\n[4] Processing in chunks of {CHUNK_SIZE:,}...")

        for start_idx in range(0, total_records, CHUNK_SIZE):
            end_idx = min(start_idx + CHUNK_SIZE, total_records)
            chunk_num += 1

            # Extract chunk
            chunk_pandas = df_pandas.iloc[start_idx:end_idx]
            df_chunk = pl.from_pandas(chunk_pandas)

            # Process chunk
            df_processed = process_chunk(df_chunk, calculator, chunk_num)

            if df_processed is not None and len(df_processed) > 0:
                # Store chunk
                with DuckDBStorage() as storage:
                    storage.insert_mbp_ticks(df_processed, symbol=SYMBOL)

                total_processed += len(df_processed)

            # Progress update
            pct = 100 * end_idx / total_records
            print(f"    Chunk {chunk_num}: {start_idx:,}-{end_idx:,} | "
                  f"Processed: {total_processed:,} | Progress: {pct:.1f}%")

            # Free memory
            del chunk_pandas
            del df_chunk
            if df_processed is not None:
                del df_processed

        # Clean up
        del df_pandas

        print(f"\n[5] Complete! Processed {total_processed:,} MNQ records")
        return total_processed

    except MemoryError:
        print(f"\n[ERROR] Out of memory! File is too large.")
        print(f"        Try processing a smaller file or increasing system RAM.")
        return total_processed

    except Exception as e:
        print(f"\n[ERROR] Failed to process file: {e}")
        import traceback
        traceback.print_exc()
        return total_processed


def process_file_iterative(file_path: Path) -> int:
    """
    Alternative: Process DBN file by iterating through records
    Use this if the streaming approach still runs out of memory

    Args:
        file_path: Path to DBN file

    Returns:
        Number of records processed
    """
    print(f"\n{'='*60}")
    print(f"Processing (iterative): {file_path.name}")
    size_mb = file_path.stat().st_size / 1024 / 1024
    print(f"File size: {size_mb:.1f} MB")
    print(f"{'='*60}")

    calculator = OrderFlowCalculator()
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

        # Iterate through records one at a time
        for i, record in enumerate(store):
            # Convert record to dict
            record_dict = {
                'ts_event': record.ts_event,
            }

            # MBP-10 records have a 'levels' array of BidAskPair objects
            # Each BidAskPair has: bid_px, ask_px, bid_sz, ask_sz, bid_ct, ask_ct
            if hasattr(record, 'levels') and record.levels:
                for level_idx, level in enumerate(record.levels):
                    # Extract bid/ask data from BidAskPair
                    record_dict[f'bid_px_{level_idx:02d}'] = level.bid_px
                    record_dict[f'ask_px_{level_idx:02d}'] = level.ask_px
                    record_dict[f'bid_sz_{level_idx:02d}'] = level.bid_sz
                    record_dict[f'ask_sz_{level_idx:02d}'] = level.ask_sz
                    record_dict[f'bid_ct_{level_idx:02d}'] = level.bid_ct
                    record_dict[f'ask_ct_{level_idx:02d}'] = level.ask_ct
            else:
                # Fallback: check for flattened attributes (older format)
                for level in range(10):
                    for side in ['bid', 'ask']:
                        for field in ['px', 'sz', 'ct']:
                            attr_name = f'{side}_{field}_{level:02d}'
                            if hasattr(record, attr_name):
                                record_dict[attr_name] = getattr(record, attr_name)

            # Add symbol - from record if available, otherwise from metadata
            if hasattr(record, 'symbol'):
                record_dict['symbol'] = record.symbol
            elif is_mnq_file:
                record_dict['symbol'] = 'MNQ'  # Add MNQ symbol for filtering

            buffer.append(record_dict)

            # Process buffer when it reaches chunk size
            if len(buffer) >= CHUNK_SIZE:
                chunk_num += 1
                df_chunk = pl.DataFrame(buffer)
                buffer = []

                # Process chunk
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


def main():
    """Main execution flow"""
    print("=" * 70)
    print("  MBP-10 Data Loader - Order Flow Analysis (Chunked Processing)")
    print("=" * 70)
    print(f"\nSymbol: {SYMBOL}")
    print(f"Data directory: {DATA_DIR}")
    print(f"Chunk size: {CHUNK_SIZE:,} records")
    print(f"\nNote: This loads MBP-10 tick data SEPARATELY from OHLCV data")
    print(f"      OHLCV candlestick data is NOT affected.\n")

    # Find MBP-10 files
    files = find_mbp10_files(DATA_DIR)

    if not files:
        print("\n[ERROR] No MBP-10 files found")
        return

    # Check current tick count
    with DuckDBStorage() as storage:
        current_count = storage.get_mbp_tick_count(SYMBOL)
        print(f"\nCurrent MBP ticks in database: {current_count:,}")

    # Ask user which files to process
    print("\n" + "=" * 70)
    print("  Select files to process:")
    print("=" * 70)
    for i, f in enumerate(files, 1):
        size_mb = f.stat().st_size / 1024 / 1024
        print(f"  [{i}] {f.name} ({size_mb:.1f} MB)")
    print(f"  [A] Process ALL files")
    print(f"  [S] Process SMALLEST file (quick test)")
    print(f"  [Q] Quit")

    choice = input("\nEnter choice: ").strip().upper()

    if choice == 'Q':
        print("Exiting...")
        return
    elif choice == 'A':
        files_to_process = files
    elif choice == 'S':
        smallest = min(files, key=lambda f: f.stat().st_size)
        files_to_process = [smallest]
    elif choice.isdigit() and 1 <= int(choice) <= len(files):
        files_to_process = [files[int(choice) - 1]]
    else:
        print("Invalid choice")
        return

    # Ask for processing method
    print("\n" + "=" * 70)
    print("  Select processing method:")
    print("=" * 70)
    print("  [1] Streaming (faster, needs more RAM)")
    print("  [2] Iterative (slower, uses less RAM)")

    method = input("\nEnter choice [1]: ").strip() or "1"

    # Process selected files
    total_processed = 0

    print("\n" + "=" * 70)
    print(f"  Processing {len(files_to_process)} file(s)")
    print("=" * 70)

    for file_path in files_to_process:
        if method == "2":
            records = process_file_iterative(file_path)
        else:
            records = process_file_streaming(file_path)
        total_processed += records

    # Final summary
    print("\n" + "=" * 70)
    print("  Processing Complete!")
    print("=" * 70)

    with DuckDBStorage() as storage:
        final_count = storage.get_mbp_tick_count(SYMBOL)

    print(f"\nSummary:")
    print(f"  Files processed: {len(files_to_process)}")
    print(f"  Records added: {total_processed:,}")
    print(f"  Total MBP ticks in DB: {final_count:,}")

    print(f"\nNext steps:")
    print(f"1. Check data: SELECT * FROM mbp_ticks LIMIT 10;")
    print(f"2. Use for order flow analysis in regime classification")


if __name__ == "__main__":
    main()
