#!/usr/bin/env python3
"""Runtime integration layer for FT8Commander v5.5.

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

RUNTIME_MARKER = 'FT8Commander v5.5 runtime policy installed'


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


def install_v55_runtime(Sequencer, QSOState, log=None):
  """Install wrappers once on the supplied Sequencer class."""
  if getattr(Sequencer, '_v55_runtime_installed', False):
    return
  Sequencer._v55_runtime_installed = True
  LOG = log or logging.getLogger('ft8ctrl')

  original_init = Sequencer.__init__
  original_best = Sequencer.best_available_candidate
  original_start = Sequencer.start_candidate
  original_clear = Sequencer.clear_current
  original_mark_engaged = Sequencer.mark_engaged
  original_log_call = Sequencer.log_call
  original_process_decode = Sequencer.process_decode
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
    # One strict recovery is enough to repair SU3YM without repeatedly fighting
    # a deliberate operator Halt Tx.
    if hasattr(self, 'attempt_rearm_max'):
      self.attempt_rearm_max = min(int(self.attempt_rearm_max), 1)
    runtime_meta(self, force=True)
    LOG.info(
      '%s: target backoff=%s TX caps=%d/profile %d/global manual idle=%.0fs',
      RUNTIME_MARKER,
      '/'.join(str(int(item)) for item in self.v55_target_policy.backoff_schedule),
      self.v55_target_policy.max_profile_tx,
      self.v55_target_policy.max_global_tx,
      self.v55_manual.idle_timeout,
    )

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
        LOG.debug('V5.5 could not hide blocked CQ %s: %s', call, err)

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
        return data
      now = time.monotonic()
      if self.v55_target_policy.should_log_block(data, _profile_key(self), now):
        LOG.info(
          'V5.5 target skipped %s on %s: %s remaining=%.0fs',
          call, _profile_key(self), reason, remaining,
        )
      hide_blocked_candidate(self, data, reason, remaining)
      if not data.get('proactive'):
        # SQLite writes are asynchronous; avoid selecting the same CQ in a loop.
        return None
    return None

  def v55_start(self, data, reason):
    if not data:
      return False
    if self.v55_manual.active:
      LOG.debug('V5.5 automatic selection suppressed during manual override')
      return False
    eligible, block_reason, remaining = self.v55_target_policy.eligible(
      data, _profile_key(self)
    )
    if not eligible:
      LOG.info(
        'V5.5 final target gate blocked %s: %s remaining=%.0fs',
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
    result = original_start(self, data, reason)
    if result:
      self.v55_target_policy.note_attempt_started(data, _profile_key(self))
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
    runtime_meta(self)
    return result

  def v55_mark_engaged(self, call, payload):
    result = original_mark_engaged(self, call, payload)
    self.v55_target_policy.note_success(call, _profile_key(self))
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
      for segment in (part.strip() for part in str(getattr(packet, 'Message', '')).split(';')):
        try:
          kind, match = self.parse_segment(segment)
        except Exception:
          continue
        if kind and match and match.get('call'):
          self.v55_target_policy.observe(
            match.get('call'), _profile_key(self), getattr(packet, 'SNR', None),
            create=False,
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
    if changed:
      runtime_meta(self, force=True)

  def v55_process_status(self, packet):
    old_frequency = int(getattr(self, 'frequency', 0) or 0)
    old_mode = getattr(self, 'v55_last_mode', None)
    old_dx = getattr(self, 'last_dx_call', None)
    old_tx_enabled = bool(getattr(self, 'tx_enabled', False))
    old_transmitting = bool(getattr(self, 'transmitting', False))
    previous_current = dict(self.current) if getattr(self, 'current', None) else None
    pending = dict(self.band_hopper.pending_switch) if self.band_hopper.pending_switch else None

    result = original_process_status(self, packet)
    now = time.monotonic()
    frequency = int(getattr(packet, 'Frequency', getattr(self, 'frequency', 0)) or 0)
    mode = getattr(packet, 'TXMode', None)
    dx_call = str(getattr(packet, 'DXCall', '') or '').upper()
    tx_enabled = bool(getattr(packet, 'TXEnabled', False))
    transmitting = bool(getattr(packet, 'Transmitting', False))

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
    if (tx_enabled or transmitting) and dx_call and dx_call != owned_call:
      enter_manual(self, f'WSJT-X manually owns {dx_call}', profile_known)
    elif previous_current and old_tx_enabled and not tx_enabled and not transmitting:
      previous_call = str(previous_current.get('call') or '').upper()
      # After the one strict automatic rearm, another Disable Tx is treated as
      # operator ownership instead of endlessly re-enabling transmission.
      if (dx_call in ('', previous_call)
          and int(getattr(self, 'current_rearm_count', 0)) >= 1):
        enter_manual(self, f'Auto Tx disabled again for {previous_call}', profile_known)

    if self.v55_manual.active and (tx_enabled or transmitting):
      self.v55_manual.activity(now)

    if (transmitting and not old_transmitting and not self.v55_manual.active
        and current and dx_call == owned_call):
      self.v55_target_policy.note_tx(owned_call, _profile_key(self), now)

    self.v55_last_frequency = frequency
    self.v55_last_mode = mode
    self.v55_last_dx_call = dx_call or old_dx
    runtime_meta(self, now)
    return result

  def v55_evaluate(self):
    if self.v55_manual.active:
      LOG.debug('V5.5 automation waits: %s (%s)', self.v55_manual.state, self.v55_manual.reason)
      return None
    return original_evaluate(self)

  def v55_maybe_hop(self, best=None, silent_only=False):
    if self.v55_manual.active:
      return False
    if (not self.band_hopper.enabled or self.state != QSOState.IDLE
        or self.transmitting or self.tx_enabled):
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
    return self.switch_band_frequency(frequency_hz, target_band, reason)

  def v55_rearm(self, reason):
    if self.v55_manual.active:
      return False
    if int(getattr(self, 'current_rearm_count', 0)) >= 1:
      LOG.warning(
        'V5.5 ATTEMPT rearm not repeated for %s; entering manual-safe release path',
        (self.current or {}).get('call'),
      )
      return False
    return original_rearm(self, reason) if original_rearm else False

  def process_command(self, now):
    path = self.v55_command_path
    try:
      payload = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
      return
    except (OSError, ValueError, TypeError) as err:
      LOG.warning('V5.5 command ignored: %s', err)
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
        LOG.warning('V5.5 cleared target policy for %s (%d records)', call, removed)
      elif action == 'resume-auto':
        profile_known = self.band_hopper.is_configured_frequency(
          getattr(self, 'frequency', 0)
        )
        mode = str(getattr(self, 'v55_last_mode', '') or '').upper()
        mode_ok = mode in {'', 'FT8', '~'}
        if not profile_known or not mode_ok:
          LOG.warning(
            'V5.5 resume refused: configured_profile=%s mode=%s; '
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
          _reset_visit_after_manual(self, now)
      elif action == 'pause-auto':
        enter_manual(self, 'paused by ft8ctrlctl', self.band_hopper.is_configured_frequency(self.frequency))
      else:
        LOG.warning('Unknown V5.5 command action: %r', action)
    finally:
      try:
        path.unlink()
      except OSError:
        pass
      runtime_meta(self, now, force=True)

  def v55_check_timeouts(self):
    now = time.monotonic()
    process_command(self, now)
    if self.v55_manual.active:
      profile_known = self.band_hopper.is_configured_frequency(getattr(self, 'frequency', 0))
      resumed = self.v55_manual.tick(
        bool(getattr(self, 'tx_enabled', False)),
        bool(getattr(self, 'transmitting', False)),
        profile_known,
        now,
      )
      if resumed:
        _reset_visit_after_manual(self, now)
      runtime_meta(self, now)
      if self.v55_manual.active:
        return None
    result = original_check_timeouts(self)
    runtime_meta(self, now)
    return result

  Sequencer.__init__ = v55_init
  Sequencer.best_available_candidate = v55_best
  Sequencer.start_candidate = v55_start
  Sequencer.clear_current = v55_clear
  Sequencer.mark_engaged = v55_mark_engaged
  Sequencer.log_call = v55_log_call
  Sequencer.process_decode = v55_process_decode
  Sequencer.process_status = v55_process_status
  Sequencer.evaluate_after_decode = v55_evaluate
  Sequencer.maybe_band_hop = v55_maybe_hop
  if original_rearm:
    Sequencer.rearm_current_attempt = v55_rearm
  Sequencer.check_timeouts = v55_check_timeouts
  Sequencer.v55_runtime_meta = runtime_meta
