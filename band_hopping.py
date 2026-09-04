#!/usr/bin/env python3
"""Portable adaptive band scheduler for FT8Commander v5.5.

V5.5 keeps the proven v5.4.1 scheduler as a fallback and layers on:
  * RX-density learning per configured band, based on complete receive slots;
  * an installation-wide regime factor that adapts after antenna/site changes;
  * adaptive silence confidence (2..4 empty FT8 periods);
  * mandatory revisit of every configured band after at most 60 minutes;
  * statistically cautious scoring for sparse observations;
  * runtime state under /run only.

The public API remains compatible with v5.4.1.
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Any

from band_hopping_legacy import (  # re-exported for ft8ctrl.py compatibility
  BandHopper as LegacyBandHopper,
  BandState,
  ProactiveDecodeGuard,
  StationSample,
  TargetTrack,
  _clamp,
  _slot_key,
)
from v55_core import RuntimeStateStore, as_bool, fingerprint

LOG = logging.getLogger(__name__)
V55_MARKER = 'FT8Commander v5.5 adaptive RX-density scheduler'


class BandHopper(LegacyBandHopper):
  """V5.4.1-compatible scheduler with continuous RX-density calibration."""

  def __init__(self, config: Any, my_continent='EU'):
    super().__init__(config, my_continent)
    self.config = config
    self.adaptive_rx = as_bool(getattr(config, 'band_hop_adaptive_rx', True))
    self.max_revisit_age = max(300.0, float(
      getattr(config, 'band_hop_max_revisit_age', 3600)
    ))
    self.revisit_grace = max(15.0, float(
      getattr(config, 'band_hop_revisit_grace', 30)
    ))
    self.hard_revisit_deferral = max(30.0, float(
      getattr(config, 'band_hop_hard_revisit_deferral', 300)
    ))
    self.silence_min_cycles = max(2, int(
      getattr(config, 'band_hop_silence_min_cycles', 2)
    ))
    self.silence_max_cycles = max(
      self.silence_min_cycles,
      int(getattr(config, 'band_hop_silence_max_cycles', 4)),
    )
    self.qsy_settle_slots = max(0, int(
      getattr(config, 'band_hop_qsy_settle_slots', 1)
    ))
    self.rate_fast_alpha = _clamp(float(
      getattr(config, 'band_hop_rate_fast_alpha', 0.35)
    ), 0.05, 1.0)
    self.rate_up_alpha = _clamp(float(
      getattr(config, 'band_hop_rate_up_alpha', 0.18)
    ), 0.01, 1.0)
    self.rate_down_alpha = _clamp(float(
      getattr(config, 'band_hop_rate_down_alpha', 0.03)
    ), 0.001, 0.5)
    self.global_regime_bands = max(2, int(
      getattr(config, 'band_hop_global_regime_bands', 3)
    ))
    self.global_regime_window = max(600.0, float(
      getattr(config, 'band_hop_global_regime_window', 1800)
    ))
    self.global_scale = 1.0
    self.regime_samples = deque()
    self.external_band_bonus = {}
    self.external_band_bonus_at = 0.0
    self.external_band_bonus_ttl = max(60.0, float(getattr(config, 'external_band_bonus_ttl', 600)))
    # Small selector-yield memory: an RX-open band that repeatedly produces
    # wanted Country candidates gets a modest exploitation bonus. Lack of a
    # wanted candidate is never enough to mark a band CLOSED or skip mandatory
    # revisits; this only breaks ties between otherwise viable bands.
    self.selector_yield_window = max(600.0, float(getattr(config, 'band_hop_selector_yield_window', 3600)))
    self.selector_yield_weight = max(0.0, float(getattr(config, 'band_hop_selector_yield_weight', 8.0)))
    self.selector_yield_events = {int(b): deque() for b in self.order}
    self.last_hop_requested_at = 0.0
    self.minimum_hop_interval = max(15.0, float(
      getattr(config, 'band_hop_minimum_hop_interval', 30)
    ))
    self.frequency_tolerance = max(10, int(
      getattr(config, 'band_hop_frequency_tolerance_hz', 150)
    ))

    # V5.5 deliberately removes fixed HF time-of-day assumptions by default.
    # A user can still configure a small weight, but unknown/future bands are
    # always valid and mandatory revisit has priority over this tie-breaker.
    self.time_prior_weight = float(getattr(config, 'band_hop_time_prior_weight', 0))

    state_path = getattr(config, 'v55_state_path', '/run/ft8commander/v55-state.json')
    state_fp = fingerprint(
      getattr(config, 'my_call', ''),
      getattr(config, 'my_grid', ''),
      [f'{band}={frequency}' for band, frequency in self.frequencies.items()],
    )
    self.state_store = RuntimeStateStore(
      state_path,
      state_fp,
      save_interval=float(getattr(config, 'v55_state_save_interval', 5)),
    )
    self._load_adaptive_state()
    for band in self.order:
      self._ensure_adaptive_fields(self._state(band))

    LOG.info(
      '%s: profiles=%s max_revisit=%.0fs silence=%d..%d state=%s',
      V55_MARKER, ','.join(str(item) for item in self.order),
      self.max_revisit_age, self.silence_min_cycles, self.silence_max_cycles,
      self.state_store.path,
    )

  # --------------------------------------------------------------------
  # Compatibility/profile helpers
  # --------------------------------------------------------------------

  def _ensure_adaptive_fields(self, state: BandState | None) -> BandState | None:
    if state is None:
      return None
    defaults = {
      'slot_calls': defaultdict(set),
      'last_decode_slot': None,
      'valid_rx_slots': 0,
      'visit_valid_rx_slots': 0,
      'visit_slot_unique_total': 0.0,
      'visit_slot_decode_total': 0,
      'recent_slot_rates': deque(),
      'fast_rate': 0.0,
      'slow_rate': 0.0,
      'reference_rate': 0.0,
      'reference_confidence': 0.0,
      'settle_slots_remaining': 0,
      'last_visit_rate': 0.0,
      'last_visit_valid_slots': 0,
      'last_visit_at': 0.0,
      'last_visit_score': 0.0,
      'adaptive_silent_limit': self.silence_max_cycles,
      'last_adaptive_log_at': 0.0,
    }
    for name, value in defaults.items():
      if not hasattr(state, name):
        setattr(state, name, value)
    return state

  def _state(self, band=None):
    state = super()._state(band)
    return self._ensure_adaptive_fields(state)

  def profile_for_frequency(self, frequency_hz: int | float | None):
    if not frequency_hz:
      return None
    frequency_hz = int(frequency_hz)
    best = None
    best_error = None
    for band, configured in self.frequencies.items():
      error = abs(int(configured) - frequency_hz)
      if best_error is None or error < best_error:
        best, best_error = band, error
    if best_error is not None and best_error <= self.frequency_tolerance:
      return best
    return None

  def current_profile_key(self) -> str:
    return str(self.current_band if self.current_band is not None else 'unknown')

  def is_configured_frequency(self, frequency_hz: int | float | None) -> bool:
    return self.profile_for_frequency(frequency_hz) is not None

  def on_frequency_status(
      self, frequency_hz, reported_band=None, config_name=None,
      now=None, now_utc=None):
    profile = self.profile_for_frequency(frequency_hz)
    if profile is None:
      return None
    self.on_band_change(profile, config_name, now=now, now_utc=now_utc)
    return profile

  # --------------------------------------------------------------------
  # Adaptive RX observations
  # --------------------------------------------------------------------

  def on_band_change(self, band, config_name=None, now=None, now_utc=None):
    now = time.monotonic() if now is None else float(now)
    old_band = self.current_band
    if old_band is not None and old_band != int(band):
      self._finish_adaptive_visit(old_band, now)
    super().on_band_change(band, config_name, now=now, now_utc=now_utc)
    state = self._state(int(band))
    if old_band != int(band):
      state.visit_valid_rx_slots = 0
      state.visit_slot_unique_total = 0.0
      state.visit_slot_decode_total = 0
      state.recent_slot_rates.clear()
      state.slot_calls.clear()
      state.last_decode_slot = None
      state.settle_slots_remaining = self.qsy_settle_slots
      state.adaptive_silent_limit = self._adaptive_silent_limit(state)
      self.last_hop_requested_at = now
      self._persist_adaptive_state(now, force=True)

  def record_decode(self, packet_time, snr=None, now=None):
    super().record_decode(packet_time, snr, now)
    if not self.enabled or self.current_band is None:
      return
    state = self._state()
    try:
      state.last_decode_slot = _slot_key(packet_time)
    except (AttributeError, TypeError):
      state.last_decode_slot = _slot_key(datetime.utcnow())

  def record_station(self, call, snr, info, now=None):
    super().record_station(call, snr, info, now)
    if not self.enabled or self.current_band is None or not call:
      return
    state = self._state()
    slot = state.last_decode_slot
    if slot is None:
      slot = _slot_key(datetime.utcnow())
    state.slot_calls[slot].add(str(call).upper())
    for key in list(state.slot_calls):
      if key < slot - 24:
        state.slot_calls.pop(key, None)

  def _adaptive_silent_limit(self, state: BandState) -> int:
    if not self.adaptive_rx:
      return self.silent_limit
    confidence = float(state.reference_confidence)
    expected = self._effective_reference(state)
    if confidence < 0.30:
      limit = self.silence_max_cycles
    elif expected >= 5.0:
      limit = self.silence_min_cycles
    elif expected >= 2.0:
      limit = min(self.silence_max_cycles, self.silence_min_cycles + 1)
    else:
      limit = self.silence_max_cycles
    return int(_clamp(limit, self.silence_min_cycles, self.silence_max_cycles))

  def _effective_reference(self, state: BandState) -> float:
    reference = float(state.reference_rate or state.slow_rate or state.fast_rate or 0.0)
    return max(0.15, reference * self.global_scale)

  def _update_rates(self, state: BandState, slot_rate: float, now: float) -> None:
    state.valid_rx_slots += 1
    state.visit_valid_rx_slots += 1
    state.visit_slot_unique_total += slot_rate
    state.recent_slot_rates.append((now, slot_rate))
    while state.recent_slot_rates and now - state.recent_slot_rates[0][0] > self.history_window:
      state.recent_slot_rates.popleft()

    if state.fast_rate <= 0:
      state.fast_rate = slot_rate
    else:
      state.fast_rate += self.rate_fast_alpha * (slot_rate - state.fast_rate)

    if state.slow_rate <= 0 and slot_rate > 0:
      state.slow_rate = slot_rate
    elif state.slow_rate > 0:
      alpha = self.rate_up_alpha if slot_rate >= state.slow_rate else self.rate_down_alpha
      if slot_rate == 0:
        alpha *= 0.35  # one QRM/silent slot must not redefine the station
      state.slow_rate += alpha * (slot_rate - state.slow_rate)

    # A cautious high-water baseline: rise reasonably quickly, decay slowly.
    if state.reference_rate <= 0 and slot_rate > 0:
      state.reference_rate = slot_rate
    elif state.reference_rate > 0:
      alpha = self.rate_up_alpha if slot_rate > state.reference_rate else self.rate_down_alpha
      if slot_rate == 0:
        alpha *= 0.25
      state.reference_rate += alpha * (slot_rate - state.reference_rate)
    state.reference_confidence = min(1.0, state.valid_rx_slots / 12.0)
    # The station-wide factor is a temporary bridge while each band learns the
    # new installation.  Relaxing it slowly towards 1 prevents double-counting
    # the same antenna/site change once per-band references have caught up.
    self.global_scale += 0.006 * (1.0 - self.global_scale)
    self.global_scale = _clamp(self.global_scale, 0.20, 5.0)
    state.adaptive_silent_limit = self._adaptive_silent_limit(state)

  def _finalize_slot(self, slot, now=None):
    state = self._state()
    if state.last_finalized_slot is not None and slot <= state.last_finalized_slot:
      return None
    state.last_finalized_slot = slot
    now = time.monotonic() if now is None else float(now)

    if slot in state.ignored_slots:
      state.ignored_slots.discard(slot)
      state.decode_counts.pop(slot, None)
      state.slot_calls.pop(slot, None)
      LOG.debug(
        'Band %sm FT8 period: TX slot ignored, silent=%d/%d',
        self.current_band, state.silent_cycles, state.adaptive_silent_limit,
      )
      return -1

    if state.settle_slots_remaining > 0:
      state.settle_slots_remaining -= 1
      state.decode_counts.pop(slot, None)
      state.slot_calls.pop(slot, None)
      state.silent_cycles = 0
      LOG.debug(
        'Band %sm FT8 period: post-QSY settling slot ignored (%d remaining)',
        self.current_band, state.settle_slots_remaining,
      )
      return -2

    count = int(state.decode_counts.get(slot, 0))
    unique_count = len(state.slot_calls.get(slot, set()))
    # Parsed unique transmitters are preferred. An unparsed but valid raw decode
    # still proves non-silence and contributes one conservative activity unit.
    slot_rate = float(unique_count if unique_count else (1 if count else 0))
    state.visit_slot_decode_total += count
    self._update_rates(state, slot_rate, now)

    if count:
      state.silent_cycles = 0
    else:
      state.silent_cycles += 1

    profile = self.propagation(self.current_band, now)
    self._remember_profile(state, profile, now)
    LOG.debug(
      'Band %sm FT8 period: %d decodes, slot_unique=%d, window_unique=%d, '
      '%s score=%.1f DX=%d/%d (%.0f%%), medianDX=%s, rx=%.2f/slot '
      'ref=%.2f ratio=%.2f confidence=%.2f silent=%d/%d',
      self.current_band, count, unique_count, profile['stations'], profile['profile'],
      profile['score'], profile['dx'], profile['stations'], profile['dx_ratio'] * 100.0,
      '?' if profile.get('median_dx_snr') is None else f"{profile['median_dx_snr']:.1f}",
      profile.get('rx_rate', 0.0), profile.get('reference_rate', 0.0),
      profile.get('density_ratio', 0.0), profile.get('confidence', 0.0),
      state.silent_cycles, state.adaptive_silent_limit,
    )
    state.decode_counts.pop(slot, None)
    state.slot_calls.pop(slot, None)
    self._persist_adaptive_state(now)
    return count

  def _current_rate(self, state: BandState) -> float:
    if state.recent_slot_rates:
      return sum(value for _, value in state.recent_slot_rates) / len(state.recent_slot_rates)
    if state.visit_valid_rx_slots:
      return state.visit_slot_unique_total / state.visit_valid_rx_slots
    return float(state.fast_rate)

  def _session_rate_median(self, now: float) -> float:
    rates = []
    for band in self.order:
      state = self._state(band)
      if band == self.current_band and state.visit_valid_rx_slots:
        rate = self._current_rate(state)
      elif (state.last_visit_rate > 0 and state.last_visit_at
            and now - state.last_visit_at <= self.global_regime_window):
        rate = state.last_visit_rate
      else:
        continue
      if rate > 0 and (state.reference_confidence >= 0.15 or state.visit_valid_rx_slots):
        rates.append(rate)
    return statistics.median(rates) if rates else 0.0

  def propagation(self, band=None, now=None):
    now = time.monotonic() if now is None else float(now)
    state = self._state(band)
    if not state:
      return super().propagation(band, now)
    result = super().propagation(band, now)
    silent_limit = self._adaptive_silent_limit(state)
    state.adaptive_silent_limit = silent_limit

    current_rate = self._current_rate(state)
    reference = self._effective_reference(state)
    density_ratio = current_rate / reference if reference > 0 else 1.0
    session_median = self._session_rate_median(now)
    session_ratio = current_rate / session_median if session_median > 0 else 1.0
    slot_confidence = min(1.0, state.visit_valid_rx_slots / 6.0)
    sample_confidence = 1.0 - math.exp(-result['stations'] / 12.0)
    confidence = _clamp(0.55 * slot_confidence + 0.45 * sample_confidence, 0.0, 1.0)

    if result['stations'] == 0:
      if state.silent_cycles >= silent_limit:
        result['profile'] = 'CLOSED'
      elif state.last_decode_at and now - state.last_decode_at <= self.history_window:
        result['profile'] = 'ACTIVE'
        result['score'] = max(result['score'], 6.0)
      else:
        result['profile'] = 'UNKNOWN'
      result.update({
        'rx_rate': current_rate,
        'reference_rate': reference,
        'density_ratio': density_ratio,
        'session_ratio': session_ratio,
        'confidence': confidence,
        'silent_limit': silent_limit,
      })
      return result

    total = result['stations']
    dx = result['dx']
    far_dx = result['far_dx']
    local = result['local']
    # Beta-prior shrinkage: 3/3 is useful evidence, but not as certain as 90/100.
    smoothed_dx_ratio = (dx + 1.5) / (total + 7.5)
    smoothed_far_ratio = (far_dx + 0.5) / (total + 5.0)
    local_ratio = local / total if total else 0.0
    density_component = _clamp(
      7.0 + 5.0 * math.log2(max(0.25, density_ratio))
      + 3.0 * math.log2(max(0.35, session_ratio)),
      0.0, 16.0,
    )
    snr_bonus = 0.0
    if result.get('median_dx_snr') is not None:
      snr_bonus = _clamp((result['median_dx_snr'] + 24.0) / 24.0 * 8.0, 0.0, 8.0)
    samples = list({sample.call: sample for sample in state.samples}.values())
    foreign_continents = {
      sample.continent for sample in samples
      if sample.continent and sample.continent != self.my_continent
      and (sample.distance is None or sample.distance >= 0)
    }
    diversity = min(9.0, len(foreign_continents) * 3.0) * (0.45 + 0.55 * confidence)
    score = (
      density_component
      + smoothed_dx_ratio * 48.0
      + smoothed_far_ratio * 18.0
      + min(8.0, dx * 1.6) * (0.35 + 0.65 * confidence)
      + snr_bonus + diversity - local_ratio * 12.0
    )
    result['score'] = round(_clamp(score, 0.0, 100.0), 1)

    visit_age = max(0.0, now - state.entered_at)
    low_relative_density = (
      state.visit_valid_rx_slots >= 3
      and density_ratio < 0.38
      and session_ratio < 0.60
    )
    # Absolute sparse thresholds remain only as a low-confidence fallback.
    fallback_sparse = confidence < 0.35 and total <= self.sparse_unique
    dx_rich = dx >= 2 and result['dx_ratio'] >= 0.35
    if visit_age >= self.sparse_min_age and (low_relative_density or fallback_sparse):
      if dx_rich and visit_age >= self.thin_dx_min_age:
        result['profile'] = 'SPARSE_DX'
      else:
        result['profile'] = 'SPARSE'
    elif result['profile'] in {'SPARSE', 'SPARSE_DX'} and density_ratio >= 0.65:
      # A small antenna can legitimately hear only a few stations. If that is
      # normal for this installation/profile, do not keep labelling it sparse.
      if dx_rich:
        result['profile'] = 'ACTIVE_DX'
      elif result['dx_ratio'] >= 0.15:
        result['profile'] = 'ACTIVE_MIXED'
      else:
        result['profile'] = 'ACTIVE'
    state.visit_profile = result['profile']
    result.update({
      'rx_rate': current_rate,
      'reference_rate': reference,
      'density_ratio': density_ratio,
      'session_ratio': session_ratio,
      'confidence': confidence,
      'silent_limit': silent_limit,
      'smoothed_dx_ratio': smoothed_dx_ratio,
    })
    return result

  def _remember_profile(self, state, profile, now):
    adjusted = dict(profile)
    confidence = float(adjusted.get('confidence', 1.0))
    adjusted['score'] = float(adjusted.get('score', 0.0)) * (0.45 + 0.55 * confidence)
    super()._remember_profile(state, adjusted, now)
    state.last_visit_score = float(profile.get('score', 0.0))

  def band_median_snr(self, profile=None):
    try:
      band = int(str(profile).rstrip('m')) if profile is not None else self.current_band
    except ValueError:
      band = self.current_band
    state = self._state(band)
    if not state:
      return None
    values = [sample.snr for sample in state.samples if sample.snr is not None]
    return statistics.median(values) if values else None

  # --------------------------------------------------------------------
  # Visit memory and installation-regime adaptation
  # --------------------------------------------------------------------

  def _finish_adaptive_visit(self, band, now):
    state = self._state(band)
    if not state or state.visit_valid_rx_slots <= 0:
      return
    rate = state.visit_slot_unique_total / state.visit_valid_rx_slots
    reference_before = self._effective_reference(state)
    state.last_visit_rate = rate
    state.last_visit_valid_slots = state.visit_valid_rx_slots
    state.last_visit_at = now
    # V6: a naturally CLOSED/zero-rate HF band must not look like an
    # installation-wide antenna loss. Only still-active visits participate in
    # global station-regime detection.
    if state.reference_confidence >= 0.30 and reference_before > 0 and rate > 0:
      self.regime_samples.append((now, int(band), rate / reference_before))
    self._adapt_global_regime(now)

  def _adapt_global_regime(self, now):
    while self.regime_samples and now - self.regime_samples[0][0] > self.global_regime_window:
      self.regime_samples.popleft()
    latest_by_band = {}
    for sample in self.regime_samples:
      latest_by_band[sample[1]] = sample
    if len(latest_by_band) < self.global_regime_bands:
      return
    ratios = [sample[2] for sample in latest_by_band.values()]
    median_ratio = statistics.median(ratios)
    old = self.global_scale
    if median_ratio < 0.48:
      self.global_scale *= _clamp(median_ratio / 0.75, 0.68, 0.88)
    elif median_ratio > 1.80:
      self.global_scale *= _clamp(median_ratio / 1.25, 1.12, 1.35)
    self.global_scale = _clamp(self.global_scale, 0.20, 5.0)
    if abs(self.global_scale - old) >= 0.05:
      LOG.warning(
        'V6 RX regime adapted across %d bands: median ratio=%.2f global_scale %.2f -> %.2f',
        len(latest_by_band), median_ratio, old, self.global_scale,
      )
      self.regime_samples.clear()
      self._persist_adaptive_state(now, force=True)

  # --------------------------------------------------------------------
  # Scheduler: hard 60-minute revisit plus v5.4.1 fallback ranking
  # --------------------------------------------------------------------

  def _band_age(self, band, now):
    state = self._state(band)
    if state.visits == 0:
      return float('inf')
    timestamp = state.last_left_at or state.last_visit_at or state.entered_at
    return max(0.0, now - timestamp) if timestamp else float('inf')

  def _overdue_bands(self, now):
    overdue = []
    for band in self.order:
      if band == self.current_band:
        continue
      age = self._band_age(band, now)
      if age >= self.max_revisit_age:
        overdue.append((age, band))
    overdue.sort(reverse=True)
    return overdue

  def _candidate_utility(self, band, now, hour):
    utility, memory, exploration, prior, age = super()._candidate_utility(band, now, hour)
    state = self._state(band)
    confidence = float(state.reference_confidence)
    # Uncertain memories cannot dominate exploration on a new/changed station.
    confidence_weight = 0.35 + 0.65 * confidence
    adjusted_memory = memory * confidence_weight
    utility += adjusted_memory - memory
    if now - self.external_band_bonus_at <= self.external_band_bonus_ttl:
      utility += float(self.external_band_bonus.get(int(band), 0.0))
    utility += self._selector_yield_bonus(band, now)
    return utility, adjusted_memory, exploration, prior, age

  def _selector_yield_bonus(self, band, now):
    events = self.selector_yield_events.setdefault(int(band), deque())
    while events and now - events[0][0] > self.selector_yield_window:
      events.popleft()
    unique = {call for _, call in events}
    # Saturate quickly: this is deliberately a small tie-breaker, not a new
    # band-selection authority. Four distinct wanted stations get full weight.
    return self.selector_yield_weight * min(1.0, len(unique) / 4.0)

  def note_selector_candidate(self, band, call, now=None):
    try:
      band = int(band)
    except (TypeError, ValueError):
      return
    if band not in self.selector_yield_events:
      return
    call = str(call or '').upper()
    if not call:
      return
    now = time.monotonic() if now is None else float(now)
    events = self.selector_yield_events[band]
    while events and now - events[0][0] > self.selector_yield_window:
      events.popleft()
    # Avoid inflating yield because the same DX is decoded every 15 seconds.
    if any(c == call and now - ts < 300.0 for ts, c in events):
      return
    events.append((now, call))

  def set_external_band_bonus(self, bonuses, now=None):
    now = time.monotonic() if now is None else float(now)
    self.external_band_bonus = {int(k): float(v) for k, v in (bonuses or {}).items() if int(k) in self.order}
    self.external_band_bonus_at = now

  def _next_band(self, now, now_local=None):
    unvisited = self._coverage_unvisited(now)
    if unvisited:
      return super()._next_band(now, now_local)
    overdue = self._overdue_bands(now)
    if overdue:
      age, band = overdue[0]
      LOG.warning(
        'Band scheduler mandatory revisit chose %sm: age=%.0fs limit=%.0fs',
        band, age, self.max_revisit_age,
      )
      return band
    return super()._next_band(now, now_local)

  def decision(
      self, interesting=False, silent_only=False, now=None, now_local=None,
      soft_interesting=False, hard_interesting=None, critical_interesting=False):
    """Return (band, dial_frequency_hz, reason) or None.

    ``interesting`` retains v5.4.1 compatibility.  V5.5 callers can distinguish
    hard interest (direct/wanted target) from a normal Any CQ.  Soft interest
    never extends a band beyond its dwell or mandatory revisit deadline.
    """
    if hard_interesting is None:
      hard_interesting = bool(interesting)
    now = time.monotonic() if now is None else float(now)
    if now - self.last_hop_requested_at < self.minimum_hop_interval:
      return None
    if not self.enabled or self.current_band is None:
      return None
    if self._switch_pending(now) or self.current_band not in self.frequencies:
      return None
    if self.qso_completed_at is None and now < self.attempt_lock_until:
      return None

    state = self._state()
    silent_limit = self._adaptive_silent_limit(state)
    overdue = self._overdue_bands(now)
    elapsed = now - state.entered_at
    mandatory = bool(
      overdue and elapsed >= self.revisit_grace and not critical_interesting
      and (not hard_interesting or elapsed >= self.hard_revisit_deferral)
    )

    reason = None
    if self.qso_completed_at is not None:
      if now - self.qso_completed_at < self.post_qso_hold or critical_interesting:
        return None
      if hard_interesting and not mandatory:
        return None
      if mandatory:
        age, due_band = overdue[0]
        reason = (
          f'mandatory revisit of {due_band}m age={age:.0f}s '
          f'limit={self.max_revisit_age:.0f}s after hard-target deferral'
        )
      else:
        reason = 'post-qso idle'
    elif state.silent_cycles >= silent_limit:
      reason = f'silent adaptive={state.silent_cycles}/{silent_limit}'
    elif mandatory:
      age, due_band = overdue[0]
      suffix = ' after hard-target deferral' if hard_interesting else ''
      reason = (
        f'mandatory revisit of {due_band}m age={age:.0f}s '
        f'limit={self.max_revisit_age:.0f}s{suffix}'
      )
    elif silent_only:
      return None
    elif hard_interesting:
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
      if elapsed < dwell:
        if now - state.last_status_log_at >= 60:
          state.last_status_log_at = now
          LOG.debug(
            'Band %sm hold: %s score=%.1f rx=%.2f ref=%.2f ratio=%.2f '
            'confidence=%.2f unique=%d DX=%d/%d elapsed=%.0fs dwell=%.0fs%s',
            self.current_band, profile['profile'], profile['score'],
            profile.get('rx_rate', 0.0), profile.get('reference_rate', 0.0),
            profile.get('density_ratio', 0.0), profile.get('confidence', 0.0),
            profile['stations'], profile['dx'], profile['stations'], elapsed, dwell,
            f' sweep_remaining={len(coverage_unvisited)}' if sweep_mode else '',
          )
        return None
      reason = (
        f"active band dwell complete; {profile['profile']} score={profile['score']:.1f} "
        f"rx={profile.get('rx_rate', 0.0):.2f} ref={profile.get('reference_rate', 0.0):.2f} "
        f"ratio={profile.get('density_ratio', 0.0):.2f} confidence={profile.get('confidence', 0.0):.2f} "
        f"unique={profile['stations']} DX={profile['dx']}/{profile['stations']} "
        f"dwell={dwell:.0f}s soft_target={bool(soft_interesting)}"
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
    self.last_hop_requested_at = now
    return band, frequency, reason

  # --------------------------------------------------------------------
  # Runtime state / operator commands
  # --------------------------------------------------------------------

  def recalibrate(self, now=None):
    now = time.monotonic() if now is None else float(now)
    self.global_scale = 1.0
    self.regime_samples.clear()
    for band in self.order:
      state = self._state(band)
      state.fast_rate = 0.0
      state.slow_rate = 0.0
      state.reference_rate = 0.0
      state.reference_confidence = 0.0
      state.valid_rx_slots = 0
      state.visit_valid_rx_slots = 0
      state.visit_slot_unique_total = 0.0
      state.recent_slot_rates.clear()
      state.memory_score = 0.0
      state.memory_at = 0.0
      if band != self.current_band:
        state.visits = 0
        state.last_left_at = 0.0
      state.adaptive_silent_limit = self.silence_max_cycles
    self._persist_adaptive_state(now, force=True)
    LOG.warning('V5.5 RX calibration reset; a new complete sweep is required')

  def _load_adaptive_state(self):
    section = self.state_store.section('band')
    self.global_scale = float(section.get('global_scale', 1.0))
    saved_states = section.get('states', {})
    if not isinstance(saved_states, dict):
      return
    for band_text, raw in saved_states.items():
      try:
        band = int(band_text)
      except ValueError:
        continue
      if band not in self.states or not isinstance(raw, dict):
        continue
      state = self._ensure_adaptive_fields(self.states[band])
      for name in (
          'visits', 'last_left_at', 'cooldown_until', 'memory_score', 'memory_at',
          'memory_unique', 'memory_dx_ratio', 'valid_rx_slots', 'fast_rate',
          'slow_rate', 'reference_rate', 'reference_confidence', 'last_visit_rate',
          'last_visit_valid_slots', 'last_visit_at', 'last_visit_score'):
        if name in raw:
          current = getattr(state, name)
          try:
            setattr(state, name, int(raw[name]) if isinstance(current, int) else float(raw[name]))
          except (TypeError, ValueError):
            pass
      if isinstance(raw.get('memory_profile'), str):
        state.memory_profile = raw['memory_profile']

  def _serialize_adaptive_state(self):
    return {
      'global_scale': self.global_scale,
      'states': {
        str(band): {
          'visits': state.visits,
          'last_left_at': state.last_left_at,
          'cooldown_until': state.cooldown_until,
          'memory_score': state.memory_score,
          'memory_profile': state.memory_profile,
          'memory_at': state.memory_at,
          'memory_unique': state.memory_unique,
          'memory_dx_ratio': state.memory_dx_ratio,
          'valid_rx_slots': state.valid_rx_slots,
          'fast_rate': state.fast_rate,
          'slow_rate': state.slow_rate,
          'reference_rate': state.reference_rate,
          'reference_confidence': state.reference_confidence,
          'last_visit_rate': state.last_visit_rate,
          'last_visit_valid_slots': state.last_visit_valid_slots,
          'last_visit_at': state.last_visit_at,
          'last_visit_score': state.last_visit_score,
        }
        for band, state in self.states.items()
      },
    }

  def _persist_adaptive_state(self, now, force=False):
    self.state_store.replace_section('band', self._serialize_adaptive_state())
    self.state_store.save(now=now, force=force)

  def status_snapshot(self, now=None):
    now = time.monotonic() if now is None else float(now)
    return {
      'current_band': self.current_band,
      'global_scale': self.global_scale,
      'max_revisit_age': self.max_revisit_age,
      'bands': {
        str(band): {
          'age': self._band_age(band, now),
          'visits': state.visits,
          'fast_rate': state.fast_rate,
          'reference_rate': state.reference_rate,
          'confidence': state.reference_confidence,
          'memory_score': self.memory_score(band, now),
          'profile': state.memory_profile,
        }
        for band, state in self.states.items()
      },
    }
