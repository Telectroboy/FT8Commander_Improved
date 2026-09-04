import unittest
from pathlib import Path

import v60_runtime


class TestV60BandPursuitHotfix(unittest.TestCase):
    def target(self, *, band=20, age=5.0, now=1000.0):
        return {
            'call': 'TK4QP', 'band': band, 'last_seen': now - age,
            'proactive': True,
        }

    def test_fresh_antiping_holds_band(self):
        now = 1000.0
        action = v60_runtime._v60_deferred_antiping_action(
            self.target(now=now), 20, 90.0,
            (False, 'anti-ping-pong', 30.0), now,
        )
        self.assertEqual(action, 'hold')

    def test_antiping_expired_rearms_while_fresh(self):
        now = 1000.0
        action = v60_runtime._v60_deferred_antiping_action(
            self.target(now=now), 20, 90.0,
            (True, 'eligible', 0.0), now,
        )
        self.assertEqual(action, 'rearm')

    def test_stale_or_other_policy_does_not_hold(self):
        now = 1000.0
        stale = v60_runtime._v60_deferred_antiping_action(
            self.target(age=91.0, now=now), 20, 90.0,
            (False, 'anti-ping-pong', 30.0), now,
        )
        capped = v60_runtime._v60_deferred_antiping_action(
            self.target(now=now), 20, 90.0,
            (False, 'profile-TX-cap', 300.0), now,
        )
        wrong_band = v60_runtime._v60_deferred_antiping_action(
            self.target(band=15, now=now), 20, 90.0,
            (False, 'anti-ping-pong', 30.0), now,
        )
        self.assertEqual(stale, 'drop')
        self.assertEqual(capped, 'drop')
        self.assertEqual(wrong_band, 'drop')

    def test_active_pursuit_never_expires_mid_attempt(self):
        rec = {'last_seen': 100.0}
        self.assertFalse(v60_runtime._v60_pursuit_should_expire(
            'TK4QP', 'TK4QP', rec, 1000.0, 90.0,
        ))
        self.assertTrue(v60_runtime._v60_pursuit_should_expire(
            'TK4QP', '', rec, 1000.0, 90.0,
        ))

    def test_runtime_never_shortens_attempt_lock(self):
        source = Path(v60_runtime.__file__).read_text()
        self.assertNotIn('attempt_lock_until = min(', source)


if __name__ == '__main__':
    unittest.main()
