import unittest
from activity_guard import inactive_candidate

class ActivityGuardTests(unittest.TestCase):
    def test_old_activity_is_eligible(self):
        self.assertTrue(inactive_candidate(100, 200))
    def test_missing_or_invalid_never_kicks(self):
        for value in (None, 0, "bad", float("nan"), float("inf")):
            self.assertFalse(inactive_candidate(value, 200))
    def test_recent_evidence_protects_member(self):
        for extra in ({"recent_activity": 201}, {"joined_at": 201}, {"in_voice": True}):
            self.assertFalse(inactive_candidate(100, 200, **extra))
    def test_boundary_is_not_inactive(self):
        self.assertFalse(inactive_candidate(200, 200))
