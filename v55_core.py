#!/usr/bin/env python3
"""Shared adaptive policy primitives for FT8Commander v5.5.

The module is deliberately independent from WSJT-X packet classes.  It keeps
small runtime-only state under /run, so service restarts retain short-term
learning while a reboot or moving the package to another operator starts with
a clean station model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

LOG = logging.getLogger(__name__)
SCHEMA_VERSION = 1


def clamp(value: float, low: float, high: float) -> float:
  return max(low, min(high, value))


def as_bool(value: Any) -> bool:
  if isinstance(value, bool):
    return value
  if isinstance(value, str):
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}
  return bool(value)


def parse_float_list(value: Any, default: Iterable[float]) -> tuple[float, ...]:
  if value is None:
    return tuple(float(item) for item in default)
  if isinstance(value, (list, tuple)):
    parts = value
  else:
    parts = str(value).replace(';', ',').split(',')
  parsed: list[float] = []
  for item in parts:
    try:
      parsed.append(float(str(item).strip()))
    except ValueError:
      continue
  return tuple(parsed) or tuple(float(item) for item in default)


def fingerprint(my_call: str, my_grid: str, profiles: Iterable[Any]) -> str:
  canonical = {
    'schema': SCHEMA_VERSION,
    'my_call': str(my_call or '').upper(),
    'my_grid': str(my_grid or '').upper(),
    'profiles': [str(item) for item in profiles],
  }
  raw = json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode('utf-8')
  return hashlib.sha256(raw).hexdigest()[:20]


class RuntimeStateStore:
  """Atomic, throttled JSON state store intended for a tmpfs under /run."""

  def __init__(self, path: str | Path, state_fingerprint: str, save_interval: float = 5.0):
    self.path = Path(path)
    self.fingerprint = state_fingerprint
    self.save_interval = max(1.0, float(save_interval))
    self.data: dict[str, Any] = {
      'schema': SCHEMA_VERSION,
      'fingerprint': self.fingerprint,
      'updated_monotonic': 0.0,
    }
    self.dirty = False
    self.last_save = 0.0
    self._load()

  def _load(self) -> None:
    try:
      payload = json.loads(self.path.read_text(encoding='utf-8'))
    except FileNotFoundError:
      return
    except (OSError, ValueError, TypeError) as err:
      LOG.warning('V5.5 runtime state ignored: %s', err)
      return
    if not isinstance(payload, dict):
      return
    if payload.get('schema') != SCHEMA_VERSION:
      LOG.info('V5.5 runtime state schema changed; starting clean')
      return
    if payload.get('fingerprint') != self.fingerprint:
      LOG.info('V5.5 runtime state belongs to another station/profile set; starting clean')
      return
    self.data = payload

  def section(self, name: str) -> dict[str, Any]:
    value = self.data.setdefault(name, {})
    if not isinstance(value, dict):
      value = {}
      self.data[name] = value
    return value

  def replace_section(self, name: str, value: dict[str, Any]) -> None:
    self.data[name] = value
    self.mark_dirty()

  def mark_dirty(self) -> None:
    self.dirty = True

  def save(self, now: float | None = None, force: bool = False) -> bool:
    now = time.monotonic() if now is None else float(now)
    if not self.dirty and not force:
      return False
    if not force and now - self.last_save < self.save_interval:
      return False
    try:
      self.path.parent.mkdir(parents=True, exist_ok=True)
      self.data['schema'] = SCHEMA_VERSION
      self.data['fingerprint'] = self.fingerprint
      self.data['updated_monotonic'] = now
      encoded = json.dumps(self.data, sort_keys=True, separators=(',', ':')) + '\n'
      fd, temp_name = tempfile.mkstemp(
        prefix=self.path.name + '.', suffix='.tmp', dir=str(self.path.parent)
      )
      try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
          handle.write(encoded)
          handle.flush()
          os.fsync(handle.fileno())
        os.replace(temp_name, self.path)
      finally:
        try:
          os.unlink(temp_name)
        except FileNotFoundError:
          pass
    except OSError as err:
      LOG.warning('Cannot save V5.5 runtime state %s: %s', self.path, err)
      return False
    self.dirty = False
    self.last_save = now
    return True

  def clear_section(self, name: str, now: float | None = None) -> None:
    self.data.pop(name, None)
    self.mark_dirty()
    self.save(now=now, force=True)


@dataclass
class TargetRecord:
  call: str
  profile: str
  failures: int = 0
  cooldown_until: float = 0.0
  anti_pingpong_until: float = 0.0
  last_attempt_at: float = 0.0
  last_failure_at: float = 0.0
  last_success_at: float = 0.0
  snr_observations: deque = field(default_factory=deque)
  tx_times: deque = field(default_factory=deque)
  last_block_log_at: float = 0.0


class TargetPolicy:
  """Common target backoff/cap policy for Country, Any and future selectors."""

  def __init__(self, config: Any, store: RuntimeStateStore, band_median_provider=None):
    self.enabled = as_bool(getattr(config, 'target_policy_enabled', True))
    self.store = store
    self.band_median_provider = band_median_provider
    schedule = parse_float_list(
      getattr(config, 'target_backoff_schedule', '300,600,1200,1800'),
      (300, 600, 1200, 1800),
    )
    self.backoff_schedule = tuple(max(30.0, item) for item in schedule)
    self.backoff_min = max(30.0, float(getattr(config, 'target_backoff_min', 180)))
    self.backoff_max = max(self.backoff_min, float(getattr(config, 'target_backoff_max', 1800)))
    self.tx_window = max(300.0, float(getattr(config, 'target_tx_window', 3600)))
    self.max_profile_tx = max(4, int(getattr(config, 'target_tx_max_profile', 16)))
    self.max_global_tx = max(self.max_profile_tx, int(getattr(config, 'target_tx_max_global', 24)))
    self.anti_pingpong = max(0.0, float(getattr(config, 'target_anti_pingpong', 90)))
    self.min_failed_tx = max(1, int(getattr(config, 'target_min_failed_tx', 4)))
    self.history_ttl = max(self.tx_window, float(getattr(config, 'target_history_ttl', 7200)))
    hard = getattr(config, 'target_hard_selectors', 'Country')
    if isinstance(hard, (list, tuple)):
      hard_values = hard
    else:
      hard_values = str(hard).replace(';', ',').split(',')
    self.hard_selectors = {str(item).strip().lower() for item in hard_values if str(item).strip()}
    self.records: dict[tuple[str, str], TargetRecord] = {}
    self.global_tx: dict[str, deque] = defaultdict(deque)
    self.last_selected_call: str | None = None
    self.last_selected_at = 0.0
    self._load()

  @staticmethod
  def normalize_call(call: Any) -> str:
    return str(call or '').strip().upper()

  @staticmethod
  def profile_key(profile: Any) -> str:
    return str(profile if profile is not None else 'unknown')

  def _key(self, call: Any, profile: Any) -> tuple[str, str]:
    return self.normalize_call(call), self.profile_key(profile)

  def _record(self, call: Any, profile: Any, create: bool = True) -> TargetRecord | None:
    key = self._key(call, profile)
    if not key[0]:
      return None
    record = self.records.get(key)
    if record is None and create:
      record = TargetRecord(*key)
      self.records[key] = record
    return record

  @staticmethod
  def _trim(queue: deque, cutoff: float) -> None:
    while queue and queue[0] < cutoff:
      queue.popleft()

  def _prune(self, now: float) -> None:
    cutoff = now - self.tx_window
    for call, queue in list(self.global_tx.items()):
      self._trim(queue, cutoff)
      if not queue:
        self.global_tx.pop(call, None)
    for key, record in list(self.records.items()):
      self._trim(record.tx_times, cutoff)
      while record.snr_observations and now - record.snr_observations[0][0] > self.history_ttl:
        record.snr_observations.popleft()
      latest = max(
        record.last_attempt_at, record.last_failure_at, record.last_success_at,
        record.cooldown_until, record.anti_pingpong_until,
      )
      if not record.tx_times and not record.snr_observations and latest and now - latest > self.history_ttl:
        del self.records[key]

  def observe(
      self, call: Any, profile: Any, snr: Any, now: float | None = None,
      create: bool = True) -> None:
    if not self.enabled or snr is None:
      return
    now = time.monotonic() if now is None else float(now)
    record = self._record(call, profile, create=create)
    if not record:
      return
    try:
      value = float(snr)
    except (TypeError, ValueError):
      return
    record.snr_observations.append((now, value))
    while len(record.snr_observations) > 32:
      record.snr_observations.popleft()
    self._persist(now)

  def note_attempt_started(self, data: dict[str, Any], profile: Any, now: float | None = None) -> None:
    if not self.enabled or not data:
      return
    now = time.monotonic() if now is None else float(now)
    record = self._record(data.get('call'), profile)
    if not record:
      return
    record.last_attempt_at = now
    self.last_selected_call = record.call
    self.last_selected_at = now
    self.observe(record.call, profile, data.get('snr'), now)
    self._persist(now)

  def note_tx(self, call: Any, profile: Any, now: float | None = None) -> None:
    if not self.enabled:
      return
    now = time.monotonic() if now is None else float(now)
    record = self._record(call, profile)
    if not record:
      return
    record.tx_times.append(now)
    self.global_tx[record.call].append(now)
    self._prune(now)
    self._persist(now)

  def _snr_features(self, record: TargetRecord, band_median: float | None = None) -> tuple[float | None, float, float | None]:
    observations = [value for _, value in record.snr_observations]
    latest = observations[-1] if observations else None
    trend = 0.0
    if len(observations) >= 4:
      half = max(2, len(observations) // 2)
      early = sum(observations[:half]) / half
      late_values = observations[-half:]
      late = sum(late_values) / len(late_values)
      trend = late - early
    relative = None if latest is None or band_median is None else latest - band_median
    return latest, trend, relative

  def _backoff_seconds(self, record: TargetRecord) -> float:
    index = min(max(0, record.failures - 1), len(self.backoff_schedule) - 1)
    delay = self.backoff_schedule[index]
    median = None
    if self.band_median_provider:
      try:
        median = self.band_median_provider(record.profile)
      except Exception:  # defensive: policy must never stop the controller
        median = None
    latest, trend, relative = self._snr_features(record, median)

    modifier = 1.0
    if latest is not None:
      if latest >= -8:
        modifier *= 0.65
      elif latest <= -20:
        modifier *= 1.35
      elif latest <= -15:
        modifier *= 1.15
    if relative is not None:
      if relative >= 6:
        modifier *= 0.80
      elif relative <= -6:
        modifier *= 1.20
    if trend >= 3:
      modifier *= 0.70
    elif trend <= -3:
      modifier *= 1.25
    return clamp(delay * modifier, self.backoff_min, self.backoff_max)

  def note_failure(
      self, call: Any, profile: Any, tx_count: int, reason: str = '',
      now: float | None = None, engaged: bool = False) -> float:
    if not self.enabled:
      return 0.0
    now = time.monotonic() if now is None else float(now)
    record = self._record(call, profile)
    if not record:
      return 0.0
    if int(tx_count) < self.min_failed_tx and not engaged:
      return 0.0
    record.failures = min(20, record.failures + 1)
    record.last_failure_at = now
    delay = self._backoff_seconds(record)
    if engaged:
      delay = min(delay, max(self.backoff_min, 300.0))
    record.cooldown_until = max(record.cooldown_until, now + delay)
    record.anti_pingpong_until = max(record.anti_pingpong_until, now + self.anti_pingpong)
    LOG.info(
      'V5.5 target backoff %s on %s: failure=%d TX=%d delay=%.0fs reason=%s',
      record.call, record.profile, record.failures, int(tx_count), delay, reason or '?',
    )
    self._persist(now, force=True)
    return delay

  def note_interrupted(
      self, call: Any, profile: Any, reason: str = '', now: float | None = None,
      anti_pingpong: bool = True) -> None:
    if not self.enabled:
      return
    now = time.monotonic() if now is None else float(now)
    record = self._record(call, profile)
    if not record:
      return
    if anti_pingpong and self.anti_pingpong > 0:
      record.anti_pingpong_until = max(record.anti_pingpong_until, now + self.anti_pingpong)
    LOG.debug('V5.5 target interrupted %s on %s: %s', record.call, record.profile, reason)
    self._persist(now)

  def note_success(self, call: Any, profile: Any, now: float | None = None) -> None:
    if not self.enabled:
      return
    now = time.monotonic() if now is None else float(now)
    record = self._record(call, profile)
    if not record:
      return
    record.failures = 0
    record.cooldown_until = 0.0
    record.anti_pingpong_until = 0.0
    record.last_success_at = now
    LOG.info('V5.5 target success clears backoff: %s on %s', record.call, record.profile)
    self._persist(now, force=True)

  @staticmethod
  def is_direct(data: dict[str, Any] | None) -> bool:
    if not data:
      return False
    return str(data.get('source') or '').lower() == 'direct' or str(data.get('selector') or '').lower() == 'directcall'

  def is_hard_interest(self, data: dict[str, Any] | None) -> bool:
    if not data:
      return False
    if self.is_direct(data) or data.get('proactive'):
      return True
    selector = str(data.get('selector') or '').strip().lower()
    return bool(selector and selector in self.hard_selectors)

  def eligible(
      self, data: dict[str, Any] | None, profile: Any, now: float | None = None
  ) -> tuple[bool, str, float]:
    if not self.enabled or not data:
      return True, 'policy-disabled-or-empty', 0.0
    if self.is_direct(data):
      return True, 'direct-call-bypass', 0.0
    now = time.monotonic() if now is None else float(now)
    self._prune(now)
    call = self.normalize_call(data.get('call'))
    record = self._record(call, profile, create=False)
    if record is None:
      return True, 'new-target', 0.0
    if record.cooldown_until > now:
      return False, 'backoff', record.cooldown_until - now
    if record.anti_pingpong_until > now:
      return False, 'anti-ping-pong', record.anti_pingpong_until - now
    if len(record.tx_times) >= self.max_profile_tx:
      oldest = record.tx_times[0]
      return False, 'profile-TX-cap', max(0.0, oldest + self.tx_window - now)
    global_queue = self.global_tx.get(call, ())
    if len(global_queue) >= self.max_global_tx:
      oldest = global_queue[0]
      return False, 'global-TX-cap', max(0.0, oldest + self.tx_window - now)
    return True, 'eligible', 0.0

  def should_log_block(self, data: dict[str, Any], profile: Any, now: float) -> bool:
    record = self._record(data.get('call'), profile)
    if not record:
      return False
    if now - record.last_block_log_at < 30:
      return False
    record.last_block_log_at = now
    return True

  def clear_call(self, call: Any, now: float | None = None) -> int:
    call = self.normalize_call(call)
    removed = 0
    for key in list(self.records):
      if key[0] == call:
        del self.records[key]
        removed += 1
    self.global_tx.pop(call, None)
    self._persist(time.monotonic() if now is None else float(now), force=True)
    return removed

  def _load(self) -> None:
    section = self.store.section('targets')
    records = section.get('records', {})
    if not isinstance(records, dict):
      return
    for encoded_key, raw in records.items():
      if not isinstance(raw, dict) or '|' not in encoded_key:
        continue
      call, profile = encoded_key.split('|', 1)
      record = TargetRecord(call, profile)
      record.failures = int(raw.get('failures', 0))
      record.cooldown_until = float(raw.get('cooldown_until', 0.0))
      record.anti_pingpong_until = float(raw.get('anti_pingpong_until', 0.0))
      record.last_attempt_at = float(raw.get('last_attempt_at', 0.0))
      record.last_failure_at = float(raw.get('last_failure_at', 0.0))
      record.last_success_at = float(raw.get('last_success_at', 0.0))
      record.snr_observations = deque(
        (float(item[0]), float(item[1]))
        for item in raw.get('snr_observations', [])
        if isinstance(item, (list, tuple)) and len(item) == 2
      )
      record.tx_times = deque(float(item) for item in raw.get('tx_times', []))
      self.records[(call, profile)] = record
    global_tx = section.get('global_tx', {})
    if isinstance(global_tx, dict):
      for call, values in global_tx.items():
        if isinstance(values, list):
          self.global_tx[str(call)] = deque(float(item) for item in values)
    self._prune(time.monotonic())

  def _serialize(self) -> dict[str, Any]:
    return {
      'records': {
        f'{record.call}|{record.profile}': {
          'failures': record.failures,
          'cooldown_until': record.cooldown_until,
          'anti_pingpong_until': record.anti_pingpong_until,
          'last_attempt_at': record.last_attempt_at,
          'last_failure_at': record.last_failure_at,
          'last_success_at': record.last_success_at,
          'snr_observations': list(record.snr_observations),
          'tx_times': list(record.tx_times),
        }
        for record in self.records.values()
      },
      'global_tx': {call: list(values) for call, values in self.global_tx.items()},
    }

  def _persist(self, now: float, force: bool = False) -> None:
    self.store.replace_section('targets', self._serialize())
    self.store.save(now=now, force=force)


class ManualOverrideController:
  """Tracks user ownership of WSJT-X without issuing any radio commands."""

  AUTOMATIC = 'AUTOMATIC'
  MANUAL = 'MANUAL_OVERRIDE'
  POST_QSO = 'MANUAL_POST_QSO_HOLD'

  def __init__(self, config: Any):
    self.idle_timeout = max(15.0, float(getattr(config, 'manual_override_idle_timeout', 45)))
    self.post_qso_hold = max(0.0, float(getattr(config, 'manual_post_qso_hold', 120)))
    self.frequency_tolerance = max(1, int(getattr(config, 'manual_frequency_tolerance_hz', 50)))
    self.state = self.AUTOMATIC
    self.reason = ''
    self.entered_at = 0.0
    self.last_activity_at = 0.0
    self.resume_at = 0.0
    self.profile_known = True

  @property
  def active(self) -> bool:
    return self.state != self.AUTOMATIC

  def enter(self, reason: str, profile_known: bool = True, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else float(now)
    changed = self.state == self.AUTOMATIC
    self.state = self.MANUAL
    self.reason = reason
    self.entered_at = self.entered_at or now
    self.last_activity_at = now
    self.resume_at = 0.0
    self.profile_known = bool(profile_known)
    if changed:
      LOG.warning('V5.5 MANUAL_OVERRIDE entered: %s (configured_profile=%s)', reason, profile_known)
    return changed

  def activity(self, now: float | None = None) -> None:
    now = time.monotonic() if now is None else float(now)
    if self.active:
      self.last_activity_at = now

  def qso_logged(self, now: float | None = None) -> None:
    now = time.monotonic() if now is None else float(now)
    self.state = self.POST_QSO
    self.reason = 'manual QSO logged'
    self.last_activity_at = now
    self.resume_at = now + self.post_qso_hold
    LOG.info('V5.5 manual post-QSO hold started for %.0fs', self.post_qso_hold)

  def force_resume(self, now: float | None = None) -> None:
    now = time.monotonic() if now is None else float(now)
    self.state = self.AUTOMATIC
    self.reason = ''
    self.entered_at = 0.0
    self.last_activity_at = now
    self.resume_at = 0.0
    self.profile_known = True
    LOG.info('V5.5 automatic operation resumed by command')

  def tick(
      self, tx_enabled: bool, transmitting: bool, profile_known: bool,
      now: float | None = None) -> bool:
    now = time.monotonic() if now is None else float(now)
    self.profile_known = bool(profile_known)
    if self.state == self.AUTOMATIC:
      return False
    if tx_enabled or transmitting:
      self.last_activity_at = now
      return False
    if not profile_known:
      return False
    if self.state == self.POST_QSO:
      if now < self.resume_at:
        return False
    elif now - self.last_activity_at < self.idle_timeout:
      return False
    LOG.info('V5.5 MANUAL_OVERRIDE released after idle/hold; automation resumes')
    self.force_resume(now)
    return True

  def status(self, now: float | None = None) -> dict[str, Any]:
    now = time.monotonic() if now is None else float(now)
    return {
      'state': self.state,
      'reason': self.reason,
      'active': self.active,
      'profile_known': self.profile_known,
      'idle_seconds': max(0.0, now - self.last_activity_at) if self.last_activity_at else 0.0,
      'resume_in': max(0.0, self.resume_at - now),
    }
