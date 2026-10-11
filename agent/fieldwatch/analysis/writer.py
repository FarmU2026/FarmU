"""Overlay writers. The core only knows this tiny protocol; S3 lives in `s3_writer.py`."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
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


_scoped: ContextVar[OverlayWriter | None] = ContextVar("fieldwatch_overlay_writer", default=None)


@contextmanager
def overlay_writer(writer: OverlayWriter):
    """Opt in to a writer for registry-path analyses run inside the `with` block (the edge picks it, e.g. the scene
    pipeline's analyze Lambda). Anything outside such a block (Gateway tools, the agent, tests) gets NullWriter."""
    token = _scoped.set(writer)
    try:
        yield writer
    finally:
        _scoped.reset(token)


def current_writer() -> OverlayWriter:
    return _scoped.get() or NullWriter()
