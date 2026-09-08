"""On-disk storage for file blobs (batch input/output/error JSONL).

Flat layout: <blob_dir>/<file_id>.jsonl. FileObject.path stores the
resulting path so this module's naming scheme can change later without
touching the DB schema.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def blob_path(blob_dir: Path, file_id: str) -> Path:
    return blob_dir / f"{file_id}.jsonl"


def write_blob(blob_dir: Path, file_id: str, data: bytes) -> tuple[Path, str]:
    """Writes the blob and returns (path, sha256_hex)."""
    path = blob_path(blob_dir, file_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def read_blob(path: str | Path) -> bytes:
    return Path(path).read_bytes()
