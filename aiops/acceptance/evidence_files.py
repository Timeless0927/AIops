"""Crash-durable regular-file primitives for Acceptance Evidence."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def json_matches(path: Path, value: object) -> bool:
    try:
        return json.loads(path.read_text(encoding="utf-8")) == value
    except (OSError, json.JSONDecodeError):
        return False


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, content: bytes, *, staging_dir: Path | None = None) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=staging_dir or path.parent, prefix=".aiops-evidence-", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(path)
        fsync_directory(path.parent)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


@contextmanager
def unchanged_file_lock(path: Path, expected_sha256: str | None) -> Iterator[bool]:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current_sha256 = (
            sha256(path)
            if path.is_file() and not path.is_symlink()
            else "" if path.exists() or path.is_symlink() else None
        )
        yield current_sha256 == expected_sha256
    finally:
        os.close(descriptor)


def atomic_write_if_unchanged(
    path: Path,
    content: bytes,
    *,
    expected_sha256: str | None,
    staging_dir: Path | None = None,
) -> bool:
    with unchanged_file_lock(path, expected_sha256) as unchanged:
        if not unchanged:
            return False
        atomic_write(path, content, staging_dir=staging_dir)
        return True
