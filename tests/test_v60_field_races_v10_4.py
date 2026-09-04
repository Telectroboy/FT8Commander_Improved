import math
import unittest
from collections import deque
from pathlib import Path

import v60_runtime


class TestV10FieldRaces(unittest.TestCase):
    def test_marker_present(self):
        self.assertEqual(v60_runtime.V60_TXDF_FIELD_RACES_HOTFIX, '2026-09-03-v10.4')

    def test_qsy_accepts_fresh_status_after_intent_even_before_boundary(self):
        ok, reason, age = v60_runtime._v60_qsy_status_gate(
            last_status_at=102.0, intent_created_at=100.0, now=104.0, max_age=3.0
        )
        self.assertTrue(ok)
        self.assertEqual(reason, 'fresh-status')
        self.assertEqual(age, 2.0)

    def test_qsy_rejects_status_older_than_intent(self):
        ok, reason, age = v60_runtime._v60_qsy_status_gate(
            last_status_at=99.9, intent_created_at=100.0, now=101.0, max_age=3.0
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'no-status-since-intent')
        self.assertTrue(math.isinf(age))

    def test_qsy_rejects_stale_status(self):
        ok, reason, age = v60_runtime._v60_qsy_status_gate(
            last_status_at=100.5, intent_created_at=100.0, now=104.0, max_age=3.0
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'stale-status')
        self.assertAlmostEqual(age, 3.5)

    def test_local_map_is_cleared_across_bands(self):
        class Local:
            slots = {0: deque([1, 2]), 1: deque([3])}
        class Engine:
            local = Local()
        removed = v60_runtime._v60_clear_local_spectrum(Engine())
        self.assertEqual(removed, 3)
        self.assertEqual(list(Local.slots[0]), [])
        self.assertEqual(list(Local.slots[1]), [])

    def test_source_has_prearm_enable_grace(self):
        source = Path(v60_runtime.__file__).read_text(encoding='utf-8')
        self.assertIn("return 'prearmed-await-auto-tx'", source)
        self.assertIn('self.v60_txdf_armed_at + 3.0 if prestart else 0.0', source)
        self.assertIn('V10.4 TXDF LOCAL MAP RESET after QSY', source)
        self.assertNotIn('no WSJT-X Status received after guarded FT8 boundary yet', source)


if __name__ == '__main__':
    unittest.main()
