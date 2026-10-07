"""Pluggable file storage: local disk (default) or S3-compatible object storage."""
from __future__ import annotations

import logging
import os
import shutil
from typing import BinaryIO, Optional

from .config import (
    DATA_DIR,
    S3_ACCESS_KEY,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_REGION,
    S3_SECRET_KEY,
    STORAGE_BACKEND,
)

logger = logging.getLogger(__name__)


class StorageBackend:
    def save_bytes(self, key: str, data: bytes) -> str:
        raise NotImplementedError

    def save_file(self, key: str, source_path: str) -> str:
        raise NotImplementedError

    def open_path(self, key_or_path: str) -> Optional[str]:
        """Return a local filesystem path readable by FileResponse, or None."""
        raise NotImplementedError

    def exists(self, key_or_path: str) -> bool:
        raise NotImplementedError

    def delete(self, key_or_path: str) -> None:
        raise NotImplementedError


class LocalStorage(StorageBackend):
    def __init__(self, root: str = DATA_DIR):
        self.root = root
        os.makedirs(self.root, exist_ok=True)

    def _abs(self, key: str) -> str:
        if os.path.isabs(key):
            return key
        return os.path.join(self.root, key.replace("\\", "/"))

    def save_bytes(self, key: str, data: bytes) -> str:
        path = self._abs(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def save_file(self, key: str, source_path: str) -> str:
        path = self._abs(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.abspath(source_path) != os.path.abspath(path):
            shutil.copy2(source_path, path)
        return path

    def open_path(self, key_or_path: str) -> Optional[str]:
        path = self._abs(key_or_path) if not os.path.isabs(key_or_path) else key_or_path
        return path if os.path.exists(path) else None

    def exists(self, key_or_path: str) -> bool:
        return self.open_path(key_or_path) is not None

    def delete(self, key_or_path: str) -> None:
        path = self.open_path(key_or_path)
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.warning("Could not delete %s", path)


class S3Storage(StorageBackend):
    """S3-compatible backend. Files are also mirrored under DATA_DIR for local reads."""

    def __init__(self):
        if not S3_BUCKET:
            raise RuntimeError("S3_BUCKET is required when STORAGE_BACKEND=s3")
        try:
            import boto3
        except ImportError as err:
            raise RuntimeError("boto3 is required for S3 storage") from err
        self.bucket = S3_BUCKET
        self.local = LocalStorage()
        kwargs = {"region_name": S3_REGION}
        if S3_ENDPOINT_URL:
            kwargs["endpoint_url"] = S3_ENDPOINT_URL
        if S3_ACCESS_KEY and S3_SECRET_KEY:
            kwargs["aws_access_key_id"] = S3_ACCESS_KEY
            kwargs["aws_secret_access_key"] = S3_SECRET_KEY
        self.client = boto3.client("s3", **kwargs)

    def save_bytes(self, key: str, data: bytes) -> str:
        local_path = self.local.save_bytes(key, data)
        try:
            self.client.put_object(Bucket=self.bucket, Key=key.replace("\\", "/"), Body=data)
        except Exception:
            logger.exception("S3 put_object failed for %s; local copy kept", key)
        return local_path

    def save_file(self, key: str, source_path: str) -> str:
        local_path = self.local.save_file(key, source_path)
        try:
            self.client.upload_file(local_path, self.bucket, key.replace("\\", "/"))
        except Exception:
            logger.exception("S3 upload_file failed for %s; local copy kept", key)
        return local_path

    def open_path(self, key_or_path: str) -> Optional[str]:
        local = self.local.open_path(key_or_path)
        if local:
            return local
        key = key_or_path.replace("\\", "/")
        try:
            dest = self.local._abs(key)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            self.client.download_file(self.bucket, key, dest)
            return dest
        except Exception:
            logger.exception("S3 download failed for %s", key)
            return None

    def exists(self, key_or_path: str) -> bool:
        if self.local.exists(key_or_path):
            return True
        key = key_or_path.replace("\\", "/")
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def delete(self, key_or_path: str) -> None:
        self.local.delete(key_or_path)
        key = key_or_path.replace("\\", "/")
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception:
            logger.warning("S3 delete failed for %s", key)


_storage: Optional[StorageBackend] = None


def get_storage() -> StorageBackend:
    global _storage
    if _storage is not None:
        return _storage
    if STORAGE_BACKEND == "s3":
        try:
            _storage = S3Storage()
            logger.info("Using S3 storage backend bucket=%s", S3_BUCKET)
            return _storage
        except Exception as err:
            logger.warning("S3 storage unavailable (%s); falling back to local", err)
    _storage = LocalStorage()
    return _storage
