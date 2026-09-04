import inspect
import unittest
from dataclasses import replace

import v60_runtime
from v60_radio import FTX1SplitManager, RadioState, V60_TXDF_VS_RADIO


class FakeRadio(FTX1SplitManager):
    def __init__(self, *, main_ant=1, sub_ant=1):
        self.verify = True
        self.main_ant = int(main_ant)
        self.sub_ant = int(sub_ant)
        self.commands = []
        self.state = RadioState(
            fa=21_074_000, fb=14_074_000, fr=1, ft=0, st=0, vs=0, tx=0,
            hf_ant=self.main_ant,
        )

    def snapshot(self):
        return replace(self.state)

    def set_sub_frequency(self, hz):
        self.commands.append(('FB', int(hz)))
        self.state.fb = int(hz)
        return int(hz)

    def set_vfo_select(self, sub):
        sub = bool(sub)
        self.commands.append(('VS', 1 if sub else 0))
        self.state.vs = 1 if sub else 0
        self.state.ft = 1 if sub else 0
        self.state.hf_ant = self.sub_ant if sub else self.main_ant
        return self.state.vs

    def get_frequency(self):
        return self.state.fa

    def set_frequency(self, hz):
        self.commands.append(('FA', int(hz)))
        self.state.fa = int(hz)
        return int(hz)


class RuntimeRadio:
    def __init__(self):
        self.arms = []
        self.disarms = []

    def arm_tx_df(self, sub_hz, expected_hf_ant):
        self.arms.append((int(sub_hz), int(expected_hf_ant)))

    def disarm_tx_df(self, expected_hf_ant):
        self.disarms.append(int(expected_hf_ant))


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


class TestTXDFViaVS(unittest.TestCase):
    def test_marker_present(self):
        self.assertEqual(V60_TXDF_VS_RADIO, '2026-09-02-v5')
        self.assertEqual(v60_runtime.V60_TXDF_VS_HOTFIX, '2026-09-02-v5')

    def test_1700_rf_df_prepares_plus_200_hz_sub(self):
        radio = FakeRadio()
        base = radio.snapshot()
        sub = radio.prepare_tx_df(base.fa, 1700, 1500, base_state=base)
        self.assertEqual(sub, 21_074_200)
        self.assertEqual(radio.state.fb, 21_074_200)

    def test_prepare_only_changes_fb_and_keeps_main_rx(self):
        radio = FakeRadio()
        base = radio.snapshot()
        radio.prepare_tx_df(base.fa, 1700, 1500, base_state=base)
        self.assertEqual(radio.commands, [('FB', 21_074_200)])
        self.assertEqual((radio.state.ft, radio.state.st, radio.state.vs), (0, 0, 0))
        self.assertEqual(radio.state.hf_ant, base.hf_ant)

    def test_arm_uses_vs1_and_ft_follows_automatically(self):
        radio = FakeRadio(main_ant=1, sub_ant=1)
        base = radio.snapshot()
        sub = radio.prepare_tx_df(base.fa, 1700, 1500, base_state=base)
        armed = radio.arm_tx_df(sub, base.hf_ant)
        self.assertEqual((armed.ft, armed.st, armed.vs), (1, 0, 1))
        self.assertEqual(armed.hf_ant, 1)
        self.assertIn(('VS', 1), radio.commands)
        self.assertNotIn(('FT', 1), radio.commands)
        self.assertNotIn(('ST', 1), radio.commands)

    def test_arm_fails_closed_if_sub_antenna_differs(self):
        radio = FakeRadio(main_ant=1, sub_ant=0)
        base = radio.snapshot()
        sub = radio.prepare_tx_df(base.fa, 1700, 1500, base_state=base)
        with self.assertRaises(RuntimeError):
            radio.arm_tx_df(sub, base.hf_ant)
        self.assertEqual((radio.state.ft, radio.state.st, radio.state.vs), (0, 0, 0))
        self.assertEqual(radio.state.hf_ant, 1)
        self.assertEqual(radio.commands[-2:], [('VS', 1), ('VS', 0)])

    def test_disarm_returns_to_main_without_restoring_fb(self):
        radio = FakeRadio()
        base = radio.snapshot()
        sub = radio.prepare_tx_df(base.fa, 1700, 1500, base_state=base)
        radio.arm_tx_df(sub, base.hf_ant)
        radio.disarm_tx_df(base.hf_ant)
        self.assertEqual((radio.state.ft, radio.state.st, radio.state.vs), (0, 0, 0))
        self.assertEqual(radio.state.fb, sub)

    def test_exact_restore_after_txdf_cycle(self):
        radio = FakeRadio()
        base = radio.snapshot()
        sub = radio.prepare_tx_df(base.fa, 1700, 1500, base_state=base)
        radio.arm_tx_df(sub, base.hf_ant)
        radio.disarm_tx_df(base.hf_ant)
        final = radio.restore(base)
        self.assertEqual(final, base)

    def test_baseline_rejects_existing_split_or_sub_selection(self):
        radio = FakeRadio()
        for field in ('ft', 'st', 'vs'):
            state = radio.snapshot()
            setattr(state, field, 1)
            with self.assertRaises(RuntimeError, msg=field):
                radio.validate_txdf_baseline(state, state.fa)

    def test_baseline_rejects_unvalidated_dual_receive_mode(self):
        radio = FakeRadio()
        state = radio.snapshot()
        state.fr = 0
        with self.assertRaises(RuntimeError):
            radio.validate_txdf_baseline(state, state.fa)

    def test_slot_timing_arms_at_tail_of_opposite_slot(self):
        # Slot 100 is EVEN; TX slot 1 starts at 1515.0.
        current, phase, lead = v60_runtime._v60_txdf_slot_timing(1513.2, 1)
        self.assertEqual(current, 0)
        self.assertAlmostEqual(phase, 13.2, places=6)
        self.assertAlmostEqual(lead, 1.8, places=6)

    def test_slot_timing_never_rearms_mid_wanted_slot(self):
        current, phase, lead = v60_runtime._v60_txdf_slot_timing(1515.5, 1)
        self.assertEqual(current, 1)
        self.assertAlmostEqual(phase, 0.5, places=6)
        self.assertAlmostEqual(lead, 29.5, places=6)

    def test_runtime_disarms_on_tx_falling_edge_and_services_guard(self):
        source = inspect.getsource(v60_runtime.install_v60_runtime)
        self.assertIn("v60_disarm_txdf(self, 'WSJT-X TX ended')", source)
        self.assertIn('v60_service_txdf_switch(self, now)', source)
        self.assertIn('lead <= self.v60_txdf_pre_tx_guard', source)
        self.assertIn("return 'wait-next-slot'", source)
        self.assertIn("return 'tx-disabled'", source)

    def test_runtime_no_halt_if_owned_tx_starts_unarmed(self):
        source = inspect.getsource(v60_runtime.install_v60_runtime)
        self.assertNotIn('V6 TXDF SAFETY: WSJT-X started TX', source)
        self.assertNotIn('before verified VS1 arm; halting TX', source)
        self.assertIn('V10 TXDF UNARMED TX', source)
        self.assertIn('NO-HALT policy', source)

    def runtime_seq(self):
        seq = DummySequencer.__new__(DummySequencer)
        seq.v60_txdf = type('TxDF', (), {'enabled': True})()
        seq.v60_radio = RuntimeRadio()
        seq.v60_txdf_active = False
        seq.v60_saved_radio_state = type('Saved', (), {'hf_ant': 1})()
        seq.v60_txdf_prepared_sub = 21_074_200
        seq.v60_txdf_prepared_call = 'TEST1'
        seq.v60_txdf_prepared_df = 1700
        seq.v60_txdf_tx_slot = 1
        seq.v60_txdf_armed_at = 0.0
        seq.v60_txdf_pre_tx_guard = 1.8
        seq.v60_txdf_start_grace = 1.5
        seq.v55_manual = type('Manual', (), {'active': False})()
        seq.transmitting = False
        seq.tx_enabled = True
        seq.current = {'call': 'TEST1'}
        return seq

    def test_runtime_arms_only_inside_pre_tx_guard(self):
        seq = self.runtime_seq()
        self.assertEqual(seq.v60_service_txdf_switch(now=10.0, wall=1512.0), 'waiting')
        self.assertFalse(seq.v60_txdf_active)
        self.assertEqual(seq.v60_service_txdf_switch(now=11.0, wall=1513.2), 'armed')
        self.assertTrue(seq.v60_txdf_active)
        self.assertEqual(seq.v60_radio.arms, [(21_074_200, 1)])

    def test_runtime_disarms_when_wanted_slot_passes_without_tx(self):
        seq = self.runtime_seq()
        self.assertEqual(seq.v60_service_txdf_switch(now=11.0, wall=1513.2), 'armed')
        self.assertEqual(seq.v60_service_txdf_switch(now=12.0, wall=1515.5), 'armed-awaiting-tx')
        self.assertEqual(seq.v60_service_txdf_switch(now=13.0, wall=1516.6), 'missed-tx-slot')
        self.assertFalse(seq.v60_txdf_active)
        self.assertEqual(seq.v60_radio.disarms, [1])


if __name__ == '__main__':
    unittest.main()