import unittest

import tests  # noqa: F401
from codeforge_shared.states import (
    ALLOWED_TRANSITIONS, ExecutionStatus as S, InvalidTransition, TERMINAL_STATUSES,
    assert_transition, can_transition, sources_for,
)


class StateMachineTests(unittest.TestCase):
    def test_terminal_states_have_no_outgoing_transitions(self):
        for s in TERMINAL_STATUSES:
            self.assertEqual(ALLOWED_TRANSITIONS[s], frozenset())
            for t in S:
                self.assertFalse(can_transition(s, t))

    def test_every_status_is_defined(self):
        self.assertEqual(set(ALLOWED_TRANSITIONS), set(S))

    def test_happy_paths(self):
        for a, b in [(S.QUEUED, S.RUNNING), (S.RUNNING, S.SUCCEEDED), (S.RUNNING, S.FAILED),
                     (S.RUNNING, S.TIMED_OUT), (S.RUNNING, S.CANCELLED), (S.QUEUED, S.CANCELLED),
                     (S.RUNNING, S.QUEUED)]:
            assert_transition(a, b)

    def test_illegal_transitions_raise(self):
        for a, b in [(S.QUEUED, S.SUCCEEDED), (S.QUEUED, S.TIMED_OUT), (S.SUCCEEDED, S.RUNNING),
                     (S.CANCELLED, S.SUCCEEDED), (S.FAILED, S.QUEUED), (S.TIMED_OUT, S.CANCELLED)]:
            with self.assertRaises(InvalidTransition):
                assert_transition(a, b)

    def test_sources_for_matches_table(self):
        self.assertEqual(sources_for(S.RUNNING), frozenset({S.QUEUED}))
        self.assertEqual(sources_for(S.SUCCEEDED), frozenset({S.RUNNING}))
        self.assertIn(S.QUEUED, sources_for(S.CANCELLED))


if __name__ == "__main__":
    unittest.main()
