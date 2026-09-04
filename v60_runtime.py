#!/usr/bin/env python3
"""Runtime integration layer for FT8Commander v6.0.

Only one small marker call is inserted in ft8ctrl.py.  The wrappers below keep
all new target policy, manual-override and command handling isolated from the
legacy sequencer, making rollback straightforward.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from v55_core import ManualOverrideController, TargetPolicy
from v60_dxcc import DXCCBandMemory
from v60_txdf import TxDFEngine
from v60_radio import FTX1SplitManager
from pskr_intel import PSKReporterIntel
from dxcluster_intel import DXClusterIntel

RUNTIME_MARKER = 'FT8Commander v6.0 runtime policy installed'
BAND_PURSUIT_HOTFIX = '2026-09-02-v1'
V60_OPPORTUNITY_QSY_HOTFIX = '2026-09-02-v2'
V60_WANTED_SELECTION_HOTFIX = '2026-09-02-v3'
V60_PURSUIT_WAIT_HOLD_HOTFIX = '2026-09-02-v4'
V60_TXDF_VS_HOTFIX = '2026-09-02-v5'
V60_TXDF_STATUS_ALIAS_HOTFIX = '2026-09-02-v6'
V60_TXDF_HANDOFF_HOTFIX = '2026-09-02-v7'
V60_RF_PRIORITY_TXDF_HOTFIX = '2026-09-03-v8'
V60_TXDF_HOLE_PLANNER_HOTFIX = '2026-09-03-v9'
V60_TXDF_SIGNAL_START_HOTFIX = '2026-09-03-v10'
V60_TXDF_FIELD_RACES_HOTFIX = '2026-09-03-v10.4'
V60_TXDF_RECOVERY_PLANNER_HOTFIX = '2026-09-03-v10.5'
V60_QSO_COMPLETION_BUSY_HOTFIX = '2026-09-03-v10.6.1'


def _v60_deferred_antiping_action(target, current_band, freshness, policy_result, now):
  """Return hold/rearm/drop for one anti-ping deferred proactive target."""
  if not target or str(target.get('band')) != str(current_band):
    return 'drop'
  last_seen = float(target.get('last_seen') or target.get('first_seen') or 0.0)
  if not last_seen or float(now) - last_seen > float(freshness):
    return 'drop'
  eligible, reason, _remaining = policy_result
  if eligible:
    return 'rearm'
  if reason == 'anti-ping-pong':
    return 'hold'
  return 'drop'


def _v60_pursuit_should_expire(call, active_call, rec, now, timeout):
  """Return True only for a non-active pursuit whose last decode is stale."""
  if str(call or '').upper() == str(active_call or '').upper() and active_call:
    return False
  last_seen = float((rec or {}).get('last_seen') or 0.0)
  return bool(last_seen and float(now) - last_seen > float(timeout))


def _v60_qsy_guard_delay(wall_now, after_boundary=1.0):
  """Seconds until just after the next 15 s FT8 boundary."""
  wall_now = float(wall_now)
  phase = wall_now % 15.0
  to_boundary = 15.0 - phase
  if to_boundary < 0.050:
    to_boundary = 15.0
  return to_boundary + max(0.0, float(after_boundary))


def _v60_qsy_status_gate(last_status_at, intent_created_at, now, max_age):
  """Accept a recent idle Status observed after QSY intent creation.

  WSJT-X does not necessarily emit another Status immediately after every 15 s
  boundary while idle. Requiring a *post-boundary* packet can therefore delay a
  safe QSY by almost a complete FT8 period. The intent scheduler already waits
  until just after the boundary and separately checks IDLE/TXEnabled/TX state;
  this gate only requires that Status ownership was refreshed after the intent
  was created and is still recent at execution time.
  """
  last = float(last_status_at or 0.0)
  created = float(intent_created_at or 0.0)
  now = float(now)
  max_age = max(0.1, float(max_age))
  if not last or last < created:
    return False, 'no-status-since-intent', float('inf')
  age = max(0.0, now - last)
  if age > max_age:
    return False, 'stale-status', age
  return True, 'fresh-status', age


def _v60_clear_local_spectrum(engine):
  """Clear the local parity map and return the number of removed observations.

  Local DF is relative to the current dial frequency, so observations from one
  amateur band are invalid immediately after a band change.
  """
  local = getattr(engine, 'local', None)
  slots = getattr(local, 'slots', None)
  if not isinstance(slots, dict):
    return 0
  removed = 0
  for queue in slots.values():
    try:
      removed += len(queue)
      queue.clear()
    except Exception:
      continue
  return removed


def _v60_txdf_slot_timing(wall_now, tx_slot):
  """Return (current_slot, phase_seconds, seconds_to_next_wanted_slot)."""
  wall_now = float(wall_now)
  tx_slot = int(tx_slot) & 1
  slot_index = int(wall_now // 15.0)
  phase = wall_now - slot_index * 15.0
  current_slot = slot_index & 1
  if current_slot == tx_slot:
    next_start = (slot_index + 2) * 15.0
  else:
    next_start = (slot_index + 1) * 15.0
  return current_slot, phase, max(0.0, next_start - wall_now)


def _v60_txdf_canonical_status_frequency(raw_frequency, *, saved_fa=None,
                                           prepared_sub=None, active=False,
                                           owned_recovery=False,
                                           alias_until=0.0, recent_aliases=None,
                                           now=None, tolerance=10):
  """Map only known TXDF SUB Status frequencies back to their MAIN dial."""
  raw = int(raw_frequency or 0)
  now = time.monotonic() if now is None else float(now)
  tolerance = max(0, int(tolerance))

  # A target handoff can replace prepared_sub before one trailing WSJT-X Status
  # packet from the previous VS1 transaction arrives. Keep a short exact-value
  # history so that stale transport detail is not mistaken for a human QSY.
  for alias_sub, payload in dict(recent_aliases or {}).items():
    try:
      alias_main, expires = payload
      alias_sub = int(alias_sub)
      alias_main = int(alias_main)
      expires = float(expires)
    except (TypeError, ValueError):
      continue
    if now <= expires and abs(raw - alias_sub) <= tolerance:
      return alias_main, raw != alias_main

  if saved_fa is None or prepared_sub is None:
    return raw, False
  main = int(saved_fa)
  sub = int(prepared_sub)
  alias_open = bool(active) or bool(owned_recovery) or now <= float(alias_until or 0.0)
  if alias_open and abs(raw - sub) <= tolerance:
    return main, raw != main
  if abs(raw - main) <= tolerance:
    return main, False
  return raw, False


def _v60_txdf_arm_allowed(state_name, proactive, tx_attempts, unanswered_cycles, window_tx):
  """Do not arm a third TX when a bounded proactive window is already done."""
  if str(state_name or '').upper() != 'ATTEMPT' or not proactive:
    return True
  limit = max(1, int(window_tx))
  return not (int(tx_attempts or 0) >= limit and int(unanswered_cycles or 0) >= limit)


def _v60_txdf_selection_window(sequencer, data, wall=None):
  """Compatibility timing probe; V10 no longer defers or halts a wanted TX.

  It returns whether the normal asynchronous guard has plenty of time, but V10
  will synchronously arm VS1 *before* handing WSReply to WSJT-X when the reply
  is close to, or just after, the wanted boundary.
  """
  engine = getattr(sequencer, 'v60_txdf', None)
  if not engine or not getattr(engine, 'enabled', False) or not data:
    return True, 999.0, None, 0.0
  try:
    target_slot = engine.local.slot_parity(data.get('time'))
    tx_slot = target_slot ^ 1
    current_slot, phase, lead = _v60_txdf_slot_timing(
      time.time() if wall is None else float(wall), tx_slot)
  except Exception:
    return True, 999.0, None, 0.0
  required = float(getattr(sequencer, 'v60_txdf_pre_tx_guard', 1.8)) + float(
    getattr(sequencer, 'v60_txdf_selection_margin', 0.8))
  # During the first seconds of our wanted slot WSJT-X may still start
  # immediately when it receives WSReply. Treat that as an imminent boundary
  # so we pre-arm synchronously before sending the Reply.
  early_slot = max(2.0, float(getattr(sequencer, 'v60_txdf_start_grace', 1.5)) + 0.8)
  enough_async_time = bool(
    (current_slot != tx_slot and lead >= required) or
    (current_slot == tx_slot and phase > early_slot)
  )
  return enough_async_time, lead, tx_slot, required


def _v60_txdf_prearm_before_reply(sequencer, data, wall=None):
  """Return (prearm, lead, tx_slot, phase, reason) for pre-WSReply arming."""
  engine = getattr(sequencer, 'v60_txdf', None)
  if not engine or not getattr(engine, 'enabled', False) or not data:
    return False, 999.0, None, 0.0, 'disabled'
  target_slot = engine.local.slot_parity(data.get('time'))
  tx_slot = target_slot ^ 1
  wall_now = time.time() if wall is None else float(wall)
  slot_index = int(wall_now // 15.0)
  phase = wall_now - slot_index * 15.0
  current_slot, _phase2, lead = _v60_txdf_slot_timing(wall_now, tx_slot)
  required = float(getattr(sequencer, 'v60_txdf_pre_tx_guard', 1.8)) + float(
    getattr(sequencer, 'v60_txdf_selection_margin', 0.8))
  early_slot = max(2.0, float(getattr(sequencer, 'v60_txdf_start_grace', 1.5)) + 0.8)
  if current_slot != tx_slot and lead <= required:
    return True, lead, tx_slot, phase, 'approaching wanted boundary'
  if current_slot == tx_slot and phase <= early_slot:
    return True, 0.0, tx_slot, phase, 'already in early wanted slot'
  return False, lead, tx_slot, phase, 'normal asynchronous guard'



def _v60_rf_roles(kind, match):
  """Return (called, emitter, event) using immutable FT8 role order.

  Normal FT8 text is CALLED EMITTER MESSAGE. Therefore in a parsed REPLY,
  match['to'] is the called station and match['call'] is ALWAYS the emitter.
  For CQ, the called party is conceptually everyone and match['call'] is the
  emitter. Never infer roles from who we are pursuing.
  """
  kind = str(kind or '').upper()
  match = match or {}
  emitter = str(match.get('call') or '').upper()
  if not emitter:
    return '', '', None
  if kind == 'CQ':
    return 'CQ', emitter, 'CQ'
  if kind != 'REPLY':
    return '', emitter, None
  called = str(match.get('to') or '').upper()
  payload = list(match.get('payload') or [])
  event = str(payload[-1]).upper() if payload else None
  return called, emitter, event


def _v60_fresh_rearm_event(kind, match):
  """Return emitter/event only for explicit fresh RF re-arm transmissions."""
  _called, emitter, event = _v60_rf_roles(kind, match)
  if emitter and event in {'CQ', 'RRR', 'RR73', '73'}:
    return emitter, event
  return '', None


def _v60_busy_target_blocked(rec, now):
  """True while a fixed pursuit-busy hold still applies to this station."""
  if not rec:
    return False
  try:
    return float(rec.get('busy_hold_until') or 0.0) > float(now)
  except (TypeError, ValueError):
    return False


# Legacy 'terminal' name retained for compatibility; V10.6.1 also accepts CQ as an explicit free-station event.
V60_TERMINAL_REARM_EVENTS = frozenset({'CQ', 'RRR', 'RR73', '73'})


def _v60_terminal_rearm_event(event):
  """True for fresh target-emitted availability events accepted after a lost ENGAGED QSO."""
  return str(event or '').upper() in V60_TERMINAL_REARM_EVENTS


def _v60_engaged_foreign_action(state_name, current_call, emitter, called, event, mycall):
  """Classify a pursued ENGAGED target transmitting to somebody else.

  A non-terminal transmission means our QSO ownership has been lost: stop
  calling and wait. CQ/RRR/RR73/73 means the target is now available or finishing that other QSO
  and may be re-armed for our next eligible TX opportunity.
  """
  if str(state_name or '').upper() != 'ENGAGED':
    return None
  current_call = str(current_call or '').upper()
  emitter = str(emitter or '').upper()
  called = str(called or '').upper()
  mycall = str(mycall or '').upper()
  if not current_call or emitter != current_call or not called or called == mycall:
    return None
  return 'terminal-rearm' if _v60_terminal_rearm_event(event) else 'wait-terminal'


def _v60_fresh_rf_policy_gate(policy, call, profile, now):
  """Allow a fresh RF event to bypass anti-ping only, never real safety caps."""
  policy._prune(float(now))
  record = policy._record(call, profile, create=False)
  if record is None:
    return True, 'new-target', False
  if record.cooldown_until > now:
    return False, 'backoff', False
  if len(record.tx_times) >= policy.max_profile_tx:
    return False, 'profile-TX-cap', False
  if len(policy.global_tx.get(str(call).upper(), ())) >= policy.max_global_tx:
    return False, 'global-TX-cap', False
  bypassed = record.anti_pingpong_until > now
  if bypassed:
    record.anti_pingpong_until = 0.0
    policy._persist(now)
  return True, 'fresh-RF', bypassed


def _v60_direct_preempt_allowed(state_name, transmitting):
  """Direct calls pre-empt IDLE/ATTEMPT, never a real ENGAGED QSO or active TX."""
  return str(state_name or '').upper() != 'ENGAGED' and not bool(transmitting)


def _v60_fresh_select_allowed(state_name, transmitting, tx_enabled):
  """Fresh proactive RF events get next-slot priority only from safe IDLE."""
  return (str(state_name or '').upper() == 'IDLE'
          and not bool(transmitting) and not bool(tx_enabled))


def _v60_foreign_owner_grace(state_name, has_current, tx_enabled, transmitting,
                              dx_call, matching_direct=False):
  """Classify an IDLE Auto-Tx Status that may precede its direct Decode packet."""
  if str(state_name or '').upper() != 'IDLE' or has_current or not dx_call:
    return 'none'
  if transmitting:
    return 'direct' if matching_direct else 'manual'
  if tx_enabled:
    return 'direct' if matching_direct else 'grace'
  return 'none'


def _v60_opportunity_retry_allowed(trigger, current_snr, baseline_snr,
                                     since_last_tx, min_gap=30.0,
                                     snr_gain=3.0, strong_snr=-12.0):
  """Return (allowed, reason, gain) for a CQ-only anti-ping opportunity retry."""
  if str(trigger or '').upper() != 'CQ':
    return False, 'not-CQ', None
  try:
    current = float(current_snr)
    gap = float(since_last_tx)
  except (TypeError, ValueError):
    return False, 'missing-SNR-or-gap', None
  if gap < float(min_gap):
    return False, 'minimum-gap', None
  gain = None
  if baseline_snr is not None:
    try:
      gain = current - float(baseline_snr)
    except (TypeError, ValueError):
      gain = None
  if current >= float(strong_snr):
    return True, 'strong-CQ', gain
  if gain is not None and gain >= float(snr_gain):
    return True, 'improving-CQ', gain
  return False, 'no-opportunity', gain


def _profile_key(sequencer) -> str:
  hopper = getattr(sequencer, 'band_hopper', None)
  if hopper and hasattr(hopper, 'current_profile_key'):
    return hopper.current_profile_key()
  return str(getattr(sequencer, 'band', 0) or 'unknown')


def _reset_visit_after_manual(sequencer, now: float) -> None:
  hopper = getattr(sequencer, 'band_hopper', None)
  if not hopper or not getattr(hopper, 'enabled', False):
    return
  profile = hopper.profile_for_frequency(getattr(sequencer, 'frequency', 0))
  if profile is None:
    return
  hopper.on_frequency_status(
    getattr(sequencer, 'frequency', 0), getattr(sequencer, 'band', 0),
    getattr(sequencer, 'config_name', None), now=now, now_utc=datetime.utcnow(),
  )
  state = hopper._state(profile)  # stable internal API of the bundled scheduler
  state.entered_at = now
  state.silent_cycles = 0
  state.last_finalized_slot = None
  state.samples.clear()
  state.decode_counts.clear()
  state.ignored_slots.clear()
  state.slot_calls.clear()
  state.visit_valid_rx_slots = 0
  state.visit_slot_unique_total = 0.0
  state.recent_slot_rates.clear()
  state.settle_slots_remaining = hopper.qsy_settle_slots
  hopper.qso_completed_at = None


def install_v60_runtime(Sequencer, QSOState, log=None):
  """Install wrappers once on the supplied Sequencer class."""
  if getattr(Sequencer, '_v60_runtime_installed', False):
    return
  Sequencer._v60_runtime_installed = True
  LOG = log or logging.getLogger('ft8ctrl')

  original_init = Sequencer.__init__
  original_best = Sequencer.best_available_candidate
  original_start = Sequencer.start_candidate
  original_clear = Sequencer.clear_current
  original_mark_engaged = Sequencer.mark_engaged
  original_log_call = Sequencer.log_call
  original_process_decode = Sequencer.process_decode
  original_remember_proactive = Sequencer.remember_proactive_target
  original_process_status = Sequencer.process_status
  original_evaluate = Sequencer.evaluate_after_decode
  original_maybe_hop = Sequencer.maybe_band_hop
  original_rearm = getattr(Sequencer, 'rearm_current_attempt', None)
  original_check_timeouts = Sequencer.check_timeouts

  def runtime_meta(self, now=None, force=False):
    now = time.monotonic() if now is None else float(now)
    store = getattr(getattr(self, 'band_hopper', None), 'state_store', None)
    if not store:
      return
    target = getattr(self, 'current', None) or {}
    store.replace_section('runtime', {
      'manual': self.v55_manual.status(now),
      'frequency': int(getattr(self, 'frequency', 0) or 0),
      'band': str(getattr(self, 'band', '') or ''),
      'profile': _profile_key(self),
      'qso_state': getattr(getattr(self, 'state', None), 'value', str(getattr(self, 'state', ''))),
      'target': target.get('call'),
      'tx_enabled': bool(getattr(self, 'tx_enabled', False)),
      'transmitting': bool(getattr(self, 'transmitting', False)),
      'watchdog': bool(getattr(self, 'tx_watchdog', False)),
      'owner_call': str(getattr(self, 'v60_owner_call', '') or ''),
      'tx_df_enabled': bool(getattr(getattr(self, 'v60_txdf', None), 'enabled', False)),
      'tx_df_current': getattr(getattr(self, 'v60_txdf', None), 'current_df', None),
      'tx_df_locked': getattr(getattr(self, 'v60_txdf', None), 'locked_df', None),
      'tx_df_active': bool(getattr(self, 'v60_txdf_active', False)),
      'tx_df_tx_slot': getattr(self, 'v60_txdf_tx_slot', None),
      'dxcc_memory_updated': getattr(getattr(self, 'v60_dxcc', None), 'updated_at', None),
      'pskr_connected': bool(getattr(getattr(self, 'v60_pskr', None), 'connected', False)),
      'dxcluster_connected': bool(getattr(getattr(self, 'v60_dxcluster', None), 'connected', False)),
      'updated_monotonic': now,
    })
    store.save(now=now, force=force)

  def v55_init(self, config, queue, call_select):
    original_init(self, config, queue, call_select)
    store = self.band_hopper.state_store
    self.v55_target_policy = TargetPolicy(
      config, store, band_median_provider=self.band_hopper.band_median_snr
    )
    self.v55_manual = ManualOverrideController(config)
    self.v55_command_path = Path(getattr(
      config, 'v55_command_path', '/run/ft8commander/v55-command.json'
    ))
    self.v55_seen_status = False
    self.v55_last_frequency = 0
    self.v55_last_mode = None
    self.v55_last_dx_call = None
    self.v55_neutral_clear = False

    # V6 central state. External/network sources are optional and never gate
    # the local sequencer.
    self.v60_dxcc = DXCCBandMemory(getattr(
      config, 'dxcc_memory_path', '/run/ft8commander/dxcc-memory.json'))
    self.v60_exclude_worked = str(getattr(config, 'dxcc_exclude_worked', 'true')).lower() in {'1','true','yes','on'}
    self.v60_exclude_confirmed = str(getattr(config, 'dxcc_exclude_confirmed', 'true')).lower() in {'1','true','yes','on'}
    self.v60_txdf = TxDFEngine(config)
    self.v60_radio = FTX1SplitManager(config) if self.v60_txdf.enabled else None
    self.v60_saved_radio_state = None
    self.v60_txdf_prepared_call = None
    self.v60_txdf_prepared_df = None
    self.v60_txdf_prepared_sub = None
    self.v60_txdf_active = False
    self.v60_txdf_tx_slot = None
    self.v60_txdf_armed_at = 0.0
    self.v60_txdf_status_alias_until = 0.0
    self.v60_txdf_recent_aliases = {}
    self.v60_txdf_status_main_fa = 0
    self.v60_txdf_tx_seen = False
    self.v60_txdf_cat_wait_log_at = 0.0
    self.v60_txdf_prearm_hold_until = 0.0
    self.v60_txdf_recovery_needed = False
    self.v60_txdf_recovery_log_at = 0.0
    self.v60_last_raw_frequency = 0
    self.v60_txdf_pre_tx_guard = max(0.8, min(3.0, float(
      getattr(config, 'tx_df_pre_tx_guard_s', 1.8))))
    self.v60_txdf_start_grace = max(0.5, min(3.0, float(
      getattr(config, 'tx_df_tx_start_grace_s', 1.5))))
    self.v60_txdf_selection_margin = max(0.4, min(2.0, float(
      getattr(config, 'tx_df_selection_margin_s', 0.8))))
    self.v60_txdf_slot_defer_call = ''
    self.v60_txdf_slot_defer_until = 0.0
    self.v60_txdf_slot_defer_direct = False
    self.v60_pursuit_window_tx = max(1, int(getattr(config, 'dx_pursuit_window_tx', 2)))
    self.v60_pursuit_max_windows = max(1, int(getattr(config, 'dx_pursuit_max_windows', 6)))
    self.v60_pursuit_max_age = max(300.0, float(getattr(config, 'dx_pursuit_max_age', 1800)))
    self.v60_pursuit_lost_timeout = max(30.0, float(getattr(config, 'dx_pursuit_lost_timeout', 90)))
    self.v60_pursuit_busy_hold = max(15.0, min(
      float(getattr(config, 'dx_pursuit_busy_hold', 90.0)),
      self.v60_pursuit_lost_timeout,
    ))
    self.v60_pursuit = {}
    self.v60_deferred_anti_ping = {}
    self.v60_qsy_intent = None
    self.v60_last_status_at = 0.0
    self.v60_qsy_guard_after_boundary = max(
      0.5, float(getattr(config, 'v60_qsy_guard_after_boundary', 1.0)))
    self.v60_qsy_status_max_age = max(
      1.0, float(getattr(config, 'v60_qsy_status_max_age', 3.0)))
    self.v60_opportunity_min_gap = max(
      15.0, float(getattr(config, 'dx_pursuit_opportunity_min_gap', 30.0)))
    self.v60_opportunity_snr_gain = max(
      0.0, float(getattr(config, 'dx_pursuit_opportunity_snr_gain', 3.0)))
    self.v60_opportunity_strong_snr = float(
      getattr(config, 'dx_pursuit_opportunity_strong_snr', -12.0))
    self.v60_manual_tx_lock_started = 0.0
    self.v60_manual_tx_lock_call = ''
    self.v60_owner_call = ''
    self.v60_owner_until = 0.0
    self.v60_post_qso_owner_call = ''
    self.v60_post_qso_owner_until = 0.0
    self.v60_last_actual_tx_call = ''
    self.v60_last_actual_tx_at = 0.0
    self.v60_fresh_rf_priority = None
    self.v60_engaged_foreign_event = None
    self.v60_foreign_owner_pending_call = ''
    self.v60_foreign_owner_pending_until = 0.0
    self.v60_foreign_owner_grace_s = max(1.0, min(5.0, float(
      getattr(config, 'v60_direct_status_grace_s', 2.5))))
    self.v60_pskr = PSKReporterIntel(config, self.mycall)
    self.v60_pskr_seen = set()
    self.v60_pskr.start()
    self.v60_dxcluster = DXClusterIntel(config, self.mycall)
    self.v60_dxcluster.start()
    # V6: watchdog/Auto-Tx recovery is automatic. Keep it bounded, but do not
    # convert the second automatic disable into manual ownership.
    if hasattr(self, 'attempt_rearm_max'):
      self.attempt_rearm_max = max(1, int(getattr(config, 'v60_attempt_rearm_max', self.attempt_rearm_max)))
    runtime_meta(self, force=True)
    LOG.info(
      '%s: target backoff=%s TX caps=%d/profile %d/global manual idle=%.0fs',
      RUNTIME_MARKER,
      '/'.join(str(int(item)) for item in self.v55_target_policy.backoff_schedule),
      self.v55_target_policy.max_profile_tx,
      self.v55_target_policy.max_global_tx,
      self.v55_manual.idle_timeout,
    )

  def v60_band_name(self, data=None):
    value = (data or {}).get('band', getattr(self, 'band', ''))
    text = str(value or '').lower()
    return text if text.endswith('m') else (text + 'm' if text.isdigit() else text)

  def v60_dxcc_gate(self, data):
    if not data or self.v55_target_policy.is_direct(data):
      return True, 'direct-or-empty'
    # DXCC-by-band memory is a proactive filter. It does not block a station
    # calling us directly and does not change WSJT-X display filters.
    if not (data.get('proactive') or str(data.get('selector') or '').lower() == 'country'):
      return True, 'not-country'
    dxcc = data.get('dxcc')
    if dxcc in (None, '') and data.get('call'):
      try:
        info = self.lookup_candidate(data.get('call'), data.get('grid'), data.get('band', self.band))
        dxcc = info.get('dxcc')
        data.setdefault('dxcc', dxcc)
      except Exception:
        dxcc = None
    return self.v60_dxcc.eligible(
      dxcc, v60_band_name(self, data),
      exclude_worked=self.v60_exclude_worked,
      exclude_confirmed=self.v60_exclude_confirmed,
    )

  def v60_owner_lease(self, call, seconds=20.0):
    self.v60_owner_call = str(call or '').upper()
    self.v60_owner_until = time.monotonic() + max(2.0, float(seconds))

  def v60_owner_matches(self, call, now=None):
    now = time.monotonic() if now is None else float(now)
    call = str(call or '').upper()
    return bool(call and call == self.v60_owner_call and now <= self.v60_owner_until)

  def v60_pursuit_record(self, call):
    call = str(call or '').upper()
    rec = self.v60_pursuit.get(call)
    if rec is None:
      rec = {'started': time.monotonic(), 'windows': 0, 'last_seen': 0.0,
             'waiting': False, 'attempted_df': [], 'actual_tx': 0,
             'exhausted': False, 'busy_hold_started': 0.0,
             'busy_hold_until': 0.0, 'busy_band': None,
             'busy_hold_log_at': 0.0, 'terminal_only_rearm': False}
      self.v60_pursuit[call] = rec
    return rec

  def v60_busy_wait_band_hold(self, now=None):
    """Return (call, remaining, rec) while any valid busy pursuit holds this band."""
    now = time.monotonic() if now is None else float(now)
    active = []
    for call, rec in list(getattr(self, 'v60_pursuit', {}).items()):
      until = float(rec.get('busy_hold_until') or 0.0)
      if not until:
        continue

      # Expired or no longer meaningful holds are cleared, not extended.
      target = getattr(self, 'proactive_targets', {}).get(call)
      started = float(rec.get('started') or now)
      invalid = bool(
        until <= now
        or str(rec.get('busy_band')) != str(self.band)
        or not target
        or str(target.get('band')) != str(self.band)
        or rec.get('exhausted')
        or int(rec.get('windows', 0) or 0) >= int(self.v60_pursuit_max_windows)
        or now - started > float(self.v60_pursuit_max_age)
      )
      if invalid:
        rec['busy_hold_started'] = 0.0
        rec['busy_hold_until'] = 0.0
        rec['busy_band'] = None
        rec['busy_hold_log_at'] = 0.0
        continue

      active.append((until - now, call, rec))

    if not active:
      return None
    # If round-robin created more than one valid wait, retain the band until
    # the latest bounded deadline. Selection of same-band targets still runs.
    remaining, call, rec = max(active, key=lambda item: item[0])
    return call, max(0.0, remaining), rec

  def v60_begin_busy_wait_band_hold(self, call, now=None):
    """Start one non-extending QSY hold for a target just heard busy."""
    now = time.monotonic() if now is None else float(now)
    call = str(call or '').upper()
    if not call:
      return 0.0
    rec = v60_pursuit_record(self, call)
    if (rec.get('exhausted')
        or int(rec.get('windows', 0) or 0) >= int(self.v60_pursuit_max_windows)):
      return 0.0

    until = float(rec.get('busy_hold_until') or 0.0)
    if until <= now:
      hold = min(float(self.v60_pursuit_busy_hold), float(self.v60_pursuit_lost_timeout))
      rec['busy_hold_started'] = now
      rec['busy_hold_until'] = now + max(0.0, hold)
      rec['busy_band'] = self.band
      rec['busy_hold_log_at'] = 0.0
      until = float(rec['busy_hold_until'])

    # A pre-existing guarded QSY must not survive the transition to WAIT.
    if getattr(self, 'v60_qsy_intent', None):
      v60_cancel_qsy_intent(self, f'pursuit wait for {call}')
    return max(0.0, until - now)

  def v60_cancel_qsy_intent(self, reason):
    # V10.7.6 mandatory revisit cancellation guard
    import v1076_terminal_revisit as _v1076
    if not _v1076.qsy_cancel_allowed(self, reason):
      return False
    intent = getattr(self, 'v60_qsy_intent', None)
    if not intent:
      return False
    self.v60_qsy_intent = None
    self.band_hopper.cancel_pending_switch()
    LOG.info(
      'V6 QSY_PENDING cancelled: %sm -> %sm (%s)',
      intent.get('from_band'), intent.get('target_band'), reason,
    )
    return True

  def v60_pending_priority_target(self, now=None):
    """Return a fresh direct/wanted target that must win over a pending QSY."""
    now = time.monotonic() if now is None else float(now)

    # Direct callers are absolute priority and must cancel a guarded QSY too.
    for data in list(getattr(self, 'pending_direct_calls', {}).values()):
      if str(data.get('band')) != str(self.band):
        continue
      last_seen = float(data.get('last_seen') or data.get('queued_at') or 0.0)
      if last_seen and now - last_seen <= float(self.direct_call_timeout):
        return data

    # A queued proactive target only holds the band while it is genuinely
    # selectable: fresh, same band, within pursuit bounds, and not TX-capped or
    # backoff-blocked. This prevents stale queue entries from pinning a band.
    for call in list(getattr(self, 'proactive_queue', ())):
      target = self.proactive_targets.get(call)
      if not target or str(target.get('band')) != str(self.band):
        continue
      last_seen = float(target.get('last_seen') or target.get('first_seen') or 0.0)
      if not last_seen or now - last_seen > float(self.proactive_retry_freshness):
        continue
      rec = self.v60_pursuit.get(str(call).upper())
      if rec:
        if int(rec.get('windows', 0) or 0) >= int(self.v60_pursuit_max_windows):
          continue
        if now - float(rec.get('started') or now) > float(self.v60_pursuit_max_age):
          continue
      eligible, _reason, _remaining = self.v55_target_policy.eligible(
        target, _profile_key(self), now
      )
      if not eligible:
        continue
      dxcc_ok, _dxcc_reason = v60_dxcc_gate(self, target)
      if dxcc_ok:
        return target
    return None

  def v60_schedule_qsy_intent(self, target_band, frequency_hz, reason, now=None):
    now = time.monotonic() if now is None else float(now)
    delay = _v60_qsy_guard_delay(time.time(), self.v60_qsy_guard_after_boundary)
    boundary_at = now + max(0.0, delay - self.v60_qsy_guard_after_boundary)
    self.v60_qsy_intent = {
      'from_band': self.band,
      'from_frequency': int(getattr(self, 'frequency', 0) or 0),
      'target_band': target_band,
      'frequency': int(frequency_hz),
      'reason': reason,
      'created_at': now,
      'boundary_at': boundary_at,
      'not_before': now + delay,
      'last_wait_log': 0.0,
    }
    # decision() creates a real pending_switch immediately. During the guard
    # phase that would make a not-yet-executed QSY look real, so clear it until
    # the final safety check passes.
    self.band_hopper.cancel_pending_switch()
    LOG.info(
      'V6 QSY_PENDING %sm -> %sm in %.1fs: waiting for post-boundary WSJT-X safety check (%s)',
      self.band, target_band, delay, reason,
    )
    return True

  def v60_service_qsy_intent(self, now=None):
    """Return none/waiting/cancelled/executed for the guarded QSY transaction."""
    intent = getattr(self, 'v60_qsy_intent', None)
    if not intent:
      return 'none'
    now = time.monotonic() if now is None else float(now)

    busy_hold = v60_busy_wait_band_hold(self, now)
    if busy_hold:
      call, remaining, _rec = busy_hold
      v60_cancel_qsy_intent(
        self, f'pursuit wait band hold for {call} ({remaining:.0f}s remaining)'
      )
      self.decision_due_at = now + float(self.decision_settle_time)
      return 'cancelled'

    if str(self.band) != str(intent.get('from_band')):
      v60_cancel_qsy_intent(self, 'band changed before execution')
      return 'cancelled'
    if self.v55_manual.active:
      v60_cancel_qsy_intent(self, 'manual override became active')
      return 'cancelled'
    if self.state != QSOState.IDLE or self.transmitting or self.tx_enabled:
      v60_cancel_qsy_intent(self, 'TX/target ownership appeared before execution')
      return 'cancelled'
    if self.band_hopper.qso_completed_at is None and now < self.band_hopper.attempt_lock_until:
      v60_cancel_qsy_intent(self, 'band-hop attempt lock became active')
      return 'cancelled'

    pending_target = v60_pending_priority_target(self, now)
    if pending_target:
      v60_cancel_qsy_intent(
        self, f'fresh priority target {pending_target.get("call") or "?"} is pending'
      )
      self.decision_due_at = now + float(self.decision_settle_time)
      return 'cancelled'

    if now < float(intent.get('not_before') or 0.0):
      return 'waiting'

    last_status_at = float(getattr(self, 'v60_last_status_at', 0.0) or 0.0)
    status_ok, status_reason, status_age = _v60_qsy_status_gate(
      last_status_at, intent.get('created_at'), now, self.v60_qsy_status_max_age
    )
    if not status_ok:
      last_log = float(intent.get('last_wait_log') or 0.0)
      if now - last_log >= 5.0:
        intent['last_wait_log'] = now
        if status_reason == 'no-status-since-intent':
          LOG.warning(
            'V10.4 QSY_PENDING waits: no WSJT-X Status received since QSY intent was created'
          )
        else:
          LOG.warning(
            'V10.4 QSY_PENDING waits: latest WSJT-X Status is %.1fs old (limit %.1fs)',
            status_age, self.v60_qsy_status_max_age,
          )
      return 'waiting'

    # Final safety check passed. Re-create the scheduler transaction only now,
    # immediately before the real CAT operation.
    target_band = intent['target_band']
    frequency_hz = int(intent['frequency'])
    reason = intent['reason']
    self.band_hopper.pending_switch = {
      'from_band': self.band_hopper.current_band,
      'to_band': target_band,
      'frequency': frequency_hz,
      'reason': reason,
      'requested_at': now,
      'deadline': now + self.band_hopper.switch_timeout,
    }
    self.band_hopper.last_hop_requested_at = now

    v60_restore_radio(self, 'before guarded band QSY')
    if self.v60_saved_radio_state is not None:
      self.band_hopper.cancel_pending_switch()
      intent['not_before'] = now + 1.0
      return 'waiting'

    # Consume the intent before CAT so a failure cannot be replayed blindly.
    self.v60_qsy_intent = None
    result = self.switch_band_frequency(frequency_hz, target_band, reason)
    if result:
      removed = _v60_clear_local_spectrum(self.v60_txdf)
      LOG.info('V10.4 TXDF LOCAL MAP RESET after QSY %sm -> %sm: removed=%d',
               intent.get('from_band'), target_band, removed)
      LOG.info('V6 QSY_TRANSACTION guarded start target=%sm frequency=%d', target_band, frequency_hz)
      return 'executed'
    return 'cancelled'

  def v60_note_manual_tx_lock(self, call, now=None):
    """Create one band-hop hold at the first TX edge of one manual session."""
    now = time.monotonic() if now is None else float(now)
    if self.v60_manual_tx_lock_started:
      return False
    self.v60_manual_tx_lock_started = now
    self.v60_manual_tx_lock_call = str(call or '').upper()
    hold = float(getattr(self.band_hopper, 'attempt_hold', 300.0))
    self.band_hopper.qso_completed_at = None
    self.band_hopper.attempt_lock_until = max(
      float(self.band_hopper.attempt_lock_until), now + hold
    )
    state = self.band_hopper._state()
    if state:
      state.silent_cycles = 0
    LOG.info(
      'V6 MANUAL TX BAND LOCK %s: hold=%.0fs lock_remaining=%.0fs',
      self.v60_manual_tx_lock_call or '-', hold,
      max(0.0, self.band_hopper.attempt_lock_until - now),
    )
    return True

  def v60_register_antiping_defer(self, data, remaining, now=None):
    """Remember a fresh wanted target blocked only by anti-ping-pong."""
    if not data or not data.get('proactive'):
      return False
    now = time.monotonic() if now is None else float(now)
    call = str(data.get('call') or '').upper()
    target = self.proactive_targets.get(call)
    if not call or not target or str(target.get('band')) != str(self.band):
      return False
    last_seen = float(target.get('last_seen') or target.get('first_seen') or 0.0)
    freshness = float(self.proactive_retry_freshness)
    if not last_seen or now - last_seen > freshness:
      return False

    pursuit = self.v60_pursuit.get(call)
    if pursuit:
      if int(pursuit.get('windows', 0) or 0) >= int(self.v60_pursuit_max_windows):
        return False
      if now - float(pursuit.get('started') or now) > float(self.v60_pursuit_max_age):
        return False

    # eligible() has just pruned these queues. Do not create a band hold when
    # anti-ping-pong is merely masking a TX-cap that would still block the call.
    policy = self.v55_target_policy
    record = policy._record(call, _profile_key(self), create=False)
    if record is not None and len(record.tx_times) >= policy.max_profile_tx:
      return False
    if len(policy.global_tx.get(call, ())) >= policy.max_global_tx:
      return False

    fresh_until = last_seen + freshness
    block_until = now + max(0.0, float(remaining))
    previous = self.v60_deferred_anti_ping.get(call)
    self.v60_deferred_anti_ping[call] = {
      'band': self.band,
      'fresh_until': fresh_until,
      'block_until': block_until,
    }
    if not previous or fresh_until > float(previous.get('fresh_until') or 0.0) + 1.0:
      LOG.info(
        'V6 ANTI-PING HOLD %s: fresh %.0fs, block %.0fs; keeping band until first expiry',
        call, max(0.0, fresh_until - now), max(0.0, block_until - now),
      )
    return True

  def v60_service_antiping_defer(self, now=None):
    """Hold the band, or re-arm once anti-ping-pong expires while still fresh."""
    now = time.monotonic() if now is None else float(now)
    hold = False
    for call in list(self.v60_deferred_anti_ping):
      target = self.proactive_targets.get(call)
      pursuit = self.v60_pursuit.get(call)
      if pursuit and (
          int(pursuit.get('windows', 0) or 0) >= int(self.v60_pursuit_max_windows)
          or now - float(pursuit.get('started') or now) > float(self.v60_pursuit_max_age)):
        self.v60_deferred_anti_ping.pop(call, None)
        continue
      policy_result = self.v55_target_policy.eligible(target, _profile_key(self), now)
      action = _v60_deferred_antiping_action(
        target, self.band, self.proactive_retry_freshness, policy_result, now
      )
      if action == 'hold':
        hold = True
        continue
      self.v60_deferred_anti_ping.pop(call, None)
      if action != 'rearm':
        continue
      queued = self.queue_proactive_target(
        call, front=False, reason='anti-ping-pong expired while target still fresh'
      )
      if queued:
        LOG.info('V6 ANTI-PING REARM %s: target still fresh; QSY deferred', call)
        self.decision_due_at = now + float(self.decision_settle_time)
        hold = True
    return hold

  def v60_remember_proactive_target(self, packet, match, trigger):
    result = original_remember_proactive(self, packet, match, trigger)
    if not result:
      return result

    call = str((match or {}).get('call') or '').upper()
    target = self.proactive_targets.get(call)
    # V10.7.4: merely hearing a proactive target must not cancel QSY.
    # v60_service_qsy_intent() already calls v60_pending_priority_target(),
    # which performs freshness + TargetPolicy + DXCC checks before cancelling.

    if str(trigger or '').upper() != 'CQ' or not call or not target:
      return result
    current_call = str(((getattr(self, 'current', None) or {}).get('call')) or '').upper()
    if current_call == call:
      return result

    rec = self.v60_pursuit.get(call)
    if not rec or int(rec.get('windows', 0) or 0) <= 0:
      return result
    if int(rec.get('windows', 0) or 0) >= int(self.v60_pursuit_max_windows):
      return result
    now = time.monotonic()
    if now - float(rec.get('started') or now) > float(self.v60_pursuit_max_age):
      return result
    last_tx = float(rec.get('last_tx_at') or 0.0)
    if not last_tx:
      return result

    allowed, why, gain = _v60_opportunity_retry_allowed(
      trigger,
      getattr(packet, 'SNR', None),
      rec.get('last_window_snr'),
      now - last_tx,
      self.v60_opportunity_min_gap,
      self.v60_opportunity_snr_gain,
      self.v60_opportunity_strong_snr,
    )
    if not allowed:
      return result

    policy = self.v55_target_policy
    policy._prune(now)
    record = policy._record(call, _profile_key(self), create=False)
    if record is None or record.anti_pingpong_until <= now:
      return result
    # Never bypass a real cooldown or either rolling TX cap.
    if record.cooldown_until > now:
      return result
    if len(record.tx_times) >= policy.max_profile_tx:
      return result
    if len(policy.global_tx.get(call, ())) >= policy.max_global_tx:
      return result

    record.anti_pingpong_until = 0.0
    policy._persist(now)
    self.v60_deferred_anti_ping.pop(call, None)
    self.queue_proactive_target(
      call, front=True,
      reason=f'opportunity {why}: fresh CQ after {now-last_tx:.0f}s',
    )
    self.decision_due_at = now + float(self.decision_settle_time)
    gain_text = '?' if gain is None else f'{gain:+.1f}dB'
    LOG.info(
      'V6 OPPORTUNITY RETRY %s: CQ SNR=%s baseline=%s gain=%s lastTX=%.0fs; anti-ping bypassed',
      call, getattr(packet, 'SNR', None), rec.get('last_window_snr'),
      gain_text, now - last_tx,
    )
    return result

  def v60_refresh_external_intel(self, target_data, now=None):
    if not target_data or not getattr(self, 'v60_pskr', None):
      return
    now = time.monotonic() if now is None else float(now)
    call = str(target_data.get('call') or '').upper()
    if not call or not self.frequency:
      return
    band_name = v60_band_name(self, target_data)
    self.v60_pskr.watch_target(call, band_name, target_data.get('grid'))
    if len(self.v60_pskr_seen) > 20000:
      self.v60_pskr_seen.clear()
    # Direct target reports are the strongest external evidence.
    direct_spots = self.v60_pskr.target_remote_map(call, band_name, now)
    for spot in direct_spots:
      spot_key = ('target', spot.receiver, spot.sender, spot.frequency, spot.tx_time)
      if spot_key in self.v60_pskr_seen:
        continue
      self.v60_pskr_seen.add(spot_key)
      df = int(spot.frequency) - int(self.frequency)
      if self.v60_txdf.min_df - 200 <= df <= self.v60_txdf.max_df + 200:
        self.v60_txdf.remote.add(
          call, df, spot.snr, spot.sender, confidence=1.0,
          source='pskr-target', active=True, ts=now,
        )
        if spot.sender == self.mycall:
          self.v60_txdf.remote.add_hears_us(
            call, df, spot.snr, confidence=1.0, source='pskr-target', ts=now,
          )
    # If the target is not a reporter, use one active nearby receiver as a
    # lower-confidence regional proxy. Absence of a PSKR spot is never treated
    # as proof of a free frequency.
    if not direct_spots:
      proxy, spots = self.v60_pskr.best_proxy_remote_map(target_data.get('grid'), band_name, now)
      if proxy:
        confidence = float(proxy.get('confidence', 0.4)) * 0.75
        for spot in spots:
          spot_key = ('proxy', spot.receiver, spot.sender, spot.frequency, spot.tx_time)
          if spot_key in self.v60_pskr_seen:
            continue
          self.v60_pskr_seen.add(spot_key)
          df = int(spot.frequency) - int(self.frequency)
          if self.v60_txdf.min_df - 200 <= df <= self.v60_txdf.max_df + 200:
            self.v60_txdf.remote.add(
              call, df, spot.snr, spot.sender, confidence=confidence,
              source=f'pskr-proxy:{proxy.get("receiver")}', active=True, ts=now,
            )
            if spot.sender == self.mycall:
              self.v60_txdf.remote.add_hears_us(
                call, df, spot.snr, confidence=confidence,
                source=f'pskr-proxy:{proxy.get("receiver")}', ts=now,
              )

  def v60_remember_txdf_alias(self, sub_hz=None, main_hz=None, seconds=5.0):
    sub_hz = self.v60_txdf_prepared_sub if sub_hz is None else sub_hz
    saved = getattr(self, 'v60_saved_radio_state', None)
    if main_hz is None:
      main_hz = getattr(saved, 'fa', None) or getattr(self, 'v60_txdf_status_main_fa', 0)
    if sub_hz is None or not main_hz:
      return
    now = time.monotonic()
    aliases = dict(getattr(self, 'v60_txdf_recent_aliases', {}) or {})
    aliases = {
      int(k): v for k, v in aliases.items()
      if isinstance(v, (tuple, list)) and len(v) == 2 and float(v[1]) >= now
    }
    aliases[int(sub_hz)] = (int(main_hz), now + max(1.0, float(seconds)))
    # Bounded history; only exact known SUB frequencies are ever aliased.
    if len(aliases) > 8:
      aliases = dict(sorted(aliases.items(), key=lambda item: float(item[1][1]))[-8:])
    self.v60_txdf_recent_aliases = aliases
    self.v60_txdf_status_main_fa = int(main_hz)

  def v60_disarm_txdf(self, reason):
    if not self.v60_txdf.enabled or not self.v60_radio or not self.v60_txdf_active:
      return True
    if self.transmitting:
      return False
    expected_ant = getattr(self.v60_saved_radio_state, 'hf_ant', None)
    if expected_ant is None:
      LOG.error('V6 TXDF VS0 cannot verify antenna: saved radio state missing')
      return False
    # WSJT-X clears Status.Transmitting slightly before the FTX-1 CAT TX state
    # falls. Never drive VS0 on that early UDP edge; wait for the radio itself.
    try:
      get_tx_state = getattr(self.v60_radio, 'get_tx_state', None)
      cat_tx = int(get_tx_state()) if callable(get_tx_state) else 0
    except Exception as err:
      LOG.warning('V6 TXDF VS0 deferred: cannot read CAT TX state (%s): %s', reason, err)
      return False
    if cat_tx != 0:
      now = time.monotonic()
      if now - float(getattr(self, 'v60_txdf_cat_wait_log_at', 0.0) or 0.0) >= 1.0:
        self.v60_txdf_cat_wait_log_at = now
        LOG.debug('V6 TXDF WAIT CAT TX0: %s', reason)
      return False
    try:
      v60_remember_txdf_alias(self)
      self.v60_radio.disarm_tx_df(expected_ant)
      self.v60_txdf_active = False
      self.v60_txdf_armed_at = 0.0
      self.v60_txdf_tx_seen = False
      self.v60_txdf_cat_wait_log_at = 0.0
      self.v60_txdf_prearm_hold_until = 0.0
      self.v60_txdf_recovery_needed = False
      self.v60_txdf_status_alias_until = max(
        float(getattr(self, 'v60_txdf_status_alias_until', 0.0) or 0.0),
        time.monotonic() + 3.0,
      )
      LOG.info('V6 TXDF DISARM VS0: %s', reason)
      return True
    except Exception as err:
      # If VS0 itself succeeded but a later coherence check failed, avoid
      # falsely treating MAIN as still armed. Full restore remains pending.
      try:
        state = self.v60_radio.snapshot()
        if state.vs == 0 and state.ft == 0 and state.st == 0 and state.tx == 0:
          v60_remember_txdf_alias(self)
          self.v60_txdf_active = False
          self.v60_txdf_armed_at = 0.0
          self.v60_txdf_tx_seen = False
          self.v60_txdf_cat_wait_log_at = 0.0
          self.v60_txdf_prearm_hold_until = 0.0
          self.v60_txdf_recovery_needed = False
          self.v60_txdf_status_alias_until = max(
            float(getattr(self, 'v60_txdf_status_alias_until', 0.0) or 0.0),
            time.monotonic() + 3.0,
          )
      except Exception:
        pass
      LOG.error('V6 TXDF VS0 disarm failed (%s): %s', reason, err)
      return False

  def v60_recover_txdf_selector(self, reason):
    """Recover a stale physical VS1/FT1 selector only when ownership is provable.

    This is independent from v60_txdf_active: a UDP/CAT race can leave the
    software transaction marked inactive while the FTX-1 later reports VS1/FT1.
    Recovery is allowed only at CAT TX0, ST0, with the exact saved MAIN state
    and prepared FB still matching this FT8Commander transaction.  It never
    sends PTT or ST1 and never changes the antenna selection.
    """
    if not self.v60_txdf.enabled or not self.v60_radio or self.transmitting:
      return False
    if self.v60_txdf_active:
      return True
    saved = getattr(self, 'v60_saved_radio_state', None)
    prepared_sub = getattr(self, 'v60_txdf_prepared_sub', None)
    if saved is None or prepared_sub is None:
      return False
    try:
      state = self.v60_radio.snapshot()
    except Exception as err:
      now = time.monotonic()
      if now - float(getattr(self, 'v60_txdf_recovery_log_at', 0.0) or 0.0) >= 1.0:
        self.v60_txdf_recovery_log_at = now
        LOG.warning('V10.5 TXDF selector recovery deferred: CAT snapshot failed (%s): %s', reason, err)
      return False
    if int(state.tx) != 0:
      return False
    if int(state.st) != 0:
      LOG.error('V10.5 TXDF selector recovery refused: ST=%s is not owned by TXDF (%s)', state.st, reason)
      return False
    if int(state.vs) == 0 and int(state.ft) == 0:
      self.v60_txdf_recovery_needed = False
      self.v60_txdf_recovery_log_at = 0.0
      return True
    tolerance = int(getattr(self.v55_manual, 'frequency_tolerance', 10) or 10)
    owns_selector = bool(
      abs(int(state.fa) - int(saved.fa)) <= tolerance
      and abs(int(state.fb) - int(prepared_sub)) <= tolerance
      and int(state.hf_ant) == int(saved.hf_ant)
    )
    if not owns_selector:
      LOG.error(
        'V10.5 TXDF selector recovery refused: physical state is not exact owned transaction '
        'FA=%s FB=%s FT=%s ST=%s VS=%s TX=%s ANT=%s expected FA=%s FB=%s ANT=%s (%s)',
        state.fa, state.fb, state.ft, state.st, state.vs, state.tx, state.hf_ant,
        saved.fa, prepared_sub, saved.hf_ant, reason,
      )
      return False
    try:
      v60_remember_txdf_alias(self, sub_hz=state.fb, main_hz=saved.fa, seconds=10.0)
      final = self.v60_radio.disarm_tx_df(saved.hf_ant)
      self.v60_txdf_active = False
      self.v60_txdf_armed_at = 0.0
      self.v60_txdf_tx_seen = False
      self.v60_txdf_prearm_hold_until = 0.0
      self.v60_txdf_recovery_needed = False
      self.v60_txdf_recovery_log_at = 0.0
      self.v60_txdf_status_alias_until = max(
        float(getattr(self, 'v60_txdf_status_alias_until', 0.0) or 0.0),
        time.monotonic() + 10.0,
      )
      LOG.warning(
        'V10.5 TXDF SELECTOR RECOVERY VS0: %s; FA=%s FB=%s FT=%s ST=%s VS=%s TX=%s ANT=%s',
        reason, final.fa, final.fb, final.ft, final.st, final.vs, final.tx, final.hf_ant,
      )
      return True
    except Exception as err:
      now = time.monotonic()
      if now - float(getattr(self, 'v60_txdf_recovery_log_at', 0.0) or 0.0) >= 1.0:
        self.v60_txdf_recovery_log_at = now
        LOG.error('V10.5 TXDF selector recovery failed (%s): %s', reason, err)
      return False

  def v60_restore_radio(self, reason):
    if not self.v60_radio or not self.v60_saved_radio_state:
      return
    if self.transmitting or self.tx_enabled:
      LOG.warning('V6 radio restore deferred while TX active/enabled: %s', reason)
      return
    try:
      get_tx_state = getattr(self.v60_radio, 'get_tx_state', None)
      if callable(get_tx_state) and int(get_tx_state()) != 0:
        LOG.debug('V6 radio restore deferred while CAT reports TX1: %s', reason)
        return
    except Exception as err:
      LOG.warning('V6 radio restore deferred: cannot read CAT TX state (%s): %s', reason, err)
      return
    if self.v60_txdf_active and not v60_disarm_txdf(self, f'before full restore: {reason}'):
      return
    try:
      v60_remember_txdf_alias(self)
      self.v60_radio.restore(self.v60_saved_radio_state)
      LOG.info('V6 radio state restored: %s', reason)
    except Exception as err:
      # Keep the saved state so a later idle status can retry the exact
      # restoration. A failed restore must also keep QSY inhibited.
      LOG.error('V6 radio state restore failed (state retained for retry): %s', err)
      return
    self.v60_saved_radio_state = None
    self.v60_txdf_prepared_call = None
    self.v60_txdf_prepared_df = None
    self.v60_txdf_prepared_sub = None
    self.v60_txdf_tx_slot = None
    self.v60_txdf_active = False
    self.v60_txdf_armed_at = 0.0
    self.v60_txdf_tx_seen = False
    self.v60_txdf_prearm_hold_until = 0.0
    self.v60_txdf_recovery_needed = False
    self.v60_txdf_recovery_log_at = 0.0
    self.v60_txdf_status_alias_until = 0.0
    self.v60_txdf.unlock()

  def v60_prepare_radio_df(self, call, wanted):
    """Prepare SUB frequency while keeping VS0/MAIN selected for normal RX."""
    # Never tear down an armed VS1 transaction merely because the scheduler
    # found another target/DF. The switch service owns disarm timing and waits
    # for CAT TX0; selection retries after the previous transaction is safe.
    if self.v60_txdf_active:
      LOG.debug('V6 TXDF PREPARE deferred for %s: previous VS1 transaction still active', call)
      return None
    created_snapshot = False
    try:
      if self.v60_saved_radio_state is None:
        base = self.v60_radio.snapshot()
        self.v60_radio.validate_txdf_baseline(base, self.frequency)
        self.v60_saved_radio_state = base
        created_snapshot = True
      base = self.v60_saved_radio_state
      self.v60_txdf_status_main_fa = int(base.fa)
      v60_remember_txdf_alias(self)
      sub_hz = self.v60_radio.prepare_tx_df(
        self.frequency, int(wanted), self.v60_txdf.audio_df, base_state=base
      )
      return sub_hz
    except Exception:
      if created_snapshot and self.v60_saved_radio_state is not None:
        # prepare_tx_df may already have changed FB; when Auto Tx is not yet
        # active this restores the exact baseline immediately.
        v60_restore_radio(self, 'TXDF prepare rollback')
      raise

  def v60_prepare_specific_df(self, call, wanted):
    if not self.v60_txdf.enabled or not self.v60_radio:
      self.v60_txdf.current_df = int(wanted)
      return True
    if self.transmitting:
      return False
    try:
      sub_hz = v60_prepare_radio_df(self, call, wanted)
      if sub_hz is None:
        return False
      self.v60_txdf.current_df = int(wanted)
      self.v60_txdf_prepared_call = str(call or '').upper()
      self.v60_txdf_prepared_df = int(wanted)
      self.v60_txdf_prepared_sub = sub_hz
      if self.v60_txdf_tx_slot is None and getattr(self, 'current', None):
        target_slot = self.v60_txdf.local.slot_parity(self.current.get('time'))
        self.v60_txdf_tx_slot = target_slot ^ 1
      LOG.info('V6 TXDF SET %s df=%d SUB=%d (VS0 RX)', call, wanted, sub_hz)
      return True
    except Exception as err:
      LOG.error('V6 TXDF preparation failed for %s df=%s: %s', call, wanted, err)
      return False

  def v60_prepare_txdf(self, data):
    if not self.v60_txdf.enabled or not self.v60_radio or not data:
      return True
    if self.transmitting:
      return False
    call = str(data.get('call') or '').upper()
    target_df = int(data.get('packet', {}).get('DeltaFrequency', self.v60_txdf.audio_df) or self.v60_txdf.audio_df)
    # We transmit in the opposite parity to the target decode.
    target_slot = self.v60_txdf.local.slot_parity(data.get('time'))
    tx_slot = target_slot ^ 1
    rec = v60_pursuit_record(self, call)
    wanted = self.v60_txdf.choose(call, target_df, tx_slot, rec.get('attempted_df', []))
    choice = dict(getattr(self.v60_txdf, 'last_choice_debug', {}) or {})
    if choice:
      clearance = choice.get('clearance')
      clearance_text = 'inf' if clearance is None or clearance == float('inf') else f'{float(clearance):.0f}'
      nearest = choice.get('nearest_df')
      nearest_text = '-' if nearest is None else f'{float(nearest):.0f}'
      proxy_clearance = choice.get('proxy_clearance')
      proxy_clearance_text = 'inf' if proxy_clearance is None or proxy_clearance == float('inf') else f'{float(proxy_clearance):.0f}'
      proxy_nearest = choice.get('proxy_nearest_df')
      proxy_nearest_text = '-' if proxy_nearest is None else f'{float(proxy_nearest):.0f}'
      LOG.info(
        'V10 TXDF CHOOSE %s target_start=%d chosen_start=%d range=%s..%s slot=%s '
        'same_observed=%s same_edge_clearance=%sHz same_nearest_start=%s '
        'proxy_observed=%s proxy_edge_clearance=%sHz proxy_nearest_start=%s '
        'signal_width=%sHz mode=%s planner=v10.5',
        call, target_df, wanted, choice.get('low'), choice.get('high'),
        choice.get('slot'), choice.get('observed'), clearance_text, nearest_text,
        choice.get('proxy_observed', 0), proxy_clearance_text, proxy_nearest_text,
        choice.get('signal_width_hz', getattr(self.v60_txdf, 'signal_width_hz', '?')),
        choice.get('mode'),
      )
    try:
      sub_hz = v60_prepare_radio_df(self, call, wanted)
      if sub_hz is None:
        return False
    except Exception as err:
      LOG.error('V6 TXDF PREPARE failed for %s: %s', call, err)
      v60_restore_radio(self, 'TXDF prepare failure')
      return False
    self.v60_txdf.current_df = wanted
    self.v60_txdf_prepared_call = call
    self.v60_txdf_prepared_df = wanted
    self.v60_txdf_prepared_sub = sub_hz
    self.v60_txdf_tx_slot = tx_slot
    LOG.info(
      'V6 TXDF PREPARE %s target_df=%d chosen_df=%d SUB=%d tx_slot=%d (VS0 RX)',
      call, target_df, wanted, sub_hz, tx_slot,
    )
    return True

  def v60_arm_txdf(self, reason, lead=None, prestart=False):
    if (not self.v60_txdf.enabled or not self.v60_radio
        or self.v60_txdf_active or self.v60_saved_radio_state is None
        or self.v60_txdf_prepared_sub is None):
      return False
    if self.transmitting or self.v55_manual.active:
      return False
    current = getattr(self, 'current', None) or {}
    state_name = getattr(getattr(self, 'state', None), 'value', str(getattr(self, 'state', '')))
    if (not prestart) and not _v60_txdf_arm_allowed(
        state_name, bool(current.get('proactive')),
        int(getattr(self, 'current_tx_attempts', 0) or 0),
        int(getattr(self, 'current_unanswered_cycles', 0) or 0),
        getattr(self, 'v60_pursuit_window_tx', 2)):
      LOG.debug(
        'V6 TXDF ARM skipped for %s: proactive window already complete (%d TX/%d RX)',
        current.get('call') or '-', int(getattr(self, 'current_tx_attempts', 0) or 0),
        int(getattr(self, 'current_unanswered_cycles', 0) or 0),
      )
      return False
    expected_ant = self.v60_saved_radio_state.hf_ant
    try:
      self.v60_radio.arm_tx_df(self.v60_txdf_prepared_sub, expected_ant)
    except Exception as err:
      self.v60_txdf_recovery_needed = True
      LOG.error('V6 TXDF ARM VS1 failed: %s', err)
      if not self.transmitting:
        v60_recover_txdf_selector(self, 'arm baseline failure')
      return False
    self.v60_txdf_active = True
    self.v60_txdf_tx_seen = False
    self.v60_txdf_armed_at = time.monotonic()
    self.v60_txdf_prearm_hold_until = (
      self.v60_txdf_armed_at + 3.0 if prestart else 0.0
    )
    self.v60_txdf_status_alias_until = max(
      float(getattr(self, 'v60_txdf_status_alias_until', 0.0) or 0.0),
      self.v60_txdf_armed_at + self.v60_txdf_pre_tx_guard + self.v60_txdf_start_grace + 3.0,
    )
    lead_text = '' if lead is None else f' lead={float(lead):.2f}s'
    LOG.info(
      'V6 TXDF ARM VS1 call=%s df=%s SUB=%s%s: %s',
      self.v60_txdf_prepared_call or '-', self.v60_txdf_prepared_df,
      self.v60_txdf_prepared_sub, lead_text, reason,
    )
    return True

  def v60_service_txdf_switch(self, now=None, wall=None):
    """Arm VS1 only at the tail of RX; return VS0 immediately after/missed TX."""
    if not self.v60_txdf.enabled or not self.v60_radio:
      return 'disabled'
    now = time.monotonic() if now is None else float(now)
    wall = time.time() if wall is None else float(wall)

    if self.v55_manual.active:
      if self.v60_txdf_active and not self.transmitting:
        v60_disarm_txdf(self, 'manual override')
      return 'manual'

    if self.transmitting:
      return 'tx-active' if self.v60_txdf_active else 'tx-unarmed'

    if (not self.v60_txdf_active and bool(getattr(self, 'v60_txdf_recovery_needed', False))):
      if not v60_recover_txdf_selector(self, 'idle service after unarmed/stale selector'):
        return 'selector-recovery-pending'

    current = getattr(self, 'current', None) or {}
    prepared = bool(
      current and self.v60_saved_radio_state is not None
      and self.v60_txdf_prepared_sub is not None
      and self.v60_txdf_tx_slot is not None
      and str(current.get('call') or '').upper() == str(self.v60_txdf_prepared_call or '').upper()
    )
    if not prepared:
      if self.v60_txdf_active:
        v60_disarm_txdf(self, 'no matching prepared target')
      return 'not-prepared'

    # V10.5: TXEnabled=True is not proof that RF has started. Keep the full
    # synchronous PREARM propagation hold until real Transmitting=True, or its
    # timer expires. V10.4 cleared this timer on TXEnabled and could VS0 at
    # +1.9 s immediately before WSJT-X actually keyed the radio.
    hold_until = float(getattr(self, 'v60_txdf_prearm_hold_until', 0.0) or 0.0)
    if self.v60_txdf_active and now < hold_until:
      return 'prearmed-await-auto-tx'

    if not self.tx_enabled:
      if self.v60_txdf_active:
        v60_disarm_txdf(self, 'WSJT-X Auto Tx disabled')
      return 'tx-disabled'

    current_slot, phase, lead = _v60_txdf_slot_timing(wall, self.v60_txdf_tx_slot)
    wanted_slot = int(self.v60_txdf_tx_slot) & 1

    if self.v60_txdf_active:
      if self.v60_txdf_tx_seen:
        if v60_disarm_txdf(self, 'CAT TX ended after actual TX'):
          return 'disarmed-after-tx'
        return 'await-cat-rx'
      if current_slot == wanted_slot:
        if phase <= self.v60_txdf_start_grace:
          return 'armed-awaiting-tx'
        # No WSJT-X TX edge was observed. v60_disarm_txdf still verifies CAT
        # TX0, so a missed UDP edge cannot force VS0 during a real RF transmit.
        if v60_disarm_txdf(self, f'no TX by +{phase:.2f}s in wanted slot'):
          return 'missed-tx-slot'
        return 'await-cat-rx'
      if lead <= self.v60_txdf_pre_tx_guard + 0.5:
        return 'armed-pre-tx'
      if v60_disarm_txdf(self, 'outside pre-TX guard'):
        return 'disarmed-outside-guard'
      return 'await-cat-rx'

    # Never switch to SUB in the middle of our nominal TX slot. If a target was
    # selected too late, wait for the next same-parity opportunity instead.
    if current_slot == wanted_slot:
      return 'wait-next-slot'
    if lead <= self.v60_txdf_pre_tx_guard:
      if v60_arm_txdf(self, 'pre-TX guard', lead=lead):
        return 'armed'
      return 'arm-failed'
    return 'waiting'

  def hide_blocked_candidate(self, data, reason, remaining):
    call = str(data.get('call') or '').upper()
    if data.get('proactive'):
      remover = getattr(self, '_remove_proactive_from_queue', None)
      if remover:
        remover(call)
      target = getattr(self, 'proactive_targets', {}).get(call)
      if target:
        target['waiting_event'] = True
        target['rearm_after_burst'] = False
        target['v55_blocked_until'] = time.monotonic() + remaining
    else:
      # Hide the blocked ordinary CQ from the immediate SQLite selector pass.
      globals_dict = getattr(original_best, '__globals__', {})
      DBCommand = globals_dict.get('DBCommand')
      try:
        if DBCommand is not None and hasattr(DBCommand, 'DELETE'):
          self.queue.put((DBCommand.DELETE, {
            'call': call,
            'band': data.get('band', getattr(self, 'band', 0)),
          }))
        elif DBCommand is not None and hasattr(DBCommand, 'STATUS'):
          self.queue.put((DBCommand.STATUS, {
            'call': call, 'status': 1,
            'band': data.get('band', getattr(self, 'band', 0)),
          }))
      except Exception as err:
        LOG.debug('V6 could not hide blocked CQ %s: %s', call, err)

  def v55_best(self):
    if getattr(self, 'v55_manual', None) and self.v55_manual.active:
      return None
    seen = set()
    for _ in range(12):
      data = original_best(self)
      if not data:
        return None
      call = str(data.get('call') or '').upper()
      identity = (call, str(data.get('source')), bool(data.get('proactive')))
      if identity in seen:
        return None
      seen.add(identity)
      eligible, reason, remaining = self.v55_target_policy.eligible(
        data, _profile_key(self)
      )
      if eligible:
        dxcc_ok, dxcc_reason = v60_dxcc_gate(self, data)
        if dxcc_ok:
          return data
        LOG.info('V6 DXCC-band skipped %s on %s: %s dxcc=%s',
                 call, v60_band_name(self, data), dxcc_reason, data.get('dxcc'))
        hide_blocked_candidate(self, data, dxcc_reason, 3600.0)
        if not data.get('proactive'):
          return None
        continue
      now = time.monotonic()
      if self.v55_target_policy.should_log_block(data, _profile_key(self), now):
        LOG.info(
          'V6 target skipped %s on %s: %s remaining=%.0fs',
          call, _profile_key(self), reason, remaining,
        )
      if reason == 'anti-ping-pong' and data.get('proactive'):
        v60_register_antiping_defer(self, data, remaining, now)
      hide_blocked_candidate(self, data, reason, remaining)
      if not data.get('proactive'):
        # SQLite writes are asynchronous; avoid selecting the same CQ in a loop.
        return None
    return None

  def v60_exhaust_pursuit(self, data, rec, reason):
    call = str((data or {}).get('call') or '').upper()
    if not call:
      return 0.0
    if rec.get('exhausted'):
      return 0.0
    rec['exhausted'] = True
    rec['waiting'] = True
    tx_count = int(rec.get('actual_tx', 0) or 0)
    delay = self.v55_target_policy.note_failure(
      call, _profile_key(self), tx_count, f'V6 pursuit exhausted: {reason}'
    )
    # If fewer than the legacy minimum failed TX were actually made, still
    # suppress immediate reselection briefly. This is a pursuit limit, not a
    # fabricated radio failure.
    if delay <= 0:
      delay = min(300.0, max(60.0, self.v60_pursuit_lost_timeout))
    hide_blocked_candidate(self, data, 'pursuit-limit', delay)
    target = self.proactive_targets.get(call)
    if target:
      target['waiting_event'] = True
      target['rearm_after_burst'] = False
      target['v55_blocked_until'] = time.monotonic() + delay
      self._remove_proactive_from_queue(call)
    LOG.warning('V6 PURSUIT EXHAUSTED %s: windows=%d actual_tx=%d cooldown=%.0fs reason=%s',
                call, int(rec.get('windows', 0)), tx_count, delay, reason)
    return delay

  def v55_start(self, data, reason):
    if not data:
      return False
    if self.v55_manual.active:
      LOG.debug('V6 automatic selection suppressed during manual override')
      return False
    dxcc_ok, dxcc_reason = v60_dxcc_gate(self, data)
    if not dxcc_ok:
      LOG.info('V6 final DXCC-band gate blocked %s: %s', data.get('call'), dxcc_reason)
      return False
    if data.get('proactive'):
      call_key = str(data.get('call') or '').upper()
      rec = v60_pursuit_record(self, call_key)
      # Once the TargetPolicy cooldown has elapsed, a genuinely fresh event may
      # start a new bounded pursuit. Otherwise an exhausted record would pin the
      # station forever while it remains continuously audible.
      if rec.get('exhausted'):
        policy_ok, _, _ = self.v55_target_policy.eligible(data, _profile_key(self))
        if policy_ok:
          last_seen = float(rec.get('last_seen') or 0.0)
          self.v60_pursuit.pop(call_key, None)
          rec = v60_pursuit_record(self, call_key)
          rec['last_seen'] = last_seen
          LOG.info('V6 PURSUIT RESET %s after cooldown/fresh event', call_key)
      if rec.get('windows', 0) >= self.v60_pursuit_max_windows:
        v60_exhaust_pursuit(self, data, rec, 'max windows')
        return False
      if time.monotonic() - rec.get('started', time.monotonic()) > self.v60_pursuit_max_age:
        v60_exhaust_pursuit(self, data, rec, 'max age')
        return False
    prearm_needed, prearm_lead, prearm_slot, prearm_phase, prearm_reason = _v60_txdf_prearm_before_reply(self, data)

    if not v60_prepare_txdf(self, data):
      LOG.warning('V6 selection delayed: TX-DF radio preparation not safe for %s', data.get('call'))
      return False
    eligible, block_reason, remaining = self.v55_target_policy.eligible(
      data, _profile_key(self)
    )
    if not eligible:
      LOG.info(
        'V6 final target gate blocked %s: %s remaining=%.0fs',
        data.get('call'), block_reason, remaining,
      )
      hide_blocked_candidate(self, data, block_reason, remaining)
      return False

    old = getattr(self, 'current', None)
    old_profile = _profile_key(self)
    if old and old.get('call') != data.get('call'):
      comparable = bool(
        not self.v55_target_policy.is_direct(data)
        and not self.v55_target_policy.is_direct(old)
        and bool(old.get('proactive')) == bool(data.get('proactive'))
      )
      self.v55_target_policy.note_interrupted(
        old.get('call'), old_profile, f'switch to {data.get("call")}: {reason}',
        anti_pingpong=comparable,
      )
    v10_prearmed = False
    if self.v60_txdf.enabled and prearm_needed:
      if not v60_arm_txdf(
          self, f'V10 synchronous pre-WSReply arm: {prearm_reason}',
          lead=prearm_lead, prestart=True):
        # No TX has been handed to WSJT-X yet, so this is not a TX halt.  Keep
        # the selection pending instead of transmitting at an unverified CAT DF.
        LOG.error(
          'V10 TXDF PREARM failed for %s before WSReply; Reply withheld (no TX halt)',
          data.get('call') or '-',
        )
        v60_restore_radio(self, 'V10 pre-WSReply arm failure')
        return False
      v10_prearmed = True
      LOG.info(
        'V10 TXDF PREARM %s slot=%s phase=%.2fs lead=%.2fs: VS1 verified before WSReply',
        data.get('call') or '-', prearm_slot, float(prearm_phase), float(prearm_lead),
      )

    result = original_start(self, data, reason)
    if not result and v10_prearmed:
      v60_disarm_txdf(self, 'WSReply/start rejected after V10 prearm')
      v60_restore_radio(self, 'WSReply/start rejected after V10 prearm')
    if result:
      # V10.7.4: only a candidate that actually entered ATTEMPT owns the band.
      # Rejected DXCC/backoff/TXDF candidates leave QSY_PENDING untouched.
      v60_cancel_qsy_intent(self, f'V10.7.4 eligible target committed {data.get("call") or "?"}')
      self.v55_target_policy.note_attempt_started(data, _profile_key(self))
      v60_owner_lease(self, data.get('call'), 30.0)
      if data.get('proactive'):
        rec = v60_pursuit_record(self, data.get('call'))
        rec['windows'] = int(rec.get('windows', 0)) + 1
        rec['waiting'] = False
        rec['busy_hold_started'] = 0.0
        rec['busy_hold_until'] = 0.0
        rec['busy_band'] = None
        rec['busy_hold_log_at'] = 0.0
        rec['last_seen'] = max(float(rec.get('last_seen') or 0.0), float(data.get('last_seen') or time.monotonic()))
        try:
          rec['last_window_snr'] = float(data.get('snr')) if data.get('snr') is not None else None
        except (TypeError, ValueError):
          rec['last_window_snr'] = None
        rec['last_window_started'] = time.monotonic()
        LOG.info('V6 PURSUIT WINDOW %s %d/%d', data.get('call'), rec['windows'], self.v60_pursuit_max_windows)
      runtime_meta(self)
    return result

  def classify_clear_failure(self, current, state, attempts, reason):
    reason_low = str(reason or '').lower()
    if self.v55_neutral_clear:
      return 'neutral'
    neutral_tokens = (
      'manual', 'band changed', 'replacement', 'suspended', 'higher priority',
      'direct caller', 'selection timeout', 'cat-', 'switch ', 'pre-empt',
      'preempt', 'another wanted', 'next target',
    )
    success_tokens = ('qso logged', 'terminal message completed')
    if any(token in reason_low for token in success_tokens):
      return 'success'
    if any(token in reason_low for token in neutral_tokens):
      return 'neutral'
    state_name = getattr(state, 'value', str(state))
    if state_name == 'ENGAGED' and any(
        token in reason_low for token in ('retry limit', 'no progress', 'qso timeout', 'stalled')):
      return 'engaged-failure'
    if int(attempts) >= self.v55_target_policy.min_failed_tx:
      return 'failure'
    return 'neutral'

  def v55_clear(self, reason, delete_candidate=False):
    current = dict(self.current) if getattr(self, 'current', None) else None
    state = getattr(self, 'state', None)
    attempts = int(getattr(self, 'current_tx_attempts', 0))
    profile = _profile_key(self)
    if current:
      classification = classify_clear_failure(self, current, state, attempts, reason)
      if classification == 'success':
        self.v55_target_policy.note_success(current.get('call'), profile)
      elif classification == 'failure':
        self.v55_target_policy.note_failure(
          current.get('call'), profile, attempts, str(reason)
        )
      elif classification == 'engaged-failure':
        self.v55_target_policy.note_failure(
          current.get('call'), profile, max(attempts, self.v55_target_policy.min_failed_tx),
          str(reason), engaged=True,
        )
      else:
        reason_low = str(reason or '').lower()
        comparable_switch = not any(token in reason_low for token in (
          'manual', 'band changed', 'direct caller', 'higher priority',
          'suspended', 'cat-', 'selection timeout', 'pre-empt', 'preempt',
        ))
        self.v55_target_policy.note_interrupted(
          current.get('call'), profile, str(reason),
          anti_pingpong=comparable_switch,
        )
    result = original_clear(self, reason, delete_candidate)
    reason_low = str(reason or '').lower()
    if current and any(token in reason_low for token in ('qso logged', 'terminal message completed')):
      self.v60_pursuit.pop(str(current.get('call') or '').upper(), None)
    # TX-DF SUB/VS state is only owned while an automatic target/QSO is active. Actual
    # restoration may be deferred until the next status edge if TX is still on.
    if not getattr(self, 'current', None):
      v60_restore_radio(self, f'current cleared: {reason}')
    runtime_meta(self)
    return result

  def v55_mark_engaged(self, call, payload):
    was_engaged = getattr(self.state, 'value', str(self.state)) == 'ENGAGED'
    old_stage = int(getattr(self, 'engaged_rx_stage', 0) or 0)
    result = original_mark_engaged(self, call, payload)
    new_stage = int(getattr(self, 'engaged_rx_stage', 0) or 0)
    self.v55_target_policy.note_success(call, _profile_key(self))

    if not was_engaged:
      # Keep the most recent ACTUAL TX DF for initial continuity, but do not
      # walk an arbitrary historical list (e.g. 1540 -> 1500) on later stalls.
      # V10.6 replans from fresh <=120 s spectrum evidence instead.
      seq = self.v60_txdf.actual_df_fallback_sequence(call)
      initial_df = int(seq[0]) if seq else (
        int(self.v60_txdf.current_df) if self.v60_txdf.current_df is not None else None
      )
      self.v60_engaged_df_candidates = []
      self.v60_engaged_df_index = 0
      self.v60_engaged_df_pair_tx = 0
      self.v60_engaged_first_stage = new_stage
      self.v60_txdf.unlock()
      if initial_df is not None:
        v60_prepare_specific_df(self, call, initial_df)
        LOG.info('V10.6 ENGAGED TXDF starts at last actual DF=%d; later stalls use fresh replanning',
                 initial_df)
    elif new_stage > old_stage:
      # A later real QSO progression while using a candidate proves that this
      # DF works. Freeze it for the remainder of the QSO.
      if self.v60_txdf.current_df is not None and self.v60_txdf.locked_df is None:
        self.v60_txdf.lock(self.v60_txdf.current_df)
        LOG.info('V6 TXDF LOCK %s df=%d after QSO progression stage %d->%d',
                 call, self.v60_txdf.current_df, old_stage, new_stage)
    runtime_meta(self)
    return result

  def v55_log_call(self, packet):
    was_manual = self.v55_manual.active
    current_before = dict(self.current) if getattr(self, 'current', None) else None
    frequency = getattr(packet, 'DialFrequency', 0)
    profile = self.band_hopper.profile_for_frequency(frequency) or _profile_key(self)
    result = original_log_call(self, packet)
    logged_call = str(getattr(packet, 'DXCall', '') or '').upper()
    if not current_before or str(current_before.get('call') or '').upper() != logged_call:
      self.v55_target_policy.note_success(logged_call, profile)
    if was_manual or not current_before:
      self.v55_manual.qso_logged()
    # WSJT-X often keeps DXCall populated briefly after our own logged QSO. That
    # trailing state is not a manual takeover.
    self.v60_post_qso_owner_call = logged_call
    self.v60_post_qso_owner_until = time.monotonic() + 20.0
    if logged_call:
      try:
        info = self.lookup_candidate(logged_call, None, getattr(self, 'band', None))
        if self.v60_dxcc.add_worked(info.get('dxcc'), v60_band_name(self)):
          self.v60_dxcc.save()
          LOG.info('V6 DXCC memory immediate WORKED: call=%s dxcc=%s band=%s',
                   logged_call, info.get('dxcc'), v60_band_name(self))
      except Exception as err:
        LOG.debug('V6 could not update immediate DXCC memory for %s: %s', logged_call, err)
    runtime_meta(self, force=True)
    return result

  def v55_process_decode(self, packet):
    hopper = self.band_hopper
    profile_known = hopper.is_configured_frequency(getattr(self, 'frequency', 0))
    mode = str(getattr(self, 'v55_last_mode', '') or '').upper()
    valid_rx_context = profile_known and mode in {'', 'FT8', '~'}

    # The legacy parser also feeds the band model.  Temporarily suspend only the
    # band model when the operator is on an unconfigured frequency or another
    # mode, so those decodes cannot contaminate the previous FT8 profile.
    hopper_enabled = hopper.enabled
    if hopper_enabled and not valid_rx_context:
      hopper.enabled = False
    try:
      result = original_process_decode(self, packet)
    finally:
      hopper.enabled = hopper_enabled

    if (valid_rx_context and getattr(packet, 'New', True)
        and not getattr(packet, 'LowConfidence', False)
        and not getattr(packet, 'OffAir', False)):
      engaged_foreign = None
      for segment in (part.strip() for part in str(getattr(packet, 'Message', '')).split(';')):
        try:
          kind, match = self.parse_segment(segment)
        except Exception:
          continue
        if kind and match and match.get('call'):
          call = match.get('call')
          self.v55_target_policy.observe(
            call, _profile_key(self), getattr(packet, 'SNR', None), create=False,
          )
          try:
            info = self.lookup_candidate(call, match.get('grid'), self.band)
          except Exception:
            info = {}
          self.v60_txdf.note_decode(
            packet, call=call, locator=info.get('grid'), dxcc=info.get('dxcc')
          )

          # If our pursued DX is reporting a signal to a third station and we
          # recently heard that peer, correlate the peer's local DF with the SNR
          # reported by the DX. This progressively builds the remote map even
          # without Internet.
          current = getattr(self, 'current', None) or {}
          target = str(current.get('call') or '').upper()
          if kind == 'REPLY' and target and call == target and match.get('to') != self.mycall:
            peer = str(match.get('to') or '').upper()
            payload = match.get('payload') or []
            event = str(payload[-1]).upper() if payload else ''
            state_now = getattr(getattr(self, 'state', None), 'value', str(getattr(self, 'state', '')))
            action = _v60_engaged_foreign_action(
              state_now, target, call, peer, event, getattr(self, 'mycall', '')
            )
            if action:
              engaged_foreign = {
                'call': target, 'peer': peer, 'event': event, 'action': action,
                'seen_at': time.monotonic(),
              }
            report = None
            if payload:
              token = str(payload[-1]).upper()
              try:
                if token.startswith('R') and len(token) > 1:
                  report = int(token[1:])
                elif token not in {'RRR', 'RR73', '73'}:
                  report = int(token)
              except ValueError:
                report = None
            if report is not None:
              peer_obs = self.v60_txdf.local.latest_for_call(peer)
              if peer_obs and time.monotonic() - peer_obs.ts <= 60.0:
                self.v60_txdf.remote.add(
                  target, peer_obs.df, report, peer, confidence=0.85,
                  source='local-correlation', active=True,
                )
                LOG.info('V6 REMOTE MAP %s hears %s=%+d at DF=%.0f (local correlation)',
                         target, peer, report, peer_obs.df)
            if payload and str(payload[-1]).upper() in {'RRR', 'RR73', '73'}:
              if self.v60_txdf.remote.mark_inactive(target, peer):
                LOG.info('V6 REMOTE MAP peer ended: target=%s peer=%s', target, peer)

          # Selector yield is only a small scheduler hint. If the legacy
          # Country memory recognized this station as wanted, remember that the
          # current band recently produced a useful target.
          if call in getattr(self, 'proactive_targets', {}):
            self.band_hopper.note_selector_candidate(getattr(self, 'band', 0), call)

          # Keep pursuit presence fresh even while waiting for the target to
          # finish somebody else's QSO.
          if call in self.v60_pursuit:
            self.v60_pursuit[call]['last_seen'] = time.monotonic()
      if engaged_foreign:
        self.v60_engaged_foreign_event = engaged_foreign
        LOG.info(
          'V10.6.1 ENGAGED FOREIGN %s -> %s event=%s: QSO ownership lost%s',
          engaged_foreign['call'], engaged_foreign['peer'],
          engaged_foreign['event'] or '?',
          '; free-event rearm available now' if engaged_foreign['action'] == 'terminal-rearm' else '',
        )
    return result

  def enter_manual(self, reason, profile_known):
    changed = self.v55_manual.enter(reason, profile_known=profile_known)
    self.band_hopper.cancel_pending_switch()
    if getattr(self, 'current', None):
      call = self.current.get('call')
      self.v55_target_policy.note_interrupted(
        call, _profile_key(self), f'manual override: {reason}',
        anti_pingpong=False,
      )
      self.band_hopper.note_attempt_abandoned(call)
      self.v55_neutral_clear = True
      try:
        original_clear(self, f'manual override: {reason}', delete_candidate=False)
      finally:
        self.v55_neutral_clear = False
    # A true manual takeover must not inherit an automation-owned SUB/split.
    # If TX is still active the restore is deliberately deferred and retried
    # from check_timeouts once RX is idle.
    v60_restore_radio(self, 'manual override')
    if changed:
      self.v60_manual_tx_lock_started = 0.0
      self.v60_manual_tx_lock_call = ''
      runtime_meta(self, force=True)

  def v55_process_status(self, packet):
    old_frequency = int(getattr(self, 'frequency', 0) or 0)
    old_band = getattr(self, 'band', None)
    old_mode = getattr(self, 'v55_last_mode', None)
    old_dx = getattr(self, 'last_dx_call', None)
    old_tx_enabled = bool(getattr(self, 'tx_enabled', False))
    old_transmitting = bool(getattr(self, 'transmitting', False))
    old_watchdog = bool(getattr(self, 'tx_watchdog', False))
    previous_current = dict(self.current) if getattr(self, 'current', None) else None
    pending = dict(self.band_hopper.pending_switch) if self.band_hopper.pending_switch else None
    txdf_was_active = bool(getattr(self, 'v60_txdf_active', False))
    raw_frequency = int(getattr(packet, 'Frequency', getattr(self, 'frequency', 0)) or 0)

    result = original_process_status(self, packet)
    now = time.monotonic()
    new_band = getattr(self, 'band', None)
    if old_band and new_band and str(old_band) != str(new_band):
      removed = _v60_clear_local_spectrum(self.v60_txdf)
      if removed:
        LOG.info('V10.4 TXDF LOCAL MAP RESET on confirmed band change %sm -> %sm: removed=%d',
                 old_band, new_band, removed)
    self.v60_last_status_at = now
    self.v60_last_raw_frequency = raw_frequency
    saved_state = getattr(self, 'v60_saved_radio_state', None)
    aliases = dict(getattr(self, 'v60_txdf_recent_aliases', {}) or {})
    aliases = {
      int(k): v for k, v in aliases.items()
      if isinstance(v, (tuple, list)) and len(v) == 2 and float(v[1]) >= now
    }
    self.v60_txdf_recent_aliases = aliases
    saved_fa = getattr(saved_state, 'fa', None) or getattr(self, 'v60_txdf_status_main_fa', 0) or None
    frequency, txdf_frequency_alias = _v60_txdf_canonical_status_frequency(
      raw_frequency,
      saved_fa=saved_fa,
      prepared_sub=getattr(self, 'v60_txdf_prepared_sub', None),
      active=txdf_was_active or bool(getattr(self, 'v60_txdf_active', False)),
      owned_recovery=bool(getattr(self, 'v60_txdf_recovery_needed', False)),
      alias_until=getattr(self, 'v60_txdf_status_alias_until', 0.0),
      recent_aliases=aliases,
      now=now,
      tolerance=getattr(self.v55_manual, 'frequency_tolerance', 10),
    )
    # WSJT-X reports FB as Frequency while VS1 is selected. That is an
    # expected TXDF transport detail, not a human QSY. Keep policy state on FA.
    if frequency != int(getattr(self, 'frequency', 0) or 0):
      self.frequency = frequency
    if txdf_frequency_alias:
      LOG.debug(
        'V6 TXDF STATUS alias: WSJT-X frequency %d is prepared SUB; logical MAIN remains %d',
        raw_frequency, frequency,
      )
    mode = getattr(packet, 'TXMode', None)
    dx_call = str(getattr(packet, 'DXCall', '') or '').upper()
    tx_enabled = bool(getattr(packet, 'TXEnabled', False))
    transmitting = bool(getattr(packet, 'Transmitting', False))
    if transmitting and self.v60_txdf_active:
      self.v60_txdf_prearm_hold_until = 0.0
    watchdog = bool(getattr(packet, 'TXWatchdog', getattr(self, 'tx_watchdog', False)))
    if old_transmitting and not transmitting:
      if self.v60_txdf_active:
        v60_disarm_txdf(self, 'WSJT-X TX ended')
      elif bool(getattr(self, 'v60_txdf_recovery_needed', False)):
        v60_recover_txdf_selector(self, 'WSJT-X TX ended after unarmed TX')

    if watchdog and not old_watchdog:
      LOG.warning('V6 WATCHDOG entered: DXCall=%s owner=%s state=%s',
                  dx_call or '-', self.v60_owner_call or '-',
                  getattr(getattr(self, 'state', None), 'value', str(getattr(self, 'state', ''))))
    elif old_watchdog and not watchdog:
      LOG.info('V6 WATCHDOG cleared: automation may resume normally')

    profile = self.band_hopper.on_frequency_status(
      frequency, getattr(self, 'band', 0), getattr(packet, 'ConfigName', None),
      now=now, now_utc=datetime.utcnow(),
    )
    profile_known = profile is not None
    expected_qsy = bool(
      pending and abs(int(pending.get('frequency', 0)) - frequency)
      <= self.v55_manual.frequency_tolerance
    )

    if not self.v55_seen_status:
      self.v55_seen_status = True
      if frequency and not profile_known:
        enter_manual(self, f'started on unconfigured frequency {frequency} Hz', False)
      elif mode is not None and str(mode).upper() not in {'FT8', '~'}:
        enter_manual(self, f'started in non-FT8 mode {mode}', profile_known)
    else:
      frequency_changed = old_frequency and abs(frequency - old_frequency) > self.v55_manual.frequency_tolerance
      if frequency_changed and not expected_qsy:
        enter_manual(
          self,
          f'external frequency change {old_frequency} -> {frequency} Hz',
          profile_known,
        )
      elif old_mode is not None and mode is not None and mode != old_mode and not expected_qsy:
        enter_manual(self, f'external mode change {old_mode} -> {mode}', profile_known)

    current = getattr(self, 'current', None)
    owned_call = str(current.get('call') or '').upper() if current else ''
    owner_grace = v60_owner_matches(self, dx_call, now)
    post_qso_grace = bool(
      dx_call and dx_call == self.v60_post_qso_owner_call
      and now <= self.v60_post_qso_owner_until
    )
    previous_owned = str((previous_current or {}).get('call') or '').upper()
    previous_grace = bool(dx_call and dx_call == previous_owned and now <= self.v60_owner_until)
    if (tx_enabled or transmitting) and dx_call and dx_call != owned_call:
      if owner_grace or post_qso_grace or previous_grace:
        LOG.debug('V6 ownership grace accepts trailing WSJT-X DXCall=%s', dx_call)
      else:
        enter_manual(self, f'WSJT-X manually owns {dx_call}', profile_known)
    # Auto Tx falling after our own attempt/watchdog is no longer treated as a
    # manual takeover. The bounded ATTEMPT recovery path decides whether to
    # re-arm or release the target.

    if self.v55_manual.active and (tx_enabled or transmitting):
      self.v55_manual.activity(now)

    if (tx_enabled or transmitting or self.v55_manual.active) and self.v60_qsy_intent:
      v60_cancel_qsy_intent(self, 'WSJT-X TX/manual state appeared')

    if transmitting and not old_transmitting and self.v55_manual.active:
      v60_note_manual_tx_lock(self, dx_call, now)

    if transmitting and not old_transmitting and not self.v55_manual.active:
      if not current:
        LOG.error('V6 ORPHAN_TX: WSJT-X started TX with no FT8Commander target (DXCall=%s); stopping Auto Tx',
                  dx_call or '-')
        self.stop_transmit(self.last_ip_from, immediate=False)
      elif dx_call != owned_call:
        LOG.error('V6 ORPHAN_TX: WSJT-X TX call=%s but owner target=%s; stopping Auto Tx',
                  dx_call or '-', owned_call or '-')
        self.stop_transmit(self.last_ip_from, immediate=False)

    txdf_unarmed_nohalt = bool(
      transmitting and not old_transmitting and not self.v55_manual.active
      and current and dx_call == owned_call and self.v60_txdf.enabled
      and not self.v60_txdf_active
    )
    if txdf_unarmed_nohalt:
      self.v60_txdf_recovery_needed = True
      # Explicit V10 field policy: never abort an already-started wanted TX just
      # because the TXDF CAT arm missed its race.  The normal path prevents this
      # by synchronously arming VS1 before WSReply when timing is tight.
      LOG.error(
        'V10 TXDF UNARMED TX %s: NO-HALT policy; TX continues at WSJT-X reported TxDF',
        owned_call,
      )

    if (transmitting and not old_transmitting and not self.v55_manual.active
        and current and dx_call == owned_call):
      self.v60_txdf_tx_seen = bool(self.v60_txdf_active)
      self.v55_target_policy.note_tx(owned_call, _profile_key(self), now)
      if txdf_unarmed_nohalt:
        actual_df = int(getattr(packet, 'TxDF', getattr(self.v60_txdf, 'audio_df', 1500)) or self.v60_txdf.audio_df)
        actual_sub = None
      else:
        actual_df = getattr(self.v60_txdf, 'current_df', None)
        if actual_df is None:
          actual_df = int(getattr(packet, 'TxDF', getattr(self.v60_txdf, 'audio_df', 1500)) or self.v60_txdf.audio_df)
        actual_sub = self.v60_txdf_prepared_sub
      tx_item = self.v60_txdf.note_actual_tx(
        owned_call, str(getattr(packet, 'TxMessage', '') or ''), df=actual_df,
        sub_hz=actual_sub, now=now,
      )
      self.v60_last_actual_tx_call = owned_call
      self.v60_last_actual_tx_at = now
      if owned_call in self.v60_pursuit:
        rec = self.v60_pursuit[owned_call]
        rec['actual_tx'] = int(rec.get('actual_tx', 0) or 0) + 1
        rec['last_tx_at'] = now
        attempted = rec.setdefault('attempted_df', [])
        if not attempted or attempted[-1] != tx_item.df:
          attempted.append(tx_item.df)
      if txdf_unarmed_nohalt:
        LOG.warning('V10 ACTUAL TX UNARMED %s df=%d sub=None (NO-HALT)', owned_call, tx_item.df)
      else:
        LOG.info('V6 ACTUAL TX %s df=%d sub=%s', owned_call, tx_item.df, tx_item.sub_hz)

    self.v55_last_frequency = frequency
    self.v55_last_mode = mode
    self.v55_last_dx_call = dx_call or old_dx
    runtime_meta(self, now)
    return result

  def v55_evaluate(self):
    if self.v55_manual.active:
      LOG.debug('V6 automation waits: %s (%s)', self.v55_manual.state, self.v55_manual.reason)
      return None

    current = getattr(self, 'current', None) or {}
    state_name = getattr(getattr(self, 'state', None), 'value', str(getattr(self, 'state', '')))
    if current:
      v60_refresh_external_intel(self, current)

    # If an ENGAGED target is now transmitting to somebody else, our QSO has
    # lost ownership. Stop calling it. For this specific recovery path, CQ is
    # now also sufficient to re-arm: wait for a fresh target-emitted
    # CQ/RRR/RR73/73. CQ proves the station is free again.
    foreign = getattr(self, 'v60_engaged_foreign_event', None)
    if state_name == 'ENGAGED' and current and foreign:
      self.v60_engaged_foreign_event = None
      call = str(current.get('call') or '').upper()
      if str(foreign.get('call') or '').upper() == call:
        event = str(foreign.get('event') or '').upper()
        peer = str(foreign.get('peer') or '').upper()
        action = _v60_engaged_foreign_action(
          state_name, call, call, peer, event, getattr(self, 'mycall', '')
        )
        if action:
          self.stop_transmit(self.last_ip_from, immediate=False)
          target = self.proactive_targets.get(call)
          if target:
            target['last_attempted'] = time.monotonic()
            target['waiting_event'] = True
            target['rearm_after_burst'] = False
            self._remove_proactive_from_queue(call)
          rec = v60_pursuit_record(self, call)
          rec['waiting'] = True
          rec['terminal_only_rearm'] = True
          hold_remaining = v60_begin_busy_wait_band_hold(self, call)
          self.v55_neutral_clear = True
          try:
            original_clear(
              self,
              'V10.6.1 ENGAGED target busy; wait for target CQ/RRR/RR73/73',
              delete_candidate=True,
            )
          finally:
            self.v55_neutral_clear = False
          self.band_hopper.note_attempt_abandoned(call)
          LOG.info(
            'V10.6.1 ENGAGED BUSY WAIT %s: target now working %s; waiting for fresh target CQ/RRR/RR73/73',
            call, peer or '?',
          )
          if hold_remaining > 0.0:
            LOG.info(
              'V10.6 ENGAGED BUSY BAND HOLD %s: %.0fs; QSY deferred, same-band selection continues',
              call, hold_remaining,
            )
          if action == 'terminal-rearm' and target:
            rec['terminal_only_rearm'] = False
            rec['busy_hold_started'] = 0.0
            rec['busy_hold_until'] = 0.0
            rec['busy_band'] = None
            rec['busy_hold_log_at'] = 0.0
            rec['waiting'] = False
            target['waiting_event'] = False
            queued = self.queue_proactive_target(
              call, front=True, reason=f'V10.6.1 lost-QSO free-event {event} emitted by {call}'
            )
            if queued:
              self.v60_fresh_rf_priority = {
                'call': call, 'event': event, 'seen_at': time.monotonic(),
                'band': self.band, 'lost_engaged_recovery': True,
              }
              LOG.info(
                'V10.6.1 ENGAGED FREE REARM %s event=%s: immediate next eligible TX opportunity',
                call, event,
              )
          runtime_meta(self)
          return None

    # Enforce rolling TX caps only before a QSO has become ENGAGED. Once the
    # target has answered us, QSO completion is absolute: profile/global caps
    # may block a new attempt but may never interrupt R-report/RR73/73 closure.
    if (current and state_name != 'ENGAGED' and not self.transmitting
        and int(getattr(self, 'current_tx_attempts', 0)) > 0):
      cap_ok, cap_reason, cap_remaining = self.v55_target_policy.eligible(
        current, _profile_key(self)
      )
      if not cap_ok and cap_reason in {'profile-TX-cap', 'global-TX-cap'}:
        call = str(current.get('call') or '').upper()
        LOG.warning('V6 strict TX cap stops %s: %s remaining=%.0fs',
                    call, cap_reason, cap_remaining)
        self.stop_transmit(self.last_ip_from, immediate=False)
        hide_blocked_candidate(self, current, cap_reason, cap_remaining)
        self.v55_neutral_clear = True
        try:
          original_clear(self, f'V6 strict {cap_reason}', delete_candidate=True)
        finally:
          self.v55_neutral_clear = False
        self.band_hopper.note_attempt_abandoned(call)
        runtime_meta(self)
        return None

    # V6 DX_PURSUIT: short event-driven windows. A proactive target is not
    # pre-empted by another ordinary Country candidate in the middle of its
    # window. If we hear it working somebody else, stop calling and wait for a
    # CQ/RRR/RR73/73 event before opening the next window.
    if state_name == 'ATTEMPT' and current.get('proactive'):
      call = str(current.get('call') or '').upper()
      if (getattr(self, 'current_working_other', False)
          and int(getattr(self, 'current_tx_attempts', 0)) >= 1):
        target = self.proactive_targets.get(call)
        self.stop_transmit(self.last_ip_from, immediate=False)
        if target:
          target['last_attempted'] = time.monotonic()
          target['waiting_event'] = True
          target['rearm_after_burst'] = False
          self._remove_proactive_from_queue(call)
        self.current_working_other = False
        self.v55_neutral_clear = True
        try:
          original_clear(self, 'V6 pursuit target busy; wait for terminal/CQ', delete_candidate=True)
        finally:
          self.v55_neutral_clear = False
        self.band_hopper.note_attempt_abandoned(call)
        rec = v60_pursuit_record(self, call)
        rec['waiting'] = True
        hold_remaining = v60_begin_busy_wait_band_hold(self, call)
        LOG.info('V6 PURSUIT WAIT %s: target working another station', call)
        if hold_remaining > 0.0:
          LOG.info(
            'V6 PURSUIT WAIT BAND HOLD %s: %.0fs; QSY deferred, same-band selection continues',
            call, hold_remaining,
          )
        runtime_meta(self)
        return None

      # Between the first and second call, try another locally/remotely ranked
      # DF while still in RX. This is disabled until the FTX-1 split test has
      # validated tx_df_enabled in the field.
      if (self.v60_txdf.enabled and int(getattr(self, 'current_tx_attempts', 0)) == 1
          and int(getattr(self, 'current_unanswered_cycles', 0)) >= 1):
        target_df = int(current.get('packet', {}).get('DeltaFrequency', self.v60_txdf.audio_df)
                        or self.v60_txdf.audio_df)
        target_slot = self.v60_txdf.local.slot_parity(current.get('time'))
        rec = v60_pursuit_record(self, call)
        wanted = self.v60_txdf.choose(call, target_df, target_slot ^ 1, rec.get('attempted_df', []))
        if wanted != self.v60_txdf.current_df:
          v60_prepare_specific_df(self, call, wanted)

      # Re-use the mature legacy final-RX semantics, but make a proactive V6
      # window two actual TX and suppress same-priority ping-pong mid-window.
      original_retries = self.tx_retries
      had_instance_alt = 'proactive_alternative' in self.__dict__
      old_instance_alt = self.__dict__.get('proactive_alternative')
      before_state = state_name
      before_call = call
      try:
        self.tx_retries = self.v60_pursuit_window_tx
        self.proactive_alternative = lambda: None
        result = original_evaluate(self)
      finally:
        self.tx_retries = original_retries
        if had_instance_alt:
          self.proactive_alternative = old_instance_alt
        else:
          self.__dict__.pop('proactive_alternative', None)
      after_state = getattr(getattr(self, 'state', None), 'value', str(getattr(self, 'state', '')))
      if before_state == 'ATTEMPT' and after_state == 'IDLE':
        self.band_hopper.note_attempt_abandoned(before_call)
        rec = v60_pursuit_record(self, before_call)
        rec['waiting'] = True
        target = self.proactive_targets.get(before_call)
        if target:
          target['waiting_event'] = True
          target['rearm_after_burst'] = False
          self._remove_proactive_from_queue(before_call)
        LOG.info('V6 PURSUIT WINDOW END %s: scheduler released until fresh CQ/RRR/RR73/73', before_call)
      return result

    # Once the target answered, do not walk arbitrary historical DFs. After
    # two real TX without stage progression, ask the V10.5 fast planner again
    # using fresh same-slot evidence (<=120 s) plus opposite-slot proxy. If no
    # safe shifted hole exists, the planner itself falls back exactly to the
    # caller signal-start DF.
    if state_name == 'ENGAGED' and current and self.v60_txdf.enabled and self.v60_txdf.locked_df is None:
      pair_tx = int(getattr(self, 'engaged_tx_since_progress', 0))
      if pair_tx >= 2:
        call = str(current.get('call') or '').upper()
        packet = current.get('packet', {}) or {}
        target_df = int(packet.get('DeltaFrequency', self.v60_txdf.audio_df) or self.v60_txdf.audio_df)
        target_slot = self.v60_txdf.local.slot_parity(current.get('time'))
        rec = v60_pursuit_record(self, call)
        attempted = list(rec.get('attempted_df', []) or [])
        previous_df = self.v60_txdf.current_df
        wanted = self.v60_txdf.choose(call, target_df, target_slot ^ 1, attempted)
        if previous_df is None or int(wanted) != int(previous_df):
          if v60_prepare_specific_df(self, call, wanted):
            self.engaged_tx_since_progress = 0
            LOG.info(
              'V10.6 ENGAGED TXDF REPLAN %s: no progress after 2 TX, target_start=%d previous=%s chosen=%d',
              call, target_df, previous_df, wanted,
            )
            return None
        else:
          LOG.info(
            'V10.6 ENGAGED TXDF REPLAN %s: no fresher better DF after 2 TX; keeping %d',
            call, int(wanted),
          )

    return original_evaluate(self)

  def v55_maybe_hop(self, best=None, silent_only=False):
    if self.v55_manual.active:
      return False
    if (not self.band_hopper.enabled or self.state != QSOState.IDLE
        or self.transmitting or self.tx_enabled):
      return False

    busy_hold = v60_busy_wait_band_hold(self)
    if busy_hold:
      call, remaining, rec = busy_hold
      if getattr(self, 'v60_qsy_intent', None):
        v60_cancel_qsy_intent(
          self, f'pursuit wait band hold for {call} ({remaining:.0f}s remaining)'
        )
      now = time.monotonic()
      last_log = float(rec.get('busy_hold_log_at') or 0.0)
      if now - last_log >= 30.0:
        rec['busy_hold_log_at'] = now
        LOG.info(
          'V6 PURSUIT WAIT BAND HOLD %s: %.0fs remaining; QSY deferred, same-band selection continues',
          call, remaining,
        )
      self.decision_due_at = now + float(self.decision_settle_time)
      return False

    intent_state = v60_service_qsy_intent(self)
    if intent_state in {'waiting', 'executed'}:
      return True

    if v60_service_antiping_defer(self):
      return True

    pending_target = v60_pending_priority_target(self)
    if pending_target:
      now = time.monotonic()
      last_log = float(pending_target.get('_v60_band_hold_log_at') or 0.0)
      if now - last_log >= 30.0:
        pending_target['_v60_band_hold_log_at'] = now
        LOG.info(
          'V6 WANTED BAND HOLD %s: fresh queued target prevents QSY',
          pending_target.get('call') or '?',
        )
      # IMPORTANT: False means "no band hop was performed".  The legacy
      # IDLE evaluator must continue after this guard so it can call
      # best_available_candidate() and start this exact queued target. Returning
      # True here starves selection: the target holds the band forever but is
      # never allowed to become current/CALL/TX.
      self.decision_due_at = now + float(self.decision_settle_time)
      return False

    recent = bool(best and self.band_hopper.candidate_is_recent(best))
    hard = bool(recent and self.v55_target_policy.is_hard_interest(best))
    critical = bool(recent and self.v55_target_policy.is_direct(best))
    decision = self.band_hopper.decision(
      interesting=False,
      hard_interesting=hard,
      critical_interesting=critical,
      soft_interesting=recent and not hard,
      silent_only=silent_only,
    )
    if not decision:
      return False
    target_band, frequency_hz, reason = decision
    return v60_schedule_qsy_intent(self, target_band, frequency_hz, reason)

  def v55_rearm(self, reason):
    if self.v55_manual.active:
      return False
    if int(getattr(self, 'current_rearm_count', 0)) >= int(getattr(self, 'attempt_rearm_max', 1)):
      LOG.warning('V6 ATTEMPT rearm limit reached for %s', (self.current or {}).get('call'))
      return False
    return original_rearm(self, reason) if original_rearm else False

  def process_command(self, now):
    path = self.v55_command_path
    try:
      payload = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
      return
    except (OSError, ValueError, TypeError) as err:
      LOG.warning('V6 command ignored: %s', err)
      try:
        path.unlink()
      except OSError:
        pass
      return
    try:
      action = str(payload.get('action') or '').lower()
      if action == 'recalibrate':
        self.band_hopper.recalibrate(now)
      elif action == 'clear-target':
        call = payload.get('call')
        removed = self.v55_target_policy.clear_call(call, now)
        LOG.warning('V6 cleared target policy for %s (%d records)', call, removed)
      elif action == 'resume-auto':
        profile_known = self.band_hopper.is_configured_frequency(
          getattr(self, 'frequency', 0)
        )
        mode = str(getattr(self, 'v55_last_mode', '') or '').upper()
        mode_ok = mode in {'', 'FT8', '~'}
        if not profile_known or not mode_ok:
          LOG.warning(
            'V6 resume refused: configured_profile=%s mode=%s; '
            'return WSJT-X to a configured FT8 frequency first',
            profile_known, mode or '?',
          )
          if not self.v55_manual.active:
            enter_manual(
              self, 'resume refused outside a configured FT8 profile',
              profile_known,
            )
        else:
          self.v55_manual.force_resume(now)
          self.v60_manual_tx_lock_started = 0.0
          self.v60_manual_tx_lock_call = ''
          _reset_visit_after_manual(self, now)
      elif action == 'pause-auto':
        enter_manual(self, 'paused by ft8ctrlctl', self.band_hopper.is_configured_frequency(self.frequency))
      else:
        LOG.warning('Unknown V6 command action: %r', action)
    finally:
      try:
        path.unlink()
      except OSError:
        pass
      runtime_meta(self, now, force=True)

  def v55_check_timeouts(self):
    now = time.monotonic()
    process_command(self, now)
    self.v60_dxcc.load()
    # DX Cluster is a band-hint only: count recent spots whose DXCC is still
    # missing on that band. It can bias exploration but never initiates TX.
    if getattr(self, 'v60_dxcluster', None) and self.v60_dxcluster.enabled:
      bonuses = {}
      for spot in self.v60_dxcluster.recent(now):
        try:
          info = self.lookup_candidate(spot.call, None, spot.band)
          ok, _ = self.v60_dxcc.eligible(
            info.get('dxcc'), f'{spot.band}m',
            exclude_worked=self.v60_exclude_worked,
            exclude_confirmed=self.v60_exclude_confirmed,
          )
        except Exception:
          ok = False
        if ok:
          bonuses[spot.band] = min(25.0, bonuses.get(spot.band, 0.0) + 3.0)
      self.band_hopper.set_external_band_bonus(bonuses, now)
    if getattr(self, 'current', None):
      v60_refresh_external_intel(self, self.current, now)
    v60_service_txdf_switch(self, now)
    if (self.v60_saved_radio_state is not None and not self.transmitting
        and not self.tx_enabled and not getattr(self, 'current', None)):
      v60_restore_radio(self, 'idle deferred restore')
    active_call = str(((getattr(self, 'current', None) or {}).get('call')) or '').upper()
    for call, rec in list(self.v60_pursuit.items()):
      if not _v60_pursuit_should_expire(
          call, active_call, rec, now, self.v60_pursuit_lost_timeout):
        continue
      last_seen = float(rec.get('last_seen') or 0.0)
      self.v60_pursuit.pop(call, None)
      target = self.proactive_targets.get(call)
      if target:
        self.drop_proactive_target(call, f'V6 pursuit lost for {now-last_seen:.0f}s')
      LOG.info('V6 PURSUIT EXPIRE %s after %.0fs not heard', call, now-last_seen)
    if self.v55_manual.active:
      profile_known = self.band_hopper.is_configured_frequency(getattr(self, 'frequency', 0))
      resumed = self.v55_manual.tick(
        bool(getattr(self, 'tx_enabled', False)),
        bool(getattr(self, 'transmitting', False)),
        profile_known,
        now,
      )
      if resumed:
        self.v60_manual_tx_lock_started = 0.0
        self.v60_manual_tx_lock_call = ''
        _reset_visit_after_manual(self, now)
      runtime_meta(self, now)
      if self.v55_manual.active:
        return None
    qsy_state = v60_service_qsy_intent(self, now)
    if qsy_state in {'waiting', 'executed'}:
      runtime_meta(self, now)
      return None
    result = original_check_timeouts(self)
    runtime_meta(self, now)
    return result

  def v8_remember_proactive_target(self, packet, match, trigger):
    """V8 RF-event rearm: emitter-confirmed CQ/RRR/RR73/73 bypass anti-ping only."""
    result = v60_remember_proactive_target(self, packet, match, trigger)
    if not result:
      return result

    trigger_name = str(trigger or '').upper()
    kind = 'CQ' if trigger_name == 'CQ' else 'REPLY'
    emitter, event = _v60_fresh_rearm_event(kind, match)
    if not emitter or emitter == str(getattr(self, 'mycall', '') or '').upper():
      return result

    target = getattr(self, 'proactive_targets', {}).get(emitter)
    if not target or str(target.get('band')) != str(getattr(self, 'band', None)):
      return result

    now = time.monotonic()
    rec = getattr(self, 'v60_pursuit', {}).get(emitter)
    lost_engaged_recovery = False
    if rec:
      terminal_only = bool(rec.get('terminal_only_rearm', False))
      if terminal_only and not _v60_terminal_rearm_event(event):
        # original_remember_proactive() may already have queued a waiting target
        # on CQ. Undo that generic behavior for this lost-ENGAGED recovery path.
        target['waiting_event'] = True
        target['rearm_after_burst'] = False
        self._remove_proactive_from_queue(emitter)
        LOG.info(
          'V10.6.1 LOST-QSO WAIT %s: emitter event=%s ignored; waiting for CQ/RRR/RR73/73',
          emitter, event,
        )
        return result
      lost_engaged_recovery = terminal_only and _v60_terminal_rearm_event(event)

      # Normal pursuit accepts CQ or terminal. A lost ENGAGED QSO deliberately
      # accepts CQ/RRR/RR73/73; once observed the previous busy hold is over.
      rec['busy_hold_started'] = 0.0
      rec['busy_hold_until'] = 0.0
      rec['busy_band'] = None
      rec['busy_hold_log_at'] = 0.0
      rec['waiting'] = False
      rec['terminal_only_rearm'] = False

      if not lost_engaged_recovery:
        if int(rec.get('windows', 0) or 0) >= int(self.v60_pursuit_max_windows):
          return result
        if now - float(rec.get('started') or now) > float(self.v60_pursuit_max_age):
          return result

    if lost_engaged_recovery:
      # This is continuation of a QSO we had already ENGAGED, not a new random
      # proactive start. Clear only anti-ping and allow one fresh availability-event
      # recovery opportunity even if the normal rolling start cap is full.
      policy_rec = self.v55_target_policy._record(emitter, _profile_key(self), create=False)
      bypassed = bool(policy_rec and policy_rec.anti_pingpong_until > now)
      if bypassed:
        policy_rec.anti_pingpong_until = 0.0
        self.v55_target_policy._persist(now)
      allowed, block_reason = True, 'lost-engaged-free-event-recovery'
    else:
      allowed, block_reason, bypassed = _v60_fresh_rf_policy_gate(
        self.v55_target_policy, emitter, _profile_key(self), now
      )
    if not allowed:
      LOG.info('V8 FRESH RF %s %s observed but %s remains authoritative',
               event, emitter, block_reason)
      return result

    self.v60_deferred_anti_ping.pop(emitter, None)
    current_call = str(((getattr(self, 'current', None) or {}).get('call')) or '').upper()
    if current_call == emitter:
      target['rearm_after_burst'] = True
      if bypassed:
        LOG.info('V8 FRESH RF REARM %s event=%s: emitter-confirmed; anti-ping bypassed',
                 emitter, event)
      return result

    queued = self.queue_proactive_target(
      emitter, front=True, reason=f'fresh RF {event} emitted by {emitter}'
    )
    if queued:
      self.v60_fresh_rf_priority = {
        'call': emitter,
        'event': event,
        'seen_at': now,
        'band': self.band,
        'lost_engaged_recovery': bool(lost_engaged_recovery),
      }
      self.decision_due_at = now + float(self.decision_settle_time)
      LOG.info(
        'V8 FRESH RF PRIORITY %s event=%s: next eligible TX opportunity%s',
        emitter, event, '; anti-ping bypassed' if bypassed else '',
      )
    return result

  def v8_best(self):
    """Never rotate a pursuit-busy station back in until it emits a fresh rearm event."""
    now = time.monotonic()
    defer_call = str(getattr(self, 'v60_txdf_slot_defer_call', '') or '').upper()
    defer_until = float(getattr(self, 'v60_txdf_slot_defer_until', 0.0) or 0.0)
    if defer_call and now < defer_until:
      # A few hundred milliseconds / seconds of deliberate IDLE is preferable
      # to enabling a different target or reselecting the same one at a boundary
      # where VS1 cannot be verified before RF starts. Direct-call priority is
      # therefore preserved as well.
      return None
    if defer_call and now >= defer_until:
      self.v60_txdf_slot_defer_call = ''
      self.v60_txdf_slot_defer_until = 0.0
      self.v60_txdf_slot_defer_direct = False
    for _ in range(12):
      data = v55_best(self)
      if not data:
        return None
      if self.v55_target_policy.is_direct(data):
        return data
      call = str(data.get('call') or '').upper()
      rec = getattr(self, 'v60_pursuit', {}).get(call)
      now = time.monotonic()
      terminal_only = bool(rec and rec.get('terminal_only_rearm', False))
      if not terminal_only and not _v60_busy_target_blocked(rec, now):
        return data

      remaining = max(0.0, float((rec or {}).get('busy_hold_until') or 0.0) - now)
      if data.get('proactive'):
        target = self.proactive_targets.get(call)
        if target:
          target['waiting_event'] = True
          target['rearm_after_burst'] = False
        self._remove_proactive_from_queue(call)
      else:
        hide_blocked_candidate(self, data, 'pursuit-busy', remaining)
      last_log = float(rec.get('busy_selection_log_at') or 0.0)
      if now - last_log >= 15.0:
        rec['busy_selection_log_at'] = now
        if terminal_only:
          LOG.info('V10.6.1 LOST-QSO TARGET SKIP %s: waiting for emitter CQ/RRR/RR73/73', call)
        else:
          LOG.info('V8 BUSY TARGET SKIP %s: %.0fs remaining; waiting for emitter CQ/RRR/RR73/73',
                   call, remaining)
    return None

  def v8_process_decode(self, packet):
    result = v55_process_decode(self, packet)
    pending = str(getattr(self, 'v60_foreign_owner_pending_call', '') or '').upper()
    if pending and pending in getattr(self, 'pending_direct_calls', {}):
      self.v60_foreign_owner_pending_call = ''
      self.v60_foreign_owner_pending_until = 0.0
      v60_owner_lease(self, pending, 30.0)
      LOG.info('V8 DIRECT OWNERSHIP correlated: Status DXCall=%s matched fresh direct Decode', pending)
    return result

  def v8_process_status(self, packet):
    """Give Status->Decode ordering a short grace without hiding real manual TX."""
    dx_call = str(getattr(packet, 'DXCall', '') or '').upper()
    tx_enabled = bool(getattr(packet, 'TXEnabled', False))
    transmitting = bool(getattr(packet, 'Transmitting', False))
    state_name = getattr(getattr(self, 'state', None), 'value', str(getattr(self, 'state', '')))
    current = getattr(self, 'current', None)
    matching_direct = bool(dx_call and dx_call in getattr(self, 'pending_direct_calls', {}))
    decision = _v60_foreign_owner_grace(
      state_name, bool(current), tx_enabled, transmitting, dx_call, matching_direct
    )
    now = time.monotonic()

    if decision in {'grace', 'direct'} and not getattr(self, 'v55_manual', None).active:
      # v55_process_status recognizes this owner lease and therefore does not
      # misclassify WSJT-X Auto-Tx selection as a human takeover before the
      # matching Decode packet reaches us.
      v60_owner_lease(self, dx_call, max(3.0, self.v60_foreign_owner_grace_s + 0.5))
      if decision == 'grace':
        if str(getattr(self, 'v60_foreign_owner_pending_call', '') or '').upper() != dx_call:
          LOG.debug('V8 DIRECT STATUS grace started for DXCall=%s awaiting Decode', dx_call)
        self.v60_foreign_owner_pending_call = dx_call
        self.v60_foreign_owner_pending_until = now + self.v60_foreign_owner_grace_s
      else:
        self.v60_foreign_owner_pending_call = ''
        self.v60_foreign_owner_pending_until = 0.0
    elif decision == 'manual':
      # Never let an earlier grace lease mask an actual orphan/manual TX edge.
      self.v60_owner_until = 0.0
      self.v60_foreign_owner_pending_call = ''
      self.v60_foreign_owner_pending_until = 0.0

    result = v55_process_status(self, packet)
    current_after = getattr(self, 'current', None) or {}
    if dx_call and str(current_after.get('call') or '').upper() == dx_call:
      self.v60_foreign_owner_pending_call = ''
      self.v60_foreign_owner_pending_until = 0.0
    return result

  def v8_evaluate(self):
    """Direct > fresh RF > normal V7 scheduler, while ENGAGED remains absolute."""
    state_name = getattr(getattr(self, 'state', None), 'value', str(getattr(self, 'state', '')))
    current = getattr(self, 'current', None) or {}
    direct = self.next_direct_call(self.band) if getattr(self, 'band', None) else None

    if self.v55_manual.active:
      post_qso_direct = bool(
        self.v55_manual.state == self.v55_manual.POST_QSO
        and direct and state_name == 'IDLE'
        and not self.transmitting and not self.tx_enabled
      )
      if post_qso_direct:
        # This clears only the manual controller. band_hopper's independent
        # post-QSO observation/lock remains intact.
        LOG.info('V8 DIRECT PRIORITY %s: bypassing MANUAL_POST_QSO_HOLD; band hold preserved',
                 direct.get('call'))
        self.v55_manual.force_resume()
      else:
        return v55_evaluate(self)

    # A complete decode batch has already been processed here. If the current
    # station answered anywhere in that batch, state_name is ENGAGED and wins.
    state_name = getattr(getattr(self, 'state', None), 'value', str(getattr(self, 'state', '')))
    current = getattr(self, 'current', None) or {}
    direct = self.next_direct_call(self.band) if getattr(self, 'band', None) else None
    if direct and _v60_direct_preempt_allowed(state_name, self.transmitting):
      direct_call = str(direct.get('call') or '').upper()
      current_call = str(current.get('call') or '').upper()
      if direct_call and direct_call != current_call:
        if state_name != 'IDLE' or not self.tx_enabled or str(getattr(self, 'v55_last_dx_call', '') or '').upper() == direct_call:
          LOG.info('V8 DIRECT PRIORITY %s: pre-empting %s before next TX slot',
                   direct_call, current_call or 'IDLE')
          if v55_start(self, direct, 'fresh direct caller absolute priority'):
            self.v60_foreign_owner_pending_call = ''
            self.v60_foreign_owner_pending_until = 0.0
            return None

    fresh = getattr(self, 'v60_fresh_rf_priority', None)
    if fresh:
      now = time.monotonic()
      fresh_call = str(fresh.get('call') or '').upper()
      fresh_age = now - float(fresh.get('seen_at') or now)
      if (fresh_age > 20.0 or str(fresh.get('band')) != str(getattr(self, 'band', None))):
        self.v60_fresh_rf_priority = None
      elif _v60_fresh_select_allowed(state_name, self.transmitting, self.tx_enabled):
        current_call = str(current.get('call') or '').upper()
        target = self.proactive_targets.get(fresh_call)
        if target and fresh_call != current_call:
          recovery = bool(fresh.get('lost_engaged_recovery', False))
          if recovery:
            eligible, reason, _remaining = True, 'lost-engaged-free-event-recovery', 0.0
          else:
            eligible, reason, _remaining = self.v55_target_policy.eligible(
              target, _profile_key(self), now
            )
          rec = self.v60_pursuit.get(fresh_call)
          terminal_wait = bool(rec and rec.get('terminal_only_rearm', False))
          if eligible and not terminal_wait and not _v60_busy_target_blocked(rec, now):
            event = fresh.get('event') or '?'
            if recovery:
              LOG.info('V10.6.1 LOST-QSO FREE PRIORITY %s event=%s: resuming because target is available again',
                       fresh_call, event)
            else:
              LOG.info('V8 FRESH RF PRIORITY %s event=%s: selecting from IDLE for immediate next-slot attempt',
                       fresh_call, event)
            self.v60_fresh_rf_priority = None
            if v55_start(self, dict(target), f'fresh RF {event} emitter opportunity'):
              return None
          elif not eligible and reason != 'anti-ping-pong':
            self.v60_fresh_rf_priority = None

    return v55_evaluate(self)

  def v8_check_timeouts(self):
    """Resolve unmatched Status grace into true manual ownership after timeout."""
    # V7 delegate retains the guarded QSY service call:
    # v60_service_qsy_intent(self, now)
    now = time.monotonic()
    pending = str(getattr(self, 'v60_foreign_owner_pending_call', '') or '').upper()
    deadline = float(getattr(self, 'v60_foreign_owner_pending_until', 0.0) or 0.0)
    if pending and deadline and now >= deadline:
      current_call = str(((getattr(self, 'current', None) or {}).get('call')) or '').upper()
      matching_direct = pending in getattr(self, 'pending_direct_calls', {})
      if matching_direct or current_call == pending:
        v60_owner_lease(self, pending, 30.0)
      elif (not self.v55_manual.active and self.tx_enabled and not self.transmitting
            and str(getattr(self, 'v55_last_dx_call', '') or '').upper() == pending):
        self.v60_owner_until = 0.0
        enter_manual(self, f'WSJT-X manually owns {pending} after direct-decode grace expired',
                     self.band_hopper.is_configured_frequency(getattr(self, 'frequency', 0)))
      self.v60_foreign_owner_pending_call = ''
      self.v60_foreign_owner_pending_until = 0.0
    return v55_check_timeouts(self)

  Sequencer.__init__ = v55_init
  Sequencer.best_available_candidate = v8_best
  Sequencer.start_candidate = v55_start
  Sequencer.clear_current = v55_clear
  Sequencer.mark_engaged = v55_mark_engaged
  Sequencer.log_call = v55_log_call
  Sequencer.process_decode = v8_process_decode
  Sequencer.remember_proactive_target = v8_remember_proactive_target
  Sequencer.process_status = v8_process_status
  Sequencer.evaluate_after_decode = v8_evaluate
  Sequencer.maybe_band_hop = v55_maybe_hop
  Sequencer.v60_busy_wait_band_hold = v60_busy_wait_band_hold
  Sequencer.v60_begin_busy_wait_band_hold = v60_begin_busy_wait_band_hold
  Sequencer.v60_arm_txdf = v60_arm_txdf
  Sequencer.v60_disarm_txdf = v60_disarm_txdf
  Sequencer.v60_recover_txdf_selector = v60_recover_txdf_selector
  Sequencer.v60_service_txdf_switch = v60_service_txdf_switch
  if original_rearm:
    Sequencer.rearm_current_attempt = v55_rearm
  Sequencer.check_timeouts = v8_check_timeouts
  Sequencer.v55_runtime_meta = runtime_meta
  Sequencer.v60_rf_roles = staticmethod(_v60_rf_roles)
  # V10.7.6 expose guarded QSY cancellation
  def _v1076_cancel_qsy_intent_method(self, reason):
    return v60_cancel_qsy_intent(self, reason)
  Sequencer.v1076_cancel_qsy_intent = _v1076_cancel_qsy_intent_method
