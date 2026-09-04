import inspect
import unittest
from collections import defaultdict, deque

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


class FakeRecord:
    def __init__(self):
        self.cooldown_until = 0.0
        self.anti_pingpong_until = 0.0
        self.tx_times = deque()


class FakePolicy:
    def __init__(self):
        self.record = FakeRecord()
        self.max_profile_tx = 4
        self.max_global_tx = 6
        self.global_tx = defaultdict(deque)
        self.persisted = 0

    def _prune(self, now):
        pass

    def _record(self, call, profile, create=False):
        return self.record

    def _persist(self, now):
        self.persisted += 1


class TestRFPolicyTimingV8(unittest.TestCase):
    def test_marker_present_and_v7_retained(self):
        self.assertEqual(v60_runtime.V60_RF_PRIORITY_TXDF_HOTFIX, '2026-09-03-v8')
        self.assertEqual(v60_runtime.V60_TXDF_HANDOFF_HOTFIX, '2026-09-02-v7')

    def test_ft8_role_rule_called_emitter_message_ve3yul(self):
        called, emitter, event = v60_runtime._v60_rf_roles(
            'REPLY', {'to': 'TJ1GD', 'call': 'VE3YUL', 'payload': ['73']}
        )
        self.assertEqual((called, emitter, event), ('TJ1GD', 'VE3YUL', '73'))

    def test_ft8_role_rule_called_emitter_message_ys1gmv(self):
        called, emitter, event = v60_runtime._v60_rf_roles(
            'REPLY', {'to': 'W4TME', 'call': 'YS1GMV', 'payload': ['RR73']}
        )
        self.assertEqual((called, emitter, event), ('W4TME', 'YS1GMV', 'RR73'))

    def test_cq_role_emitter_is_callsign_after_cq(self):
        called, emitter, event = v60_runtime._v60_rf_roles(
            'CQ', {'call': 'TJ1GD', 'grid': 'JJ53'}
        )
        self.assertEqual((called, emitter, event), ('CQ', 'TJ1GD', 'CQ'))

    def test_only_emitter_terminal_or_cq_is_fresh_rearm(self):
        self.assertEqual(
            v60_runtime._v60_fresh_rearm_event(
                'REPLY', {'to': 'TJ1GD', 'call': 'VE3YUL', 'payload': ['73']}
            ),
            ('VE3YUL', '73'),
        )
        self.assertEqual(
            v60_runtime._v60_fresh_rearm_event(
                'REPLY', {'to': 'VE3YUL', 'call': 'TJ1GD', 'payload': ['-12']}
            ),
            ('', None),
        )

    def test_fresh_rf_bypasses_only_antiping(self):
        policy = FakePolicy()
        policy.record.anti_pingpong_until = 200.0
        ok, reason, bypassed = v60_runtime._v60_fresh_rf_policy_gate(
            policy, 'TJ1GD', '20', 100.0
        )
        self.assertTrue(ok)
        self.assertEqual(reason, 'fresh-RF')
        self.assertTrue(bypassed)
        self.assertEqual(policy.record.anti_pingpong_until, 0.0)
        self.assertEqual(policy.persisted, 1)

    def test_fresh_rf_does_not_bypass_backoff(self):
        policy = FakePolicy()
        policy.record.anti_pingpong_until = 200.0
        policy.record.cooldown_until = 300.0
        ok, reason, bypassed = v60_runtime._v60_fresh_rf_policy_gate(
            policy, 'TJ1GD', '20', 100.0
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'backoff')
        self.assertFalse(bypassed)
        self.assertEqual(policy.record.anti_pingpong_until, 200.0)

    def test_fresh_rf_does_not_bypass_tx_caps(self):
        policy = FakePolicy()
        policy.record.anti_pingpong_until = 200.0
        policy.record.tx_times.extend([1.0, 2.0, 3.0, 4.0])
        ok, reason, bypassed = v60_runtime._v60_fresh_rf_policy_gate(
            policy, 'TJ1GD', '20', 100.0
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'profile-TX-cap')
        self.assertFalse(bypassed)

    def test_busy_hold_blocks_rotation_until_expiry(self):
        self.assertTrue(v60_runtime._v60_busy_target_blocked({'busy_hold_until': 120.0}, 100.0))
        self.assertFalse(v60_runtime._v60_busy_target_blocked({'busy_hold_until': 99.0}, 100.0))

    def test_direct_preempts_idle_and_attempt_but_never_engaged(self):
        self.assertTrue(v60_runtime._v60_direct_preempt_allowed('IDLE', False))
        self.assertTrue(v60_runtime._v60_direct_preempt_allowed('ATTEMPT', False))
        self.assertFalse(v60_runtime._v60_direct_preempt_allowed('ENGAGED', False))
        self.assertFalse(v60_runtime._v60_direct_preempt_allowed('ATTEMPT', True))

    def test_fresh_proactive_priority_is_idle_only(self):
        self.assertTrue(v60_runtime._v60_fresh_select_allowed('IDLE', False, False))
        self.assertFalse(v60_runtime._v60_fresh_select_allowed('ATTEMPT', False, True))
        self.assertFalse(v60_runtime._v60_fresh_select_allowed('ENGAGED', False, False))
        self.assertFalse(v60_runtime._v60_fresh_select_allowed('IDLE', False, True))

    def test_status_before_direct_decode_gets_short_grace(self):
        self.assertEqual(
            v60_runtime._v60_foreign_owner_grace('IDLE', False, True, False, 'OH0KCE', False),
            'grace',
        )
        self.assertEqual(
            v60_runtime._v60_foreign_owner_grace('IDLE', False, True, False, 'OH0KCE', True),
            'direct',
        )
        self.assertEqual(
            v60_runtime._v60_foreign_owner_grace('IDLE', False, True, True, 'OH0KCE', False),
            'manual',
        )

    def test_source_direct_priority_precedes_v7_scheduler_and_post_qso_bypasses(self):
        source = inspect.getsource(v60_runtime.install_v60_runtime)
        start = source.index('def v8_evaluate')
        end = source.index('def v8_check_timeouts')
        block = source[start:end]
        self.assertIn('bypassing MANUAL_POST_QSO_HOLD; band hold preserved', block)
        self.assertIn('fresh direct caller absolute priority', block)
        self.assertIn('_v60_direct_preempt_allowed', block)
        self.assertIn('return v55_evaluate(self)', block)

    def test_source_busy_rotation_and_fresh_emitter_rearm_are_installed(self):
        source = inspect.getsource(v60_runtime.install_v60_runtime)
        self.assertIn('V8 BUSY TARGET SKIP', source)
        self.assertIn('fresh RF {event} emitted by {emitter}', source)
        self.assertIn("Sequencer.remember_proactive_target = v8_remember_proactive_target", source)
        self.assertIn("Sequencer.best_available_candidate = v8_best", source)


if __name__ == '__main__':
    unittest.main()
