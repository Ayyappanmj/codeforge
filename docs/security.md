# Security Model & Threat Assessment

**Educational / Internal Platform Warning**:
Docker sharing a single Linux kernel is not a zero-trust virtualization boundary. Hostile, multi-tenant untrusted code requires defense-in-depth: dedicated sandboxing virtualization (e.g. gVisor, Firecracker microVMs), rootless containers, and hard cgroup enforcement.

---

## Threat Model: Threat -> Control -> Residual Risk

| Threat | Control | Residual Risk |
|---|---|---|
| **Host Network Access & SSRF** | `--network none` disconnects container from all virtual bridges and loopback host routes. | Local IPC namespace exploits if container escapes user namespace. |
| **Privilege Escalation** | `--user 10001:10001`, `--cap-drop ALL`, `--security-opt no-new-privileges`. User cannot gain root inside or outside container. | Unpatched Linux kernel zero-day vulnerability (privilege escalation via kernel syscall). |
| **Filesystem Tampering / Persistence** | `--read-only` rootfs. Writable tmpfs on `/sandbox` and `/tmp` mounted with `noexec,nosuid,nodev` and size cap (16 MiB). Fresh ephemeral container per run. | In-memory code execution by interpreter (interpreters read scripts as data and execute bytecode). |
| **Host Resource Exhaustion (DoS / Fork Bomb)** | `--pids-limit 32`, `--cpus 1`, `--memory 128m --memory-swap 128m`, `--ulimit nofile=64:64 --ulimit core=0`. Process group killed via `docker kill` upon deadline. | Kernel thread scheduling pressure during rapid fork burst prior to cgroup threshold. |
| **Infinite Loops & Hanging Processes** | Wall-clock deadline in runner (default 5s) triggers `docker kill` (sends SIGKILL to all processes in container). | ~20ms latency during runner kill poll cycle. |
| **Output / Disk Flooding** | Streaming `OutputBudget` caps captured stdout/stderr at 64 KiB; kills container immediately if exceeded. Host log driver disabled (`--log-driver none`). | Ephemeral memory allocation in runner buffer before termination. |
| **Command Injection via Payload** | Source code and stdin are sent strictly via binary stream protocol over container stdin (`CF1 <src_len> <stdin_len>\n`). Never passed through shell commands or CLI args. | Vulnerability in custom Python/Node entrypoint payload parser. |
| **Host Path / Docker Socket Compromise** | No volume mounts (`-v` or `--mount`). Docker socket (`/var/run/docker.sock`) is NEVER mounted in the API or the sandbox containers; strictly confined to worker service. | Worker process itself holds the Docker socket; worker host environment must remain isolated. |
| **API Denial of Service & Spam** | Per-IP rate limiting (`RATE_LIMIT_PER_MINUTE`, default 60) and optional API Key validation (`API_KEY` header). | Distributed DoS across rotating IP addresses if no reverse-proxy WAF (Cloudflare/AWS Shield) is in front. |
| **Kernel Syscall Escape** | Docker default seccomp profile; optional gVisor runtime `--runtime=runsc` (intercepts and sandboxes syscalls in user-space Sentry). | Performance overhead of gVisor syscall interception (~10-25%). |
| **Orphaned Containers Post-Worker Crash** | Background orphan container reaper in worker scans containers labeled `codeforge.sandbox=1` older than `timeout + grace` and removes them (`docker rm -f`). | Containers remain on disk between worker crash and the next sweep interval (30s). |

---

## Hardened Execution Options

### 1. gVisor (`runsc`) Runtime
For enhanced kernel-level isolation, CodeForge supports running sandboxes inside Google's gVisor sandbox runtime:
1. Install `runsc` on the Docker host (`apt-get install runsc` or configure `/etc/docker/daemon.json` with `"runtimes": { "runsc": { "path": "/usr/bin/runsc" } }`).
2. Set `SANDBOX_RUNTIME=runsc` in `.env`.
3. Worker argument builder automatically appends `--runtime runsc` to container creation.

### 2. Container Orphan Reaper
If a worker process is terminated abruptly (SIGKILL or power failure) mid-run, its sandbox container might otherwise remain on disk. CodeForge includes an automated orphan reaper:
- Identifies containers labeled `codeforge.sandbox=1`.
- Compares creation timestamp against `timeout_seconds + stale_running_grace_seconds`.
- Safely purges only orphaned containers without interrupting actively running peer worker executions.

### 3. Authentication-Lite & Rate Limiting
- **API Key**: Set `API_KEY=your-secret-key` in `.env`. The API validates `X-API-Key: your-secret-key` or `Authorization: Bearer your-secret-key` on all protected endpoints.
- **Rate Limiting**: Set `RATE_LIMIT_PER_MINUTE=120` in `.env` to restrict submission rate per client IP. Returns `429 Too Many Requests` when exceeded.
