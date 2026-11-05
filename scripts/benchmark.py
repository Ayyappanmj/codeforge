#!/usr/bin/env python3
"""Benchmark script: submits N trivial Python jobs and measures end-to-end latency and throughput.
Supports running against a live API endpoint (default: http://localhost:8000/api/v1)
or standalone in-process pipeline mode (SqlStore + Processor + Runner) to measure baseline overhead.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Ensure package imports work when run standalone
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/shared"))
sys.path.insert(0, str(ROOT / "apps/worker"))
sys.path.insert(0, str(ROOT / "apps/api"))


def get_system_specs() -> dict[str, str]:
    return {
        "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "cpu": platform.processor() or "Unknown CPU",
        "python": platform.python_version(),
    }


def run_standalone_benchmark(n_jobs: int = 100, workers: int = 1) -> dict:
    from codeforge_shared.config import ResourceLimits, Settings
    from codeforge_shared.models import Base
    from codeforge_shared.results import RunResult
    from codeforge_shared.states import ExecutionStatus as S
    from codeforge_shared.store import SqlStore
    from codeforge_worker.processor import Processor
    from tests.support.fakes import FakeRunner

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import tempfile
    db_file = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    db_file.close()
    db_path = db_file.name

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
        conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    store = SqlStore(sf)
    settings = Settings(limits=ResourceLimits(timeout_seconds=2.0))

    # Pre-populate jobs
    eids = []
    for i in range(n_jobs):
        ex = store.create_execution(
            language="python",
            source_code=f"print({i})",
            stdin="",
            correlation_id=f"bench-{i}",
        )
        eids.append(ex.id)

    # Publish outbox
    published = []
    store.publish_due_outbox(lambda eid, cid: published.append(eid), batch_size=n_jobs)

    latencies_ms = []
    failures = 0

    def process_one(eid):
        nonlocal failures
        t0 = time.perf_counter()
        runner = FakeRunner(RunResult(S.SUCCEEDED, 0, "ok\n", "", 1))
        p = Processor(store, runner, settings, f"bench-worker-{workers}", sleep=lambda _: None)
        msg = json.dumps({"execution_id": str(eid), "correlation_id": "bench"}).encode()
        dec = p.process(msg)
        elapsed = (time.perf_counter() - t0) * 1000
        if dec.name != "ACK":
            failures += 1
        return elapsed

    try:
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(process_one, eids))
        total_time = time.perf_counter() - start

        latencies_ms = sorted(results)
        p50 = statistics.median(latencies_ms)
        p95 = latencies_ms[int(len(latencies_ms) * 0.95)] if latencies_ms else 0.0

        return {
            "jobs": n_jobs,
            "workers": workers,
            "total_time_seconds": round(total_time, 3),
            "throughput_jobs_sec": round(n_jobs / total_time, 2) if total_time > 0 else 0,
            "p50_latency_ms": round(p50, 2),
            "p95_latency_ms": round(p95, 2),
            "failures": failures,
        }
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def run_http_benchmark(api_url: str, n_jobs: int = 100, workers: int = 1, api_key: str = "") -> dict:
    import urllib.request
    import urllib.error

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    def submit_and_poll(job_idx):
        t0 = time.perf_counter()
        payload = json.dumps({"language": "python", "source_code": f"print({job_idx})"}).encode()
        req = urllib.request.Request(f"{api_url}/executions", data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                eid = data["execution_id"]
        except Exception:
            return None

        # Poll for completion
        for _ in range(60):
            time.sleep(0.1)
            get_req = urllib.request.Request(f"{api_url}/executions/{eid}", headers=headers)
            try:
                with urllib.request.urlopen(get_req, timeout=5) as r2:
                    st = json.loads(r2.read().decode()).get("status")
                    if st in ("SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"):
                        break
            except Exception:
                pass
        return (time.perf_counter() - t0) * 1000

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        res = list(pool.map(submit_and_poll, range(n_jobs)))
    total_time = time.perf_counter() - start

    valid = [r for r in res if r is not None]
    failures = len(res) - len(valid)
    valid.sort()
    p50 = statistics.median(valid) if valid else 0.0
    p95 = valid[int(len(valid) * 0.95)] if valid else 0.0

    return {
        "jobs": n_jobs,
        "workers": workers,
        "total_time_seconds": round(total_time, 3),
        "throughput_jobs_sec": round(n_jobs / total_time, 2) if total_time > 0 else 0,
        "p50_latency_ms": round(p50, 2),
        "p95_latency_ms": round(p95, 2),
        "failures": failures,
    }


def main():
    parser = argparse.ArgumentParser(description="CodeForge Benchmark Runner")
    parser.add_argument("-n", "--jobs", type=int, default=100, help="Number of jobs to submit")
    parser.add_argument("-w", "--workers", type=int, default=1, help="Worker concurrency")
    parser.add_argument("--api", type=str, default="http://localhost:8000/api/v1", help="API base URL")
    args = parser.parse_args()

    specs = get_system_specs()
    print("=" * 60)
    print("CodeForge Benchmark Suite")
    print(f"Host Specs: {specs['cpu']} | OS: {specs['os']} | Python {specs['python']}")
    print("=" * 60)

    # Check if live HTTP API is reachable
    is_live = False
    try:
        import urllib.request
        with urllib.request.urlopen(f"{args.api}/health/live", timeout=2) as r:
            if r.status == 200:
                is_live = True
    except Exception:
        is_live = False

    if is_live:
        print(f"Running HTTP benchmark against {args.api} (N={args.jobs}, workers={args.workers})...")
        res = run_http_benchmark(args.api, n_jobs=args.jobs, workers=args.workers)
    else:
        print(f"Running standalone pipeline benchmark (N={args.jobs}, workers={args.workers})...")
        res = run_standalone_benchmark(n_jobs=args.jobs, workers=args.workers)

    print("\nResults:")
    for k, v in res.items():
        print(f"  {k}: {v}")
    print("=" * 60)


if __name__ == "__main__":
    main()
