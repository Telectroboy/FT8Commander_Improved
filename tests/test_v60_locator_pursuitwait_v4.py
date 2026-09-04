import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import geo
from decode_normalizer import normalize_message_segments
from dbutils import create_db
from ft8ctrl import QSOState, Sequencer, V60_LOCATOR_TIMEOUT_HOTFIX
from v1076_terminal_revisit import _terminal_from_packet
from v60_runtime import V60_PURSUIT_WAIT_HOLD_HOTFIX


class TestV60LocatorPursuitWaitV4(unittest.TestCase):
    def bare_seq(self, db_path=None):
        seq = Sequencer.__new__(Sequencer)
        if db_path is not None:
            seq.db_name = Path(db_path)
        seq.origin = geo.grid2latlon('JN29')
        seq.band = 15
        seq.proactive_targets = {}
        seq.v60_pursuit = {}
        seq.v60_pursuit_max_windows = 6
        seq.v60_pursuit_max_age = 1800.0
        seq.v60_pursuit_lost_timeout = 90.0
        seq.v60_pursuit_busy_hold = 90.0
        seq.v60_qsy_intent = None
        seq.dxe_lookup = lambda call: SimpleNamespace(
            country='Vietnam', continent='AS', cqzone=26, ituzone=49, adif=293,
        )
        return seq

    def test_reply_rr73_is_terminal_not_locator(self):
        seq = Sequencer.__new__(Sequencer)
        kind, match = seq.parse_segment('F4HIK XV9T RR73')
        self.assertEqual(kind, 'REPLY')
        self.assertEqual(match['call'], 'XV9T')
        self.assertEqual(match['payload'], ['RR73'])
        self.assertIsNone(match['grid'])

    def test_mshv_terminal_segment_keeps_received_call_order(self):
        seq = Sequencer.__new__(Sequencer)
        segments = normalize_message_segments(
            'F4EGM RR73; JA1MLV <CN8NS> -08'
        )
        first_kind, first = seq.parse_segment(segments[0])
        second_kind, second = seq.parse_segment(segments[1])

        self.assertEqual(first_kind, 'REPLY')
        self.assertEqual(first['to'], 'F4EGM')
        self.assertEqual(first['call'], 'CN8NS')
        self.assertEqual(first['payload'], ['RR73'])
        self.assertEqual(second_kind, 'REPLY')
        self.assertEqual(second['to'], 'JA1MLV')
        self.assertEqual(second['call'], 'CN8NS')
        self.assertEqual(second['payload'], ['-08'])

    def test_mshv_terminal_reaches_v1076_terminal_watch(self):
        seq = Sequencer.__new__(Sequencer)
        seq.mycall = 'F4EGM'
        seq.current = {'call': 'CN8NS'}
        seq.band = 20
        seq.frequency = 14074000
        packet = SimpleNamespace(
            Message='F4EGM RR73; JA1MLV <CN8NS> -08',
            Time=123456,
            SNR=-8,
        )

        token, data = _terminal_from_packet(seq, packet, 'CN8NS')

        self.assertEqual(token, 'RR73')
        self.assertEqual(data['call'], 'CN8NS')
        self.assertEqual(data['source'], 'terminal-73-retry')

    def test_pending_direct_does_not_expire_behind_engaged_qso(self):
        seq = Sequencer.__new__(Sequencer)
        seq.band = 20
        seq.state = QSOState.ENGAGED
        seq.current = {'call': 'VK1AAA', 'source': 'cq'}
        seq.direct_call_timeout = 90.0
        seq.pending_direct_calls = {
            'CN8NS': {
                'call': 'CN8NS', 'band': 20, 'continent': 'AF',
                'source': 'direct',
                'queued_at': 10.0, 'last_seen': 10.0,
            },
        }

        with patch('ft8ctrl.time.monotonic', return_value=1000.0):
            direct = seq.next_direct_call(20)

        self.assertEqual(direct['call'], 'CN8NS')
        self.assertIn('CN8NS', seq.pending_direct_calls)

    def test_pending_direct_gets_fresh_window_after_qso_release(self):
        seq = Sequencer.__new__(Sequencer)
        seq.band = 20
        seq.state = QSOState.IDLE
        seq.current = None
        seq.direct_call_timeout = 90.0
        seq.pending_direct_calls = {
            'CN8NS': {
                'call': 'CN8NS', 'band': 20, 'continent': 'AF',
                'source': 'direct',
                'queued_at': 10.0, 'last_seen': 10.0,
            },
        }

        with patch('ft8ctrl.LOG', Mock()), \
             patch('ft8ctrl.time.monotonic', return_value=1000.0):
            seq.release_pending_direct_calls()
        with patch('ft8ctrl.time.monotonic', return_value=1089.0):
            self.assertEqual(seq.next_direct_call(20)['call'], 'CN8NS')
        with patch('ft8ctrl.time.monotonic', return_value=1091.0):
            self.assertIsNone(seq.next_direct_call(20))

        self.assertNotIn('CN8NS', seq.pending_direct_calls)

    def test_terminal_direct_from_another_station_is_queued(self):
        seq = Sequencer.__new__(Sequencer)
        seq.current = {'call': 'VK1AAA'}
        seq.mark_engaged = Mock()
        seq.queue_direct_call = Mock()
        packet = SimpleNamespace(Message='F4EGM CN8NS RR73')
        match = {
            'to': 'F4EGM', 'call': 'CN8NS', 'payload': ['RR73'], 'grid': None,
        }

        seq.handle_direct_call(packet, match)

        seq.queue_direct_call.assert_called_once_with(packet, match)
        seq.mark_engaged.assert_not_called()

    def test_cq_rr73_can_still_be_a_locator(self):
        seq = Sequencer.__new__(Sequencer)
        kind, match = seq.parse_segment('CQ TEST1 RR73')
        self.assertEqual(kind, 'CQ')
        self.assertEqual(match['grid'], 'RR73')

    def test_known_locator_survives_non_grid_reply_after_db_row_is_gone(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'calls.db'
            create_db(db)
            seq = self.bare_seq(db)
            seq.proactive_targets['XV9T'] = {
                'call': 'XV9T', 'band': 15, 'grid': 'OK33',
                'distance': geo.distance(seq.origin, geo.grid2latlon('OK33')),
            }
            data = seq.lookup_candidate('XV9T', None, 15)
            self.assertEqual(data['grid'], 'OK33')
            self.assertEqual(int(data['distance']), 9772)

    def test_unknown_locator_stays_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'calls.db'
            create_db(db)
            seq = self.bare_seq(db)
            data = seq.lookup_candidate('SPECIAL1', None, 15)
            self.assertIsNone(data['grid'])
            self.assertIsNone(data['distance'])

    def test_rr73_regression_matches_observed_false_distance(self):
        origin = geo.grid2latlon('JN29')
        self.assertEqual(int(geo.distance(origin, geo.grid2latlon('RR73'))), 5326)
        self.assertEqual(int(geo.distance(origin, geo.grid2latlon('OK33'))), 9772)

    def test_busy_hold_is_active_on_same_band(self):
        seq = self.bare_seq()
        seq.proactive_targets['XV9T'] = {'call': 'XV9T', 'band': 15}
        seq.v60_pursuit['XV9T'] = {
            'started': 10.0, 'windows': 5, 'last_seen': 100.0,
            'waiting': True, 'exhausted': False,
            'busy_hold_started': 100.0, 'busy_hold_until': 190.0,
            'busy_band': 15, 'busy_hold_log_at': 0.0,
        }
        hold = seq.v60_busy_wait_band_hold(now=130.0)
        self.assertIsNotNone(hold)
        call, remaining, _rec = hold
        self.assertEqual(call, 'XV9T')
        self.assertEqual(remaining, 60.0)

    def test_busy_hold_expires_without_extension(self):
        seq = self.bare_seq()
        seq.proactive_targets['XV9T'] = {'call': 'XV9T', 'band': 15}
        rec = {
            'started': 10.0, 'windows': 5, 'last_seen': 185.0,
            'waiting': True, 'exhausted': False,
            'busy_hold_started': 100.0, 'busy_hold_until': 190.0,
            'busy_band': 15, 'busy_hold_log_at': 0.0,
        }
        seq.v60_pursuit['XV9T'] = rec
        # Even if last_seen moved forward because XV9T kept working others,
        # the fixed hold deadline remains 190 and is cleared at expiry.
        self.assertIsNone(seq.v60_busy_wait_band_hold(now=191.0))
        self.assertEqual(rec['busy_hold_until'], 0.0)

    def test_busy_hold_cannot_pin_after_max_windows(self):
        seq = self.bare_seq()
        seq.proactive_targets['XV9T'] = {'call': 'XV9T', 'band': 15}
        rec = {
            'started': 10.0, 'windows': 6, 'last_seen': 100.0,
            'waiting': True, 'exhausted': False,
            'busy_hold_started': 100.0, 'busy_hold_until': 190.0,
            'busy_band': 15, 'busy_hold_log_at': 0.0,
        }
        seq.v60_pursuit['XV9T'] = rec
        self.assertIsNone(seq.v60_busy_wait_band_hold(now=130.0))
        self.assertEqual(rec['busy_hold_until'], 0.0)

    def test_busy_hold_does_not_recreate_selection_starvation(self):
        source = inspect.getsource(Sequencer.maybe_band_hop)
        marker = "busy_hold = v60_busy_wait_band_hold(self)"
        self.assertIn(marker, source)
        block = source[source.index(marker):source.index("intent_state = v60_service_qsy_intent", source.index(marker))]
        self.assertIn('return False', block)
        self.assertNotIn('return True', block)
        self.assertIn('same-band selection continues', block)

    def test_pending_qsy_is_guarded_by_busy_hold(self):
        source = inspect.getsource(Sequencer.check_timeouts)
        # check_timeouts services the guarded transaction; the service itself
        # must contain the busy-hold cancellation guard.
        self.assertIn('v60_service_qsy_intent(self, now)', source)
        import v60_runtime
        runtime_source = inspect.getsource(v60_runtime.install_v60_runtime)
        service_start = runtime_source.index('def v60_service_qsy_intent')
        service_end = runtime_source.index('def v60_note_manual_tx_lock', service_start)
        service = runtime_source[service_start:service_end]
        self.assertIn('busy_hold = v60_busy_wait_band_hold(self, now)', service)
        self.assertIn("return 'cancelled'", service)

    def test_fresh_start_consumes_old_busy_hold(self):
        source = inspect.getsource(Sequencer.start_candidate)
        self.assertIn("rec['busy_hold_until'] = 0.0", source)
        self.assertIn("rec['busy_band'] = None", source)

    def test_listening_log_uses_real_v6_pursuit_timeout(self):
        source = inspect.getsource(Sequencer.finish_proactive_burst)
        self.assertIn('v60_pursuit_lost_timeout', source)
        self.assertIn('pursuit timeout %.0fs since last heard', source)
        self.assertNotIn('expires after %.0fs without hearing it', source)

    def test_v4_markers_present(self):
        self.assertEqual(V60_LOCATOR_TIMEOUT_HOTFIX, '2026-09-02-v4')
        self.assertEqual(V60_PURSUIT_WAIT_HOLD_HOTFIX, '2026-09-02-v4')


if __name__ == '__main__':
    unittest.main()
