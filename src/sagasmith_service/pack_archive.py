from __future__ import annotations

import json
import re
import zipfile
from typing import Any, BinaryIO

ARCHIVE_DESCRIPTOR = "package.sagasmith.json"
ARCHIVE_EXTENSION = ".sagasmith-pack"
CONTENT_PACKAGE_FORMAT = "sagasmith.content-package"
_BLOB_PATH = re.compile(r"blobs/sha256/[0-9a-f]{64}")


class PackArchiveError(ValueError):
    pass


def validate_pack_archive(
    source: BinaryIO,
    *,
    expected_id: str,
    expected_version: str,
    expected_kind: str,
    max_bytes: int,
    max_uncompressed_bytes: int,
) -> dict[str, Any]:
    source.seek(0, 2)
    compressed_size = source.tell()
    source.seek(0)
    if compressed_size <= 0:
        raise PackArchiveError("Pack archive is empty")
    if compressed_size > max_bytes:
        raise PackArchiveError("upload exceeds configured Pack size limit")
    try:
        archive = zipfile.ZipFile(source, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackArchiveError("Pack must be a valid unified content archive") from exc
    try:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(infos) > 100_000:
            raise PackArchiveError("Pack archive contains too many entries")
        if len(names) != len(set(names)):
            raise PackArchiveError("Pack archive contains duplicate paths")
        if ARCHIVE_DESCRIPTOR not in names:
            raise PackArchiveError("Pack archive descriptor is missing")
        if any(item.flag_bits & 0x1 for item in infos):
            raise PackArchiveError("encrypted Pack entries are unsupported")
        if any(name != ARCHIVE_DESCRIPTOR and _BLOB_PATH.fullmatch(name) is None for name in names):
            raise PackArchiveError("Pack archive contains unsupported paths")
        if sum(item.file_size for item in infos) > max_uncompressed_bytes:
            raise PackArchiveError("Pack archive exceeds the uncompressed size limit")
        if any(
            item.file_size > 64 * 1024 * 1024
            and item.compress_size > 0
            and item.file_size / item.compress_size > 200
            for item in infos
        ):
            raise PackArchiveError("Pack archive contains an unsafe compression ratio")
        descriptor_info = archive.getinfo(ARCHIVE_DESCRIPTOR)
        if descriptor_info.file_size > 8 * 1024 * 1024:
            raise PackArchiveError("Pack descriptor exceeds the size limit")
        try:
            descriptor = json.loads(archive.read(ARCHIVE_DESCRIPTOR).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PackArchiveError("Pack archive descriptor is invalid") from exc
    finally:
        archive.close()
        source.seek(0)
    if not isinstance(descriptor, dict):
        raise PackArchiveError("Pack archive descriptor must be an object")
    expected = {
        "format": CONTENT_PACKAGE_FORMAT,
        "id": expected_id,
        "version": expected_version,
        "kind": expected_kind,
    }
    mismatched = [key for key, value in expected.items() if descriptor.get(key) != value]
    if mismatched:
        raise PackArchiveError(
            "Pack form does not match descriptor fields: " + ", ".join(mismatched)
        )
    return descriptor
