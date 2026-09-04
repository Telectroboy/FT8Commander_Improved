#!/usr/bin/env python3
"""Adaptive exploration/exploitation band scheduler for FT8Commander v5.

The policy distinguishes a truly silent band from a busy local band and a
DX-rich opening. It remembers recently observed propagation per band, revisits
promising bands preferentially, still probes stale/unvisited bands, and never
counts a slot containing our own TX as a silent receive period.

This module never opens CAT. The caller applies returned frequencies through
its radio-control backend.
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOG = logging.getLogger(__name__)


def _clamp(value, low, high):
  return max(low, min(high, value))


def _as_bool(value):
  if isinstance(value, bool):
    return value
  if isinstance(value, str):
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}
  return bool(value)


def _slot_key(value, period_seconds=15):
  """Return a UTC FT8 slot key from a naive datetime."""
  seconds = value.hour * 3600 + value.minute * 60 + value.second
  return value.toordinal() * (86400 // period_seconds) + seconds // period_seconds


@dataclass
class StationSample:
  seen_at: float
  call: str
  snr: float | None
  distance: float | None
  continent: str | None


@dataclass
class BandState:
  band: int
  entered_at: float = 0.0
  last_left_at: float = 0.0
  cooldown_until: float = 0.0
  silent_cycles: int = 0
  last_finalized_slot: int | None = None
  decode_counts: dict[int, int] = field(default_factory=lambda: defaultdict(int))
  ignored_slots: set[int] = field(default_factory=set)
  samples: deque = field(default_factory=deque)
  last_decode_at: float = 0.0
  visits: int = 0

  # Persisted propagation memory. Fresh per-visit samples may be cleared on a
  # QSY, but this snapshot survives and decays gradually.
  memory_score: float = 0.0
  memory_profile: str = 'UNKNOWN'
  memory_at: float = 0.0
  memory_unique: int = 0
  memory_dx_ratio: float = 0.0

  last_status_log_at: float = 0.0
  visit_profile: str = 'UNKNOWN'


@dataclass
class TargetTrack:
  call: str
  band: int
  started_at: float
  last_attempt_at: float
  observations: deque = field(default_factory=deque)
  peer_home: int = 0
  peer_other: int = 0
  peer_unknown: int = 0
  working_other: int = 0
  total_tx: int = 0
  bursts: int = 0


class ProactiveDecodeGuard:
  """Require repeated non-CQ decodes before a proactive automatic call.

  Explicit CQ and direct calls to us are trusted immediately.  Other messages
  (a station working somebody else, RRR/RR73/73, etc.) must be seen in
  distinct FT8 slots several times inside a short window.  This reduces the
  cost of plausible-looking false decodes without delaying real CQs.
  """

  def __init__(self, enabled=True, required=2, window=90.0):
    self.enabled = _as_bool(enabled)
    self.required = max(1, int(required))
    self.window = max(15.0, float(window))
    self.pending = {}

  def _prune(self, now):
    for key, entry in list(self.pending.items()):
      if now - entry['last_seen'] > self.window:
        del self.pending[key]

  def confirm(self, call, band, trigger, packet_time=None, now=None):
    now = time.monotonic() if now is None else now
    trigger = str(trigger or '').upper()
    call = str(call or '').upper()
    if not self.enabled or self.required <= 1 or trigger in {'CQ', 'DIRECT'}:
      return True, self.required, self.required
    if not call or not band:
      return False, 0, self.required

    self._prune(now)
    key = (int(band), call)
    entry = self.pending.get(key)
    if entry is None or now - entry['first_seen'] > self.window:
      entry = {'first_seen': now, 'last_seen': now, 'slots': set()}
      self.pending[key] = entry
    entry['last_seen'] = now

    if isinstance(packet_time, datetime):
      slot = _slot_key(packet_time)
    else:
      slot = int(now // 15)
    entry['slots'].add(slot)
    count = len(entry['slots'])
    if count >= self.required:
      del self.pending[key]
      return True, count, self.required
    return False, count, self.required

  def clear_band(self, band):
    band = int(band) if band else None
    for key in list(self.pending):
      if band is None or key[0] == band:
        del self.pending[key]


class BandHopper:
  """Adaptive hopping policy and short-term DX success estimation."""

  def __init__(self, config, my_continent='EU'):
    self.enabled = _as_bool(getattr(config, 'band_hopping', False))
    self.my_continent = (my_continent or '').upper()

    self.silent_limit = max(1, int(getattr(config, 'band_hop_silent_cycles', 2)))
    self.closed_cooldown = float(getattr(config, 'band_hop_closed_cooldown', 1800))
    self.normal_cooldown = float(getattr(config, 'band_hop_normal_cooldown', 180))

    # V5 dwell policy. A busy/local band is deliberately much shorter than a
    # DX-rich one; number of raw decodes alone cannot stretch the dwell.
    self.local_dwell = float(getattr(config, 'band_hop_local_dwell', 150))
    self.active_dwell = float(getattr(config, 'band_hop_active_dwell', 210))
    self.mixed_dwell = float(getattr(config, 'band_hop_mixed_dwell', 300))

    # V5.4: ACTIVE_DX dwell is score-driven rather than almost flat.
    # band_hop_dx_dwell is the minimum dwell at/below dx_score_floor. A truly
    # exceptional opening reaches max_active_dwell only at/above dx_score_full.
    self.dx_dwell = float(getattr(config, 'band_hop_dx_dwell', 240))
    self.max_active_dwell = float(getattr(config, 'band_hop_max_active_dwell', 480))
    self.dx_score_floor = float(getattr(config, 'band_hop_dx_score_floor', 40))
    self.dx_score_full = max(
      self.dx_score_floor + 1.0,
      float(getattr(config, 'band_hop_dx_score_full', 90)),
    )

    # V5.3 sparse/thin-opening policy. A high DX percentage with only a
    # handful of unique stations is not equivalent to a broad opening.
    # Likewise, one intermittent decode must not keep an almost-dead band
    # alive forever by continually resetting the two-silent-slot rule.
    self.sparse_min_age = max(30.0, float(
      getattr(config, 'band_hop_sparse_min_age', 90)
    ))
    self.sparse_unique = max(1, int(
      getattr(config, 'band_hop_sparse_unique', 3)
    ))
    self.sparse_dwell = max(30.0, float(
      getattr(config, 'band_hop_sparse_dwell', 90)
    ))
    self.sparse_cooldown = max(60.0, float(
      getattr(config, 'band_hop_sparse_cooldown', 600)
    ))
    self.thin_dx_min_age = max(self.sparse_min_age, float(
      getattr(config, 'band_hop_thin_dx_min_age', 120)
    ))
    self.thin_dx_unique = max(self.sparse_unique + 1, int(
      getattr(config, 'band_hop_thin_dx_unique', 12)
    ))
    self.thin_dx_dwell = max(self.sparse_dwell, float(
      getattr(config, 'band_hop_thin_dx_dwell', 180)
    ))
    self.weak_dx_snr = float(getattr(config, 'band_hop_weak_dx_snr', -12))
    self.weak_thin_dx_dwell = max(self.sparse_dwell, float(
      getattr(config, 'band_hop_weak_thin_dx_dwell', 150)
    ))
    self.thin_dx_memory_scale = _clamp(float(
      getattr(config, 'band_hop_thin_dx_memory_scale', 0.60)
    ), 0.1, 1.0)

    self.attempt_hold = float(getattr(config, 'band_hop_attempt_hold', 300))
    self.post_qso_hold = float(getattr(config, 'band_hop_post_qso_hold', 120))
    self.history_window = float(getattr(config, 'band_hop_history_window', 300))
    self.interesting_freshness = float(
      getattr(config, 'band_hop_interesting_freshness', 60)
    )
    self.switch_timeout = float(getattr(config, 'band_hop_switch_timeout', 15))

    # Exploration/exploitation memory. Observations dominate the weak time-of-
    # day prior. A DX-rich band can therefore stay attractive at an unusual hour.
    self.memory_half_life = max(
      60.0, float(getattr(config, 'band_hop_memory_half_life', 1200))
    )
    self.reprobe_interval = max(
      60.0, float(getattr(config, 'band_hop_reprobe_interval', 600))
    )
    self.time_prior_weight = float(getattr(config, 'band_hop_time_prior_weight', 6))
    self.complete_sweep_first = _as_bool(
      getattr(config, 'band_hop_complete_sweep_first', True)
    )
    # During the initial coverage sweep, do not spend the full exploitation
    # dwell on a good band.  We first map the whole spectrum quickly, then
    # return to the best observed openings using propagation memory.
    self.sweep_max_dwell = max(30.0, float(
      getattr(config, 'band_hop_sweep_max_dwell', 120)
    ))
    self.timezone_name = str(getattr(config, 'band_hop_timezone', 'Europe/Paris'))
    try:
      self.timezone = ZoneInfo(self.timezone_name)
    except ZoneInfoNotFoundError:
      LOG.warning('Unknown band_hop_timezone %r; using system local time', self.timezone_name)
      self.timezone = None

    self.target_eval_after = float(getattr(config, 'dx_success_eval_after', 300))
    self.target_abort_score = float(getattr(config, 'dx_success_abort_score', 28))
    self.target_memory = max(
      self.target_eval_after,
      float(getattr(config, 'dx_success_target_memory', 1800)),
    )

    self.frequencies = self._parse_frequencies(
      getattr(config, 'band_hop_frequencies', [])
    )
    self.order = list(self.frequencies)
    if self.enabled and len(self.order) < 2:
      LOG.warning(
        'Band hopping enabled but fewer than two band_hop_frequencies are configured; disabling'
      )
      self.enabled = False

    self.states = {band: BandState(band) for band in self.order}
    self.current_band = None
    self.current_config = None
    self.pending_switch = None
    self.attempt_lock_until = 0.0
    self.qso_completed_at = None

    self.target = None
    self.target_history = {}
    self.last_qso_call = None
    self.last_qso_mark = 0.0

  @staticmethod
  def _parse_frequencies(raw):
    """Accept ['40=7074000', ...] or a plain mapping of band -> Hz."""
    frequencies = {}
    if isinstance(raw, dict):
      items = raw.items()
    else:
      items = []
      for item in raw or []:
        if isinstance(item, str) and '=' in item:
          band, frequency = item.split('=', 1)
          items.append((band.strip(), frequency.strip()))
        elif isinstance(item, dict):
          items.extend(item.items())

    for band, frequency in items:
      try:
        band = int(str(band).strip().lower().rstrip('m'))
        frequency = int(str(frequency).strip())
      except ValueError:
        LOG.warning(
          'Ignoring invalid band_hop_frequencies entry: %r=%r', band, frequency
        )
        continue
      if band > 0 and frequency > 0:
        frequencies[band] = frequency
    return frequencies

  def _state(self, band=None):
    band = self.current_band if band is None else band
    if band is None:
      return None
    return self.states.setdefault(int(band), BandState(int(band)))

  # ----------------------------------------------------------------------
  # Band observations and propagation memory
  # ----------------------------------------------------------------------

  def on_band_change(self, band, config_name=None, now=None, now_utc=None):
    if not self.enabled or not band:
      return
    now = time.monotonic() if now is None else now
    band = int(band)
    old_band = self.current_band

    if old_band == band:
      self.current_config = config_name or self.current_config
      if not self._state().entered_at:
        self._state().entered_at = now
      return

    if old_band is not None:
      old = self._state(old_band)
      self._snapshot_memory(old_band, now)
      old.last_left_at = now
      reason = None
      if self.pending_switch and self.pending_switch['from_band'] == old_band:
        reason = self.pending_switch['reason']
      if reason and 'silent' in reason:
        cooldown = self.closed_cooldown
      elif reason and 'SPARSE' in reason.upper():
        cooldown = self.sparse_cooldown
      else:
        cooldown = self.normal_cooldown
      old.cooldown_until = max(old.cooldown_until, now + cooldown)

    self.current_band = band
    self.current_config = config_name
    state = self._state(band)
    state.entered_at = now
    state.visits += 1
    state.silent_cycles = 0
    state.last_finalized_slot = (
      _slot_key(now_utc) if isinstance(now_utc, datetime) else None
    )
    state.decode_counts.clear()
    state.ignored_slots.clear()
    # Current-visit profile must be based on what we hear after this QSY. The
    # previous visit is retained separately in memory_*.
    state.samples.clear()
    state.last_decode_at = 0.0
    state.last_status_log_at = 0.0
    state.visit_profile = 'UNKNOWN'

    if self.pending_switch:
      expected = self.pending_switch['to_band']
      if expected == band:
        LOG.info(
          'Band hop confirmed: %sm -> %sm (%s)',
          self.pending_switch['from_band'], band, self.pending_switch['reason']
        )
      else:
        LOG.warning('Band changed to %sm while hopper expected %sm', band, expected)
      self.pending_switch = None

    self.qso_completed_at = None
    self.target = None

  def record_decode(self, packet_time, snr=None, now=None):
    """Count every valid WSJT-X Decode packet, parsed or not.

    Raw decode count is used only to distinguish RF silence from activity. It is
    NOT used as the main DX score, so a crowded local band does not look better
    merely because the same stations generate many lines.
    """
    del snr
    if not self.enabled or self.current_band is None:
      return
    now = time.monotonic() if now is None else now
    state = self._state()
    try:
      slot = _slot_key(packet_time)
    except (AttributeError, TypeError):
      slot = _slot_key(datetime.utcnow())
    state.decode_counts[slot] += 1
    state.last_decode_at = now
    for key in list(state.decode_counts):
      if key < slot - 12:
        del state.decode_counts[key]
    for key in list(state.ignored_slots):
      if key < slot - 12:
        state.ignored_slots.discard(key)

  def record_station(self, call, snr, info, now=None):
    if not self.enabled or self.current_band is None or not call:
      return
    now = time.monotonic() if now is None else now
    state = self._state()
    sample = StationSample(
      now,
      str(call).upper(),
      float(snr) if snr is not None else None,
      float(info['distance']) if info and info.get('distance') is not None else None,
      (info.get('continent') or '').upper() if info else None,
    )
    state.samples.append(sample)
    self._prune_samples(state, now)

    if self.target and sample.call == self.target.call:
      self.target.observations.append((now, sample.snr))
      while self.target.observations and now - self.target.observations[0][0] > self.target_memory:
        self.target.observations.popleft()

  def note_tx_period(self, now_utc=None):
    """Mark the current FT8 slot as unusable for silence detection."""
    if not self.enabled or self.current_band is None:
      return
    now_utc = datetime.utcnow() if now_utc is None else now_utc
    slot = _slot_key(now_utc)
    state = self._state()
    state.ignored_slots.add(slot)
    # A TX slot carries no valid receive evidence. If it was tentatively counted
    # by an odd WSJT-X edge, remove it from raw activity accounting too.
    state.decode_counts.pop(slot, None)
    LOG.debug('Band %sm FT8 slot marked TX; excluded from silence detection', self.current_band)

  def record_target_peer(self, target_call, peer_info, now=None):
    del now
    if not self.enabled or not self.target:
      return
    if str(target_call).upper() != self.target.call:
      return
    self.target.working_other += 1
    if not peer_info:
      self.target.peer_unknown += 1
      return
    continent = (peer_info.get('continent') or '').upper()
    distance = peer_info.get('distance')
    if continent and continent == self.my_continent:
      self.target.peer_home += 1
    elif distance is not None and float(distance) <= 2500:
      self.target.peer_home += 1
    elif continent or distance is not None:
      self.target.peer_other += 1
    else:
      self.target.peer_unknown += 1

  def _finalize_slot(self, slot, now=None):
    state = self._state()
    if state.last_finalized_slot is not None and slot <= state.last_finalized_slot:
      return None
    state.last_finalized_slot = slot

    if slot in state.ignored_slots:
      state.ignored_slots.discard(slot)
      LOG.debug(
        'Band %sm FT8 period: TX slot ignored, silent=%d/%d',
        self.current_band, state.silent_cycles, self.silent_limit,
      )
      return -1

    count = state.decode_counts.get(slot, 0)
    if count:
      state.silent_cycles = 0
    else:
      state.silent_cycles += 1

    now = time.monotonic() if now is None else now
    profile = self.propagation(self.current_band, now)
    self._remember_profile(state, profile, now)
    ratio_pct = profile['dx_ratio'] * 100.0
    LOG.debug(
      'Band %sm FT8 period: %d decodes, unique=%d, %s score=%.1f '
      'DX=%d/%d (%.0f%%), medianDX=%s, silent=%d/%d',
      self.current_band, count, profile['stations'], profile['profile'],
      profile['score'], profile['dx'], profile['stations'], ratio_pct,
      '?' if profile.get('median_dx_snr') is None else f"{profile['median_dx_snr']:.1f}",
      state.silent_cycles, self.silent_limit,
    )
    return count

  def complete_period(self, now_utc=None):
    """Finalize the receive period reported complete by WSJT-X."""
    if not self.enabled or self.current_band is None:
      return None
    now_utc = datetime.utcnow() if now_utc is None else now_utc
    slot = _slot_key(now_utc)
    if now_utc.second % 15 < 7:
      slot -= 1
    return self._finalize_slot(slot)

  def poll_period(self, now_utc=None):
    """Finalize a fully elapsed slot when Improved emits no Decoding edge."""
    if not self.enabled or self.current_band is None:
      return None
    now_utc = datetime.utcnow() if now_utc is None else now_utc
    current_slot = _slot_key(now_utc)
    state = self._state()
    if state.last_finalized_slot is None:
      state.last_finalized_slot = current_slot
      return None
    previous_slot = current_slot - 1
    if previous_slot <= state.last_finalized_slot:
      return None
    return self._finalize_slot(previous_slot)

  def _prune_samples(self, state, now):
    while state.samples and now - state.samples[0].seen_at > self.history_window:
      state.samples.popleft()

  def propagation(self, band=None, now=None):
    """Return a propagation profile based on UNIQUE transmitting stations.

    Different-continent stations are DX even if a bad/missing locator makes the
    distance misleading. Repeated lines from one callsign count once.
    """
    now = time.monotonic() if now is None else now
    state = self._state(band)
    if not state:
      return self._empty_profile('UNKNOWN')
    self._prune_samples(state, now)

    latest = {}
    for sample in state.samples:
      latest[sample.call] = sample
    samples = list(latest.values())

    recent_raw_activity = bool(
      state.last_decode_at and now - state.last_decode_at <= self.history_window
    )
    if not samples:
      if state.silent_cycles >= self.silent_limit:
        return self._empty_profile('CLOSED')
      if recent_raw_activity:
        result = self._empty_profile('ACTIVE')
        result['score'] = 8.0
        return result
      return self._empty_profile('UNKNOWN')

    local = []
    mid = []
    dx = []
    far_dx = []
    unknown = []

    for sample in samples:
      foreign_continent = bool(
        sample.continent and self.my_continent and sample.continent != self.my_continent
      )
      distance = sample.distance
      if foreign_continent or (distance is not None and distance >= 3000):
        dx.append(sample)
        if distance is not None and distance >= 6000:
          far_dx.append(sample)
      elif distance is not None and distance < 1500:
        local.append(sample)
      elif distance is not None:
        mid.append(sample)
      else:
        unknown.append(sample)

    total = len(samples)
    dx_ratio = len(dx) / total
    far_ratio = len(far_dx) / total
    local_ratio = len(local) / total
    mid_ratio = len(mid) / total

    # Activity is useful, but deliberately capped low. DX proportion and
    # geographic diversity dominate so a packed EU 40m band stays LOW score.
    activity = min(12.0, math.sqrt(total) * 2.2)
    ratio_score = dx_ratio * 48.0
    far_score = far_ratio * 18.0
    dx_count_score = min(8.0, len(dx) * 1.6)

    snrs = [s.snr for s in dx if s.snr is not None]
    median_dx_snr = statistics.median(snrs) if snrs else None
    snr_bonus = 0.0
    if median_dx_snr is not None:
      snr_bonus = _clamp((median_dx_snr + 24.0) / 24.0 * 8.0, 0.0, 8.0)

    foreign_continents = {
      s.continent for s in dx
      if s.continent and s.continent != self.my_continent
    }
    diversity = min(9.0, len(foreign_continents) * 3.0)
    local_penalty = local_ratio * 12.0

    score = round(_clamp(
      activity + ratio_score + far_score + dx_count_score
      + snr_bonus + diversity - local_penalty,
      0.0, 100.0,
    ), 1)

    # Sparse/thin-opening classification comes before the normal DX-ratio
    # hysteresis.  It is deliberately gated by visit age so a freshly entered
    # band gets enough time to build a representative sample.
    state = self._state(band)
    previous = state.visit_profile if state else 'UNKNOWN'
    visit_age = max(0.0, now - state.entered_at) if state else 0.0
    weak_dx = bool(
      median_dx_snr is not None and median_dx_snr <= self.weak_dx_snr
    )

    if visit_age >= self.sparse_min_age and total <= self.sparse_unique:
      profile = 'SPARSE'
    elif (visit_age >= self.thin_dx_min_age
          and total <= self.thin_dx_unique
          and len(dx) >= 2
          and dx_ratio >= 0.35):
      profile = 'SPARSE_DX'
    # Hysteresis prevents the ordinary profile from flipping every FT8 period
    # around a threshold (e.g. 11% <-> 13% DX). Enter MIXED at 15%, leave it
    # only below 8%; enter DX at 35%, leave it only below 22%.
    elif previous == 'ACTIVE_DX':
      if len(dx) >= 2 and dx_ratio >= 0.22:
        profile = 'ACTIVE_DX'
      elif len(dx) >= 2 and dx_ratio >= 0.08:
        profile = 'ACTIVE_MIXED'
      elif local_ratio >= 0.55:
        profile = 'ACTIVE_LOCAL'
      else:
        profile = 'ACTIVE'
    elif previous == 'ACTIVE_MIXED':
      if len(dx) >= 3 and dx_ratio >= 0.35:
        profile = 'ACTIVE_DX'
      elif len(dx) >= 2 and dx_ratio >= 0.08:
        profile = 'ACTIVE_MIXED'
      elif local_ratio >= 0.55 or (len(dx) <= 1 and dx_ratio < 0.08):
        profile = 'ACTIVE_LOCAL'
      else:
        profile = 'ACTIVE'
    else:
      if len(dx) >= 3 and dx_ratio >= 0.35:
        profile = 'ACTIVE_DX'
      elif len(dx) >= 2 and dx_ratio >= 0.15:
        profile = 'ACTIVE_MIXED'
      elif local_ratio >= 0.55 or (len(dx) <= 1 and dx_ratio < 0.08):
        profile = 'ACTIVE_LOCAL'
      else:
        profile = 'ACTIVE'
    if state:
      state.visit_profile = profile

    return {
      'profile': profile,
      'score': score,
      'stations': total,
      'dx': len(dx),
      'far_dx': len(far_dx),
      'local': len(local),
      'mid': len(mid),
      'unknown': len(unknown),
      'dx_ratio': dx_ratio,
      'far_ratio': far_ratio,
      'local_ratio': local_ratio,
      'mid_ratio': mid_ratio,
      'median_dx_snr': median_dx_snr,
      'weak_dx': weak_dx,
      'visit_age': visit_age,
    }

  @staticmethod
  def _empty_profile(profile):
    return {
      'profile': profile,
      'score': 0.0,
      'stations': 0,
      'dx': 0,
      'far_dx': 0,
      'local': 0,
      'mid': 0,
      'unknown': 0,
      'dx_ratio': 0.0,
      'far_ratio': 0.0,
      'local_ratio': 0.0,
      'mid_ratio': 0.0,
      'median_dx_snr': None,
      'weak_dx': False,
      'visit_age': 0.0,
    }

  def _remember_profile(self, state, profile, now):
    # Do not overwrite useful memory with UNKNOWN during a partial/empty visit.
    if profile['profile'] == 'UNKNOWN':
      return
    memory_score = float(profile['score'])
    if profile['profile'] == 'SPARSE_DX':
      memory_score *= self.thin_dx_memory_scale
    elif profile['profile'] == 'SPARSE':
      memory_score *= 0.35
    state.memory_score = memory_score
    state.memory_profile = profile['profile']
    state.memory_at = now
    state.memory_unique = int(profile['stations'])
    state.memory_dx_ratio = float(profile['dx_ratio'])

  def _snapshot_memory(self, band, now):
    state = self._state(band)
    if not state:
      return
    profile = self.propagation(band, now)
    self._remember_profile(state, profile, now)

  def memory_score(self, band, now=None):
    now = time.monotonic() if now is None else now
    state = self._state(band)
    if not state or not state.memory_at:
      return 0.0
    age = max(0.0, now - state.memory_at)
    decay = 0.5 ** (age / self.memory_half_life)
    return state.memory_score * decay

  # ----------------------------------------------------------------------
  # QSO target success model
  # ----------------------------------------------------------------------

  def _prune_target_history(self, now):
    for call, track in list(self.target_history.items()):
      if now - track.last_attempt_at > self.target_memory:
        del self.target_history[call]

  def note_attempt_started(self, data, now=None):
    if not self.enabled or not data:
      return
    now = time.monotonic() if now is None else now
    call = str(data.get('call') or '').upper()
    if not call:
      return

    self.qso_completed_at = None
    state = self._state()
    if state:
      state.silent_cycles = 0

    self._prune_target_history(now)
    track = self.target_history.get(call)
    if (not track or track.band != self.current_band
        or now - track.last_attempt_at > self.target_memory):
      track = TargetTrack(
        call=call,
        band=int(self.current_band or 0),
        started_at=now,
        last_attempt_at=now,
      )
      self.target_history[call] = track
    else:
      track.last_attempt_at = now
    track.bursts += 1
    self.target = track

    # The five-minute hop lock belongs to a pursuit session, not to every
    # four-TX burst. Repeated bursts for the same remembered target therefore
    # do not push the lock five minutes into the future forever. A genuinely
    # new target can extend the global band lock from its own first attempt.
    session_lock_until = track.started_at + self.attempt_hold
    self.attempt_lock_until = max(self.attempt_lock_until, session_lock_until)

    if data.get('snr') is not None:
      track.observations.append((now, float(data['snr'])))
    LOG.debug(
      'Band hopping lock for %s: burst=%d cumulative_age=%.0fs lock_remaining=%.0fs',
      call, track.bursts, now - track.started_at,
      max(0.0, self.attempt_lock_until - now),
    )

  def note_target_tx(self, call=None):
    if not self.enabled or not self.target:
      return
    if call and self.target.call != str(call).upper():
      return
    self.target.total_tx += 1

  def note_attempt_abandoned(self, call=None):
    if not self.enabled:
      return
    if call and self.target and self.target.call != str(call).upper():
      return
    self.target = None

  def note_qso_completed(self, call=None, now=None):
    if not self.enabled:
      return
    now = time.monotonic() if now is None else now
    call = str(call or '').upper()
    if call and call == self.last_qso_call and now - self.last_qso_mark < 30:
      return
    self.last_qso_call = call or self.last_qso_call
    self.last_qso_mark = now
    self.qso_completed_at = now
    self.attempt_lock_until = 0.0
    if call:
      self.target_history.pop(call, None)
    self.target = None
    state = self._state()
    if state:
      state.silent_cycles = 0
      state.last_finalized_slot = _slot_key(datetime.utcnow())
    LOG.debug('Post-QSO band hold started for %.0fs', self.post_qso_hold)

  def candidate_is_recent(self, data, now=None):
    if not data:
      return False
    now = time.monotonic() if now is None else now
    queued_at = data.get('queued_at')
    if queued_at is not None:
      return now - float(queued_at) <= self.interesting_freshness
    decoded_at = data.get('time')
    if isinstance(decoded_at, datetime):
      age = abs((datetime.utcnow() - decoded_at).total_seconds())
      return age <= self.interesting_freshness
    return False

  def target_success_score(self, data, tx_attempts=0, now=None):
    """Return a 0..100 heuristic for short-term QSO success, not probability."""
    now = time.monotonic() if now is None else now
    if not self.target or not data:
      return None
    if self.target.call != str(data.get('call') or '').upper():
      return None

    observations = [(t, s) for t, s in self.target.observations if s is not None]
    latest_snr = observations[-1][1] if observations else data.get('snr')
    if latest_snr is None:
      latest_snr = -18.0
    latest_snr = float(latest_snr)

    score = 10.0 + (latest_snr + 24.0) * 3.3

    if len(observations) >= 4:
      half = max(2, len(observations) // 2)
      early = statistics.mean(s for _, s in observations[:half])
      late = statistics.mean(s for _, s in observations[-half:])
      score += _clamp((late - early) * 3.0, -18.0, 18.0)

    elapsed = max(0.0, now - self.target.started_at)
    if elapsed > self.target_eval_after:
      score -= min(38.0, (elapsed - self.target_eval_after) / 60.0 * 3.0)

    cumulative_tx = max(int(tx_attempts), self.target.total_tx)
    score -= max(0, cumulative_tx - 4) * 1.2

    known_peers = self.target.peer_home + self.target.peer_other
    if known_peers >= 2:
      home_ratio = self.target.peer_home / known_peers
      score += (home_ratio - 0.5) * 20.0

    if self.target.working_other >= 3:
      score -= min(10.0, (self.target.working_other - 2) * 1.5)

    return round(_clamp(score, 0.0, 100.0), 1)

  def should_abandon_target(self, data, is_dx, tx_attempts=0, now=None):
    if not self.enabled or not is_dx or not self.target:
      return (False, None)
    now = time.monotonic() if now is None else now
    if now - self.target.started_at < self.target_eval_after:
      return (False, self.target_success_score(data, tx_attempts, now))
    score = self.target_success_score(data, tx_attempts, now)
    return (score is not None and score < self.target_abort_score, score)

  # ----------------------------------------------------------------------
  # Exploration / exploitation scheduler
  # ----------------------------------------------------------------------

  def _switch_pending(self, now):
    if not self.pending_switch:
      return False
    if now <= self.pending_switch['deadline']:
      return True
    LOG.warning(
      'Band hop to %sm (%d Hz) not confirmed within %.0fs',
      self.pending_switch['to_band'], self.pending_switch['frequency'], self.switch_timeout
    )
    self.pending_switch = None
    return False

  @staticmethod
  def _time_prior(band, hour):
    """Weak local-time propagation prior in the range roughly -1..+1.

    This is intentionally weak. Actual recent decodes outweigh it quickly.
    """
    hour = int(hour) % 24
    if band in (10, 12):
      if 8 <= hour < 19:
        return 1.0
      if hour >= 22 or hour < 6:
        return -0.7
      return 0.1
    if band in (15, 17):
      if 7 <= hour < 21:
        return 0.7
      if hour < 5:
        return -0.3
      return 0.1
    if band == 20:
      return 0.4 if 6 <= hour < 23 else 0.1
    if band in (30, 40):
      return 0.6 if hour >= 18 or hour < 8 else -0.1
    return 0.0

  def _candidate_utility(self, band, now, hour):
    state = self._state(band)
    memory = self.memory_score(band, now)

    if state.visits == 0:
      exploration = 42.0
      age = float('inf')
    else:
      age = max(0.0, now - state.last_left_at)
      # Overdue bands gradually earn another probe. This prevents a once-good
      # band from starving the rest of the spectrum forever.
      exploration = min(36.0, age / self.reprobe_interval * 24.0)

    observed = memory * 0.78
    time_bonus = self._time_prior(band, hour) * self.time_prior_weight

    # Tiny stable tie-breaker in configured order; it is not a round-robin rule.
    rotation = max(0.0, 2.0 - self.order.index(band) * 0.15)
    utility = observed + exploration + time_bonus + rotation
    return utility, memory, exploration, time_bonus, age

  def _coverage_unvisited(self, now):
    """Return eligible never-visited bands for the current coverage sweep."""
    if not self.complete_sweep_first:
      return []
    return [
      band for band in self.order
      if band != self.current_band
      and self._state(band).visits == 0
      and self._state(band).cooldown_until <= now
    ]

  def _next_band(self, now, now_local=None):
    if self.current_band not in self.order:
      return None
    if now_local is not None:
      hour = now_local.hour
    elif self.timezone is not None:
      hour = datetime.now(self.timezone).hour
    else:
      hour = datetime.now().hour
    eligible = []
    for band in self.order:
      if band == self.current_band:
        continue
      state = self._state(band)
      if state.cooldown_until > now:
        continue
      eligible.append(band)

    if not eligible:
      return None

    # Coverage phase: while an eligible band has never been sampled by this
    # process, a remembered band may NOT jump ahead of it.  This guarantees a
    # complete sweep before exploitation starts.  Silent bands already sampled
    # can remain in cooldown and do not block coverage of the others.
    unvisited = self._coverage_unvisited(now)
    coverage = bool(unvisited)
    pool = unvisited if coverage else eligible

    ranked = []
    for band in pool:
      utility, memory, explore, prior, age = self._candidate_utility(band, now, hour)
      if coverage:
        # Memory is necessarily zero for an unvisited band.  Keep the weak
        # time prior/tie breaker only to choose the order of the initial sweep.
        utility = explore + prior + max(0.0, 2.0 - self.order.index(band) * 0.15)
      ranked.append((utility, band, memory, explore, prior, age))

    ranked.sort(reverse=True)
    utility, band, memory, explore, prior, age = ranked[0]
    age_text = 'never' if math.isinf(age) else f'{age:.0f}s'
    if coverage:
      LOG.debug(
        'Band scheduler coverage chose %sm utility=%.1f explore=%.1f time=%.1f age=%s',
        band, utility, explore, prior, age_text,
      )
    else:
      LOG.debug(
        'Band scheduler chose %sm utility=%.1f memory=%.1f explore=%.1f time=%.1f age=%s',
        band, utility, memory, explore, prior, age_text,
      )
    return band

  def _dwell_for_profile(self, profile):
    name = profile['profile']
    score = float(profile['score'])
    if name == 'SPARSE':
      return self.sparse_dwell
    if name == 'SPARSE_DX':
      if profile.get('weak_dx'):
        return self.weak_thin_dx_dwell
      return self.thin_dx_dwell
    if name == 'ACTIVE_LOCAL':
      # 2.5 to ~3.5 min; local crowding never earns a long dwell by volume.
      return min(self.max_active_dwell, self.local_dwell + score * 1.2)
    if name == 'ACTIVE_MIXED':
      return min(self.max_active_dwell, self.mixed_dwell + score * 0.8)
    if name == 'ACTIVE_DX':
      # V5.4: use the numerical propagation score, not only the categorical
      # ACTIVE_DX label. Hysteresis may keep a band labelled ACTIVE_DX after
      # its DX ratio falls, so the score controls how much radio time it earns.
      span = max(0.0, self.max_active_dwell - self.dx_dwell)
      normalized = (score - self.dx_score_floor) / (self.dx_score_full - self.dx_score_floor)
      return self.dx_dwell + span * _clamp(normalized, 0.0, 1.0)
    if name == 'ACTIVE':
      return min(self.max_active_dwell, self.active_dwell + score * 0.8)
    return self.active_dwell

  def cancel_pending_switch(self):
    self.pending_switch = None

  def decision(self, interesting=False, silent_only=False, now=None, now_local=None):
    """Return (band, dial_frequency_hz, reason) or None."""
    if not self.enabled or self.current_band is None:
      return None
    now = time.monotonic() if now is None else now
    if self._switch_pending(now):
      return None
    if self.current_band not in self.frequencies:
      return None

    if self.qso_completed_at is None and now < self.attempt_lock_until:
      return None

    state = self._state()
    reason = None

    if self.qso_completed_at is not None:
      if now - self.qso_completed_at < self.post_qso_hold or interesting:
        return None
      reason = (
        'post-qso silent' if state.silent_cycles >= self.silent_limit
        else 'post-qso idle'
      )
    elif state.silent_cycles >= self.silent_limit:
      reason = 'silent'
    elif silent_only:
      return None
    elif interesting:
      return None
    else:
      profile = self.propagation(self.current_band, now)
      if profile['profile'] in {'UNKNOWN', 'CLOSED'}:
        return None
      dwell = self._dwell_for_profile(profile)
      coverage_unvisited = self._coverage_unvisited(now)
      sweep_mode = bool(coverage_unvisited)
      if sweep_mode:
        dwell = min(dwell, self.sweep_max_dwell)
      elapsed = now - state.entered_at
      if elapsed < dwell:
        if now - state.last_status_log_at >= 60:
          state.last_status_log_at = now
          LOG.debug(
            'Band %sm hold: %s score=%.1f unique=%d DX=%d/%d (%.0f%%) '
            'medianDX=%s elapsed=%.0fs dwell=%.0fs%s',
            self.current_band, profile['profile'], profile['score'],
            profile['stations'], profile['dx'], profile['stations'],
            profile['dx_ratio'] * 100.0,
            '?' if profile.get('median_dx_snr') is None else f"{profile['median_dx_snr']:.1f}",
            elapsed, dwell,
            f' sweep_remaining={len(coverage_unvisited)}' if sweep_mode else '',
          )
        return None
      sweep_note = (
        f" initial-sweep remaining={len(coverage_unvisited)}"
        if sweep_mode else ''
      )
      median_dx_text = (
        '?' if profile.get('median_dx_snr') is None
        else f"{profile['median_dx_snr']:.1f}"
      )
      reason = (
        f"active band, no interesting station; {profile['profile']} "
        f"score={profile['score']:.1f} unique={profile['stations']} "
        f"DX={profile['dx']}/{profile['stations']}({profile['dx_ratio']*100:.0f}%) "
        f"medianDX={median_dx_text} dwell={dwell:.0f}s{sweep_note}"
      )

    band = self._next_band(now, now_local=now_local)
    if band is None:
      return None
    frequency = self.frequencies[band]
    self.pending_switch = {
      'from_band': self.current_band,
      'to_band': band,
      'frequency': frequency,
      'reason': reason,
      'requested_at': now,
      'deadline': now + self.switch_timeout,
    }
    return (band, frequency, reason)
