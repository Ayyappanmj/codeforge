import json
import threading
import unittest

import tests  # noqa: F401
from tests.support.stubs import install_prometheus_stub_if_missing

install_prometheus_stub_if_missing()

from codeforge_shared.config import Settings  # noqa: E402
from codeforge_shared.results import CancelOutcome, RunResult  # noqa: E402
from codeforge_shared.states import ErrorType, ExecutionStatus as S  # noqa: E402
from codeforge_worker.processor import Decision, Processor  # noqa: E402
from codeforge_worker.runner import SandboxInfrastructureError  # noqa: E402
from tests.support.fakes import FakeRunner, FakeStore  # noqa: E402

OK = RunResult(S.SUCCEEDED, 0, "hi\n", "", 12)


def msg(eid):
    return json.dumps({"execution_id": str(eid), "correlation_id": "t"}).encode()


class ProcessorTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.settings = Settings.from_env({})
        self.runner = FakeRunner(OK)
        self.p = Processor(self.store, self.runner, self.settings, "w1", sleep=lambda s: None)

    def test_success_records_result_and_acks(self):
        eid = self.store.add()
        self.assertIs(self.p.process(msg(eid)), Decision.ACK)
        self.assertIs(self.store.status(eid), S.SUCCEEDED)
        self.assertEqual(self.store.rows[eid]["result"].stdout, "hi\n")

    def test_duplicate_delivery_after_completion_does_not_rerun(self):  # failure window C/D
        eid = self.store.add()
        self.p.process(msg(eid))
        self.assertIs(self.p.process(msg(eid)), Decision.ACK)
        self.assertEqual(self.runner.calls, 1)
        self.assertIs(self.store.status(eid), S.SUCCEEDED)

    def test_duplicate_while_running_is_acked_without_running(self):
        eid = self.store.add()
        self.store.claim(eid, "other-worker")
        self.assertIs(self.p.process(msg(eid)), Decision.ACK)
        self.assertEqual(self.runner.calls, 0)
        self.assertIs(self.store.status(eid), S.RUNNING)

    def test_concurrent_deliveries_run_exactly_once(self):
        eid = self.store.add()
        barrier = threading.Barrier(5)
        decisions = []

        def worker():
            barrier.wait()
            decisions.append(Processor(self.store, self.runner, self.settings, "w", sleep=lambda s: None).process(msg(eid)))

        ts = [threading.Thread(target=worker) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(self.runner.calls, 1)
        self.assertEqual(decisions.count(Decision.ACK), 5)

    def test_poison_messages_go_to_dead_letter(self):
        for body in (b"not json", b"{}", json.dumps({"execution_id": "zzz"}).encode(), b"[]"):
            self.assertIs(self.p.process(body), Decision.DEAD)
        import uuid
        self.assertIs(self.p.process(msg(uuid.uuid4())), Decision.DEAD)  # unknown id
        self.assertEqual(self.runner.calls, 0)

    def test_db_down_on_claim_retries_message(self):  # window E
        eid = self.store.add()
        self.store.fail_claim = 1
        self.assertIs(self.p.process(msg(eid)), Decision.RETRY)
        self.assertIs(self.store.status(eid), S.QUEUED)
        self.assertIs(self.p.process(msg(eid)), Decision.ACK)
        self.assertIs(self.store.status(eid), S.SUCCEEDED)

    def test_db_flaps_during_record_is_retried_in_process(self):  # window E
        eid = self.store.add()
        self.store.fail_finish = 2
        self.assertIs(self.p.process(msg(eid)), Decision.ACK)
        self.assertIs(self.store.status(eid), S.SUCCEEDED)

    def test_db_down_for_good_during_record_leaves_row_running_for_sweeper(self):
        eid = self.store.add()
        self.store.fail_finish = 99
        self.assertIs(self.p.process(msg(eid)), Decision.RETRY)
        self.assertIs(self.store.status(eid), S.RUNNING)  # sweeper will requeue after stale timeout

    def test_infra_error_requeues_until_attempts_exhausted(self):
        runner = FakeRunner(exc=SandboxInfrastructureError("docker down"))
        p = Processor(self.store, runner, self.settings, "w1", sleep=lambda s: None)
        eid = self.store.add()
        for expected in (S.QUEUED, S.QUEUED, S.FAILED):  # max_attempts == 3
            self.assertIs(p.process(msg(eid)), Decision.ACK)
            self.assertIs(self.store.status(eid), expected)
        self.assertEqual(self.store.rows[eid]["result"].error_type, ErrorType.INFRASTRUCTURE_ERROR)
        self.assertEqual(runner.calls, 3)
        self.assertEqual(len(self.store.outbox), 3)  # initial + 2 requeues

    def test_cancel_before_start_prevents_run(self):
        eid = self.store.add()
        self.assertIs(self.store.request_cancel(eid), CancelOutcome.CANCELLED)
        self.assertIs(self.p.process(msg(eid)), Decision.ACK)
        self.assertEqual(self.runner.calls, 0)
        self.assertIs(self.store.status(eid), S.CANCELLED)

    def test_cancel_racing_with_natural_completion_never_flips_a_finished_job(self):
        eid = self.store.add()
        # cancel request arrives while the container is finishing on its own
        runner = FakeRunner(OK, hook=lambda: self.store.request_cancel(eid))
        p = Processor(self.store, runner, self.settings, "w1", sleep=lambda s: None)
        p.process(msg(eid))
        self.assertIs(self.store.status(eid), S.SUCCEEDED)
        self.assertIs(self.store.request_cancel(eid), CancelOutcome.NOT_CANCELLABLE)

    def test_killed_run_is_recorded_as_cancelled(self):
        eid = self.store.add()
        cancelled = RunResult(S.CANCELLED, None, "", "", 5, ErrorType.CANCELLED)
        runner = FakeRunner(cancelled, hook=lambda: self.store.request_cancel(eid))
        Processor(self.store, runner, self.settings, "w1", sleep=lambda s: None).process(msg(eid))
        self.assertIs(self.store.status(eid), S.CANCELLED)

    def test_late_result_cannot_overwrite_terminal_state(self):  # idempotent recording
        eid = self.store.add()
        self.p.process(msg(eid))
        late = RunResult(S.FAILED, 1, "", "late", 1, ErrorType.RUNTIME_ERROR)
        self.assertFalse(self.store.finish(eid, late))
        self.assertIs(self.store.status(eid), S.SUCCEEDED)

    def test_timeout_result_is_recorded(self):
        eid = self.store.add()
        runner = FakeRunner(RunResult(S.TIMED_OUT, None, "", "", 5000, ErrorType.TIMEOUT))
        Processor(self.store, runner, self.settings, "w", sleep=lambda s: None).process(msg(eid))
        self.assertIs(self.store.status(eid), S.TIMED_OUT)


if __name__ == "__main__":
    unittest.main()
