# CodeForge — Distributed Asynchronous Code Execution Engine

[![CI](https://github.com/Ayyappanmj/codeforge/actions/workflows/ci.yml/badge.svg)](https://github.com/Ayyappanmj/codeforge/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![RabbitMQ](https://img.shields.io/badge/RabbitMQ-3.13-FF6600?logo=rabbitmq&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Sandbox-2496ED?logo=docker&logoColor=white)
![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black)
![TypeScript](https://img.shields.io/badge/TypeScript-5.4-3178C6?logo=typescript&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green.svg)

> **CodeForge** is a production-grade, distributed code execution platform engineered to safely run untrusted user code at scale. Designed for online judges (LeetCode / HackerRank style), interactive IDEs, and LLM code-evaluation pipelines, CodeForge prioritizes **distributed transaction consistency**, **at-least-once resilience**, and **strict OS-level sandbox isolation**.

---

## Highlights & System Capabilities

- **Transactional Outbox Pattern**: Submissions write execution state and outbox events in a single atomic database transaction. If RabbitMQ is down or restarting, the API continues accepting requests with zero stranded or lost jobs.
- **Strict Idempotency State Machine**: Concurrency-safe Compare-And-Set (`UPDATE ... WHERE status = 'QUEUED'`) guarantees that duplicate broker messages never duplicate executions or corrupt attempts.
- **Defense-in-Depth Container Sandboxing**: Runs untrusted Python and Node.js in ephemeral, isolated containers with `--cap-drop ALL`, `--security-opt no-new-privileges`, `--read-only`, `--network none`, cgroup memory/CPU limits, and `--pids-limit 64` (fork-bomb immunity).
- **Zero-Mount Stdin Payload Protocol (`CF1`)**: Source code and stdin stream over stdin via a custom binary protocol, eliminating host volume bind-mounts, symlink exploits, and host permission complexities.
- **gVisor (`runsc`) Sandboxing Support**: Optional runtime flag redirects all Linux system calls to Google's user-space Sentry kernel for multi-tenant virtualization.
- **Distributed Orphan Container Reaper**: Background daemon safely identifies and purges abandoned sandbox containers (`codeforge.sandbox=1`) after worker crashes (`kill -9`) without racing active sibling workers.
- **Dead-Letter Queue (DLQ) & Poison Message Routing**: Malformed payloads or repeated unprocessable jobs route automatically to `codeforge.executions.dead` without blocking the worker pool.
- **Comprehensive Failure-Injection Suite**: Includes end-to-end automated tests injecting broker outages, worker `SIGKILL` mid-run, DB network freezes, duplicate delivery races, and cancellation races.
- **Full-Stack Observability**: Native Prometheus metrics (`/metrics`), pre-configured Grafana dashboard, sliding-window IP rate limiting, API key authentication, and an interactive React/TypeScript UI.

---

## Architecture

```mermaid
flowchart LR
  subgraph Ingestion ["Ingestion Layer"]
    UI["React UI (Vite + TS)"] -->|REST API| API["FastAPI Gateway"]
    API -->|1 Atomic Transaction| PG[("PostgreSQL 16")]
  end

  subgraph Brokerage ["Reliable Delivery"]
    PUB["Outbox Publisher"] -->|Polls Unsent Events| PG
    PUB -->|Publisher Confirms| MQ{{"RabbitMQ (Durable Queue)"}}
    MQ -.->|Dead-Letter Route| DLQ[("Dead-Letter Queue (DLQ)")]
  end

  subgraph Execution ["Isolated Execution Pool"]
    MQ -->|Prefetch=1, Manual ACK| W["Worker Pool (xN)"]
    W -->|Compare-And-Set Claim| PG
    W -->|Binary Payload Stream| SB[["Docker / gVisor Sandbox"]]
    W -->|Terminal State & Output| PG
  end

  subgraph Observability ["Telemetry & Metrics"]
    API -.->|/metrics| PROM["Prometheus"]
    W -.->|/metrics| PROM
    PROM -.-> GRAF["Grafana Dashboard"]
  end
```

Detailed architectural references:
- [Architecture & Data Flow](docs/architecture.md)
- [Failure Recovery & Outbox Internals](docs/failure-recovery.md)
- [Security & Sandbox Threat Model](docs/security.md)
- [API Specification](docs/api.md)
- [Benchmark Methodology & Bottlenecks](docs/benchmarks.md)

---

## Live Demo & Visuals

### 1. Interactive React Dashboard
The frontend provides real-time polling, status indicators, submission history, execution timers, and formatted stdout/stderr views:

![CodeForge React Dashboard](docs/dashboard.png)

### 2. Live CLI Demo Execution (`./scripts/demo.sh`)
```json
{
  "status": "SUCCEEDED",
  "exit_code": 0,
  "stdout": "HELLO CODEFORGE\n",
  "stderr": "",
  "execution_duration_ms": 42,
  "error_type": null
}
{
  "status": "SUCCEEDED",
  "exit_code": 0,
  "stdout": "[ 1, 4, 9 ]\n",
  "stderr": "",
  "execution_duration_ms": 78,
  "error_type": null
}
{
  "status": "TIMED_OUT",
  "exit_code": null,
  "stdout": "",
  "stderr": "",
  "execution_duration_ms": 5002,
  "error_type": "TIMEOUT"
}
```

---

## Resiliency & Failure-Injection Verification

A dedicated suite of failure-injection tests (`tests/failure_scenarios/test_real_services.py`) programmatically validates system behavior under real infrastructure faults:

| Fault Injected | Expected System Behavior | Verified Outcome | Test Case |
|---|---|---|---|
| **RabbitMQ Outage**<br>`docker stop rabbitmq` | API accepts submissions with `202 Accepted`; jobs buffer safely in PostgreSQL outbox table; when RabbitMQ resumes, publisher catches up automatically. | **All 5 buffered submissions reached terminal `SUCCEEDED` with zero loss.** | `test_scenario_a_outbox_recovery_when_broker_fails` |
| **Worker SIGKILL Mid-Run**<br>`kill -9 <worker>` | Stale `RUNNING` execution detected by sweeper after grace period; requeued up to `MAX_ATTEMPTS`; marked `FAILED/WORKER_LOST` if attempts exhausted. | **Job recovered and terminalized; container cleaned up; no indefinite hang.** | `test_scenario_b_worker_kill_sweeper_recovery` |
| **Database Freeze / Latency**<br>`docker pause postgres` | Publisher and worker apply exponential backoff on connection failure; resume state transitions cleanly upon unpause. | **Queue retained state; zero dropped jobs or duplicated records.** | `test_scenario_c_database_flappiness_and_retry` |
| **Duplicate Broker Delivery**<br>Redelivery of same execution message | Worker executes atomic compare-and-set claim (`UPDATE ... WHERE status = 'QUEUED'`). Subsequent redelivery detects terminal status and acknowledges without re-run. | **Exactly 1 execution attempt record; idempotent output preserved.** | `test_scenario_d_duplicate_message_delivery_idempotency` |
| **Cancellation Race**<br>User cancels job while worker is finishing | State machine forbids overwriting terminal states (`SUCCEEDED` / `FAILED`). | **Completed job remains terminal `SUCCEEDED`; never flipped to `CANCELLED`.** | `test_scenario_e_cancel_racing_natural_completion` |
| **Poison / Malformed Payload**<br>Corrupted or non-deserializable JSON | Worker catches deserialization error and rejects message with `basic_reject(requeue=False)`. | **Routed to `codeforge.executions.dead` without worker crash.** | `test_scenario_f_poison_message_dead_lettering` |

---

## Defense-in-Depth Sandbox Security

Untrusted code execution employs a layered defense model to guarantee that malicious user code cannot compromise the host, peer containers, or internal networks:

| Security Vector | Implementation Flag | Threat Mitigated | Residual Risk & Layer 2 Control |
|---|---|---|---|
| **Network Access** | `--network none` | Data exfiltration, port scanning, botnets, DDoS, metadata service access (`169.254.169.254`). | Zero host socket access; DNS and outbound sockets disabled. |
| **Filesystem Mutation** | `--read-only`, unprivileged `10001:10001` | Modification of runtime binaries, writing backdoors, rootkit persistence. | Ephemeral `/tmp` mounted with `noexec,nosuid,nodev`. |
| **Privilege Escalation** | `--security-opt no-new-privileges`, `--cap-drop ALL` | SUID exploitation, capability abuse (`CAP_SYS_ADMIN`, `CAP_NET_ADMIN`). | Linux capabilities dropped to empty set. |
| **Resource Depletion** | `--memory=128m`, `--cpus=1.0`, `--pids-limit=64` | OOM crashes, CPU denial-of-service, process table exhaustion (fork bombs). | Hard cgroup limits; fork bombs hit PID limit and abort. |
| **Host Disk Exposure** | **Zero host bind mounts** (`CF1` binary stream via stdin) | Host filesystem traversal, symlink escapes, container escape via socket mount. | Container has zero visibility into host paths. |
| **Kernel Vulnerabilities** | **Google gVisor** (`--runtime runsc`) | Linux kernel zero-days, unpatched privilege escalation. | Syscalls intercepted and emulated in user-space Sentry sandbox. |

---

## Measured Performance & Benchmarks

Benchmarked using `scripts/benchmark.py` running $N=100$ executions:

| Concurrency | Throughput | Total Time | p50 Latency | p95 Latency | Failures |
|---|---|---|---|---|---|
| **1 Worker** | **161.62 jobs/s** | 0.619s | **4.91 ms** | **7.47 ms** | 0 / 100 |
| **3 Workers** | **31.14 jobs/s** | 3.212s | **36.29 ms** | **668.84 ms** | 0 / 100 |

### Bottleneck Analysis
- **SQLite Concurrency Contention**: SQLite in-process write serialization causes thread lock contention under concurrent worker threads. In production, PostgreSQL with row-level locks and `SKIP LOCKED` scales linearly without database serialization bottlenecks.
- **Container Lifecycle Startup**: In physical Docker execution, `docker run --rm` incurs ~150–350 ms of kernel container setup overhead per job. Production scaling achieves sub-50ms latency by pairing CodeForge with a pre-warmed container pool or Firecracker microVM snapshotting.

---

## Design Decisions & Engineering Trade-Offs

### 1. Transactional Outbox vs. Direct Broker Publish from API
- **Decision**: The API inserts the execution record and an outbox event in the same atomic database transaction. An independent outbox publisher daemon polls and forwards events to RabbitMQ with publisher confirms.
- **Trade-Off**: Introduces a minor polling latency (~10–50 ms), but eliminates the **dual-write vulnerability** where an API crashes after committing to the database but before publishing to the broker, leaving jobs stranded in an inconsistent state.

### 2. At-Least-Once Delivery with Idempotent Storage vs. Exactly-Once
- **Decision**: CodeForge embraces at-least-once message delivery. Concurrency safety is enforced at the database layer via atomic state transitions (`UPDATE ... WHERE status = 'QUEUED'`).
- **Trade-Off**: In failure conditions (worker crash mid-run), a job may be re-executed by another worker up to `MAX_ATTEMPTS`. However, database state transitions are monotonic and terminal; once an execution reaches `SUCCEEDED` or `FAILED`, subsequent writes are discarded.

### 3. Binary Stdin Protocol (`CF1`) vs. Host Bind Mounts
- **Decision**: Code, stdin, and configuration stream into the container over stdin using a lightweight binary framing protocol: `CF1 <src_len> <stdin_len>\n<source_bytes><stdin_bytes>`.
- **Trade-Off**: Requires a lightweight entrypoint wrapper in execution images, but completely eliminates host volume mounts (`-v /tmp/code:/sandbox`). Containers cannot traverse host paths, read host files, or leak permissions across host nodes.

### 4. Docker vs. gVisor / MicroVMs
- **Decision**: Standard Docker containers share the host Linux kernel. While hardened with cgroups, seccomp, and dropped capabilities, multi-tenant isolation requires kernel boundary protection. CodeForge natively integrates Google gVisor (`SANDBOX_RUNTIME=runsc`) to route syscalls through a memory-safe user-space kernel.

---

## Tech Stack

| Component | Technology | Role |
|---|---|---|
| **API Gateway** | Python 3.12, FastAPI, Pydantic v2 | High-performance asynchronous REST API, schema validation, rate limiting |
| **Data Store** | PostgreSQL 16, SQLAlchemy 2 (Async), Alembic | Relational state machine, transactional outbox tables, atomic row locking |
| **Message Broker** | RabbitMQ 3.13, Pika | Durable messaging, publisher confirms, prefetch control, dead-letter routing |
| **Execution Sandbox** | Docker, Google gVisor (`runsc`) | Ephemeral process isolation, cgroups v2, custom binary stdin entrypoint |
| **Frontend** | React 18, TypeScript, Vite, Tailwind CSS | Live execution dashboard, code editor, execution logs, polling UI |
| **Observability** | Prometheus, Grafana | Request metrics, job duration histograms, queue lag, error classification |
| **Quality & CI** | Pytest, Ruff, GitHub Actions | Zero-skip CI pipeline, failure injection, multi-language compiler tests |

---

## Quickstart

### Prerequisites
- Docker & Docker Compose v2 (or Python 3.11+ / Node.js 22 for local mode)
- `curl`, `jq` (optional)

### Option A: Complete Docker Compose Stack
```bash
# 1. Clone repository
git clone https://github.com/Ayyappanmj/codeforge.git
cd codeforge

# 2. Configure environment
cp .env.example .env

# 3. Build sandbox runner images (Python & JavaScript)
docker compose --profile images build

# 4. Start all services
docker compose up -d --build

# 5. Run the end-to-end verification demo
./scripts/demo.sh
```

**Services will be live at:**
- **React Dashboard**: [http://localhost:8080](http://localhost:8080)
- **FastAPI Documentation**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Grafana Dashboard**: [http://localhost:3000](http://localhost:3000) (`admin` / `admin`)
- **Prometheus Metrics**: [http://localhost:9090](http://localhost:9090)

Scale worker instances horizontally:
```bash
docker compose up -d --scale worker=3
```

---

### Option B: Native Local Development Mode
To run without Docker daemon:
```powershell
# 1. Set up Python virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

# 2. Install frontend dependencies
cd apps/frontend
npm install
cd ../..

# 3. Launch backend API, worker loop, and React UI in one command
python scripts/run_local.py
```
- Web UI: [http://localhost:5173](http://localhost:5173)
- API Docs: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

---

## Verification & Test Suites

```bash
# 1. Complete offline test suite (Unit, Store, API, Process Runner)
python -m pytest tests -v -rs --ignore=tests/execution/test_real_docker.py

# 2. Real failure-injection tests (Outbox recovery, worker crash, DB pause, DLQ)
python -m pytest tests/failure_scenarios/test_real_services.py -v

# 3. Real-Docker container isolation tests (executed in Linux/CI)
python -m pytest tests/execution/test_real_docker.py -v

# 4. Linter & Typechecks
ruff check .
cd apps/frontend && npx tsc --noEmit && npm run build
```

---

## Author & Contact

**Ayyappan M J**
- GitHub: [@Ayyappanmj](https://github.com/Ayyappanmj)
- Email: ayyappanmadhuj@gmail.com
- Repository: [github.com/Ayyappanmj/codeforge](https://github.com/Ayyappanmj/codeforge)

---

## License
MIT License. See [LICENSE](LICENSE) for details.
