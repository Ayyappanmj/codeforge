import io
import unittest

import tests  # noqa: F401
from codeforge_shared.config import ResourceLimits
from codeforge_shared.output import TRUNCATION_MARKER, OutputBudget, capture_stream, decode_output
from codeforge_shared.protocol import decode_payload, encode_payload
from codeforge_shared.retry import RetryPolicy
from codeforge_shared.sandbox import build_docker_run_args, classify_result, validate_container_name
from codeforge_shared.states import ErrorType, ExecutionStatus as S


class RetryPolicyTests(unittest.TestCase):
    def test_bounded(self):
        p = RetryPolicy(max_attempts=3)
        self.assertTrue(p.should_retry(0))
        self.assertTrue(p.should_retry(2))
        self.assertFalse(p.should_retry(3))

    def test_exponential_backoff_capped(self):
        p = RetryPolicy(base_delay=1, max_delay=5)
        self.assertEqual([p.delay_for(i) for i in (1, 2, 3, 4, 10)], [1, 2, 4, 5, 5])


class OutputTests(unittest.TestCase):
    def test_budget_is_shared_and_enforced_while_reading(self):
        budget = OutputBudget(10)
        a, b = bytearray(), bytearray()
        capture_stream(io.BytesIO(b"x" * 100), budget, a, chunk_size=4)
        capture_stream(io.BytesIO(b"y" * 100), budget, b, chunk_size=4)
        self.assertEqual(len(a) + len(b), 10)
        self.assertTrue(budget.exceeded.is_set())

    def test_within_budget_not_flagged(self):
        budget = OutputBudget(100)
        sink = bytearray()
        capture_stream(io.BytesIO(b"hello"), budget, sink)
        self.assertEqual(bytes(sink), b"hello")
        self.assertFalse(budget.exceeded.is_set())

    def test_decode_marks_truncation_and_strips_nul(self):
        self.assertTrue(decode_output(b"abc", True).endswith(TRUNCATION_MARKER))
        self.assertNotIn("\x00", decode_output(b"a\x00b", False))
        self.assertEqual(decode_output(b"\xff", False), "\ufffd")


class SandboxArgsTests(unittest.TestCase):
    NAME = "cf-" + "a" * 32

    def args(self, **kw):
        return build_docker_run_args(self.NAME, "codeforge-python-runner:latest", ResourceLimits(**kw))

    def test_required_hardening_flags_present(self):
        a = self.args()
        pairs = [("--network", "none"), ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges"),
                 ("--user", "10001:10001"), ("--memory", "128m"), ("--memory-swap", "128m"),
                 ("--pids-limit", "32"), ("--cpus", "1"), ("--pull", "never"), ("--log-driver", "none")]
        for k, v in pairs:
            self.assertEqual(a[a.index(k) + 1], v, k)
        self.assertIn("--read-only", a)
        self.assertEqual(a[-1], "codeforge-python-runner:latest")

    def test_no_dangerous_flags_or_mounts(self):
        a = " ".join(self.args())
        for bad in ("--privileged", "-v ", "--volume", "--mount", "docker.sock", "--network host",
                    "--cap-add", "--env", "-e ", "--pid host"):
            self.assertNotIn(bad, a)

    def test_limits_are_applied(self):
        a = self.args(memory_mb=64, pids=8, cpus=0.5)
        self.assertEqual(a[a.index("--memory") + 1], "64m")
        self.assertEqual(a[a.index("--pids-limit") + 1], "8")
        self.assertEqual(a[a.index("--cpus") + 1], "0.5")

    def test_gvisor_runtime_flag(self):
        a = self.args(sandbox_runtime="runsc")
        self.assertIn("--runtime", a)
        self.assertEqual(a[a.index("--runtime") + 1], "runsc")

    def test_name_and_image_validation(self):
        for bad in ("x", "cf-../etc", "cf-" + "g" * 32, "cf-" + "a" * 31 + ";"):
            with self.assertRaises(ValueError):
                validate_container_name(bad)
        with self.assertRaises(ValueError):
            build_docker_run_args(self.NAME, "img --privileged", ResourceLimits())


class ClassificationTests(unittest.TestCase):
    def test_matrix(self):
        c = classify_result
        self.assertEqual(c(0).status, S.SUCCEEDED)
        self.assertEqual((c(1).status, c(1).error_type), (S.FAILED, ErrorType.RUNTIME_ERROR))
        self.assertEqual((c(None, timed_out=True).status, c(None, timed_out=True).error_type),
                         (S.TIMED_OUT, ErrorType.TIMEOUT))
        self.assertEqual(c(137, oom_killed=True).error_type, ErrorType.MEMORY_LIMIT_EXCEEDED)
        self.assertEqual(c(None, output_exceeded=True).error_type, ErrorType.OUTPUT_LIMIT_EXCEEDED)
        self.assertEqual(c(None, cancelled=True).status, S.CANCELLED)

    def test_our_kills_outrank_exit_code(self):
        self.assertEqual(classify_result(0, timed_out=True).status, S.TIMED_OUT)
        self.assertEqual(classify_result(0, cancelled=True, timed_out=True).status, S.CANCELLED)


class ProtocolTests(unittest.TestCase):
    def test_roundtrip_including_newlines_and_binary(self):
        src, stdin = b"print('a\\n')\n\x00\xff", b"line1\nline2\n"
        self.assertEqual(decode_payload(encode_payload(src, stdin)), (src, stdin))

    def test_bad_payloads(self):
        for bad in (b"", b"CF1 1 1\nab" + b"c", b"XX 1 1\nab", b"CF1 a b\nxx"):
            with self.assertRaises(ValueError):
                decode_payload(bad)


if __name__ == "__main__":
    unittest.main()
