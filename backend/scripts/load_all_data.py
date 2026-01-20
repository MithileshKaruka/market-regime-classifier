"""
Unified Data Loader - Market Regime Classifier

This script runs the complete data loading pipeline:
1. Load OHLCV candlestick data (all timeframes)
2. Load MBP-10 tick data (selected files)
3. Update order_book with DOM imbalance and CVD from tick data

Usage:
    python scripts/load_all_data.py              # Interactive mode
    python scripts/load_all_data.py --ohlcv      # Load OHLCV only
    python scripts/load_all_data.py --mbp        # Load MBP-10 only
    python scripts/load_all_data.py --update     # Update orderflow metrics only
    python scripts/load_all_data.py --all        # Run all steps non-interactively
"""
import sys
import argparse
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from load_ohlcv import main as load_ohlcv_main, DATA_FILE as OHLCV_FILE
from load_mbp10 import main as load_mbp10_main, find_mbp10_files, process_file_streaming, DATA_DIR
from update_orderflow_metrics import update_orderflow_metrics
from app.data.storage import DuckDBStorage


def print_header(title: str):
    """Print a formatted header"""
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}\n")


def check_data_status():
    """Check current data status in database"""
    print_header("Current Data Status")

    with DuckDBStorage() as storage:
        # Check order_book
        for tf in ['1M', '5M', '15M', '1H', '4H', '1D']:
            count = storage.conn.execute(f"""
                SELECT COUNT(*) FROM order_book
                WHERE symbol = 'MNQ' AND timeframe = '{tf}'
            """).fetchone()[0]

            updated = storage.conn.execute(f"""
                SELECT COUNT(*) FROM order_book
                WHERE symbol = 'MNQ' AND timeframe = '{tf}' AND dom_imbalance != 0.5
            """).fetchone()[0]

            print(f"  order_book {tf}: {count:,} bars ({updated:,} with orderflow data)")

        # Check mbp_ticks
        tick_count = storage.conn.execute("SELECT COUNT(*) FROM mbp_ticks WHERE symbol = 'MNQ'").fetchone()[0]
        print(f"\n  mbp_ticks: {tick_count:,} ticks")

        if tick_count > 0:
            time_range = storage.conn.execute("""
                SELECT MIN(timestamp), MAX(timestamp) FROM mbp_ticks WHERE symbol = 'MNQ'
            """).fetchone()
            print(f"  Time range: {time_range[0]} to {time_range[1]}")


def load_ohlcv():
    """Load OHLCV data"""
    print_header("Step 1: Loading OHLCV Data")

    if not OHLCV_FILE.exists():
        print(f"[ERROR] OHLCV file not found: {OHLCV_FILE}")
        return False

    print(f"File: {OHLCV_FILE.name}")
    print(f"Size: {OHLCV_FILE.stat().st_size / 1024 / 1024:.1f} MB")

    try:
        load_ohlcv_main()
        return True
    except Exception as e:
        print(f"[ERROR] Failed to load OHLCV: {e}")
        return False


def load_mbp10(file_indices: list = None, method: str = "1"):
    """Load MBP-10 tick data

    Args:
        file_indices: List of file indices to process (1-based), or None for all
        method: "1" for streaming, "2" for iterative
    """
    print_header("Step 2: Loading MBP-10 Tick Data")

    files = find_mbp10_files(DATA_DIR)

    if not files:
        print("[ERROR] No MBP-10 files found")
        return False

    # Determine which files to process
    if file_indices is None:
        files_to_process = files
    else:
        files_to_process = [files[i-1] for i in file_indices if 0 < i <= len(files)]

    if not files_to_process:
        print("[ERROR] No valid files selected")
        return False

    print(f"Processing {len(files_to_process)} file(s)...")

    total_processed = 0
    for file_path in files_to_process:
        try:
            records = process_file_streaming(file_path)
            total_processed += records
        except Exception as e:
            print(f"[ERROR] Failed to process {file_path.name}: {e}")

    print(f"\nTotal records processed: {total_processed:,}")
    return total_processed > 0


def update_metrics():
    """Update order_book with DOM/CVD from tick data"""
    print_header("Step 3: Updating Order Flow Metrics")

    try:
        update_orderflow_metrics()
        return True
    except Exception as e:
        print(f"[ERROR] Failed to update metrics: {e}")
        return False


def interactive_mode():
    """Run in interactive mode with menu"""
    while True:
        print_header("Market Regime Classifier - Data Loader")

        print("  [1] Check data status")
        print("  [2] Load OHLCV data (candlesticks)")
        print("  [3] Load MBP-10 data (tick data)")
        print("  [4] Update order flow metrics (DOM/CVD)")
        print("  [5] Run full pipeline (all steps)")
        print("  [Q] Quit")

        choice = input("\nEnter choice: ").strip().upper()

        if choice == 'Q':
            print("Exiting...")
            break
        elif choice == '1':
            check_data_status()
        elif choice == '2':
            load_ohlcv()
        elif choice == '3':
            # Show MBP file selection
            files = find_mbp10_files(DATA_DIR)
            if files:
                print("\nSelect files to process:")
                for i, f in enumerate(files, 1):
                    size_mb = f.stat().st_size / 1024 / 1024
                    print(f"  [{i}] {f.name} ({size_mb:.1f} MB)")
                print(f"  [A] All files")

                file_choice = input("\nEnter choice (comma-separated for multiple): ").strip().upper()

                if file_choice == 'A':
                    load_mbp10()
                elif file_choice:
                    try:
                        indices = [int(x.strip()) for x in file_choice.split(',')]
                        load_mbp10(file_indices=indices)
                    except ValueError:
                        print("Invalid selection")
        elif choice == '4':
            update_metrics()
        elif choice == '5':
            print("\nRunning full pipeline...")
            if load_ohlcv():
                if load_mbp10():
                    update_metrics()
            print("\nPipeline complete!")
        else:
            print("Invalid choice")

        input("\nPress Enter to continue...")


def main():
    parser = argparse.ArgumentParser(description='Load market data for regime classifier')
    parser.add_argument('--ohlcv', action='store_true', help='Load OHLCV data only')
    parser.add_argument('--mbp', action='store_true', help='Load MBP-10 data only')
    parser.add_argument('--update', action='store_true', help='Update orderflow metrics only')
    parser.add_argument('--all', action='store_true', help='Run all steps non-interactively')
    parser.add_argument('--status', action='store_true', help='Check data status')

    args = parser.parse_args()

    # If no flags, run interactive mode
    if not any([args.ohlcv, args.mbp, args.update, args.all, args.status]):
        interactive_mode()
        return

    # Run specific steps
    if args.status:
        check_data_status()

    if args.ohlcv or args.all:
        load_ohlcv()

    if args.mbp or args.all:
        load_mbp10()

    if args.update or args.all:
        update_metrics()

    # Show final status
    if args.all:
        check_data_status()


if __name__ == "__main__":
    main()
