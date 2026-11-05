#!/usr/bin/env python3
"""TEST HARNESS ONLY. A stand-in for the docker CLI that runs the payload with a local
interpreter. It lets us test the runner's process management (timeouts, kill, output caps,
cleanup calls) without a Docker daemon. It provides NO isolation and proves nothing about the
sandbox flags - those need the real-Docker tests."""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages/shared"))
from codeforge_shared.protocol import decode_payload  # noqa: E402

STATE = Path(os.environ.get("FAKE_DOCKER_STATE", tempfile.gettempdir())) / "fake_docker_state"
STATE.mkdir(parents=True, exist_ok=True)


def main(argv):
    cmd = argv[0]
    if cmd == "run":
        if os.environ.get("FAKE_DOCKER_FAIL"):
            sys.stderr.write("docker: Cannot connect to the Docker daemon\n")
            return 125
        name = argv[argv.index("--name") + 1]
        image = argv[-1]
        (STATE / f"{name}.exists").write_text("1")
        source, stdin = decode_payload(sys.stdin.buffer.read())
        work = Path(tempfile.mkdtemp())
        try:
            interp = [sys.executable, "-u"] if "python" in image else ["node"]
            ext = ".py" if "python" in image else ".js"
            (work / f"main{ext}").write_bytes(source)
            (work / "in.txt").write_bytes(stdin)
            with open(work / "in.txt", "rb") as fin:
                proc = subprocess.Popen(interp + [str(work / f"main{ext}")], stdin=fin,
                                        start_new_session=True)
                (STATE / f"{name}.pid").write_text(str(proc.pid))
                rc = proc.wait()
            return 137 if rc < 0 else rc
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if cmd == "kill":
        pidfile = STATE / f"{argv[1]}.pid"
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text())
                if hasattr(os, "killpg"):
                    os.killpg(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                else:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            except (ProcessLookupError, OSError):
                pass
        return 0
    if cmd == "rm":
        name = argv[-1]
        for suffix in (".exists", ".pid"):
            (STATE / f"{name}{suffix}").unlink(missing_ok=True)
        return 0
    if cmd == "inspect":
        print("false")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
