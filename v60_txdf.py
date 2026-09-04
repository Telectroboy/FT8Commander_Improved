#!/usr/bin/env python3
"""FT8Commander v6 TX-DF planning primitives.

This module is deliberately independent from WSJT-X and CAT transports.  It
maintains the local EVEN/ODD spectrum map, the progressively learned remote map
for a pursued DX, and the actual-transmission history used after a reply.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _clamp(value: float, low: float, high: float) -> float:
  return max(low, min(high, value))


def _signal_edge_gap(start_a: float, start_b: float, width_hz: float) -> float:
  """Edge-to-edge gap between two FT8 signals whose DF values are signal starts.

  WSJT-X DeltaFrequency is treated here as the beginning of the occupied signal,
  not its centre.  Overlapping/touching intervals therefore have zero clearance.
  """
  width = max(0.0, float(width_hz))
  a0 = float(start_a); a1 = a0 + width
  b0 = float(start_b); b1 = b0 + width
  if a1 < b0:
    return b0 - a1
  if b1 < a0:
    return a0 - b1
  return 0.0


@dataclass
class SpectrumObservation:
  df: float
  snr: float | None
  call: str
  ts: float
  slot: int
  locator: str | None = None
  dxcc: int | None = None


@dataclass
class RemoteObservation:
  df: float
  snr: float | None
  call: str
  ts: float
  confidence: float
  source: str
  active: bool = True


@dataclass
class ActualTx:
  df: int
  ts: float
  call: str
  message: str
  slot: int | None = None
  sub_hz: int | None = None


class ActualTxHistory:
  """Only records transmissions observed as actually started by WSJT-X."""

  def __init__(self, maxlen: int = 64):
    self.items: deque[ActualTx] = deque(maxlen=max(8, int(maxlen)))

  def add(self, df: int, call: str, message: str, *, ts=None, slot=None, sub_hz=None):
    item = ActualTx(
      df=int(df), ts=time.monotonic() if ts is None else float(ts),
      call=str(call or '').upper(), message=str(message or ''), slot=slot,
      sub_hz=None if sub_hz is None else int(sub_hz),
    )
    self.items.append(item)
    return item

  def recent_for(self, call: str, max_age: float = 1800.0, now=None) -> list[ActualTx]:
    now = time.monotonic() if now is None else float(now)
    call = str(call or '').upper()
    return [item for item in self.items if item.call == call and now - item.ts <= max_age]

  def distinct_df_reverse(self, call: str, max_age: float = 1800.0, now=None) -> list[int]:
    result = []
    seen = set()
    for item in reversed(self.recent_for(call, max_age=max_age, now=now)):
      if item.df not in seen:
        seen.add(item.df)
        result.append(item.df)
    return result


class LocalSpectrumMap:
  """Short-lived local FT8 occupation map separated by transmit parity."""

  def __init__(self, ttl: float = 180.0, max_per_slot: int = 800, signal_width_hz: float = 50.0):
    self.ttl = max(30.0, float(ttl))
    self.signal_width_hz = max(1.0, float(signal_width_hz))
    self.slots = {0: deque(maxlen=max_per_slot), 1: deque(maxlen=max_per_slot)}

  @staticmethod
  def slot_parity(time_ms: int | float | datetime | None) -> int:
    if time_ms is None:
      return 0
    # wsjtx.py converts Decode.Time to datetime. Keep numeric millisecond
    # support for raw protocol values and existing tests/callers.
    if isinstance(time_ms, datetime):
      seconds = (
        time_ms.hour * 3600
        + time_ms.minute * 60
        + time_ms.second
        + time_ms.microsecond / 1_000_000.0
      )
    else:
      seconds = float(time_ms) / 1000.0
    return int(seconds // 15) & 1

  def add(self, df: float, snr: float | None, call: str, *, time_ms=None, ts=None,
          locator=None, dxcc=None):
    ts = time.monotonic() if ts is None else float(ts)
    slot = self.slot_parity(time_ms)
    self.slots[slot].append(SpectrumObservation(
      df=float(df), snr=None if snr is None else float(snr), call=str(call or '').upper(),
      ts=ts, slot=slot, locator=locator, dxcc=dxcc,
    ))

  def observations(self, tx_slot: int, now=None) -> list[SpectrumObservation]:
    now = time.monotonic() if now is None else float(now)
    queue = self.slots[int(tx_slot) & 1]
    while queue and now - queue[0].ts > self.ttl:
      queue.popleft()
    return list(queue)

  @staticmethod
  def _spectral_penalty(delta: float, snr: float | None) -> float:
    delta = abs(float(delta))
    if delta >= 220:
      return 0.0
    # Strong local signals get a broader guard. Weak signals still cost heavily
    # inside the FT8 occupied bandwidth.
    strength = 1.0
    if snr is not None:
      strength = _clamp((float(snr) + 30.0) / 30.0, 0.2, 1.8)
    if delta <= 55:
      base = 100.0
    elif delta <= 100:
      base = 65.0
    elif delta <= 150:
      base = 30.0
    else:
      base = 12.0
    return base * strength

  def recent_observations(self, tx_slot: int, max_age: float, now=None, exclude_call='') -> list[SpectrumObservation]:
    """Fresh observations on one FT8 parity, optionally excluding the target itself."""
    now = time.monotonic() if now is None else float(now)
    age = max(0.0, float(max_age))
    excluded = str(exclude_call or '').upper()
    return [
      obs for obs in self.observations(tx_slot, now)
      if now - obs.ts <= age and (not excluded or obs.call != excluded)
    ]

  def clearance(self, df: float, tx_slot: int, max_age: float, now=None, exclude_call=''):
    """Return edge clearance to the nearest fresh signal on one parity."""
    observations = self.recent_observations(tx_slot, max_age, now, exclude_call=exclude_call)
    if not observations:
      return math.inf, None
    nearest = min(observations, key=lambda obs: _signal_edge_gap(float(df), obs.df, self.signal_width_hz))
    return _signal_edge_gap(float(df), nearest.df, self.signal_width_hz), nearest

  def latest_for_call(self, call: str, now=None):
    now = time.monotonic() if now is None else float(now)
    call = str(call or '').upper()
    candidates = []
    for slot in (0, 1):
      candidates.extend(obs for obs in self.observations(slot, now) if obs.call == call)
    return max(candidates, key=lambda obs: obs.ts) if candidates else None

  def risk(self, df: float, tx_slot: int, now=None, exclude_call='') -> float:
    now = time.monotonic() if now is None else float(now)
    total = 0.0
    excluded = str(exclude_call or '').upper()
    for obs in self.observations(tx_slot, now):
      if excluded and obs.call == excluded:
        continue
      age_weight = _clamp(1.0 - ((now - obs.ts) / self.ttl), 0.0, 1.0)
      gap = _signal_edge_gap(df, obs.df, self.signal_width_hz)
      total += self._spectral_penalty(gap, obs.snr) * age_weight
    return total


class RemoteSpectrumMap:
  """Per-target map of what the target (or a nearby proxy) appears to hear."""

  def __init__(self, ttl: float = 600.0, signal_width_hz: float = 50.0):
    self.ttl = max(60.0, float(ttl))
    self.signal_width_hz = max(1.0, float(signal_width_hz))
    self.targets: dict[str, deque[RemoteObservation]] = defaultdict(lambda: deque(maxlen=1000))
    self.hears_us: dict[str, deque[RemoteObservation]] = defaultdict(lambda: deque(maxlen=100))

  def add(self, target: str, df: float, snr: float | None, call: str, *, confidence=1.0,
          source='local-correlation', active=True, ts=None):
    target = str(target or '').upper()
    if not target:
      return
    self.targets[target].append(RemoteObservation(
      df=float(df), snr=None if snr is None else float(snr), call=str(call or '').upper(),
      ts=time.monotonic() if ts is None else float(ts), confidence=_clamp(float(confidence), 0, 1),
      source=str(source), active=bool(active),
    ))

  def mark_inactive(self, target: str, call: str, ts=None):
    target = str(target or '').upper()
    call = str(call or '').upper()
    now = time.monotonic() if ts is None else float(ts)
    for obs in reversed(self.targets.get(target, ())):
      if obs.call == call:
        obs.active = False
        obs.ts = now
        return True
    return False

  def add_hears_us(self, target: str, df: float, snr: float | None, *, confidence=1.0,
                   source='pskr-target', ts=None):
    target = str(target or '').upper()
    if not target:
      return
    self.hears_us[target].append(RemoteObservation(
      df=float(df), snr=None if snr is None else float(snr), call='',
      ts=time.monotonic() if ts is None else float(ts), confidence=_clamp(float(confidence), 0, 1),
      source=str(source), active=True,
    ))

  def _fresh(self, queue, now):
    while queue and now - queue[0].ts > self.ttl:
      queue.popleft()
    return list(queue)

  def known_good_for_us(self, target: str, now=None) -> list[RemoteObservation]:
    now = time.monotonic() if now is None else float(now)
    return self._fresh(self.hears_us[str(target or '').upper()], now)

  @staticmethod
  def _remote_penalty(delta: float, snr: float | None, active: bool) -> float:
    delta = abs(float(delta))
    if delta >= 260:
      return 0.0
    # A positive SNR at the target is dangerous. A -25 dB decoded station is
    # still occupied while active, but after it becomes inactive its vicinity
    # is relatively attractive because the target demonstrated weak-signal copy.
    if snr is None:
      level = 0.75
    else:
      level = _clamp((float(snr) + 28.0) / 28.0, 0.12, 1.8)
    if active:
      guard = 100.0 if delta <= 55 else 65.0 if delta <= 100 else 28.0 if delta <= 160 else 10.0
      return guard * level
    # Finished weak correspondent: do not mark as occupied. Strong recently
    # finished stations keep a little residual risk; very weak ones give a bonus.
    if snr is not None and snr <= -20 and delta <= 150:
      return -18.0 * _clamp((-float(snr) - 18.0) / 10.0, 0.2, 1.0)
    if snr is not None and snr >= -5 and delta <= 120:
      return 12.0 * level
    return 0.0

  def risk(self, target: str, df: float, now=None) -> tuple[float, float]:
    now = time.monotonic() if now is None else float(now)
    target = str(target or '').upper()
    observations = self._fresh(self.targets[target], now)
    if not observations:
      return 0.0, 0.0
    weighted = 0.0
    confidence_mass = 0.0
    for obs in observations:
      age_weight = _clamp(1.0 - ((now - obs.ts) / self.ttl), 0.0, 1.0)
      weight = obs.confidence * age_weight
      gap = _signal_edge_gap(df, obs.df, self.signal_width_hz)
      weighted += self._remote_penalty(gap, obs.snr, obs.active) * weight
      confidence_mass += weight
    confidence = _clamp(confidence_mass / 5.0, 0.0, 1.0)
    return weighted, confidence


class TxDFEngine:
  """Ranks candidate TX DFs using local, remote and diversity evidence."""

  def __init__(self, config: Any):
    self.enabled = str(getattr(config, 'tx_df_enabled', 'false')).lower() in {'1', 'true', 'yes', 'on'}
    self.audio_df = int(getattr(config, 'tx_df_audio_hz', 1500))
    self.preferred_radius = int(getattr(config, 'tx_df_preferred_radius_hz', 300))
    # V9: hard placement contract requested from field tests. The live YAML is
    # also rewritten by the installer, so existing V6 values cannot silently
    # keep the old +/-700 Hz and 250..3000 Hz behavior.
    self.max_radius = max(50, int(getattr(config, 'tx_df_max_radius_hz', 500)))
    self.min_df = int(getattr(config, 'tx_df_min_hz', -100))
    self.max_df = int(getattr(config, 'tx_df_max_hz', 3500))
    if self.min_df >= self.max_df:
      raise ValueError('tx_df_min_hz must be lower than tx_df_max_hz')
    self.grid_step = max(10, int(getattr(config, 'tx_df_grid_step_hz', 20)))
    # V10 field rule: DF is the START of the FT8 signal.  Model a configurable
    # 50 Hz occupied interval [DF, DF+width] rather than a point at DF.
    self.signal_width_hz = max(1, int(getattr(config, 'tx_df_signal_width_hz', 50)))
    self.min_separation = max(30, int(getattr(config, 'tx_df_diversity_min_hz', 100)))
    self.hard_guard = max(30, int(getattr(config, 'tx_df_hard_guard_hz', 60)))
    # Field rule: local occupancy changes fast; two minutes is the absolute
    # maximum age for both exact-parity evidence and the opposite-slot proxy.
    self.hard_guard_ttl = min(120.0, max(30.0, float(getattr(config, 'tx_df_hard_guard_ttl_s', 120.0))))
    self.same_slot_ttl = min(120.0, max(30.0, float(getattr(config, 'tx_df_same_slot_ttl_s', self.hard_guard_ttl))))
    self.opposite_slot_proxy = str(getattr(config, 'tx_df_opposite_slot_proxy', 'true')).lower() in {'1','true','yes','on'}
    self.opposite_slot_ttl = min(120.0, max(30.0, float(getattr(config, 'tx_df_opposite_slot_ttl_s', 120.0))))
    self.opposite_guard = max(0, int(getattr(config, 'tx_df_opposite_guard_hz', self.hard_guard)))
    self.clearance_goal = max(self.hard_guard, int(getattr(config, 'tx_df_clearance_goal_hz', 120)))
    self.local_weight = float(getattr(config, 'tx_df_local_weight', 1.0))
    self.remote_weight = float(getattr(config, 'tx_df_remote_weight', 1.3))
    self.distance_weight = float(getattr(config, 'tx_df_distance_weight', 0.05))
    self.diversity_weight = float(getattr(config, 'tx_df_diversity_weight', 20.0))
    self.local = LocalSpectrumMap(
      ttl=min(120.0, max(self.same_slot_ttl, self.opposite_slot_ttl)),
      signal_width_hz=self.signal_width_hz)
    self.remote = RemoteSpectrumMap(
      ttl=float(getattr(config, 'tx_df_remote_ttl', 600)), signal_width_hz=self.signal_width_hz)
    self.actual = ActualTxHistory(maxlen=int(getattr(config, 'tx_df_actual_history', 64)))
    self.current_df: int | None = None
    self.locked_df: int | None = None
    self.last_score_debug: list[dict[str, float]] = []
    self.last_choice_debug: dict[str, Any] = {}

  def note_decode(self, packet, call='', locator=None, dxcc=None, now=None):
    df = getattr(packet, 'DeltaFrequency', None)
    if df is None:
      return
    self.local.add(df, getattr(packet, 'SNR', None), call, time_ms=getattr(packet, 'Time', None),
                   ts=now, locator=locator, dxcc=dxcc)

  def note_actual_tx(self, call: str, message: str, *, df=None, sub_hz=None, now=None):
    if df is None:
      df = self.current_df if self.current_df is not None else self.audio_df
    return self.actual.add(df, call, message, ts=now, sub_hz=sub_hz)

  def actual_df_fallback_sequence(self, call: str, now=None) -> list[int]:
    return self.actual.distinct_df_reverse(call, now=now)

  def _candidate_grid(self, target_df: int):
    """Yield shifted-hole candidates inside the preferred region."""
    target_df = int(target_df)
    low = max(self.min_df, target_df - self.max_radius)
    high = min(self.max_df, target_df + self.max_radius)
    if low > high:
      return
    start = math.floor(low / self.grid_step) * self.grid_step
    for df in range(int(start), int(high) + self.grid_step, self.grid_step):
      if low <= df <= high:
        yield int(df)

  def _soft_cost(self, target_call: str, target_df: int, df: int, tx_slot: int,
                 attempted: list[int], known_good, now: float):
    distance = abs(df - int(target_df))
    distance_cost = distance * self.distance_weight
    local_cost = self.local.risk(df, tx_slot, now, exclude_call=target_call) * self.local_weight
    remote_cost, remote_conf = self.remote.risk(target_call, df, now)
    remote_cost *= self.remote_weight * (0.25 + 0.75 * remote_conf)
    diversity_cost = 0.0
    for prior in attempted:
      delta = abs(df - int(prior))
      if delta < self.min_separation:
        diversity_cost += self.diversity_weight * (1.0 - delta / self.min_separation)

    # PSKReporter/remote "heard us" evidence is useful, but in V9 it is only a
    # soft preference. It can never escape the local hard guard, +/-500 target
    # radius or the absolute -100..3500 window.
    known_good_cost = 0.0
    if known_good:
      best = max(known_good, key=lambda item: (item.confidence, item.ts))
      known_good_cost = abs(df - float(best.df)) * 0.015 * max(0.2, best.confidence)

    return (distance_cost + local_cost + remote_cost + diversity_cost + known_good_cost,
            local_cost, remote_cost, distance_cost, diversity_cost, remote_conf,
            known_good_cost)

  def choose(self, target_call: str, target_df: int, tx_slot: int, attempted: list[int] | None = None,
             now=None) -> int:
    """Choose a close clear TX signal-start DF from fast local spectrum evidence.

    Same-TX-slot observations are authoritative for the hard 60 Hz edge guard
    and expire after at most 120 s.  The opposite slot is used only as a
    lower-confidence occupancy proxy: it can steer a choice away from a likely
    busy place, but it can never by itself force caller-frequency fallback.
    The target station's own decoded signal is excluded from both maps because
    it will be listening, not transmitting, in our reply slot.
    """
    now = time.monotonic() if now is None else float(now)
    if self.locked_df is not None:
      return int(self.locked_df)

    target_call = str(target_call or '').upper()
    target_df = int(target_df)
    tx_slot = int(tx_slot) & 1
    proxy_slot = tx_slot ^ 1
    attempted = list(attempted or [])
    known_good = self.remote.known_good_for_us(target_call, now)
    candidates = list(self._candidate_grid(target_df))

    same_obs = self.local.recent_observations(
      tx_slot, self.same_slot_ttl, now, exclude_call=target_call)
    proxy_obs = (
      self.local.recent_observations(
        proxy_slot, self.opposite_slot_ttl, now, exclude_call=target_call)
      if self.opposite_slot_proxy else []
    )

    def evidence(df):
      same_clearance, same_nearest = self.local.clearance(
        df, tx_slot, self.same_slot_ttl, now, exclude_call=target_call)
      if self.opposite_slot_proxy:
        proxy_clearance, proxy_nearest = self.local.clearance(
          df, proxy_slot, self.opposite_slot_ttl, now, exclude_call=target_call)
      else:
        proxy_clearance, proxy_nearest = math.inf, None
      return same_clearance, same_nearest, proxy_clearance, proxy_nearest

    if not candidates:
      chosen = target_df
      same_clearance, nearest, proxy_clearance, proxy_nearest = evidence(chosen)
      self.last_choice_debug = {
        'target': target_df, 'chosen': chosen, 'low': None, 'high': None,
        'slot': tx_slot, 'observed': len(same_obs), 'proxy_observed': len(proxy_obs),
        'clearance': same_clearance,
        'nearest_df': None if nearest is None else float(nearest.df),
        'nearest_call': '' if nearest is None else str(nearest.call or ''),
        'proxy_clearance': proxy_clearance,
        'proxy_nearest_df': None if proxy_nearest is None else float(proxy_nearest.df),
        'proxy_nearest_call': '' if proxy_nearest is None else str(proxy_nearest.call or ''),
        'proxy_used': False,
        'mode': 'caller-frequency-fallback-no-hole-window',
        'hard_guard': self.hard_guard, 'clearance_goal': self.clearance_goal,
        'same_slot_ttl_s': self.same_slot_ttl, 'opposite_slot_ttl_s': self.opposite_slot_ttl,
        'opposite_guard': self.opposite_guard,
        'signal_width_hz': self.signal_width_hz, 'clearance_basis': 'edge-to-edge',
      }
      return chosen

    low, high = min(candidates), max(candidates)
    ranked = []
    safe = []
    goal = []
    for df in candidates:
      clearance, nearest, proxy_clearance, proxy_nearest = evidence(df)
      soft = self._soft_cost(target_call, target_df, df, tx_slot, attempted, known_good, now)
      item = {
        'df': int(df), 'clearance': float(clearance), 'nearest': nearest,
        'proxy_clearance': float(proxy_clearance), 'proxy_nearest': proxy_nearest,
        'proxy_safe': float(proxy_clearance) >= float(self.opposite_guard),
        'distance': abs(int(df) - target_df), 'soft': float(soft[0]),
        'local': float(soft[1]), 'remote': float(soft[2]),
        'distance_cost': float(soft[3]), 'diversity': float(soft[4]),
        'remote_conf': float(soft[5]), 'known_good': float(soft[6]),
      }
      ranked.append(item)
      if clearance >= self.hard_guard:
        safe.append(item)
        if clearance >= self.clearance_goal:
          goal.append(item)

    proxy_used = False
    if goal:
      # Exact-parity safety wins.  Within that safe set, use the opposite slot
      # to avoid a likely occupied place when such an alternative exists.
      proxy_goal = [item for item in goal if item['proxy_safe']]
      pool = proxy_goal if proxy_goal else goal
      proxy_used = bool(proxy_goal and proxy_obs)
      chosen_item = min(pool, key=lambda item: (
        item['distance'], item['soft'], -item['proxy_clearance'],
        -item['clearance'], item['df']))
      mode = 'clear-near-target'
    elif safe:
      proxy_safe = [item for item in safe if item['proxy_safe']]
      pool = proxy_safe if proxy_safe else safe
      proxy_used = bool(proxy_safe and proxy_obs)
      # Dense exact-parity map: preserve the widest real same-slot margin;
      # opposite-slot evidence breaks ties/near-ties but is never a hard block.
      chosen_item = min(pool, key=lambda item: (
        -item['clearance'], item['distance'], -item['proxy_clearance'],
        item['soft'], item['df']))
      mode = 'best-clearance'
    else:
      # No genuinely safe shifted hole on the actual TX parity: answer exactly
      # on the caller's signal start.  The imperfect opposite slot may never
      # force an arbitrary shifted fallback.
      clearance, nearest, proxy_clearance, proxy_nearest = evidence(target_df)
      chosen_item = {
        'df': target_df, 'clearance': float(clearance), 'nearest': nearest,
        'proxy_clearance': float(proxy_clearance), 'proxy_nearest': proxy_nearest,
        'proxy_safe': float(proxy_clearance) >= float(self.opposite_guard),
        'distance': 0, 'soft': 0.0, 'local': 0.0, 'remote': 0.0,
        'distance_cost': 0.0, 'diversity': 0.0, 'remote_conf': 0.0, 'known_good': 0.0,
      }
      mode = 'caller-frequency-fallback'

    nearest = chosen_item['nearest']
    proxy_nearest = chosen_item['proxy_nearest']
    chosen = int(chosen_item['df'])
    self.last_choice_debug = {
      'target': target_df, 'chosen': chosen, 'low': low, 'high': high,
      'slot': tx_slot, 'observed': len(same_obs), 'proxy_observed': len(proxy_obs),
      'clearance': chosen_item['clearance'],
      'nearest_df': None if nearest is None else float(nearest.df),
      'nearest_call': '' if nearest is None else str(nearest.call or ''),
      'proxy_clearance': chosen_item['proxy_clearance'],
      'proxy_nearest_df': None if proxy_nearest is None else float(proxy_nearest.df),
      'proxy_nearest_call': '' if proxy_nearest is None else str(proxy_nearest.call or ''),
      'proxy_used': proxy_used,
      'mode': mode,
      'hard_guard': self.hard_guard, 'clearance_goal': self.clearance_goal,
      'same_slot_ttl_s': self.same_slot_ttl, 'opposite_slot_ttl_s': self.opposite_slot_ttl,
      'opposite_guard': self.opposite_guard,
      'signal_width_hz': self.signal_width_hz, 'clearance_basis': 'edge-to-edge',
      'inside_preferred_absolute_range': self.min_df <= chosen <= self.max_df,
    }
    self.last_score_debug = sorted([
      {
        'df': item['df'], 'score': -item['soft'], 'local': item['local'],
        'remote': item['remote'], 'distance': item['distance_cost'],
        'diversity': item['diversity'], 'remote_conf': item['remote_conf'],
        'clearance': item['clearance'], 'proxy_clearance': item['proxy_clearance'],
      }
      for item in ranked
    ], key=lambda item: (item['df'] != chosen, -item['score']))[:8]
    return chosen

  def lock(self, df: int):
    self.locked_df = int(df)
    self.current_df = int(df)

  def unlock(self):
    self.locked_df = None

  def sub_frequency(self, dial_rx_hz: int, wanted_df: int) -> int:
    return int(dial_rx_hz) + int(wanted_df) - self.audio_df
