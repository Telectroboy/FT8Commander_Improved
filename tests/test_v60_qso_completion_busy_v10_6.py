import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import v60_runtime
import v60_txdf


class PlannerConfig:
    tx_df_enabled = True
    tx_df_grid_step_hz = 20
    tx_df_max_radius_hz = 500
    tx_df_min_hz = -100
    tx_df_max_hz = 3500
    tx_df_signal_width_hz = 50
    tx_df_hard_guard_hz = 60
    tx_df_hard_guard_ttl_s = 120
    tx_df_same_slot_ttl_s = 120
    tx_df_opposite_slot_proxy = True
    tx_df_opposite_slot_ttl_s = 120
    tx_df_opposite_guard_hz = 60
    tx_df_clearance_goal_hz = 120


class FakePolicyRecord:
    def __init__(self):
        self.anti_pingpong_until = 9999999999.0
        self.cooldown_until = 0.0
        self.tx_times = [1, 2, 3, 4]


class FakePolicy:
    max_profile_tx = 4
    max_global_tx = 24

    def __init__(self):
        self.record = FakePolicyRecord()
        self.global_tx = {'KP2B': [1, 2, 3, 4]}
        self.persisted = 0
        self.eligible_calls = 0

    def _record(self, call, profile, create=False):
        return self.record

    def _persist(self, now):
        self.persisted += 1

    def _prune(self, now):
        return None

    def eligible(self, data, profile, now=None):
        self.eligible_calls += 1
        return False, 'profile-TX-cap', 2300.0


class FakeBandHopper:
    def __init__(self):
        self.state_store = None
        self.abandoned = []

    def note_attempt_abandoned(self, call):
        self.abandoned.append(call)

    def cancel_pending_switch(self):
        return None


class DummySeq:
    def __init__(self, *args, **kwargs):
        pass

    def best_available_candidate(self, *args, **kwargs):
        return None

    def start_candidate(self, data, reason):
        self.started = (dict(data), reason)
        self.current = dict(data)
        self.state = SimpleNamespace(value='ATTEMPT')
        if data.get('call') in self.proactive_queue:
            self.proactive_queue.remove(data.get('call'))
        return True

    def clear_current(self, reason, delete_candidate=False):
        self.clear_reason = reason
        self.current = None
        self.state = SimpleNamespace(value='IDLE')
        self.current_tx_attempts = 0
        self.current_unanswered_cycles = 0
        self.engaged_tx_since_progress = 0
        self.current_working_other = False

    def mark_engaged(self, *args, **kwargs):
        return None

    def log_call(self, *args, **kwargs):
        return None

    def process_decode(self, *args, **kwargs):
        return None

    def remember_proactive_target(self, packet, match, trigger):
        call = str(match.get('call') or '').upper()
        target = self.proactive_targets.get(call)
        if target and target.get('waiting_event') and str(trigger).upper() in {'CQ', 'RRR', 'RR73', '73'}:
            self.queue_proactive_target(call, reason=f'generic fresh {trigger}')
        return True

    def process_status(self, *args, **kwargs):
        return None

    def evaluate_after_decode(self, *args, **kwargs):
        self.original_evaluate_calls = getattr(self, 'original_evaluate_calls', 0) + 1
        return 'ORIGINAL'

    def maybe_band_hop(self, *args, **kwargs):
        return False

    def check_timeouts(self, *args, **kwargs):
        return None

    def next_direct_call(self, band):
        return None

    def stop_transmit(self, *args, **kwargs):
        self.stop_calls = getattr(self, 'stop_calls', 0) + 1

    def queue_proactive_target(self, call, front=False, reason='', allow_current=False):
        if call in self.proactive_queue:
            self.proactive_queue.remove(call)
        if front:
            self.proactive_queue.insert(0, call)
        else:
            self.proactive_queue.append(call)
        return True

    def _remove_proactive_from_queue(self, call):
        while call in self.proactive_queue:
            self.proactive_queue.remove(call)


class QSOState:
    pass


v60_runtime.install_v60_runtime(DummySeq, QSOState)


class TestV106Rules(unittest.TestCase):
    def test_marker(self):
        self.assertEqual(v60_runtime.V60_QSO_COMPLETION_BUSY_HOTFIX, '2026-09-03-v10.6.1')

    def test_lost_qso_rearm_accepts_cq_rrr_rr73_or_73(self):
        for event in ('CQ', 'RRR', 'RR73', '73'):
            self.assertTrue(v60_runtime._v60_terminal_rearm_event(event))
        for event in ('-13', 'R-04', ''):
            self.assertFalse(v60_runtime._v60_terminal_rearm_event(event))

    def test_engaged_target_to_other_waits_until_terminal(self):
        self.assertEqual(
            v60_runtime._v60_engaged_foreign_action(
                'ENGAGED', 'KP2B', 'KP2B', 'G4FZN', '-01', 'F4EGM'
            ),
            'wait-terminal',
        )
        self.assertEqual(
            v60_runtime._v60_engaged_foreign_action(
                'ENGAGED', 'KP2B', 'KP2B', 'G4FZN', 'RR73', 'F4EGM'
            ),
            'terminal-rearm',
        )
        self.assertIsNone(
            v60_runtime._v60_engaged_foreign_action(
                'ENGAGED', 'KP2B', 'KP2B', 'F4EGM', '-13', 'F4EGM'
            )
        )

    def base_seq(self):
        seq = object.__new__(DummySeq)
        seq.mycall = 'F4EGM'
        seq.band = 15
        seq.frequency = 21074000
        seq.current = {'call': 'KP2B', 'proactive': True, 'band': 15,
                       'packet': {'DeltaFrequency': 1535}, 'time': datetime(2026, 9, 3, 12, 13, 15)}
        seq.state = SimpleNamespace(value='ENGAGED')
        seq.transmitting = False
        seq.tx_enabled = True
        seq.current_tx_attempts = 3
        seq.current_unanswered_cycles = 0
        seq.engaged_tx_since_progress = 0
        seq.v55_manual = SimpleNamespace(active=False)
        seq.v55_target_policy = FakePolicy()
        seq.v60_pskr = None
        seq.v60_txdf = SimpleNamespace(enabled=False)
        seq.v60_engaged_foreign_event = None
        seq.v60_fresh_rf_priority = None
        seq.proactive_targets = {
            'KP2B': {'call': 'KP2B', 'band': 15, 'proactive': True,
                     'waiting_event': False, 'rearm_after_burst': False,
                     'last_seen': 1.0, 'first_seen': 1.0}
        }
        seq.proactive_queue = []
        seq.v60_pursuit = {}
        seq.v60_pursuit_max_windows = 6
        seq.v60_pursuit_max_age = 1800.0
        seq.v60_pursuit_busy_hold = 90.0
        seq.v60_pursuit_lost_timeout = 90.0
        seq.v60_qsy_intent = None
        seq.v55_neutral_clear = False
        seq.band_hopper = FakeBandHopper()
        seq.last_ip_from = ('127.0.0.1', 2333)
        seq.v60_deferred_anti_ping = {}
        seq.decision_settle_time = 0.12
        seq.stop_calls = 0
        seq.original_evaluate_calls = 0
        return seq

    def test_profile_cap_does_not_abort_engaged_qso(self):
        seq = self.base_seq()
        result = seq.evaluate_after_decode()
        self.assertEqual(result, 'ORIGINAL')
        self.assertEqual(seq.original_evaluate_calls, 1)
        self.assertEqual(seq.stop_calls, 0)
        self.assertEqual(seq.v55_target_policy.eligible_calls, 0)
        self.assertEqual(seq.current['call'], 'KP2B')

    def test_engaged_busy_releases_target_into_terminal_only_wait(self):
        seq = self.base_seq()
        seq.v60_engaged_foreign_event = {
            'call': 'KP2B', 'peer': 'G4FZN', 'event': '-01', 'action': 'wait-terminal'
        }
        result = seq.evaluate_after_decode()
        self.assertIsNone(result)
        self.assertIsNone(seq.current)
        self.assertEqual(seq.stop_calls, 1)
        self.assertEqual(seq.band_hopper.abandoned, ['KP2B'])
        rec = seq.v60_pursuit['KP2B']
        self.assertTrue(rec['terminal_only_rearm'])
        self.assertTrue(rec['waiting'])
        self.assertTrue(seq.proactive_targets['KP2B']['waiting_event'])
        self.assertNotIn('KP2B', seq.proactive_queue)

    def test_lost_engaged_each_free_event_rearms_even_at_cap(self):
        packet = SimpleNamespace(SNR=-10)
        for event in ('CQ', 'RRR', 'RR73', '73'):
            with self.subTest(event=event):
                seq = self.base_seq()
                seq.current = None
                seq.state = SimpleNamespace(value='IDLE')
                seq.tx_enabled = False
                seq.v60_pursuit['KP2B'] = {
                    'started': 1.0, 'windows': 99, 'last_seen': 1.0, 'waiting': True,
                    'attempted_df': [], 'actual_tx': 8, 'exhausted': False,
                    'busy_hold_started': 1.0, 'busy_hold_until': 9999999999.0,
                    'busy_band': 15, 'busy_hold_log_at': 0.0, 'terminal_only_rearm': True,
                }
                seq.proactive_targets['KP2B']['waiting_event'] = True
                if event == 'CQ':
                    match = {'call': 'KP2B', 'to': 'CQ', 'payload': [], 'grid': 'FK77'}
                else:
                    match = {'call': 'KP2B', 'to': 'G4FZN', 'payload': [event], 'grid': None}
                seq.remember_proactive_target(packet, match, event)
                self.assertEqual(seq.proactive_queue[0], 'KP2B')
                self.assertFalse(seq.v60_pursuit['KP2B']['terminal_only_rearm'])
                self.assertTrue(seq.v60_fresh_rf_priority['lost_engaged_recovery'])
                self.assertEqual(seq.v60_fresh_rf_priority['event'], event)

    def test_source_has_start_only_caps_and_fresh_replan(self):
        source = Path(v60_runtime.__file__).read_text(encoding='utf-8')
        self.assertIn("state_name != 'ENGAGED'", source)
        self.assertIn('V10.6 ENGAGED TXDF REPLAN', source)
        self.assertNotIn('trying previous actual DF=%d', source)
        self.assertIn('wait for target CQ/RRR/RR73/73', source)


class TestV106PlannerInheritance(unittest.TestCase):
    def engine(self):
        return v60_txdf.TxDFEngine(PlannerConfig())

    def test_guard_remains_60_hz(self):
        self.assertEqual(self.engine().hard_guard, 60)

    def test_signal_start_overlap_model(self):
        self.assertEqual(v60_txdf._signal_edge_gap(306, 320, 50), 0)
        self.assertEqual(v60_txdf._signal_edge_gap(1000, 1110, 50), 60)

    def test_same_slot_ttl_remains_120_seconds(self):
        self.assertEqual(self.engine().same_slot_ttl, 120)
        self.assertEqual(self.engine().opposite_slot_ttl, 120)

    def test_no_safe_hole_falls_back_exactly_to_caller(self):
        engine = self.engine()
        now = 1000.0
        for df in range(500, 1501, 80):
            engine.local.add(df, -5, 'OTHER', time_ms=0, ts=now)
        chosen = engine.choose('TARGET', 1000, 0, now=now)
        self.assertEqual(chosen, 1000)
        self.assertEqual(engine.last_choice_debug['mode'], 'caller-frequency-fallback')


if __name__ == '__main__':
    unittest.main()
