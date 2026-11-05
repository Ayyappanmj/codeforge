"""In-memory stand-in for SqlStore implementing the same compare-and-set semantics, used to
test the Processor's decisions without a database. SQL behaviour is covered separately by
tests/integration/test_store_sqlite.py (needs SQLAlchemy)."""
import threading
import uuid

from codeforge_shared.results import CancelOutcome, ClaimKind, ClaimResult, RunInput
from codeforge_shared.states import ExecutionStatus as S, assert_transition


class FakeStore:
    def __init__(self):
        self.rows = {}
        self.outbox = []
        self.lock = threading.Lock()
        self.fail_claim = 0
        self.fail_finish = 0

    def add(self, language="python", source="print(1)", stdin="") -> uuid.UUID:
        eid = uuid.uuid4()
        self.rows[eid] = dict(status=S.QUEUED, language=language, source=source, stdin=stdin,
                              attempts=0, cancel=False, result=None, error=None)
        self.outbox.append(eid)
        return eid

    def status(self, eid):
        return self.rows[eid]["status"]

    def claim(self, eid, worker_id):
        if self.fail_claim:
            self.fail_claim -= 1
            raise ConnectionError("db down")
        with self.lock:
            r = self.rows.get(eid)
            if r is None:
                return ClaimResult(ClaimKind.NOT_FOUND)
            if r["status"] is S.QUEUED:
                assert_transition(S.QUEUED, S.RUNNING)
                r["status"], r["attempts"] = S.RUNNING, r["attempts"] + 1
                return ClaimResult(ClaimKind.CLAIMED,
                                   RunInput(r["language"], r["source"], r["stdin"], r["attempts"]))
            if r["status"] is S.RUNNING:
                return ClaimResult(ClaimKind.IN_PROGRESS)
            return ClaimResult(ClaimKind.TERMINAL)

    def finish(self, eid, result):
        if self.fail_finish:
            self.fail_finish -= 1
            raise ConnectionError("db down")
        with self.lock:
            r = self.rows[eid]
            assert_transition(S.RUNNING, result.status)
            if r["status"] is not S.RUNNING:
                return False
            r["status"], r["result"] = result.status, result
            return True

    def requeue(self, eid, error=None, reason="", delay_seconds=0):
        with self.lock:
            r = self.rows[eid]
            if r["status"] is not S.RUNNING:
                return False
            r["status"] = S.QUEUED
            self.outbox.append(eid)
            return True

    def is_cancel_requested(self, eid):
        return self.rows[eid]["cancel"]

    def request_cancel(self, eid):
        with self.lock:
            r = self.rows.get(eid)
            if r is None:
                return CancelOutcome.NOT_FOUND
            if r["status"] is S.QUEUED:
                r["status"] = S.CANCELLED
                return CancelOutcome.CANCELLED
            if r["status"] is S.RUNNING:
                r["cancel"] = True
                return CancelOutcome.CANCEL_REQUESTED
            return CancelOutcome.NOT_CANCELLABLE


class FakeRunner:
    def __init__(self, result=None, exc=None, hook=None):
        self.result, self.exc, self.hook, self.calls = result, exc, hook, 0

    def run(self, run_input, cancel_check=lambda: False, execution_id=""):
        self.calls += 1
        if self.hook:
            self.hook()
        if self.exc:
            raise self.exc
        return self.result
