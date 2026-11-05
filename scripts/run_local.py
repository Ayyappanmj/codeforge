#!/usr/bin/env python3
"""Starts CodeForge for local interactive use (FastAPI + Local Worker + React Frontend).
Does not require Docker or RabbitMQ — uses SQLite and local execution runner.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/shared"))
sys.path.insert(0, str(ROOT / "apps/api"))
sys.path.insert(0, str(ROOT / "apps/worker"))

from codeforge_shared.config import ResourceLimits, Settings
from codeforge_shared.models import Base
from codeforge_shared.results import ClaimKind
from codeforge_shared.states import ExecutionStatus as S
from codeforge_shared.store import SqlStore, make_session_factory
from codeforge_api.app import create_app
from codeforge_worker.runner import DockerRunner
import uvicorn

stop_event = threading.Event()

def run_worker_loop(store: SqlStore, runner: DockerRunner):
    print("[Worker] Local background worker started.")
    while not stop_event.is_set():
        try:
            # Check for queued executions
            rows, _ = store.list_executions(page=1, page_size=10, status=S.QUEUED.value)
            for item in rows:
                if stop_event.is_set():
                    break
                eid = item.id
                claim = store.claim(eid, "local-worker")
                if claim.kind is ClaimKind.CLAIMED and claim.run_input is not None:
                    print(f"[Worker] Claimed execution {eid} ({claim.run_input.language}). Running...")
                    result = runner.run(
                        claim.run_input,
                        cancel_check=lambda eid=eid: store.is_cancel_requested(eid),
                        execution_id=str(eid),
                    )
                    store.finish(eid, result)
                    print(f"[Worker] Finished execution {eid} -> status: {result.status.value}, exit_code: {result.exit_code}")
        except Exception as exc:
            print(f"[Worker] Error in worker loop: {exc}")
        time.sleep(0.5)

def main():
    db_path = str(ROOT / "codeforge_app.db")
    sf = make_session_factory(f"sqlite:///{db_path}")
    Base.metadata.create_all(sf.kw["bind"])
    store = SqlStore(sf)

    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        limits=ResourceLimits(timeout_seconds=5.0),
    )
    fake_docker = str(ROOT / "tests" / "support" / "fake_docker.py")
    runner = DockerRunner(settings.limits, docker_bin=fake_docker)

    # 1. Start worker thread
    t_worker = threading.Thread(target=run_worker_loop, args=(store, runner), daemon=True)
    t_worker.start()

    # 2. Start API server thread
    app = create_app(settings, store, rabbit_check=lambda: True)
    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
    server = uvicorn.Server(config)
    t_api = threading.Thread(target=server.run, daemon=True)
    t_api.start()

    # Wait for API to become ready
    for _ in range(20):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/health/live", timeout=1) as resp:
                if resp.status == 200:
                    break
        except Exception:
            time.sleep(0.2)

    # 3. Start Frontend (Vite)
    frontend_dir = ROOT / "apps" / "frontend"
    print("[Frontend] Starting React Vite dev server...")
    p_vite = subprocess.Popen(
        ["npm.cmd", "run", "dev"],
        cwd=str(frontend_dir),
    )

    print("\n" + "=" * 65)
    print("  CodeForge is LIVE and ready!")
    print("=" * 65)
    print("  -> React Dashboard UI : http://localhost:5173")
    print("  -> API Interactive Docs : http://localhost:8000/docs")
    print("  -> Health Check        : http://localhost:8000/api/v1/health/live")
    print("=" * 65)
    print("Press Ctrl+C to stop.\n")

    try:
        p_vite.wait()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nShutting down CodeForge...")
        stop_event.set()
        p_vite.terminate()
        server.should_exit = True
        print("Done.")

if __name__ == "__main__":
    main()
