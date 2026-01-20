"""
Storage service for market data.
Supports both local filesystem (development) and AWS S3 (production).
"""
import os
from pathlib import Path
from typing import Optional, BinaryIO
from abc import ABC, abstractmethod
import logging

logger = logging.getLogger(__name__)


class StorageBackend(ABC):
    """Abstract storage backend interface"""

    @abstractmethod
    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """Upload a file to storage"""
        pass

    @abstractmethod
    def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from storage"""
        pass

    @abstractmethod
    def list_files(self, prefix: str = "") -> list[str]:
        """List files in storage with optional prefix"""
        pass

    @abstractmethod
    def delete_file(self, remote_path: str) -> bool:
        """Delete a file from storage"""
        pass

    @abstractmethod
    def file_exists(self, remote_path: str) -> bool:
        """Check if file exists in storage"""
        pass

    @abstractmethod
    def get_file_size(self, remote_path: str) -> Optional[int]:
        """Get file size in bytes"""
        pass


class LocalStorageBackend(StorageBackend):
    """Local filesystem storage backend for development"""

    def __init__(self, base_path: str = "./data"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Using local storage at: {self.base_path.absolute()}")

    def _get_full_path(self, remote_path: str) -> Path:
        """Convert remote path to full local path"""
        return self.base_path / remote_path

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """Copy file to local storage"""
        try:
            source = Path(local_path)
            dest = self._get_full_path(remote_path)
            dest.parent.mkdir(parents=True, exist_ok=True)

            import shutil
            shutil.copy2(source, dest)
            logger.info(f"Uploaded {local_path} to {dest}")
            return True
        except Exception as e:
            logger.error(f"Failed to upload {local_path}: {e}")
            return False

    def download_file(self, remote_path: str, local_path: str) -> bool:
        """Copy file from local storage"""
        try:
            source = self._get_full_path(remote_path)
            dest = Path(local_path)
            dest.parent.mkdir(parents=True, exist_ok=True)

            import shutil
            shutil.copy2(source, dest)
            logger.info(f"Downloaded {source} to {local_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to download {remote_path}: {e}")
            return False

    def list_files(self, prefix: str = "") -> list[str]:
        """List files in local storage"""
        try:
            search_path = self._get_full_path(prefix)
            if search_path.is_file():
                return [prefix]
            elif search_path.is_dir():
                files = []
                for file in search_path.rglob("*"):
                    if file.is_file():
                        rel_path = file.relative_to(self.base_path)
                        files.append(str(rel_path).replace("\\", "/"))
                return sorted(files)
            return []
        except Exception as e:
            logger.error(f"Failed to list files with prefix {prefix}: {e}")
            return []

    def delete_file(self, remote_path: str) -> bool:
        """Delete file from local storage"""
        try:
            file_path = self._get_full_path(remote_path)
            if file_path.exists():
                file_path.unlink()
                logger.info(f"Deleted {file_path}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to delete {remote_path}: {e}")
            return False

    def file_exists(self, remote_path: str) -> bool:
        """Check if file exists in local storage"""
        return self._get_full_path(remote_path).exists()

    def get_file_size(self, remote_path: str) -> Optional[int]:
        """Get file size in bytes"""
        try:
            file_path = self._get_full_path(remote_path)
            if file_path.exists():
                return file_path.stat().st_size
            return None
        except Exception as e:
            logger.error(f"Failed to get size of {remote_path}: {e}")
            return None


class S3StorageBackend(StorageBackend):
    """AWS S3 storage backend for production"""

    def __init__(self, bucket_name: str, region: str = "us-east-1"):
        try:
            import boto3
            from botocore.exceptions import ClientError

            self.bucket_name = bucket_name
            self.region = region
            self.s3_client = boto3.client('s3', region_name=region)
            self.ClientError = ClientError

            # Verify bucket exists
            try:
                self.s3_client.head_bucket(Bucket=bucket_name)
                logger.info(f"Connected to S3 bucket: {bucket_name}")
            except ClientError as e:
                error_code = e.response['Error']['Code']
                if error_code == '404':
                    logger.error(f"Bucket {bucket_name} does not exist")
                else:
                    logger.error(f"Error accessing bucket {bucket_name}: {e}")
                raise

        except ImportError:
            logger.error("boto3 not installed. Run: pip install boto3")
            raise

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """Upload file to S3"""
        try:
            self.s3_client.upload_file(local_path, self.bucket_name, remote_path)
            logger.info(f"Uploaded {local_path} to s3://{self.bucket_name}/{remote_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to upload {local_path} to S3: {e}")
            return False

    def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download file from S3"""
        try:
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            self.s3_client.download_file(self.bucket_name, remote_path, local_path)
            logger.info(f"Downloaded s3://{self.bucket_name}/{remote_path} to {local_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to download {remote_path} from S3: {e}")
            return False

    def list_files(self, prefix: str = "") -> list[str]:
        """List files in S3 bucket"""
        try:
            files = []
            paginator = self.s3_client.get_paginator('list_objects_v2')
            pages = paginator.paginate(Bucket=self.bucket_name, Prefix=prefix)

            for page in pages:
                if 'Contents' in page:
                    for obj in page['Contents']:
                        files.append(obj['Key'])

            return sorted(files)
        except Exception as e:
            logger.error(f"Failed to list files in S3 with prefix {prefix}: {e}")
            return []

    def delete_file(self, remote_path: str) -> bool:
        """Delete file from S3"""
        try:
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=remote_path)
            logger.info(f"Deleted s3://{self.bucket_name}/{remote_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete {remote_path} from S3: {e}")
            return False

    def file_exists(self, remote_path: str) -> bool:
        """Check if file exists in S3"""
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=remote_path)
            return True
        except self.ClientError as e:
            if e.response['Error']['Code'] == '404':
                return False
            logger.error(f"Error checking if {remote_path} exists in S3: {e}")
            return False

    def get_file_size(self, remote_path: str) -> Optional[int]:
        """Get file size in bytes from S3"""
        try:
            response = self.s3_client.head_object(Bucket=self.bucket_name, Key=remote_path)
            return response['ContentLength']
        except self.ClientError as e:
            if e.response['Error']['Code'] == '404':
                return None
            logger.error(f"Failed to get size of {remote_path} from S3: {e}")
            return None


class StorageService:
    """
    Unified storage service that automatically selects backend based on environment.

    Environment variables:
    - STORAGE_BACKEND: 'local' or 's3' (default: 'local')
    - AWS_S3_BUCKET: S3 bucket name (required for S3)
    - AWS_REGION: AWS region (default: 'us-east-1')
    - LOCAL_STORAGE_PATH: Local storage path (default: './data')
    """

    def __init__(self):
        backend_type = os.getenv("STORAGE_BACKEND", "local").lower()

        if backend_type == "s3":
            bucket_name = os.getenv("AWS_S3_BUCKET")
            if not bucket_name:
                raise ValueError("AWS_S3_BUCKET environment variable required for S3 storage")

            region = os.getenv("AWS_REGION", "us-east-1")
            self.backend = S3StorageBackend(bucket_name, region)
            logger.info(f"Storage service initialized with S3 backend (bucket: {bucket_name})")
        else:
            storage_path = os.getenv("LOCAL_STORAGE_PATH", "./data")
            self.backend = LocalStorageBackend(storage_path)
            logger.info(f"Storage service initialized with local backend (path: {storage_path})")

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        """Upload a file to storage"""
        return self.backend.upload_file(local_path, remote_path)

    def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from storage"""
        return self.backend.download_file(remote_path, local_path)

    def list_files(self, prefix: str = "") -> list[str]:
        """List files in storage with optional prefix"""
        return self.backend.list_files(prefix)

    def delete_file(self, remote_path: str) -> bool:
        """Delete a file from storage"""
        return self.backend.delete_file(remote_path)

    def file_exists(self, remote_path: str) -> bool:
        """Check if file exists in storage"""
        return self.backend.file_exists(remote_path)

    def get_file_size(self, remote_path: str) -> Optional[int]:
        """Get file size in bytes"""
        return self.backend.get_file_size(remote_path)

    def get_storage_stats(self, prefix: str = "") -> dict:
        """Get storage statistics for files with given prefix"""
        files = self.list_files(prefix)
        total_size = 0
        file_count = len(files)

        for file in files:
            size = self.get_file_size(file)
            if size:
                total_size += size

        return {
            "file_count": file_count,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "total_size_gb": round(total_size / (1024 * 1024 * 1024), 2),
        }


# Global storage service instance
storage_service = StorageService()
