import inspect
import unittest

import v60_runtime


class DummySequencer:
    def __init__(self, *args, **kwargs): pass
    def best_available_candidate(self): return None
    def start_candidate(self, *args, **kwargs): return True
    def clear_current(self, *args, **kwargs): return None
    def mark_engaged(self, *args, **kwargs): return None
    def log_call(self, *args, **kwargs): return None
    def process_decode(self, *args, **kwargs): return None
    def remember_proactive_target(self, *args, **kwargs): return None
    def process_status(self, *args, **kwargs): return None
    def evaluate_after_decode(self, *args, **kwargs): return None
    def maybe_band_hop(self, *args, **kwargs): return False
    def rearm_current_attempt(self, *args, **kwargs): return False
    def check_timeouts(self, *args, **kwargs): return None


v60_runtime.install_v60_runtime(DummySequencer, type('QSOState', (), {'IDLE': 'IDLE'}))


class FakeTxDF:
    enabled = True


class FakeRadio:
    def __init__(self, tx_states):
        self.tx_states = list(tx_states)
        self.disarms = []

    def get_tx_state(self):
        if len(self.tx_states) > 1:
            return self.tx_states.pop(0)
        return self.tx_states[0]

    def disarm_tx_df(self, expected_ant):
        self.disarms.append(expected_ant)


class TestTXDFHandoffV7(unittest.TestCase):
    def test_marker_present(self):
        self.assertEqual(v60_runtime.V60_TXDF_HANDOFF_HOTFIX, '2026-09-02-v7')

    def test_old_sub_alias_survives_new_prepared_sub(self):
        freq, aliased = v60_runtime._v60_txdf_canonical_status_frequency(
            14_074_920,
            saved_fa=14_074_000,
            prepared_sub=14_073_080,
            active=False,
            alias_until=0.0,
            recent_aliases={14_074_920: (14_074_000, 105.0)},
            now=102.0,
            tolerance=10,
        )
        self.assertEqual(freq, 14_074_000)
        self.assertTrue(aliased)

    def test_expired_old_sub_alias_does_not_hide_manual_qsy(self):
        freq, aliased = v60_runtime._v60_txdf_canonical_status_frequency(
            14_074_920,
            saved_fa=14_074_000,
            prepared_sub=14_073_080,
            active=False,
            alias_until=0.0,
            recent_aliases={14_074_920: (14_074_000, 101.0)},
            now=102.0,
            tolerance=10,
        )
        self.assertEqual(freq, 14_074_920)
        self.assertFalse(aliased)

    def test_third_proactive_tx_is_not_armed_after_two_unanswered(self):
        self.assertFalse(v60_runtime._v60_txdf_arm_allowed('ATTEMPT', True, 2, 2, 2))
        self.assertTrue(v60_runtime._v60_txdf_arm_allowed('ATTEMPT', True, 2, 1, 2))
        self.assertTrue(v60_runtime._v60_txdf_arm_allowed('ENGAGED', True, 4, 4, 2))
        self.assertTrue(v60_runtime._v60_txdf_arm_allowed('ATTEMPT', False, 4, 4, 2))

    def seq_for_disarm(self, tx_states):
        seq = DummySequencer.__new__(DummySequencer)
        seq.v60_txdf = FakeTxDF()
        seq.v60_radio = FakeRadio(tx_states)
        seq.v60_txdf_active = True
        seq.v60_saved_radio_state = type('Saved', (), {'fa': 14_074_000, 'hf_ant': 1})()
        seq.v60_txdf_prepared_sub = 14_074_920
        seq.v60_txdf_recent_aliases = {}
        seq.v60_txdf_status_main_fa = 14_074_000
        seq.v60_txdf_armed_at = 100.0
        seq.v60_txdf_tx_seen = True
        seq.v60_txdf_cat_wait_log_at = 0.0
        seq.v60_txdf_status_alias_until = 0.0
        seq.transmitting = False
        return seq

    def test_disarm_waits_for_physical_cat_tx0(self):
        seq = self.seq_for_disarm([1, 0])
        self.assertFalse(seq.v60_disarm_txdf('WSJT-X TX ended'))
        self.assertTrue(seq.v60_txdf_active)
        self.assertEqual(seq.v60_radio.disarms, [])
        self.assertTrue(seq.v60_disarm_txdf('retry after CAT TX0'))
        self.assertFalse(seq.v60_txdf_active)
        self.assertEqual(seq.v60_radio.disarms, [1])

    def test_source_prevents_armed_target_handoff(self):
        source = inspect.getsource(v60_runtime.install_v60_runtime)
        self.assertIn('previous VS1 transaction still active', source)
        self.assertNotIn("v60_disarm_txdf(self, 'prepare another TX DF')", source)

    def test_actual_tx_seen_prevents_missed_tx_slot_path(self):
        source = inspect.getsource(v60_runtime.install_v60_runtime)
        self.assertIn('self.v60_txdf_tx_seen = bool(self.v60_txdf_active)', source)
        self.assertIn("if self.v60_txdf_tx_seen:", source)
        self.assertIn("'CAT TX ended after actual TX'", source)
        self.assertIn("return 'await-cat-rx'", source)

    def test_restore_checks_cat_tx_before_exact_restore(self):
        source = inspect.getsource(v60_runtime.install_v60_runtime)
        start = source.index('def v60_restore_radio')
        end = source.index('def v60_prepare_radio_df')
        block = source[start:end]
        self.assertIn("get_tx_state", block)
        self.assertIn('CAT reports TX1', block)


if __name__ == '__main__':
    unittest.main()
