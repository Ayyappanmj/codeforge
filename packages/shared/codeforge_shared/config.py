"""Validated server-side configuration. Clients can never influence these values."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


class ConfigError(ValueError):
    pass


def _num(env, name, default, lo, hi, cast=int):
    raw = env.get(name)
    try:
        value = cast(raw) if raw not in (None, "") else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{name}={value} out of allowed range [{lo}, {hi}]")
    return value


@dataclass(frozen=True)
class ResourceLimits:
    timeout_seconds: float = 5.0
    memory_mb: int = 128
    cpus: float = 1.0
    pids: int = 32
    tmpfs_mb: int = 16
    max_source_bytes: int = 64 * 1024
    max_stdin_bytes: int = 16 * 1024
    max_output_bytes: int = 64 * 1024
    sandbox_runtime: str = ""

    def __post_init__(self):
        if self.sandbox_runtime and not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", self.sandbox_runtime):
            raise ConfigError(f"invalid sandbox_runtime: {self.sandbox_runtime!r}")
        checks = [
            ("timeout_seconds", self.timeout_seconds, 0.5, 60),
            ("memory_mb", self.memory_mb, 16, 2048),
            ("cpus", self.cpus, 0.1, 8),
            ("pids", self.pids, 4, 512),
            ("tmpfs_mb", self.tmpfs_mb, 1, 256),
            ("max_source_bytes", self.max_source_bytes, 1, 1024 * 1024),
            ("max_stdin_bytes", self.max_stdin_bytes, 0, 1024 * 1024),
            ("max_output_bytes", self.max_output_bytes, 1024, 4 * 1024 * 1024),
        ]
        for name, value, lo, hi in checks:
            if not lo <= value <= hi:
                raise ConfigError(f"{name}={value} out of allowed range [{lo}, {hi}]")

    @classmethod
    def from_env(cls, env=None) -> ResourceLimits:
        env = os.environ if env is None else env
        d = cls()
        return cls(
            timeout_seconds=_num(env, "EXEC_TIMEOUT_SECONDS", d.timeout_seconds, 0.5, 60, float),
            memory_mb=_num(env, "EXEC_MEMORY_MB", d.memory_mb, 16, 2048),
            cpus=_num(env, "EXEC_CPUS", d.cpus, 0.1, 8, float),
            pids=_num(env, "EXEC_PIDS_LIMIT", d.pids, 4, 512),
            tmpfs_mb=_num(env, "EXEC_TMPFS_MB", d.tmpfs_mb, 1, 256),
            max_source_bytes=_num(env, "MAX_SOURCE_BYTES", d.max_source_bytes, 1, 1024 * 1024),
            max_stdin_bytes=_num(env, "MAX_STDIN_BYTES", d.max_stdin_bytes, 0, 1024 * 1024),
            max_output_bytes=_num(
                env, "MAX_OUTPUT_BYTES", d.max_output_bytes, 1024, 4 * 1024 * 1024
            ),
            sandbox_runtime=env.get("SANDBOX_RUNTIME", env.get("DOCKER_RUNTIME", d.sandbox_runtime)),
        )


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+psycopg2://codeforge:codeforge@localhost:5432/codeforge"
    rabbitmq_url: str = "amqp://codeforge:codeforge@localhost:5672/%2F"
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    max_attempts: int = 3  # execution attempts recorded in the DB
    max_message_retries: int = 3  # broker-level retries before dead-lettering
    retry_delay_ms: int = 5000
    stale_running_grace_seconds: int = 30
    stale_queued_seconds: int = 120
    max_enqueues: int = 5
    outbox_poll_seconds: float = 1.0
    outbox_batch_size: int = 50
    docker_bin: str = "docker"
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://localhost:8080")
    worker_metrics_port: int = 9100
    publisher_metrics_port: int = 9101
    log_level: str = "INFO"
    api_key: str = ""
    rate_limit_per_minute: int = 0

    @property
    def stale_running_seconds(self) -> float:
        return self.limits.timeout_seconds + self.stale_running_grace_seconds

    @classmethod
    def from_env(cls, env=None) -> Settings:
        env = os.environ if env is None else env
        d = cls()
        docker_bin = env.get("DOCKER_BIN", d.docker_bin)
        if not re.fullmatch(r"[A-Za-z0-9_./-]{1,100}", docker_bin):
            raise ConfigError("DOCKER_BIN contains invalid characters")
        origins = tuple(
            o.strip() for o in env.get("CORS_ORIGINS", ",".join(d.cors_origins)).split(",") if o.strip()
        )
        return cls(
            database_url=env.get("DATABASE_URL", d.database_url),
            rabbitmq_url=env.get("RABBITMQ_URL", d.rabbitmq_url),
            limits=ResourceLimits.from_env(env),
            max_attempts=_num(env, "MAX_ATTEMPTS", d.max_attempts, 1, 10),
            max_message_retries=_num(env, "MAX_MESSAGE_RETRIES", d.max_message_retries, 0, 10),
            retry_delay_ms=_num(env, "RETRY_DELAY_MS", d.retry_delay_ms, 100, 600_000),
            stale_running_grace_seconds=_num(env, "STALE_RUNNING_GRACE_SECONDS", 30, 1, 3600),
            stale_queued_seconds=_num(env, "STALE_QUEUED_SECONDS", 120, 5, 86_400),
            max_enqueues=_num(env, "MAX_ENQUEUES", d.max_enqueues, 1, 50),
            outbox_poll_seconds=_num(env, "OUTBOX_POLL_SECONDS", 1.0, 0.1, 60, float),
            outbox_batch_size=_num(env, "OUTBOX_BATCH_SIZE", 50, 1, 500),
            docker_bin=docker_bin,
            cors_origins=origins,
            worker_metrics_port=_num(env, "WORKER_METRICS_PORT", 9100, 1, 65535),
            publisher_metrics_port=_num(env, "PUBLISHER_METRICS_PORT", 9101, 1, 65535),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            api_key=env.get("API_KEY", env.get("CODEFORGE_API_KEY", d.api_key)),
            rate_limit_per_minute=_num(env, "RATE_LIMIT_PER_MINUTE", d.rate_limit_per_minute, 0, 100_000),
        )
