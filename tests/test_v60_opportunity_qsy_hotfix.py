import unittest
from pathlib import Path

import v60_runtime


class TestV60OpportunityQSYHotfix(unittest.TestCase):
    def test_qsy_guard_waits_for_next_ft8_boundary(self):
        # At xx:xx:28, next boundary is :30; +1 s safety => 3 s total.
        self.assertAlmostEqual(v60_runtime._v60_qsy_guard_delay(28.0, 1.0), 3.0)
        # Immediately after a boundary, wait nearly a full slot, never execute now.
        self.assertGreater(v60_runtime._v60_qsy_guard_delay(30.01, 1.0), 15.9)

    def test_opportunity_requires_real_cq(self):
        ok, why, _ = v60_runtime._v60_opportunity_retry_allowed(
            'RR73', -8, -18, 60, 30, 3, -12
        )
        self.assertFalse(ok)
        self.assertEqual(why, 'not-CQ')

    def test_opportunity_respects_minimum_gap(self):
        ok, why, _ = v60_runtime._v60_opportunity_retry_allowed(
            'CQ', -8, -18, 29, 30, 3, -12
        )
        self.assertFalse(ok)
        self.assertEqual(why, 'minimum-gap')

    def test_improving_cq_bypasses_antiping(self):
        ok, why, gain = v60_runtime._v60_opportunity_retry_allowed(
            'CQ', -13, -18, 56, 30, 3, -12
        )
        self.assertTrue(ok)
        self.assertEqual(why, 'improving-CQ')
        self.assertEqual(gain, 5.0)

    def test_strong_cq_bypasses_without_gain(self):
        ok, why, gain = v60_runtime._v60_opportunity_retry_allowed(
            'CQ', -11, -11, 45, 30, 3, -12
        )
        self.assertTrue(ok)
        self.assertEqual(why, 'strong-CQ')
        self.assertEqual(gain, 0.0)

    def test_weak_non_improving_cq_does_not_bypass(self):
        ok, why, gain = v60_runtime._v60_opportunity_retry_allowed(
            'CQ', -18, -17, 60, 30, 3, -12
        )
        self.assertFalse(ok)
        self.assertEqual(why, 'no-opportunity')
        self.assertEqual(gain, -1.0)

    def test_runtime_contains_transactional_and_manual_guards(self):
        source = Path(v60_runtime.__file__).read_text()
        self.assertIn('V6 QSY_PENDING', source)
        self.assertIn('V6 MANUAL TX BAND LOCK', source)
        self.assertIn('V6 WANTED BAND HOLD', source)
        self.assertIn("qsy_state in {'waiting', 'executed'}", source)
        self.assertIn("rec['last_tx_at'] = now", source)
        self.assertIn('v60_pending_priority_target', source)
        self.assertIn("'boundary_at': boundary_at", source)
        self.assertIn('_v60_qsy_status_gate', source)
        self.assertIn('no WSJT-X Status received since QSY intent was created', source)
        self.assertIn('latest WSJT-X Status is %.1fs old', source)
        self.assertIn("self.v60_manual_tx_lock_started = 0.0", source)


if __name__ == '__main__':
    unittest.main()
