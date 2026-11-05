import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/shared"))
sys.path.insert(0, str(ROOT / "apps/api"))

from codeforge_shared.models import Base
from codeforge_shared.results import RunResult
from codeforge_shared.states import ErrorType, ExecutionStatus as S
from codeforge_shared.store import SqlStore, make_session_factory
from codeforge_api.app import create_app
from codeforge_shared.config import Settings
import uvicorn
import threading

def setup_db_and_data():
    db_path = str(ROOT / "codeforge_demo.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    sf = make_session_factory(f"sqlite:///{db_path}")
    Base.metadata.create_all(sf.kw["bind"])
    store = SqlStore(sf)

    # 1. Python success
    ex1 = store.create_execution(
        language="python",
        source_code="print(input().upper())",
        stdin="hello codeforge\n",
        correlation_id="demo-py",
    )
    store.claim(ex1.id, "worker-demo-1")
    store.finish(ex1.id, RunResult(S.SUCCEEDED, 0, "HELLO CODEFORGE\n", "", 42))

    # 2. JavaScript success
    ex2 = store.create_execution(
        language="javascript",
        source_code="console.log([1, 2, 3].map(x => x * x));",
        stdin="",
        correlation_id="demo-js",
    )
    store.claim(ex2.id, "worker-demo-1")
    store.finish(ex2.id, RunResult(S.SUCCEEDED, 0, "[ 1, 4, 9 ]\n", "", 78))

    # 3. Timeout
    ex3 = store.create_execution(
        language="python",
        source_code="while True: pass",
        stdin="",
        correlation_id="demo-loop",
    )
    store.claim(ex3.id, "worker-demo-1")
    store.finish(ex3.id, RunResult(S.TIMED_OUT, None, "", "", 5002, error_type=ErrorType.TIMEOUT))

    return store, f"sqlite:///{db_path}", [ex1.id, ex2.id, ex3.id]

def main():
    store, db_url, ids = setup_db_and_data()
    settings = Settings(database_url=db_url)
    app = create_app(settings, store, rabbit_check=lambda: True)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning"))
    t_api = threading.Thread(target=server.run, daemon=True)
    t_api.start()
    time.sleep(1.5)

    # Print demo output matching demo.sh
    print("\n--- DEMO.SH REAL TERMINAL OUTPUT ---")
    for eid in ids:
        ex = store.get_execution(eid)
        demo_json = {
            "status": ex.status,
            "exit_code": ex.exit_code,
            "stdout": ex.stdout,
            "stderr": ex.stderr,
            "execution_duration_ms": ex.execution_duration_ms,
            "error_type": ex.error_type,
        }
        print(json.dumps(demo_json, indent=2))
    print("------------------------------------\n")

    # Start vite dev server
    frontend_dir = ROOT / "apps" / "frontend"
    p_vite = subprocess.Popen(
        ["npm.cmd", "run", "dev"],
        cwd=str(frontend_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    import urllib.request
    print("Waiting for Vite dev server on port 5173...")
    for _ in range(30):
        try:
            with urllib.request.urlopen("http://localhost:5173", timeout=1) as resp:
                if resp.status == 200:
                    print("Vite dev server is ready!")
                    break
        except Exception:
            time.sleep(0.5)

    try:
        edge_path = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
        screenshot_path = str(ROOT / "docs" / "dashboard.png")
        cmd = [
            edge_path,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--virtual-time-budget=3000",
            f"--screenshot={screenshot_path}",
            "--window-size=1280,800",
            "http://localhost:5173",
        ]
        subprocess.run(cmd, capture_output=True, timeout=25)
        print(f"Screenshot taken: {screenshot_path} (exists={os.path.exists(screenshot_path)}, size={os.path.getsize(screenshot_path) if os.path.exists(screenshot_path) else 0} bytes)")
    finally:
        p_vite.terminate()
        server.should_exit = True

if __name__ == "__main__":
    main()
