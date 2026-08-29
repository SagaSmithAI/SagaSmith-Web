from __future__ import annotations

import hashlib
import io
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

import boto3

from sagasmith_service.pack_archive import ARCHIVE_EXTENSION


class PrivateStorageError(RuntimeError):
    """A private object cannot be materialized at the runtime boundary."""


class LocalPrivateStorage:
    """Private filesystem backend for development and single-server deployments."""

    def __init__(self, root: str, exchange_root: str) -> None:
        self.root = Path(root).resolve()
        self.exchange_root = Path(exchange_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.exchange_root.mkdir(parents=True, exist_ok=True)

    def probe(self) -> None:
        if not self.root.is_dir() or not self.exchange_root.is_dir():
            raise PrivateStorageError("private storage is unavailable")

    def put(
        self,
        key: str,
        source: BinaryIO,
        *,
        max_bytes: int,
        content_type: str = "application/vnd.sagasmith.content-package+zip",
    ) -> tuple[str, int]:
        destination = (self.root / key).resolve()
        if self.root not in destination.parents:
            raise ValueError("invalid storage key")
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        with destination.open("xb") as target:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    target.close()
                    destination.unlink(missing_ok=True)
                    raise ValueError("upload exceeds configured Pack size limit")
                digest.update(chunk)
                target.write(chunk)
        return digest.hexdigest(), size

    def read_bytes(self, key: str, *, max_bytes: int) -> bytes:
        source = (self.root / key).resolve()
        if self.root not in source.parents or not source.is_file():
            raise PrivateStorageError("private object is unavailable")
        if source.stat().st_size > max_bytes:
            raise PrivateStorageError("private object exceeds the read limit")
        return source.read_bytes()

    def put_bytes_idempotent(
        self,
        key: str,
        payload: bytes,
        *,
        max_bytes: int,
        content_type: str,
    ) -> tuple[str, int]:
        if len(payload) > max_bytes:
            raise ValueError("object exceeds configured size limit")
        destination = (self.root / key).resolve()
        if self.root not in destination.parents:
            raise ValueError("invalid storage key")
        digest = hashlib.sha256(payload).hexdigest()
        if destination.is_file():
            existing = destination.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise PrivateStorageError("idempotent object key contains different bytes")
            return digest, len(existing)
        return self.put(
            key,
            io.BytesIO(payload),
            max_bytes=max_bytes,
            content_type=content_type,
        )

    def delete(self, key: str) -> None:
        source = (self.root / key).resolve()
        if self.root not in source.parents:
            raise ValueError("invalid storage key")
        source.unlink(missing_ok=True)

    def materialize_source(self, key: str, artifact_id: str, name: str) -> Path:
        source = (self.root / key).resolve()
        if self.root not in source.parents or not source.is_file():
            raise PrivateStorageError("module source object is unavailable")
        suffix = Path(name).suffix.casefold()
        if suffix not in {".pdf", ".md", ".markdown", ".txt"}:
            raise ValueError("unsupported module source extension")
        destination = (self.exchange_root / f"module-source-{artifact_id}{suffix}").resolve()
        if self.exchange_root not in destination.parents:
            raise ValueError("invalid exchange path")
        try:
            shutil.copyfile(source, destination)
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise PrivateStorageError("module source could not be materialized") from exc
        return destination

    def materialize_for_runtime(self, key: str, artifact_id: str) -> Path:
        source = (self.root / key).resolve()
        if self.root not in source.parents or not source.is_file():
            raise PrivateStorageError("private Pack object is unavailable")
        destination = (self.exchange_root / f"{artifact_id}{ARCHIVE_EXTENSION}").resolve()
        if self.exchange_root not in destination.parents:
            raise ValueError("invalid exchange path")
        try:
            shutil.copyfile(source, destination)
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise PrivateStorageError("private Pack object could not be materialized") from exc
        return destination


class S3PrivateStorage:
    """S3-compatible private backend with a shared runtime exchange volume."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        exchange_root: str,
    ) -> None:
        self.bucket = bucket
        self.exchange_root = Path(exchange_root).resolve()
        self.exchange_root.mkdir(parents=True, exist_ok=True)
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        try:
            self.client.head_bucket(Bucket=bucket)
        except Exception:
            self.client.create_bucket(Bucket=bucket)

    def probe(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            raise PrivateStorageError("private storage is unavailable") from exc

    def put(
        self,
        key: str,
        source: BinaryIO,
        *,
        max_bytes: int,
        content_type: str = "application/vnd.sagasmith.content-package+zip",
    ) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False) as temporary:
                temporary_name = temporary.name
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("upload exceeds configured Pack size limit")
                    digest.update(chunk)
                    temporary.write(chunk)
            self.client.upload_file(
                temporary_name,
                self.bucket,
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": {"sha256": digest.hexdigest()},
                },
            )
            return digest.hexdigest(), size
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    def read_bytes(self, key: str, *, max_bytes: int) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            size = int(response.get("ContentLength") or 0)
            if size > max_bytes:
                raise PrivateStorageError("private object exceeds the read limit")
            payload = response["Body"].read(max_bytes + 1)
        except PrivateStorageError:
            raise
        except Exception as exc:
            raise PrivateStorageError("private object is unavailable") from exc
        if len(payload) > max_bytes:
            raise PrivateStorageError("private object exceeds the read limit")
        return bytes(payload)

    def put_bytes_idempotent(
        self,
        key: str,
        payload: bytes,
        *,
        max_bytes: int,
        content_type: str,
    ) -> tuple[str, int]:
        if len(payload) > max_bytes:
            raise ValueError("object exceeds configured size limit")
        return self.put(
            key,
            io.BytesIO(payload),
            max_bytes=max_bytes,
            content_type=content_type,
        )

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise PrivateStorageError("private object could not be deleted") from exc

    def materialize_source(self, key: str, artifact_id: str, name: str) -> Path:
        suffix = Path(name).suffix.casefold()
        if suffix not in {".pdf", ".md", ".markdown", ".txt"}:
            raise ValueError("unsupported module source extension")
        destination = (self.exchange_root / f"module-source-{artifact_id}{suffix}").resolve()
        if self.exchange_root not in destination.parents:
            raise ValueError("invalid exchange path")
        try:
            self.client.download_file(self.bucket, key, str(destination))
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise PrivateStorageError("module source could not be materialized") from exc
        return destination

    def materialize_for_runtime(self, key: str, artifact_id: str) -> Path:
        destination = (self.exchange_root / f"{artifact_id}{ARCHIVE_EXTENSION}").resolve()
        if self.exchange_root not in destination.parents:
            raise ValueError("invalid exchange path")
        try:
            self.client.download_file(self.bucket, key, str(destination))
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise PrivateStorageError("private Pack object could not be materialized") from exc
        return destination
