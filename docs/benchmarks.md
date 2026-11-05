# CodeForge Benchmarks & Performance Analysis

## Benchmark Environment
- **CPU**: Intel(R) Core(TM) i3-1005G1 CPU @ 1.20GHz (Intel64 Family 6 Model 126 Stepping 5)
- **RAM**: 8 GB
- **OS**: Windows 10 (AMD64)
- **Python Runtime**: Python 3.11.9
- **Benchmark Suite**: `scripts/benchmark.py` (N=100 trivial Python jobs)

---

## Benchmark Results (N=100 trivial jobs)

| Configuration | Throughput (jobs/sec) | Total Time (s) | p50 Latency (ms) | p95 Latency (ms) | Failures |
|---|---|---|---|---|---|
| **1 Worker** | **161.62 jobs/s** | **0.619s** | **4.91 ms** | **7.47 ms** | **0 / 100** |
| **3 Workers** | **31.14 jobs/s** | **3.212s** | **36.29 ms** | **668.84 ms** | **0 / 100** |

---

## Bottleneck & Performance Observations

1. **In-Memory & SQLite File Lock Contention**:
   In SQLite mode with WAL journal mode, single-worker pipeline throughput exceeds 160 jobs/sec with sub-5ms median latency. However, SQLite database-level write serialization causes lock wait queuing when multiple concurrent worker threads attempt simultaneous write transactions (`UPDATE executions` and `INSERT INTO execution_attempts`), causing p95 latency to increase to ~668 ms. In production on PostgreSQL, row-level locks and transactional `FOR UPDATE SKIP LOCKED` eliminate this database-level lock contention.

2. **Docker Container Startup Overhead (The Primary Physical Bottleneck)**:
   In live Docker execution mode, fresh container creation (`docker run --rm ...` with cgroups, seccomp, and tmpfs mounts) requires ~150–350 ms per container invocation. Wall-clock duration is dominated by kernel container lifecycle setup, not user code execution.

3. **Horizontal Scaling**:
   Because workers consume from RabbitMQ with `prefetch_count=1`, scaling workers (`docker compose up -d --scale worker=3`) distributes the container startup and CPU execution work across worker processes on multi-core systems.
