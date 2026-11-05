"""Runs one submission in a fresh, restricted container via the docker CLI (argv list, no shell)."""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable

from codeforge_shared.config import ResourceLimits
from codeforge_shared.languages import get_language
from codeforge_shared.logging_utils import log_event
from codeforge_shared.output import OutputBudget, capture_stream, decode_output
from codeforge_shared.protocol import encode_payload
from codeforge_shared.results import RunInput, RunResult
from codeforge_shared.sandbox import build_docker_run_args, classify_result

log = logging.getLogger(__name__)


class SandboxInfrastructureError(Exception):
    """Docker itself failed (daemon down, image missing...). Not the submitter's fault -> retryable."""


class DockerRunner:
    def __init__(self, limits: ResourceLimits, docker_bin: str = "docker", registry=None):
        self.limits = limits
        self.docker_bin = docker_bin
        self.registry = registry

    @property
    def _cmd(self) -> list[str]:
        if self.docker_bin.endswith(".py"):
            return [sys.executable, self.docker_bin]
        return [self.docker_bin]

    def _docker(self, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
        return subprocess.run([*self._cmd, *args], capture_output=True, timeout=timeout,
                              check=False)

    def run(self, run_input: RunInput, cancel_check: Callable[[], bool] = lambda: False,
            execution_id: str = "") -> RunResult:
        spec = get_language(run_input.language, self.registry)
        name = f"cf-{uuid.uuid4().hex}"
        argv = [*self._cmd, *build_docker_run_args(name, spec.image, self.limits)]
        payload = encode_payload(run_input.source_code.encode(), run_input.stdin.encode())

        budget = OutputBudget(self.limits.max_output_bytes)
        out, err = bytearray(), bytearray()
        timed_out = cancelled = False
        start = time.monotonic()
        proc = None
        try:
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
            except OSError as exc:
                raise SandboxInfrastructureError(f"cannot start docker: {exc}") from exc

            def feed():
                try:
                    proc.stdin.write(payload)
                    proc.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass

            threads = [
                threading.Thread(target=feed, daemon=True),
                threading.Thread(target=capture_stream, args=(proc.stdout, budget, out), daemon=True),
                threading.Thread(target=capture_stream, args=(proc.stderr, budget, err), daemon=True),
            ]
            for t in threads:
                t.start()

            deadline = start + self.limits.timeout_seconds
            next_cancel_poll = 0.0
            while proc.poll() is None:
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    break
                if budget.exceeded.is_set():
                    break
                if now >= next_cancel_poll:
                    next_cancel_poll = now + 0.5
                    try:
                        if cancel_check():
                            cancelled = True
                            break
                    except Exception:  # noqa: BLE001 - DB hiccup must not kill the run
                        log_event(log, logging.WARNING, "cancel_check_failed", execution_id=execution_id)
                time.sleep(0.02)

            if proc.poll() is None:  # we decided to stop it: kill the whole container
                self._docker("kill", name)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            for t in threads:
                t.join(timeout=2)

            duration_ms = int((time.monotonic() - start) * 1000)
            exit_code = proc.returncode
            killed_by_us = timed_out or cancelled or budget.exceeded.is_set()
            if not killed_by_us and exit_code == 125:  # `docker run` itself failed
                raise SandboxInfrastructureError(
                    "docker run failed: " + decode_output(bytes(err[-300:]), False))
            oom = False
            if not killed_by_us and exit_code == 137:
                res = self._docker("inspect", "-f", "{{.State.OOMKilled}}", name)
                oom = res.returncode == 0 and res.stdout.strip() == b"true"
            cls = classify_result(None if killed_by_us else exit_code, timed_out=timed_out,
                                  cancelled=cancelled, output_exceeded=budget.exceeded.is_set(),
                                  oom_killed=oom)
            truncated = budget.exceeded.is_set()
            return RunResult(
                status=cls.status, exit_code=None if killed_by_us else exit_code,
                stdout=decode_output(bytes(out), truncated), stderr=decode_output(bytes(err), False),
                duration_ms=duration_ms, error_type=cls.error_type, output_truncated=truncated)
        finally:
            self._cleanup(name)

    def _cleanup(self, name: str) -> None:
        try:
            self._docker("rm", "-f", name)
        except (subprocess.TimeoutExpired, OSError):
            log_event(log, logging.ERROR, "container_cleanup_failed", container=name)


def reap_orphaned_containers(docker_bin: str = "docker", max_age_seconds: float = 60.0) -> int:
    """Safely removes containers with label codeforge.sandbox=1 older than max_age_seconds.
    Safe for multiple concurrent workers: only reaps containers older than timeout + grace."""
    try:
        cmd = [sys.executable, docker_bin] if docker_bin.endswith(".py") else [docker_bin]
        res = subprocess.run(
            [*cmd, "ps", "-a", "--filter", "label=codeforge.sandbox=1", "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return 0
        container_ids = [cid.strip() for cid in res.stdout.splitlines() if cid.strip()]
        reaped = 0
        now = time.time()
        for cid in container_ids:
            insp = subprocess.run(
                [*cmd, "inspect", "-f", "{{.Created}}", cid],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if insp.returncode == 0 and insp.stdout.strip():
                created_raw = insp.stdout.strip()
                try:
                    clean_ts = created_raw.split(".")[0] + "Z" if "." in created_raw else created_raw
                    from datetime import datetime
                    created_dt = datetime.fromisoformat(clean_ts.replace("Z", "+00:00"))
                    age = now - created_dt.timestamp()
                    if age > max_age_seconds:
                        rm_res = subprocess.run([*cmd, "rm", "-f", cid], capture_output=True, timeout=5, check=False)
                        if rm_res.returncode == 0:
                            reaped += 1
                except Exception:
                    pass
        return reaped
    except Exception:
        return 0
