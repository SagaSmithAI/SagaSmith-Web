from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

import boto3


class LocalPrivateStorage:
    """Private filesystem backend for development and single-server deployments."""

    def __init__(self, root: str, exchange_root: str) -> None:
        self.root = Path(root).resolve()
        self.exchange_root = Path(exchange_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.exchange_root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, source: BinaryIO, *, max_bytes: int) -> tuple[str, int]:
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

    def materialize_for_runtime(self, key: str, artifact_id: str) -> Path:
        source = (self.root / key).resolve()
        if self.root not in source.parents or not source.is_file():
            raise FileNotFoundError(key)
        destination = (self.exchange_root / f"{artifact_id}.sagapack").resolve()
        if self.exchange_root not in destination.parents:
            raise ValueError("invalid exchange path")
        shutil.copyfile(source, destination)
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

    def put(self, key: str, source: BinaryIO, *, max_bytes: int) -> tuple[str, int]:
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
                    "ContentType": "application/vnd.sagasmith.content-package+zip",
                    "Metadata": {"sha256": digest.hexdigest()},
                },
            )
            return digest.hexdigest(), size
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    def materialize_for_runtime(self, key: str, artifact_id: str) -> Path:
        destination = (self.exchange_root / f"{artifact_id}.sagapack").resolve()
        if self.exchange_root not in destination.parents:
            raise ValueError("invalid exchange path")
        self.client.download_file(self.bucket, key, str(destination))
        return destination
