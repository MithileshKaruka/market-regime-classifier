"""Data loading utilities for Databento DBN files"""
import logging
from pathlib import Path
from typing import Optional
import databento as db
import polars as pl

logger = logging.getLogger(__name__)


class DataLoader:
    """Load and process Databento DBN files"""

    def __init__(self, data_dir: str = "data"):
        """Initialize data loader

        Args:
            data_dir: Directory containing DBN and Parquet files
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True, parents=True)
        logger.info(f"DataLoader initialized with data_dir: {self.data_dir}")

    def load_dbn_file(self, filepath: Path) -> db.DBNStore:
        """Load a DBN file

        Args:
            filepath: Path to DBN file

        Returns:
            DBNStore object
        """
        logger.info(f"Loading DBN file: {filepath}")
        store = db.DBNStore.from_file(str(filepath))
        return store

    def dbn_to_polars(self, store: db.DBNStore) -> pl.DataFrame:
        """Convert DBN store to Polars DataFrame

        Args:
            store: DBNStore object

        Returns:
            Polars DataFrame with MBP-10 data
        """
        logger.info("Converting DBN to Polars DataFrame")

        # Convert to pandas first, then to polars (more reliable)
        df_pandas = store.to_df()
        df = pl.from_pandas(df_pandas)

        logger.info(f"Loaded {len(df)} records")
        return df

    def load_parquet(self, filepath: Path) -> pl.DataFrame:
        """Load Parquet file as Polars DataFrame

        Args:
            filepath: Path to Parquet file

        Returns:
            Polars DataFrame
        """
        logger.info(f"Loading Parquet file: {filepath}")
        df = pl.read_parquet(filepath)
        logger.info(f"Loaded {len(df)} records from Parquet")
        return df

    def save_parquet(self, df: pl.DataFrame, filepath: Path) -> None:
        """Save DataFrame to Parquet format

        Args:
            df: Polars DataFrame
            filepath: Output path for Parquet file
        """
        logger.info(f"Saving {len(df)} records to Parquet: {filepath}")
        filepath.parent.mkdir(exist_ok=True, parents=True)
        df.write_parquet(filepath)
        logger.info("Parquet file saved successfully")

    def list_data_files(self, extension: str = ".dbn") -> list[Path]:
        """List all data files in data directory

        Args:
            extension: File extension to filter (.dbn or .parquet)

        Returns:
            List of file paths
        """
        files = list(self.data_dir.glob(f"*{extension}"))
        logger.info(f"Found {len(files)} {extension} files")
        return sorted(files)

    def get_latest_data_file(self, extension: str = ".parquet") -> Optional[Path]:
        """Get the most recent data file

        Args:
            extension: File extension to look for

        Returns:
            Path to latest file or None
        """
        files = self.list_data_files(extension)
        if not files:
            logger.warning(f"No {extension} files found")
            return None

        latest = max(files, key=lambda p: p.stat().st_mtime)
        logger.info(f"Latest {extension} file: {latest}")
        return latest
