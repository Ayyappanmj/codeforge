"""Runner process-management tests using tests/support/fake_docker.py (NO real isolation).
These verify: result capture, stdin, exit codes, timeout + kill, output cap while streaming,
cancellation, infrastructure-failure signalling and that `docker rm -f` cleanup is invoked."""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import tests  # noqa: F401
from codeforge_shared.config import ResourceLimits
from codeforge_shared.results import RunInput
from codeforge_shared.states import ErrorType, ExecutionStatus as S
from codeforge_worker.runner import DockerRunner, SandboxInfrastructureError, reap_orphaned_containers

FAKE = str(Path(__file__).resolve().parents[1] / "support" / "fake_docker.py")
HAVE_NODE = shutil.which("node") is not None


class RunnerFakeDockerTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        os.environ["FAKE_DOCKER_STATE"] = self.state
        os.environ.pop("FAKE_DOCKER_FAIL", None)
        self.runner = DockerRunner(ResourceLimits(timeout_seconds=3.0, max_output_bytes=4096), FAKE)

    def tearDown(self):
        shutil.rmtree(self.state, ignore_errors=True)
        os.environ.pop("FAKE_DOCKER_STATE", None)

    def run_code(self, lang, src, stdin="", **kw):
        return self.runner.run(RunInput(lang, src, stdin, 1), **kw)

    def leftovers(self):
        return list((Path(self.state) / "fake_docker_state").glob("cf-*"))

    def test_python_success(self):
        r = self.run_code("python", "print('Hello, CodeForge!')")
        self.assertEqual((r.status, r.exit_code, r.stdout.strip()), (S.SUCCEEDED, 0, "Hello, CodeForge!"))
        self.assertIsNone(r.error_type)

    @unittest.skipUnless(HAVE_NODE, "node not installed")
    def test_javascript_success(self):
        r = self.run_code("javascript", "console.log(1+2)")
        self.assertEqual((r.status, r.stdout.strip()), (S.SUCCEEDED, "3"))

    def test_stdin(self):
        r = self.run_code("python", "print(input().upper())", stdin="abc\n")
        self.assertEqual(r.stdout.strip(), "ABC")

    def test_orphan_reaper_runs_safely(self):
        reaped = reap_orphaned_containers(FAKE, max_age_seconds=0.1)
        self.assertEqual(reaped, 0)
        reaped_invalid = reap_orphaned_containers("/no/such/docker", max_age_seconds=0.1)
        self.assertEqual(reaped_invalid, 0)

    def test_runtime_error(self):
        r = self.run_code("python", "raise ValueError('boom')")
        self.assertEqual((r.status, r.error_type), (S.FAILED, ErrorType.RUNTIME_ERROR))
        self.assertIn("ValueError", r.stderr)

    def test_nonzero_exit_code(self):
        r = self.run_code("python", "import sys; sys.exit(3)")
        self.assertEqual((r.status, r.exit_code), (S.FAILED, 3))

    def test_infinite_loop_times_out_and_is_killed(self):
        t = time.monotonic()
        r = self.run_code("python", "while True: pass")
        self.assertEqual((r.status, r.error_type), (S.TIMED_OUT, ErrorType.TIMEOUT))
        self.assertLess(time.monotonic() - t, 6)

    def test_child_processes_do_not_survive_timeout(self):
        marker = Path(self.state) / "child_alive"
        child = (
            "import time\n"
            "while True:\n"
            f"    open({str(marker)!r}, 'w').write(str(time.time()))\n"
            "    time.sleep(0.1)\n"
        )
        src = (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            "time.sleep(30)\n"
        )
        r = self.run_code("python", src)
        self.assertEqual(r.status, S.TIMED_OUT)
        time.sleep(0.5)
        before = marker.read_text() if marker.exists() else ""
        time.sleep(0.6)
        after = marker.read_text() if marker.exists() else ""
        self.assertEqual(before, after, "child kept running after container kill")

    def test_excessive_output_is_capped_and_killed_early(self):
        t = time.monotonic()
        r = self.run_code("python", "import sys\nwhile True: sys.stdout.write('x'*1000)")
        self.assertEqual((r.status, r.error_type), (S.FAILED, ErrorType.OUTPUT_LIMIT_EXCEEDED))
        self.assertTrue(r.output_truncated)
        self.assertLessEqual(len(r.stdout), 4096 + 50)
        self.assertLess(time.monotonic() - t, 5)

    def test_cancellation_kills_run(self):
        t0 = time.monotonic()
        r = self.run_code("python", "import time; time.sleep(30)",
                          cancel_check=lambda: time.monotonic() - t0 > 0.3)
        self.assertEqual((r.status, r.error_type), (S.CANCELLED, ErrorType.CANCELLED))

    def test_cleanup_called_after_every_outcome(self):
        for src in ("print(1)", "raise SystemExit(2)", "while True: pass"):
            self.run_code("python", src)
        self.assertEqual(self.leftovers(), [], "container state files were not removed")

    def test_docker_failure_is_reported_as_infrastructure_error(self):
        os.environ["FAKE_DOCKER_FAIL"] = "1"
        with self.assertRaises(SandboxInfrastructureError):
            self.run_code("python", "print(1)")

    def test_missing_docker_binary_is_infrastructure_error(self):
        runner = DockerRunner(ResourceLimits(), "/nonexistent/docker")
        with self.assertRaises(SandboxInfrastructureError):
            runner.run(RunInput("python", "print(1)", "", 1))


if __name__ == "__main__":
    unittest.main()
