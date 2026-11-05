"""Wire format between the runner and the in-container entrypoint.

Source and stdin travel over the container's stdin: b"CF1 <src_len> <stdin_len>\\n" + src + stdin.
No host paths are mounted and nothing is interpolated into a shell command.
"""
from __future__ import annotations

MAGIC = b"CF1"


def encode_payload(source: bytes, stdin: bytes) -> bytes:
    return b"%s %d %d\n" % (MAGIC, len(source), len(stdin)) + source + stdin


def decode_payload(data: bytes) -> tuple[bytes, bytes]:
    header, sep, rest = data.partition(b"\n")
    parts = header.split(b" ")
    if not sep or len(parts) != 3 or parts[0] != MAGIC:
        raise ValueError("bad payload header")
    n, m = int(parts[1]), int(parts[2])
    if n < 0 or m < 0 or len(rest) != n + m:
        raise ValueError("bad payload length")
    return rest[:n], rest[n:]
