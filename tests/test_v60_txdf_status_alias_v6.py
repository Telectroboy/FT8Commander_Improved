import inspect
import unittest
import v60_runtime

class TestTXDFStatusAliasV6(unittest.TestCase):
    def test_marker_present(self):
        self.assertEqual(v60_runtime.V60_TXDF_STATUS_ALIAS_HOTFIX, '2026-09-02-v6')

    def test_active_vs1_sub_status_maps_to_main(self):
        freq, aliased = v60_runtime._v60_txdf_canonical_status_frequency(
            14_073_720, saved_fa=14_074_000, prepared_sub=14_073_720,
            active=True, alias_until=0.0, now=100.0, tolerance=10)
        self.assertEqual(freq, 14_074_000)
        self.assertTrue(aliased)

    def test_post_vs0_trailing_sub_status_maps_inside_grace(self):
        freq, aliased = v60_runtime._v60_txdf_canonical_status_frequency(
            14_073_720, saved_fa=14_074_000, prepared_sub=14_073_720,
            active=False, alias_until=103.0, now=101.0, tolerance=10)
        self.assertEqual(freq, 14_074_000)
        self.assertTrue(aliased)

    def test_sub_status_is_not_hidden_after_grace(self):
        freq, aliased = v60_runtime._v60_txdf_canonical_status_frequency(
            14_073_720, saved_fa=14_074_000, prepared_sub=14_073_720,
            active=False, alias_until=100.0, now=104.0, tolerance=10)
        self.assertEqual(freq, 14_073_720)
        self.assertFalse(aliased)

    def test_unrelated_manual_frequency_is_never_hidden(self):
        freq, aliased = v60_runtime._v60_txdf_canonical_status_frequency(
            14_075_000, saved_fa=14_074_000, prepared_sub=14_073_720,
            active=True, alias_until=200.0, now=101.0, tolerance=10)
        self.assertEqual(freq, 14_075_000)
        self.assertFalse(aliased)

    def test_main_status_stays_main(self):
        freq, aliased = v60_runtime._v60_txdf_canonical_status_frequency(
            14_074_000, saved_fa=14_074_000, prepared_sub=14_073_720,
            active=True, alias_until=200.0, now=101.0, tolerance=10)
        self.assertEqual(freq, 14_074_000)
        self.assertFalse(aliased)

    def test_process_status_canonicalizes_before_manual_check(self):
        source = inspect.getsource(v60_runtime.install_v60_runtime)
        self.assertLess(source.index('_v60_txdf_canonical_status_frequency('),
                        source.index('frequency_changed = old_frequency'))
        self.assertIn('self.frequency = frequency', source)
        self.assertIn('V6 TXDF STATUS alias', source)

    def test_alias_window_is_maintained_and_cleared(self):
        source = inspect.getsource(v60_runtime.install_v60_runtime)
        self.assertIn('time.monotonic() + 3.0', source)
        self.assertIn('self.v60_txdf_status_alias_until = 0.0', source)

    def test_original_v5_manual_override_bug_pattern_is_neutralized(self):
        freq, _ = v60_runtime._v60_txdf_canonical_status_frequency(
            14_073_720, saved_fa=14_074_000, prepared_sub=14_073_720,
            active=True, alias_until=0.0, now=100.0, tolerance=10)
        self.assertEqual(abs(freq - 14_074_000), 0)

if __name__ == '__main__':
    unittest.main()
