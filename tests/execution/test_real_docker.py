"""Real-Docker tests. They need a Docker daemon and the built execution images:
    docker compose --profile images build
They are NEVER silently skipped: without Docker they skip with an explicit reason, and
BUILD_STATUS.md records them as 'not run'."""
import shutil
import subprocess
import unittest

import tests  # noqa: F401
from codeforge_shared.config import ResourceLimits
from codeforge_shared.results import RunInput
from codeforge_shared.states import ErrorType, ExecutionStatus as S
from codeforge_worker.runner import DockerRunner


def _docker_ready() -> str | None:
    if not shutil.which("docker"):
        return "docker CLI not installed"
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        return "docker daemon not reachable"
    for img in ("codeforge-python-runner:latest", "codeforge-javascript-runner:latest"):
        if subprocess.run(["docker", "image", "inspect", img], capture_output=True).returncode != 0:
            return f"image {img} not built (run: docker compose --profile images build)"
    return None


REASON = _docker_ready()


@unittest.skipIf(REASON is not None, f"REAL DOCKER TESTS NOT RUN: {REASON}")
class RealDockerTests(unittest.TestCase):
    def setUp(self):
        self.runner = DockerRunner(ResourceLimits(timeout_seconds=5, max_output_bytes=8192))

    def run_code(self, lang, src, stdin=""):
        return self.runner.run(RunInput(lang, src, stdin, 1))

    def test_python_and_javascript_hello(self):
        self.assertEqual(self.run_code("python", "print('hi')").stdout.strip(), "hi")
        self.assertEqual(self.run_code("javascript", "console.log('hi')").stdout.strip(), "hi")

    def test_stdin(self):
        self.assertEqual(self.run_code("python", "print(input())", "x\n").stdout.strip(), "x")

    def test_timeout(self):
        self.assertEqual(self.run_code("python", "while True: pass").status, S.TIMED_OUT)

    def test_no_network(self):
        r = self.run_code("python", "import socket\nsocket.create_connection(('1.1.1.1',53),timeout=2)")
        self.assertEqual(r.status, S.FAILED)

    def test_runs_as_non_root_and_read_only_fs(self):
        r = self.run_code("python", "import os;print(os.getuid())\nopen('/etc/x','w')")
        self.assertEqual(r.stdout.strip(), "10001")
        self.assertEqual(r.status, S.FAILED)  # /etc is read-only

    def test_no_secrets_in_env(self):
        r = self.run_code("python", "import os;print(sorted(os.environ))")
        for secret in ("DATABASE_URL", "RABBITMQ_URL"):
            self.assertNotIn(secret, r.stdout)

    def test_fork_bomb_is_contained(self):
        r = self.run_code("python", "import os\nwhile True: os.fork()")
        self.assertIn(r.status, (S.TIMED_OUT, S.FAILED))

    def test_memory_limit(self):
        r = self.run_code("python", "x = bytearray(1024*1024*1024)")
        self.assertEqual(r.status, S.FAILED)

    def test_output_flood(self):
        r = self.run_code("python", "while True: print('x'*1000)")
        self.assertEqual(r.error_type, ErrorType.OUTPUT_LIMIT_EXCEEDED)

    def test_container_cleanup(self):
        self.run_code("python", "while True: pass")
        out = subprocess.run(["docker", "ps", "-a", "--filter", "label=codeforge.sandbox=1", "-q"],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
