"""Weekly maintenance job for data cleanup

Schedule: Friday 4:30 PM CST (after CME close)

Tasks:
1. Clean up DBN archive files older than retention period
2. Clean up old OHLCV data from DuckDB
3. Vacuum database

Note: Live ingestion writes directly to .dbn.zst files via Databento's
native streaming, so no archival step is needed.
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta
import logging

# Add parent directories to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.data.storage import DuckDBStorage
from config import get_databento_config, get_retention_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class WeeklyMaintenance:
    """Weekly maintenance job handler"""

    def __init__(self):
        self.db_config = get_databento_config()
        self.retention = get_retention_config()
        self.archive_dir = Path(self.db_config['database'].archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def cleanup_old_dbn(self):
        """Clean up DBN archive files older than retention period"""
        logger.info("=" * 60)
        logger.info("Cleaning up old DBN archive files...")

        mbp_retention_days = self.retention.archive_mbp_days
        trades_retention_days = self.retention.archive_trades_days

        cutoff_mbp = datetime.utcnow() - timedelta(days=mbp_retention_days)
        cutoff_trades = datetime.utcnow() - timedelta(days=trades_retention_days)

        deleted_count = 0
        freed_bytes = 0

        # Process .dbn.zst files
        for dbn_file in self.archive_dir.glob("*.dbn.zst"):
            try:
                # Parse date from filename (e.g., mbp1_2024-01-15.dbn.zst)
                # Remove .dbn.zst suffix
                stem = dbn_file.name.replace('.dbn.zst', '')
                parts = stem.split('_')

                if len(parts) >= 2:
                    date_str = parts[-1]
                    file_date = datetime.strptime(date_str, '%Y-%m-%d')

                    # Determine retention based on file type
                    if 'mbp' in stem:
                        cutoff = cutoff_mbp
                    elif 'trades' in stem:
                        cutoff = cutoff_trades
                    else:
                        continue

                    if file_date < cutoff:
                        file_size = dbn_file.stat().st_size
                        dbn_file.unlink()
                        deleted_count += 1
                        freed_bytes += file_size
                        logger.info(f"  Deleted: {dbn_file.name} ({file_size / 1024 / 1024:.1f} MB)")

            except Exception as e:
                logger.warning(f"  Could not process {dbn_file.name}: {e}")

        logger.info(f"  Deleted {deleted_count} old DBN files, freed {freed_bytes / 1024 / 1024:.1f} MB")

    def cleanup_old_ohlcv(self):
        """Clean up ohlcv_ticks older than retention period"""
        logger.info("=" * 60)
        logger.info("Cleaning up old OHLCV data...")

        storage = DuckDBStorage()

        try:
            # OHLCV retention (5 years)
            cutoff_days = self.retention.ohlcv_ticks_days
            cutoff_date = datetime.utcnow() - timedelta(days=cutoff_days)

            # Check if there's data to delete
            count_result = storage.conn.execute(f"""
                SELECT COUNT(*) FROM ohlcv_ticks
                WHERE timestamp < '{cutoff_date}'
            """).fetchone()

            rows_to_delete = count_result[0] if count_result else 0

            if rows_to_delete == 0:
                logger.info("  No old OHLCV data to delete")
                return

            # Delete old data
            storage.conn.execute(f"""
                DELETE FROM ohlcv_ticks
                WHERE timestamp < '{cutoff_date}'
            """)
            storage.conn.commit()

            logger.info(f"  Deleted {rows_to_delete:,} rows older than {cutoff_days} days")

        except Exception as e:
            logger.error(f"  Error cleaning up OHLCV: {e}", exc_info=True)
        finally:
            storage.close()

    def vacuum_database(self):
        """Vacuum the database to reclaim space"""
        logger.info("=" * 60)
        logger.info("Vacuuming database...")

        storage = DuckDBStorage()

        try:
            # Vacuum (checkpoint in DuckDB)
            storage.conn.execute("CHECKPOINT")
            logger.info("  Database vacuumed successfully")

        except Exception as e:
            logger.error(f"  Error vacuuming database: {e}", exc_info=True)
        finally:
            storage.close()

    def print_summary(self):
        """Print summary of current data state"""
        logger.info("=" * 60)
        logger.info("Current Data Summary")
        logger.info("=" * 60)

        storage = DuckDBStorage()

        try:
            # OHLCV ticks summary
            result = storage.conn.execute("""
                SELECT
                    timeframe,
                    COUNT(*) as bars,
                    MIN(timestamp) as first,
                    MAX(timestamp) as last
                FROM ohlcv_ticks
                WHERE symbol = 'MNQ'
                GROUP BY timeframe
                ORDER BY timeframe
            """).fetchall()

            logger.info("\nohlcv_ticks:")
            for row in result:
                logger.info(f"  {row[0]:4}: {row[1]:,} bars | {row[2].date()} to {row[3].date()}")

            # DBN archive summary
            dbn_files = list(self.archive_dir.glob("*.dbn.zst"))
            total_size = sum(f.stat().st_size for f in dbn_files) if dbn_files else 0

            if dbn_files:
                # Get date range from filenames
                dates = []
                for f in dbn_files:
                    try:
                        stem = f.name.replace('.dbn.zst', '')
                        date_str = stem.split('_')[-1]
                        dates.append(date_str)
                    except Exception:
                        pass

                if dates:
                    dates.sort()
                    logger.info(f"\nDBN archive: {len(dbn_files)} files, {total_size / 1024 / 1024:.1f} MB")
                    logger.info(f"  Date range: {dates[0]} to {dates[-1]}")
            else:
                logger.info("\nDBN archive: empty")

        except Exception as e:
            logger.error(f"  Error getting summary: {e}", exc_info=True)
        finally:
            storage.close()

    def run(self):
        """Run all maintenance tasks"""
        start_time = datetime.utcnow()
        logger.info("=" * 60)
        logger.info(f"Weekly Maintenance Started: {start_time}")
        logger.info("=" * 60)

        # Run tasks
        self.cleanup_old_dbn()
        self.cleanup_old_ohlcv()
        self.vacuum_database()
        self.print_summary()

        end_time = datetime.utcnow()
        duration = (end_time - start_time).total_seconds()

        logger.info("=" * 60)
        logger.info(f"Weekly Maintenance Completed in {duration:.1f}s")
        logger.info("=" * 60)


def main():
    """Entry point for weekly maintenance"""
    maintenance = WeeklyMaintenance()
    maintenance.run()


if __name__ == "__main__":
    main()
