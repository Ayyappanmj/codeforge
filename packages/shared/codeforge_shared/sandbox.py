"""Builds the `docker run` argument vector and classifies results. Pure functions, no I/O."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import ResourceLimits
from .languages import IMAGE_RE
from .states import ErrorType, ExecutionStatus

CONTAINER_NAME_RE = re.compile(r"cf-[0-9a-f]{32}")
SANDBOX_UID = 10001


def validate_container_name(name: str) -> str:
    if not CONTAINER_NAME_RE.fullmatch(name):
        raise ValueError("invalid container name")
    return name


def build_docker_run_args(name: str, image: str, limits: ResourceLimits) -> list[str]:
    """Arguments *after* the docker binary. Everything is server-controlled."""
    validate_container_name(name)
    if not IMAGE_RE.fullmatch(image):
        raise ValueError("invalid image")
    mem = f"{limits.memory_mb}m"
    tmpfs_opts = f"rw,nosuid,nodev,noexec,size={limits.tmpfs_mb}m"
    args = [
        "run", "-i",
        "--name", name,
        "--label", "codeforge.sandbox=1",
        "--pull", "never",
        "--network", "none",
        "--user", f"{SANDBOX_UID}:{SANDBOX_UID}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", f"/sandbox:{tmpfs_opts},uid={SANDBOX_UID},gid={SANDBOX_UID},mode=0700",
        "--tmpfs", f"/tmp:{tmpfs_opts},mode=1777",
        "--memory", mem,
        "--memory-swap", mem,
        "--cpus", f"{limits.cpus:g}",
        "--pids-limit", str(limits.pids),
        "--ulimit", "nofile=64:64",
        "--ulimit", "core=0",
        "--init",
        "--log-driver", "none",
        "--hostname", "sandbox",
    ]
    if limits.sandbox_runtime:
        args.extend(["--runtime", limits.sandbox_runtime])
    args.append(image)
    return args


@dataclass(frozen=True)
class Classification:
    status: ExecutionStatus
    error_type: ErrorType | None


def classify_result(
    exit_code: int | None,
    *,
    timed_out: bool = False,
    cancelled: bool = False,
    output_exceeded: bool = False,
    oom_killed: bool = False,
) -> Classification:
    # Order matters: what *we* did (cancel/timeout/limit kill) outranks the resulting exit code.
    if cancelled:
        return Classification(ExecutionStatus.CANCELLED, ErrorType.CANCELLED)
    if timed_out:
        return Classification(ExecutionStatus.TIMED_OUT, ErrorType.TIMEOUT)
    if output_exceeded:
        return Classification(ExecutionStatus.FAILED, ErrorType.OUTPUT_LIMIT_EXCEEDED)
    if oom_killed:
        return Classification(ExecutionStatus.FAILED, ErrorType.MEMORY_LIMIT_EXCEEDED)
    if exit_code == 0:
        return Classification(ExecutionStatus.SUCCEEDED, None)
    return Classification(ExecutionStatus.FAILED, ErrorType.RUNTIME_ERROR)
