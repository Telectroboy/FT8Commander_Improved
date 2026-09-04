import inspect
import unittest

import v60_runtime


class TestV60WantedSelectionHotfix(unittest.TestCase):
    def test_wanted_band_hold_does_not_consume_idle_evaluation(self):
        source = inspect.getsource(v60_runtime.install_v60_runtime)
        anchor = "pending_target = v60_pending_priority_target(self)"
        self.assertIn(anchor, source)
        block = source.split(anchor, 1)[1].split(
            "recent = bool(best and self.band_hopper.candidate_is_recent(best))", 1
        )[0]
        self.assertIn("V6 WANTED BAND HOLD", block)
        self.assertIn("return False", block)
        self.assertNotIn("return True", block)

    def test_v3_marker_present(self):
        self.assertEqual(
            v60_runtime.V60_WANTED_SELECTION_HOTFIX,
            "2026-09-02-v3",
        )


if __name__ == "__main__":
    unittest.main()
