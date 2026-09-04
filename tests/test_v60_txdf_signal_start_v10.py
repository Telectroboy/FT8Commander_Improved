import math
import unittest
from pathlib import Path
from types import SimpleNamespace

import v60_runtime
from v60_txdf import TxDFEngine, _signal_edge_gap


class Config:
    tx_df_enabled = True
    tx_df_audio_hz = 1500
    tx_df_preferred_radius_hz = 300
    tx_df_max_radius_hz = 500
    tx_df_min_hz = -100
    tx_df_max_hz = 3500
    tx_df_grid_step_hz = 20
    tx_df_signal_width_hz = 50
    tx_df_diversity_min_hz = 100
    tx_df_local_ttl = 180
    tx_df_remote_ttl = 600
    tx_df_local_weight = 1.0
    tx_df_remote_weight = 1.3
    tx_df_distance_weight = 0.05
    tx_df_diversity_weight = 20.0
    tx_df_hard_guard_hz = 80
    tx_df_hard_guard_ttl_s = 90
    tx_df_clearance_goal_hz = 120


class TestSignalStartV10(unittest.TestCase):
    def new_engine(self):
        return TxDFEngine(Config)

    def test_marker_present(self):
        self.assertEqual(v60_runtime.V60_TXDF_SIGNAL_START_HOTFIX, '2026-09-03-v10')

    def test_df_is_signal_start_and_overlap_is_zero_clearance(self):
        self.assertEqual(_signal_edge_gap(320, 306, 50), 0.0)
        self.assertEqual(_signal_edge_gap(436, 306, 50), 80.0)
        self.assertEqual(_signal_edge_gap(176, 306, 50), 80.0)

    def test_local_clearance_is_edge_to_edge_not_start_to_start(self):
        e = self.new_engine()
        e.local.add(306, -9, 'RC6OD', time_ms=15000, ts=990.0)
        clearance, nearest = e.local.clearance(320, 1, 90, now=1000.0)
        self.assertEqual(clearance, 0.0)
        self.assertEqual(nearest.df, 306)
        clearance, _ = e.local.clearance(436, 1, 90, now=1000.0)
        self.assertEqual(clearance, 80.0)

    def test_306_start_cannot_accept_320_as_safe_hole(self):
        e = self.new_engine()
        e.local.add(306, -9, 'RC6OD', time_ms=15000, ts=990.0)
        chosen = e.choose('8Q7PR', 931, 1, [], now=1000.0)
        self.assertNotEqual(chosen, 320)
        self.assertLessEqual(abs(chosen - 931), 500)
        self.assertGreaterEqual(e.last_choice_debug['clearance'], 80)
        self.assertEqual(e.last_choice_debug['clearance_basis'], 'edge-to-edge')
        self.assertEqual(e.last_choice_debug['signal_width_hz'], 50)

    def test_shifted_hole_stays_within_radius_and_preferred_absolute_window(self):
        e = self.new_engine()
        for target in (-50, 0, 100, 931, 1779, 3400):
            chosen = e.choose('X', target, 0, [], now=1000.0)
            self.assertLessEqual(abs(chosen - target), 500)
            self.assertGreaterEqual(chosen, -100)
            self.assertLessEqual(chosen, 3500)

    def test_fully_congested_window_falls_back_exactly_to_caller_start(self):
        e = self.new_engine()
        # Signals every 100 Hz cover the complete +/-500 search window once
        # each one is treated as a 50 Hz interval plus 80 Hz edge guard.
        for df in range(400, 1501, 100):
            e.local.add(df, -5, f'S{df}', time_ms=15000, ts=990.0)
        chosen = e.choose('8Q7PR', 931, 1, [], now=1000.0)
        self.assertEqual(chosen, 931)
        self.assertEqual(e.last_choice_debug['mode'], 'caller-frequency-fallback')
        self.assertLess(e.last_choice_debug['clearance'], 80)

    def test_no_hole_window_falls_back_to_caller_even_outside_preferred_range(self):
        e = self.new_engine()
        # More than 500 Hz outside the preferred absolute region => no shifted
        # candidate exists, so the explicit field rule is exact caller DF.
        chosen = e.choose('DX', 4101, 1, [], now=1000.0)
        self.assertEqual(chosen, 4101)
        self.assertEqual(e.last_choice_debug['mode'], 'caller-frequency-fallback-no-hole-window')

    def test_other_parity_is_secondary_proxy_not_hard_block(self):
        e = self.new_engine()
        e.local.add(940, +10, 'LOUD', time_ms=0, ts=990.0)
        chosen = e.choose('8Q7PR', 931, 1, [], now=1000.0)
        # V10.5+: the actual TX parity remains authoritative (no observations
        # here => infinite exact-parity clearance), while the opposite parity
        # is deliberately used as a secondary occupancy proxy.  It may steer
        # us away from the busy 940-Hz neighbourhood but is not a hard blocker.
        self.assertNotIn(chosen, (920, 940))
        self.assertTrue(math.isinf(e.last_choice_debug['clearance']))
        self.assertEqual(e.last_choice_debug.get('proxy_observed'), 1)
        self.assertTrue(e.last_choice_debug.get('proxy_used'))
        self.assertGreaterEqual(
            e.last_choice_debug.get('proxy_clearance', -1), e.opposite_guard)

    def test_prearm_before_reply_approaching_boundary(self):
        seq = SimpleNamespace(
            v60_txdf=self.new_engine(),
            v60_txdf_pre_tx_guard=1.8,
            v60_txdf_selection_margin=0.8,
            v60_txdf_start_grace=1.5,
        )
        data = {'time': 15000}
        prearm, lead, slot, phase, reason = v60_runtime._v60_txdf_prearm_before_reply(seq, data, wall=29.0)
        self.assertTrue(prearm)
        self.assertEqual(slot, 0)
        self.assertAlmostEqual(lead, 1.0, places=6)
        self.assertIn('approaching', reason)

    def test_prearm_before_reply_just_after_boundary(self):
        seq = SimpleNamespace(
            v60_txdf=self.new_engine(),
            v60_txdf_pre_tx_guard=1.8,
            v60_txdf_selection_margin=0.8,
            v60_txdf_start_grace=1.5,
        )
        data = {'time': 15000}
        prearm, lead, slot, phase, reason = v60_runtime._v60_txdf_prearm_before_reply(seq, data, wall=30.2)
        self.assertTrue(prearm)
        self.assertEqual(slot, 0)
        self.assertAlmostEqual(phase, 0.2, places=6)
        self.assertIn('early wanted slot', reason)

    def test_runtime_txdf_path_has_no_halt_on_unarmed_owned_tx(self):
        source = Path(v60_runtime.__file__).read_text(encoding='utf-8')
        self.assertNotIn('V6 TXDF SAFETY:', source)
        self.assertNotIn('before verified VS1 arm; halting TX', source)
        self.assertIn('V10 TXDF UNARMED TX', source)
        self.assertIn('NO-HALT policy', source)
        self.assertIn('V10 TXDF PREARM', source)


if __name__ == '__main__':
    unittest.main()
