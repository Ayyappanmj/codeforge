"""Reads b'CF1 <src_len> <stdin_len>\\n' + source + stdin from stdin, writes them to the
in-container tmpfs and exec()s the interpreter. Source is never passed through a shell."""
import os
import sys

SRC = "/sandbox/main.py"
IN = "/sandbox/stdin.txt"


def main() -> None:
    data = sys.stdin.buffer.read()
    header, sep, rest = data.partition(b"\n")
    parts = header.split(b" ")
    if not sep or len(parts) != 3 or parts[0] != b"CF1":
        sys.stderr.write("codeforge: bad payload\n")
        sys.exit(70)
    n, m = int(parts[1]), int(parts[2])
    if len(rest) != n + m:
        sys.stderr.write("codeforge: bad payload length\n")
        sys.exit(70)
    with open(SRC, "wb") as f:
        f.write(rest[:n])
    with open(IN, "wb") as f:
        f.write(rest[n:])
    fd = os.open(IN, os.O_RDONLY)
    os.dup2(fd, 0)
    os.close(fd)
    os.execv(sys.executable, [sys.executable, "-u", "-B", SRC])


main()
