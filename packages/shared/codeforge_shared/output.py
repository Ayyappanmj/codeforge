"""Bounded output capture: limits are enforced while reading, not afterwards."""
from __future__ import annotations

import threading

TRUNCATION_MARKER = "\n[output truncated]"


class OutputBudget:
    """Shared byte budget across stdout+stderr."""

    def __init__(self, limit: int):
        self.limit = limit
        self._used = 0
        self._lock = threading.Lock()
        self.exceeded = threading.Event()

    def take(self, n: int) -> int:
        with self._lock:
            allowed = max(0, min(n, self.limit - self._used))
            self._used += allowed
            if allowed < n:
                self.exceeded.set()
            return allowed


def capture_stream(stream, budget: OutputBudget, sink: bytearray, chunk_size: int = 8192) -> None:
    """Read until EOF, keeping at most the shared budget. Keeps draining so the child never blocks,
    but the caller is expected to kill the container once budget.exceeded is set."""
    while True:
        chunk = stream.read1(chunk_size) if hasattr(stream, "read1") else stream.read(chunk_size)
        if not chunk:
            return
        allowed = budget.take(len(chunk))
        if allowed:
            sink.extend(chunk[:allowed])


def decode_output(data: bytes, truncated: bool) -> str:
    text = data.decode("utf-8", errors="replace").replace("\x00", "\ufffd")  # PostgreSQL rejects NUL
    return text + TRUNCATION_MARKER if truncated else text
