
"""CourseVault ZIP security inspection.

Inspects an archive without extracting files to the filesystem.

Checks:
- Maximum compressed archive size
- Maximum number of entries
- Absolute paths, traversal, and unsafe path components
- Duplicate and case-colliding names
- Symbolic links and encrypted entries
- Suspicious compression ratios and expanded sizes
- Corrupt entries and CRC errors
- SHA-256 integrity hash

This is a defensive archive validator, not a malware scanner. Passing
inspection does not prove that an archive is safe.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import zipfile

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO


logger = logging.getLogger("coursevault.zip_security")

# Keep these limits aligned with the bot's upload limits and the hosting tier.
MAX_ARCHIVE_BYTES = 45 * 1024 * 1024
MAX_ENTRIES = 5_000
MAX_SINGLE_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_EXPANDED_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
MAX_FILENAME_LENGTH = 240
READ_CHUNK_BYTES = 64 * 1024

SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")


@dataclass(frozen=True)
class ZipInspectionResult:
    passed: bool
    sha256: str | None
    archive_size_bytes: int
    entry_count: int
    expanded_size_bytes: int
    errors: tuple[str, ...]


def _safe_error(message: str) -> str:
    """Keep errors concise and avoid returning archive content."""
    return message[:300]


def _is_unsafe_name(name: str) -> bool:
    """Reject dangerous or ambiguous archive entry paths."""
    if not name or "\x00" in name:
        return True

    if len(name) > MAX_FILENAME_LENGTH:
        return True

    # ZIP entry names use forward slashes by specification, but reject
    # backslashes too because some extraction tools treat them as separators.
    if "\\" in name:
        return True

    if name.startswith("/") or name.startswith("//"):
        return True

    # Reject Windows drive paths such as C:/Windows/file.
    if re.match(r"^[a-zA-Z]:", name):
        return True

    parts = name.split("/")

    # Directory entries may end with a slash, so ignore only the final
    # empty component. Empty intermediate components are rejected.
    if name.endswith("/"):
        parts = parts[:-1]

    if not parts:
        return True

    if any(part in ("", ".", "..") for part in parts):
        return True

    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        return True

    return any(ord(char) < 32 for char in name)


def _hash_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0

    while True:
        chunk = stream.read(READ_CHUNK_BYTES)
        if not chunk:
            break

        digest.update(chunk)
        total += len(chunk)

        if total > MAX_ARCHIVE_BYTES:
            raise ValueError("Archive exceeds the configured size limit")

    return digest.hexdigest(), total


def _validate_entry_metadata(
    info: zipfile.ZipInfo,
    seen_names: set[str],
) -> str | None:
    """Return a validation error, or None when metadata is acceptable."""
    name = info.filename

    if _is_unsafe_name(name):
        return "Unsafe archive path"

    normalized = name.casefold()
    if normalized in seen_names:
        return "Duplicate or case-colliding archive filename"
    seen_names.add(normalized)

    if info.flag_bits & 0x1:
        return "Encrypted ZIP entries are not supported"

    if info.file_size < 0 or info.compress_size < 0:
        return "Invalid ZIP entry size"

    if info.file_size > MAX_SINGLE_FILE_BYTES:
        return "An archive entry exceeds the per-file expanded-size limit"

    # Unix mode metadata can identify symbolic links. Do not permit them.
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    if stat.S_ISLNK(unix_mode):
        return "Symbolic links are not permitted in course archives"

    # Reject special Unix filesystem entries such as devices, FIFOs,
    # and sockets. Regular files and directories are permitted.
    file_type = stat.S_IFMT(unix_mode)
    if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
        return "Special filesystem entries are not permitted"

    if info.file_size > 0:
        if info.compress_size == 0:
            return "Suspicious compression metadata"

        ratio = info.file_size / info.compress_size
        if ratio > MAX_COMPRESSION_RATIO:
            return "Archive entry exceeds the compression-ratio limit"

    return None


def inspect_zip_path(path: str | os.PathLike[str]) -> ZipInspectionResult:
    """Inspect a ZIP file at a local temporary path.

    The caller must ensure the path points to a regular file in a
    controlled temporary directory and must remove it after processing.

    The archive is never extracted to the filesystem. Compressed content
    is read in chunks to check integrity and CRC values.
    """
    archive_size = 0
    entry_count = 0
    expanded_total = 0
    digest: str | None = None
    errors: list[str] = []

    try:
        archive_path = os.fspath(path)

        if not os.path.isfile(archive_path):
            return ZipInspectionResult(
                False, None, 0, 0, 0, ("Input is not a regular file",)
            )

        archive_size = os.path.getsize(archive_path)

        if archive_size <= 0:
            return ZipInspectionResult(
                False, None, archive_size, 0, 0, ("Archive is empty",)
            )

        if archive_size > MAX_ARCHIVE_BYTES:
            return ZipInspectionResult(
                False,
                None,
                archive_size,
                0,
                0,
                ("Archive exceeds the configured size limit",),
            )

        with open(archive_path, "rb") as raw:
            digest, bytes_read = _hash_stream(raw)

        if bytes_read != archive_size:
            return ZipInspectionResult(
                False,
                digest,
                archive_size,
                0,
                0,
                ("Archive changed during inspection",),
            )

        seen_names: set[str] = set()

        with zipfile.ZipFile(archive_path, mode="r") as archive:
            infos = archive.infolist()
            entry_count = len(infos)

            if not infos:
                errors.append("Archive contains no entries")

            if entry_count > MAX_ENTRIES:
                errors.append("Archive contains too many entries")

            for info in infos:
                if errors:
                    break

                error = _validate_entry_metadata(info, seen_names)
                if error:
                    errors.append(error)
                    break

                expanded_total += info.file_size
                if expanded_total > MAX_TOTAL_EXPANDED_BYTES:
                    errors.append("Archive expanded size exceeds the limit")
                    break

            # Read every regular file to verify decompression and CRC.
            # No archive entry is written to disk.
            if not errors:
                for info in infos:
                    if info.is_dir():
                        continue

                    try:
                        with archive.open(info, mode="r") as entry:
                            actual_size = 0

                            while True:
                                chunk = entry.read(READ_CHUNK_BYTES)
                                if not chunk:
                                    break

                                actual_size += len(chunk)

                                if actual_size > MAX_SINGLE_FILE_BYTES:
                                    errors.append(
                                        "An entry exceeded the read-time size limit"
                                    )
                                    break

                            if actual_size != info.file_size:
                                errors.append(
                                    "An entry's actual size did not match its metadata"
                                )
                                break

                    except (
                        OSError,
                        RuntimeError,
                        EOFError,
                        zipfile.BadZipFile,
                        NotImplementedError,
                    ):
                        errors.append(
                            "An entry could not be safely decompressed or verified"
                        )
                        break

    except (
        OSError,
        zipfile.BadZipFile,
        RuntimeError,
        EOFError,
        ValueError,
    ):
        logger.warning("ZIP inspection failed")
        errors.append("Archive is invalid, unreadable, or changed during inspection")

    return ZipInspectionResult(
        passed=not errors,
        sha256=digest,
        archive_size_bytes=archive_size,
        entry_count=entry_count,
        expanded_size_bytes=expanded_total,
        errors=tuple(_safe_error(error) for error in errors),
    )


def valid_sha256(value: str | None) -> bool:
    """Check whether a value is a hexadecimal SHA-256 digest."""
    return bool(value and SHA256_RE.fullmatch(value))
