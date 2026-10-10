"""Overlay writers. The core only knows this tiny protocol; S3 lives in `s3_writer.py`."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class OverlayWriter(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "image/png") -> None: ...


class NullWriter:
    """Drops everything (the default: unit tests and callers that only want the numbers)."""

    def put(self, key: str, data: bytes, content_type: str = "image/png") -> None:
        return None


class LocalWriter:
    """Writes under a root folder, e.g. agent/data, so `key` doubles as a path (contracts: OverlayRef.key)."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def put(self, key: str, data: bytes, content_type: str = "image/png") -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
