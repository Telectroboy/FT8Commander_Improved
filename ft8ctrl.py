#!/usr/bin/env python
#
# BSD 3-Clause License
#
# Copyright (c) 2023, Fred W6BSD
# All rights reserved.
#
# Modified by F4EGM
#
# Sequencing changes in this version are an independent implementation inspired
# by established FT8 operating workflows: collect a complete decode cycle,
# protect an unanswered first call for one RX cycle, finish an engaged QSO,
# and only then apply DX/direct-call pre-emption priorities.
#

import logging
import os
import re
import select
import socket
import time
from collections import deque
from argparse import ArgumentParser
from datetime import datetime
from enum import Enum
from importlib import import_module
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Queue
from threading import Event

import DXEntity

from band_hopping import BandHopper, ProactiveDecodeGuard
from v60_runtime import install_v60_runtime
from yaesu_cat2 import YaesuCAT2
import geo
import wsjtx
from config import Config
from dbutils import (DBCommand, DBInsert, Purge, create_db, get_band, get_call,
                     normalize_dxcc)

LOGFILE_SIZE = 2 << 20
LOGFILE_NAME = 'ft8ctrl-debug.log'
LOG = None

TERMINAL_TOKENS = {'RRR', 'RR73', '73'}
GRID_RE = re.compile(r'^[A-R]{2}[0-9]{2}(?:[A-X]{2})?(?:[0-9]{2})?$', re.IGNORECASE)
CALL_RE = re.compile(r'^[A-Z0-9]+(?:/[A-Z0-9]+)*$', re.IGNORECASE)
V60_LOCATOR_TIMEOUT_HOTFIX = '2026-09-02-v4'


class QSOState(Enum):
  IDLE = 'IDLE'
  ATTEMPT = 'ATTEMPT'
  ENGAGED = 'ENGAGED'


class Sequencer:
  # pylint: disable=too-many-instance-attributes
  def __init__(self, config, queue, call_select):
    self.mycall = config.my_call.upper()
    self.my_continent = getattr(config, 'my_continent', 'EU').upper()
    self.origin = geo.grid2latlon(config.my_grid)
    self.db_name = Path(config.db_name).expanduser()

    self.queue = queue
    self.selector = call_select
    self.follow_frequency = config.follow_frequency
    self.tx_power = getattr(config, 'tx_power', None)

    # Within one CQ priority class, balance distance with signal quality.
    # Direct callers do not use this penalty because mutual copy is already
    # proven by the fact that they addressed us directly.
    self.snr_km_weight = float(getattr(config, 'snr_km_weight', 300))

    # Maximum number of unanswered transmissions to the same station.
    # The last transmission always gets its following RX cycle before the
    # attempt is abandoned.
    self.tx_retries = max(1, int(getattr(config, 'tx_retries', 4)))

    # Once a station has answered us, do not let WSJT-X repeat the same QSO
    # stage forever. The counter is reset only when the remote station advances
    # to a later FT8 QSO stage.
    self.engaged_retries = max(1, int(getattr(config, 'engaged_retries', self.tx_retries)))

    # Direct calls are valuable but become stale quickly on FT8.
    self.direct_call_timeout = float(getattr(config, 'direct_call_timeout', 90))

    # Prevent a stuck QSO state forever if WSJT-X never logs/completes it.
    self.qso_timeout = float(getattr(config, 'qso_timeout', 180))

    # A WSReply should normally cause WSJT-X to select/enable the target very
    # quickly. A timeout here catches rejected non-CQ replies (e.g. Hold Tx
    # Freq not enabled in WSJT-X Improved).
    self.selection_timeout = float(getattr(config, 'selection_timeout', 8))

    # FT8Commander v5.4: recover unexpected Auto Tx disable.
    # A proactive burst may be re-armed from a very recent decode, but every
    # recovery path is bounded.  An ATTEMPT therefore cannot hold the band hop
    # forever if WSJT-X refuses the Reply or turns Auto Tx off unexpectedly.
    self.attempt_rearm_freshness = max(15.0, float(
      getattr(config, 'attempt_rearm_freshness', 30)
    ))
    self.attempt_rearm_timeout = max(3.0, float(
      getattr(config, 'attempt_rearm_timeout', 8)
    ))
    self.attempt_rearm_max = max(1, int(
      getattr(config, 'attempt_rearm_max', 3)
    ))
    self.attempt_disabled_timeout = max(15.0, float(
      getattr(config, 'attempt_disabled_timeout', 45)
    ))
    self.attempt_deadman_timeout = max(90.0, float(
      getattr(config, 'attempt_deadman_timeout', 180)
    ))

    # Give the DB thread and any trailing UDP datagrams a short time after the
    # Decoding True -> False transition before selecting the next candidate.
    self.decision_settle_time = float(getattr(config, 'decision_settle_time', 0.12))
    self.db_settle_timeout = float(getattr(config, 'db_settle_timeout', 0.8))

    # Only reselect another candidate in the SAME priority class after this
    # many unanswered transmissions. A higher priority candidate (tail-ender
    # or DX) may pre-empt after the first complete unanswered RX cycle.
    self.same_priority_reselect_attempts = max(
      1, int(getattr(config, 'same_priority_reselect_attempts', 2))
    )

    self.dxe_lookup = DXEntity.DXCC().lookup
    self.pending_direct_calls = {}

    # Proactive Country mode is owned by the Country selector itself so the
    # same country list, reverse flag, SNR limits, blacklist and LoTW policy
    # are used for both ordinary CQs and stations merely heard in a QSO.
    self.country_selector = next(
      (plugin for plugin in getattr(call_select, 'call_select', [])
       if plugin.__class__.__name__ == 'Country'),
      None,
    )
    country_cfg = getattr(self.country_selector, 'config', None)
    self.proactive_enabled = bool(
      self.country_selector and getattr(country_cfg, 'proactive', False)
    )
    self.proactive_timeout = max(
      30.0, float(getattr(country_cfg, 'proactive_timeout', 300))
    ) if country_cfg else 300.0

    # Country proactive v5.1: confirm non-CQ targets before auto-call.
    # Explicit CQ and direct calls to us remain immediate; a station merely
    # heard working somebody else must be decoded coherently in distinct FT8
    # slots before it can consume four TX attempts and a five-minute hop lock.
    self.proactive_decode_guard = ProactiveDecodeGuard(
      enabled=getattr(config, 'proactive_non_cq_confirm', True),
      required=getattr(config, 'proactive_non_cq_confirm_count', 2),
      window=getattr(config, 'proactive_non_cq_confirm_window', 90),
    )

    # Country proactive v5.2: stale retries require fresh decode evidence.
    # This is intentionally shorter than proactive_timeout: the station may be
    # remembered for propagation/history purposes without being called again
    # after it has disappeared from the band.
    self.proactive_retry_freshness = max(
      15.0, float(getattr(config, 'proactive_retry_freshness', 90))
    )

    # Wanted Country stations are kept here independently of the short-lived
    # CQ database selector. The queue implements fair round-robin bursts while
    # last_seen provides the five-minute propagation expiry.
    self.proactive_targets = {}
    self.proactive_queue = deque()

    # We deliberately stop at tx_retries before WSJT-X's own watchdog. Keep
    # the status bit only for diagnostics if the watchdog is unexpectedly hit.
    self.tx_watchdog = False

    self.state = QSOState.IDLE
    self.current = None
    self.current_tx_attempts = 0
    self.current_unanswered_cycles = 0
    self.current_tx_seen_since_decode = False
    self.current_accepted = False
    self.current_working_other = False
    self.current_terminal_seen = False
    self.current_started_at = 0.0
    self.current_last_tx_at = 0.0
    self.current_rearm_sent_at = 0.0
    self.current_rearm_count = 0
    self.engaged_at = 0.0
    self.engaged_rx_stage = 0
    self.engaged_tx_since_progress = 0

    self.frequency = 0
    self.band = 0
    self.tx_enabled = False
    self.transmitting = False
    self.decoding = False
    self.last_dx_call = None
    self.last_ip_from = None
    self.wsjt_id = None
    self.decision_due_at = None
    self.db_barrier = None
    self.db_barrier_started_at = 0.0

    # Adaptive band hopping integration (F4EGM).  WSJT-X keeps CAT-1;
    # band changes use the independent FTX-1 CAT-2 port only.
    self.config_name = None
    self.band_hopper = BandHopper(config, self.my_continent)
    self.band_qsy = YaesuCAT2(config) if self.band_hopper.enabled else None

    bind_addr = socket.gethostbyname(config.wsjt_ip)
    self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.sock.setblocking(False)
    self.sock.bind((bind_addr, config.wsjt_port))

    self.logger_ip = getattr(config, 'logger_ip', None)
    self.logger_port = getattr(config, 'logger_port', None)
    self.logger_socket = None

  # ------------------------------------------------------------------------
  # Message parsing / candidate metadata
  # ------------------------------------------------------------------------

  @staticmethod
  def normalize_call(token):
    if not token:
      return None
    token = token.strip().upper()
    if token.startswith('<') and token.endswith('>'):
      token = token[1:-1]
    if token in ('...', '') or '...' in token:
      return None
    if token in TERMINAL_TOKENS or token in {'CQ', 'QRZ', 'DX'}:
      return None
    if GRID_RE.fullmatch(token):
      return None
    if not CALL_RE.fullmatch(token):
      return None
    # Normal amateur callsigns contain at least one letter and one digit.
    if not any(ch.isalpha() for ch in token) or not any(ch.isdigit() for ch in token):
      return None
    return token

  @staticmethod
  def is_grid(token):
    return bool(token and GRID_RE.fullmatch(token))

  def parse_segment(self, segment):
    """Parse one FT8 text segment.

    Returns (kind, data) where kind is CQ, REPLY or None. Composite FT8 text
    containing ';' is split by process_decode() and each segment is considered.
    """
    tokens = segment.strip().upper().split()
    if not tokens:
      return (None, None)

    if tokens[0] in ('CQ', 'QRZ'):
      rest = tokens[1:]
      if rest and rest[0] == 'CQ':
        rest = rest[1:]
      if not rest:
        return (None, None)

      grid = None
      if len(rest) >= 2 and self.is_grid(rest[-1]):
        grid = rest[-1]
        call_token = rest[-2]
        extra_tokens = rest[:-2]
      else:
        call_token = rest[-1]
        extra_tokens = rest[:-1]

      call = self.normalize_call(call_token)
      if not call:
        return (None, None)
      extra = extra_tokens[-1] if extra_tokens else None
      return ('CQ', {'call': call, 'extra': extra, 'grid': grid})

    if len(tokens) >= 2:
      to_call = self.normalize_call(tokens[0])
      from_call = self.normalize_call(tokens[1])
      if to_call and from_call:
        payload = tokens[2:]
        # RR73 is both syntactically a Maidenhead 4-char locator and a reserved
        # FT8 terminal token. In REPLY context it must be terminal, otherwise
        # e.g. "F4HIK XV9T RR73" incorrectly relocates XV9T to grid RR73.
        grid = next((
          tok for tok in payload
          if tok not in TERMINAL_TOKENS and self.is_grid(tok)
        ), None)
        return ('REPLY', {
          'to': to_call,
          'call': from_call,
          'payload': payload,
          'grid': grid,
        })

    return (None, None)

  @staticmethod
  def is_terminal(payload):
    return bool(payload and payload[-1] in TERMINAL_TOKENS)

  @staticmethod
  def qso_stage(payload):
    """Return a coarse FT8 QSO progress stage for a received payload.

    Repeated signal reports are deliberately the SAME stage even if the SNR
    number changes. This prevents a stuck QSO from resetting its retry budget
    forever just because the remote report varies by a dB or two.
    """
    if not payload:
      return 0
    token = payload[-1].upper()
    if token in TERMINAL_TOKENS:
      return 3
    if re.fullmatch(r'R[+-]?[0-9]{1,2}', token):
      return 2
    if re.fullmatch(r'[+-]?[0-9]{1,2}', token):
      return 1
    return 0

  def lookup_candidate(self, call, grid=None, band=None):
    """Return geographic/DXCC information for a callsign.

    If a direct call contains no grid, re-use a recently known grid for that
    callsign/band from the CQ database when available.
    """
    prior = {}
    if not grid:
      # Proactive candidates can outlive deletion of their short-lived SQLite
      # CQ row. Re-use only this exact callsign/band's remembered locator.
      remembered = getattr(self, 'proactive_targets', {}).get(call)
      if (remembered and remembered.get('grid')
          and (band is None or str(remembered.get('band')) == str(band))):
        grid = remembered.get('grid')

    if not grid:
      prior = get_call(self.db_name, call, band)
      grid = prior.get('grid')

    data = {
      'grid': grid,
      'lat': None,
      'lon': None,
      'distance': None,
      'azimuth': None,
      'country': prior.get('country'),
      'continent': prior.get('continent'),
      'cqzone': prior.get('cqzone'),
      'ituzone': prior.get('ituzone'),
      'dxcc': prior.get('dxcc'),
    }

    if grid:
      try:
        lat, lon = geo.grid2latlon(grid)
        data['lat'], data['lon'] = lat, lon
        data['distance'] = geo.distance(self.origin, (lat, lon))
        data['azimuth'] = geo.azimuth(self.origin, (lat, lon))
      except (RuntimeError, ValueError, TypeError) as err:
        LOG.warning('Invalid grid %s for %s: %s', grid, call, err)

    if not data['continent'] or data.get('dxcc') in (None, ''):
      try:
        dxentity = self.dxe_lookup(call)
        data['country'] = dxentity.country
        data['continent'] = dxentity.continent
        data['cqzone'] = dxentity.cqzone
        data['ituzone'] = dxentity.ituzone
        try:
          data['dxcc'] = int(dxentity.adif)
        except (AttributeError, TypeError, ValueError):
          pass
      except KeyError:
        LOG.warning('DXEntity lookup failed for %s', call)

    # DXEntity/cty prefix matching can classify ordinary US KG4xxx calls as
    # Guantanamo Bay. Guantanamo's reserved block is KG4 + TWO suffix letters;
    # KG4 + THREE suffix letters is an ordinary US FCC-format callsign. Apply
    # the same correction here and in dbutils so cached and fresh candidates
    # agree.
    (data['country'], data['continent'], data['cqzone'], data['ituzone']) = normalize_dxcc(
      call, data.get('country'), data.get('continent'),
      data.get('cqzone'), data.get('ituzone')
    )

    return data

  def is_dx(self, data):
    continent = data.get('continent')
    if continent:
      return continent != self.my_continent
    # Fallback if DXCC lookup is unavailable but a locator is known.
    distance = data.get('distance')
    return bool(distance is not None and distance >= 3500)

  def priority_class(self, data):
    """Operating priority requested by F4EGM.

      4: direct/tail-ender DX
      3: direct/tail-ender same continent
      2: CQ DX (transcontinental)
      1: CQ same continent
    """
    direct = data.get('source') == 'direct'
    dx = self.is_dx(data)
    if direct:
      return 4 if dx else 3
    return 2 if dx else 1

  def candidate_key(self, data):
    """Return the operating rank for one candidate.

    The priority class is absolute: direct DX > direct EU > CQ DX > CQ EU.
    A direct caller is ranked by distance then SNR because mutual copy is
    already proven. CQ candidates use a distance/SNR success score so an
    extremely weak far decode does not always displace a healthier opening.
    """
    distance = data.get('distance')
    snr = data.get('snr')
    distance = float(distance) if distance is not None else 0.0
    snr = float(snr) if snr is not None else -99.0

    if data.get('source') == 'direct':
      within_class = distance
    else:
      within_class = distance + self.snr_km_weight * snr

    return (
      self.priority_class(data),
      within_class,
      distance,
      snr,
      -data.get('queued_at', time.monotonic()),
    )

  # ------------------------------------------------------------------------
  # Direct calls / tail-enders
  # ------------------------------------------------------------------------

  def queue_direct_call(self, packet, match):
    call = match['call']
    if call == self.mycall:
      return

    now = time.monotonic()
    old = self.pending_direct_calls.get(call, {})
    queued_at = old.get('queued_at', now)
    info = self.lookup_candidate(call, match.get('grid'), self.band)

    data = {
      'call': call,
      'extra': 'TAIL',
      'time': packet.Time,
      'snr': packet.SNR,
      'band': self.band,
      'frequency': self.frequency,
      'packet': packet.as_dict().copy(),
      'selector': 'DirectCall',
      'source': 'direct',
      'queued_at': queued_at,
      'last_seen': now,
      **info,
    }
    self.pending_direct_calls[call] = data

    LOG.info('Queued direct call: %s, %s, SNR: %d, Distance: %s km, priority=%d',
             call, data.get('country') or '?', packet.SNR,
             '?' if data.get('distance') is None else int(data['distance']),
             self.priority_class(data))

  def country_is_wanted(self, call, info, snr):
    """Return True when a decoded station passes the active Country selector."""
    if not self.proactive_enabled or not self.country_selector:
      return False
    if not call or call == self.mycall:
      return False

    country = info.get('country')
    if not country:
      return False

    if snr is None or not self.country_selector.min_snr <= snr <= self.country_selector.max_snr:
      return False
    if call in self.country_selector.blacklist:
      return False
    if call not in self.country_selector.lotw:
      return False

    if getattr(self.country_selector, 'band_memory', False):
      return True
    return (country in self.country_selector.c_list) ^ self.country_selector.reverse

  def _remove_proactive_from_queue(self, call):
    if not self.proactive_queue:
      return
    self.proactive_queue = deque(
      queued for queued in self.proactive_queue if queued != call
    )

  def queue_proactive_target(self, call, front=False, reason=None, allow_current=False):
    """Queue one remembered Country target once, optionally at the front."""
    target = self.proactive_targets.get(call)
    if not target or target.get('band') != self.band:
      return False
    if (self.current and call == self.current.get('call')
        and not allow_current):
      return False

    self._remove_proactive_from_queue(call)
    if front:
      self.proactive_queue.appendleft(call)
    else:
      self.proactive_queue.append(call)
    target['waiting_event'] = False

    if reason:
      LOG.info('Country proactive: queued %s (%s), reason=%s',
               call, target.get('country') or '?', reason)
    return True

  def drop_proactive_target(self, call, reason):
    target = self.proactive_targets.pop(call, None)
    self._remove_proactive_from_queue(call)
    if target:
      LOG.info('Country proactive: forgot %s (%s): %s',
               call, target.get('country') or '?', reason)

  def expire_proactive_targets(self, now=None):
    if not self.proactive_enabled:
      return
    now = time.monotonic() if now is None else now
    for call, target in list(self.proactive_targets.items()):
      if self.current and call == self.current.get('call'):
        continue
      age = now - target.get('last_seen', target.get('first_seen', now))
      if age > self.proactive_timeout:
        self.drop_proactive_target(
          call,
          f'not heard for {age:.0f}s (timeout {self.proactive_timeout:.0f}s)',
        )

  def remember_proactive_target(self, packet, match, trigger):
    """Remember a wanted Country station heard in CQ or in somebody else's QSO.

    A newly heard station is immediately eligible. After a proactive TX window a
    lone target waits for CQ/RRR/RR73/73. If another wanted Country station is
    present, old waiting targets join the round-robin rotation and may be
    called again without a fresh decode.
    """
    if not self.proactive_enabled or not self.band:
      return False

    call = match.get('call')
    if not call or call == self.mycall:
      return False

    info = self.lookup_candidate(call, match.get('grid'), self.band)
    if not self.country_is_wanted(call, info, packet.SNR):
      return False

    prior = get_call(self.db_name, call, self.band)
    if prior.get('status') == 2:
      self.drop_proactive_target(call, 'already worked on this band')
      return False

    now = time.monotonic()
    existing = self.proactive_targets.get(call)
    is_new = existing is None

    if is_new:
      confirmed, seen, required = self.proactive_decode_guard.confirm(
        call, self.band, trigger, packet.Time, now
      )
      if not confirmed:
        LOG.debug(
          'Country proactive: provisional %s (%s) via %s; coherent decodes %d/%d',
          call, info.get('country') or '?', trigger, seen, required,
        )
        return True
      if str(trigger).upper() not in {'CQ', 'DIRECT'}:
        LOG.info(
          'Country proactive: confirmed %s (%s) after %d coherent non-CQ decodes',
          call, info.get('country') or '?', seen,
        )

      existing = {
        'first_seen': now,
        'last_attempted': 0.0,
        'waiting_event': False,
        'rearm_after_burst': False,
      }
      self.proactive_targets[call] = existing

    # Always refresh the exact decode packet. WSReply then behaves like a
    # double-click on the most recent line we heard from that station.
    existing.update({
      'call': call,
      'extra': match.get('extra') or 'PROACTIVE',
      'time': packet.Time,
      'snr': packet.SNR,
      'band': self.band,
      'frequency': self.frequency,
      'packet': packet.as_dict().copy(),
      'selector': 'Country',
      'source': 'proactive',
      'proactive': True,
      'last_seen': now,
      'last_trigger': trigger,
      **info,
    })

    rearm_trigger = trigger in {'CQ', 'RRR', 'RR73', '73'}

    if self.current and call == self.current.get('call'):
      # If the final receive cycle of a burst contains CQ/RRR/RR73/73, another
      # burst may be started immediately after the proactive TX stop.
      if rearm_trigger:
        existing['rearm_after_burst'] = True
      return True

    if is_new:
      # New target first, then previously lonely/waiting targets. This gives a
      # new opening an immediate chance while guaranteeing we return to the
      # earlier rare Country afterwards.
      waiting_others = [
        target for other, target in self.proactive_targets.items()
        if other != call and target.get('band') == self.band
        and target.get('waiting_event')
      ]
      waiting_others.sort(key=lambda target: target.get('last_attempted', 0.0))

      self.queue_proactive_target(call, reason=f'first heard via {trigger}')
      for target in waiting_others:
        self.queue_proactive_target(
          target['call'], reason=f'rotation opened by new target {call}'
        )

      LOG.info(
        'Country proactive: discovered %s (%s) while %s; SNR=%s',
        call,
        existing.get('country') or '?',
        trigger,
        packet.SNR,
      )
      return True

    if existing.get('waiting_event') and rearm_trigger:
      self.queue_proactive_target(call, reason=f'fresh {trigger}')
      existing['rearm_after_burst'] = False
      return True

    return True

  def next_proactive_target(self, band, exclude=None):
    """Return the first valid round-robin target without consuming it."""
    if not self.proactive_enabled:
      return None

    self.expire_proactive_targets()
    exclude = set(exclude or [])
    now = time.monotonic()

    # Remove stale/invalid queue entries while preserving round-robin order.
    for call in list(self.proactive_queue):
      target = self.proactive_targets.get(call)
      if not target:
        self._remove_proactive_from_queue(call)
        continue
      if target.get('band') != band:
        self._remove_proactive_from_queue(call)
        continue
      if call in exclude:
        continue

      # A first burst may start from the decode that discovered the target.
      # Repeated bursts require recent evidence that the station is still on
      # the band.  Do not delete it: a fresh CQ/terminal/new decode can re-arm
      # the target later without losing its propagation history.
      if target.get('last_attempted', 0.0):
        last_seen = target.get('last_seen', target.get('first_seen', now))
        age = max(0.0, now - last_seen)
        if age > self.proactive_retry_freshness:
          target['waiting_event'] = True
          target['rearm_after_burst'] = False
          self._remove_proactive_from_queue(call)
          LOG.info(
            'Country proactive: defer retry of %s; not heard for %.0fs '
            '(retry freshness %.0fs)',
            call, age, self.proactive_retry_freshness,
          )
          continue
      return dict(target)
    return None

  def proactive_alternative(self):
    """Return another wanted Country target, if one is queued."""
    if not self.current:
      return self.next_proactive_target(self.band)
    return self.next_proactive_target(
      self.band,
      exclude={self.current.get('call')},
    )

  def finish_proactive_burst(self):
    """Stop after tx_retries and either listen or rotate to another rare DX."""
    if not self.current:
      return None

    call = self.current['call']
    target = self.proactive_targets.get(call)
    now = time.monotonic()

    if target:
      target['last_attempted'] = now

    # We are at the end of the receive cycle following the final transmission.
    # Disable automatic TX before WSJT-X can launch a fifth call.
    self.stop_transmit(self.last_ip_from, immediate=False)

    # Is another wanted station already waiting? If yes, this target is put at
    # the back of the round-robin queue so it will be retried proactively even
    # without a fresh decode.
    other = self.next_proactive_target(self.band, exclude={call})
    fresh_rearm = bool(target and target.get('rearm_after_burst'))

    self.clear_current('proactive TX window complete', delete_candidate=True)

    if target:
      target['rearm_after_burst'] = False
      if other or fresh_rearm:
        self.queue_proactive_target(
          call,
          reason='round-robin after burst' if other else 'fresh CQ/terminal after burst',
        )
      else:
        target['waiting_event'] = True
        self._remove_proactive_from_queue(call)
        pursuit_timeout = float(getattr(
          self, 'v60_pursuit_lost_timeout', self.proactive_timeout
        ))
        LOG.info(
          'Country proactive: %s now listening; waiting for CQ/RRR/RR73/73 '
          'or another wanted Country (pursuit timeout %.0fs since last heard)',
          call,
          pursuit_timeout,
        )

    return self.best_available_candidate()

  def next_direct_call(self, band):
    now = time.monotonic()
    candidates = []
    for call, data in list(self.pending_direct_calls.items()):
      age = now - data.get('last_seen', data['queued_at'])
      if age > self.direct_call_timeout:
        LOG.info('Direct call expired: %s, age %.0fs', call, age)
        del self.pending_direct_calls[call]
        continue
      if data['band'] != band:
        continue
      if self.current and call == self.current.get('call'):
        continue
      candidates.append(data)

    if not candidates:
      return None
    return max(candidates, key=self.candidate_key)

  # ------------------------------------------------------------------------
  # WSJT-X control
  # ------------------------------------------------------------------------

  def switch_band_frequency(self, frequency_hz, target_band, reason):
    if not self.band_qsy:
      LOG.warning('Cannot hop to %sm: CAT-2 controller is not available', target_band)
      self.band_hopper.cancel_pending_switch()
      return False

    LOG.info(
      'BAND HOP %sm -> %sm / %.6f MHz via FTX-1 CAT-2 (%s)',
      self.band, target_band, frequency_hz / 1_000_000.0, reason,
    )
    try:
      actual = self.band_qsy.set_frequency(frequency_hz)
    except (OSError, ValueError, RuntimeError) as err:
      LOG.error('CAT-2 QSY to %sm / %d Hz failed: %s', target_band, frequency_hz, err)
      self.band_hopper.cancel_pending_switch()
      return False

    LOG.debug('CAT-2 QSY verified at %d Hz; waiting for WSJT-X Status confirmation', actual)
    return True

  def maybe_band_hop(self, best=None, silent_only=False):
    if (not self.band_hopper.enabled or self.state != QSOState.IDLE
        or self.transmitting or self.tx_enabled):
      return False
    interesting = bool(best and self.band_hopper.candidate_is_recent(best))
    decision = self.band_hopper.decision(
      interesting=interesting,
      silent_only=silent_only,
    )
    if not decision:
      return False
    target_band, frequency_hz, reason = decision
    return self.switch_band_frequency(frequency_hz, target_band, reason)

  def call_station(self, ip_from, data, reason='selection'):
    if not ip_from:
      LOG.warning('Cannot call %s: WSJT-X UDP source is not known yet', data.get('call'))
      return False

    distance = data.get('distance')
    LOG.info('CALL %s [%s] country=%s continent=%s SNR=%s distance=%s km reason=%s',
             data.get('call'), data.get('source', 'cq'), data.get('country') or '?',
             data.get('continent') or '?', data.get('snr'),
             '?' if distance is None else int(distance), reason)

    pkt = data['packet']
    packet = wsjtx.WSReply()
    if self.wsjt_id:
      packet.ClientId = self.wsjt_id
    packet.Time = data['time']
    packet.SNR = data['snr']
    packet.DeltaTime = pkt['DeltaTime']
    packet.DeltaFrequency = pkt['DeltaFrequency']
    packet.Mode = pkt['Mode']
    # Reply must describe the exact prior decode, including composite text.
    packet.Message = pkt['Message']
    if self.follow_frequency:
      packet.Modifiers = wsjtx.Modifiers.SHIFT

    LOG.debug('Sending %s', packet)
    try:
      self.sock.sendto(packet.raw(), ip_from)
    except OSError as err:
      LOG.error('%s - %r', err, packet)
      return False
    return True

  def stop_transmit(self, ip_from, immediate=True):
    if not ip_from:
      return
    stop_pkt = wsjtx.WSHaltTx()
    if self.wsjt_id:
      stop_pkt.ClientId = self.wsjt_id
    # WSJT-X names this field 'Auto Tx Only':
    #   False -> halt the current transmission immediately.
    #   True  -> disable automatic Tx without forcing an immediate RF abort.
    stop_pkt.mode = not immediate
    try:
      self.sock.sendto(stop_pkt.raw(), ip_from)
    except OSError as err:
      LOG.error('HaltTx failed: %s', err)

  def rearm_current_attempt(self, reason):
    """Re-send WSReply without resetting an in-progress ATTEMPT counter.

    Only proactive targets are auto-rearmed, and only from a decode heard very
    recently.  This avoids fighting a deliberate manual Disable Tx while still
    recovering the live SU3YM failure mode where WSJT-X disabled Auto Tx after
    TX2 even though the station was heard again in the just-finished RX slot.
    """
    if (self.state != QSOState.ATTEMPT or not self.current
        or self.transmitting or self.tx_enabled
        or self.current_tx_attempts <= 0
        or self.current_tx_attempts >= self.tx_retries
        or not self.current.get('proactive')):
      return False
    if self.current_rearm_sent_at:
      return False
    if self.current_rearm_count >= self.attempt_rearm_max:
      LOG.warning(
        'ATTEMPT rearm limit reached for %s: %d/%d',
        self.current.get('call'), self.current_rearm_count, self.attempt_rearm_max,
      )
      return False

    now = time.monotonic()
    call = self.current.get('call')
    target = self.proactive_targets.get(call)
    if not target:
      return False
    last_seen = target.get('last_seen', target.get('first_seen', 0.0))
    age = max(0.0, now - last_seen) if last_seen else float('inf')
    if age > self.attempt_rearm_freshness:
      LOG.info(
        'Country proactive: not rearming %s; last heard %.0fs ago (limit %.0fs)',
        call, age, self.attempt_rearm_freshness,
      )
      return False

    data = dict(target)
    if not self.call_station(self.last_ip_from, data, reason):
      return False

    # Keep the ATTEMPT state and the already-transmitted count. Refresh only
    # the exact decode fields used by WSReply/diagnostics.
    for key in ('time', 'snr', 'packet', 'last_seen', 'grid', 'distance',
                'azimuth', 'country', 'continent', 'cqzone', 'ituzone'):
      if key in data:
        self.current[key] = data[key]
    self.current_rearm_count += 1
    self.current_rearm_sent_at = now
    LOG.warning(
      'ATTEMPT rearm sent for %s after unexpected Auto Tx disable; '
      'preserving TX counter %d/%d rearm=%d/%d',
      call, self.current_tx_attempts, self.tx_retries,
      self.current_rearm_count, self.attempt_rearm_max,
    )
    return True

  def abort_stuck_attempt(self, reason):
    """Release an automation-owned ATTEMPT so band hopping cannot deadlock."""
    if self.state != QSOState.ATTEMPT or not self.current:
      return False
    call = self.current.get('call')
    source = self.current.get('source')
    proactive = bool(self.current.get('proactive'))
    LOG.warning(
      'ATTEMPT safety release: %s after %d/%d TX (%s)',
      call, self.current_tx_attempts, self.tx_retries, reason,
    )
    if self.tx_enabled or self.transmitting:
      self.stop_transmit(self.last_ip_from, immediate=False)
    if proactive:
      target = self.proactive_targets.get(call)
      if target:
        target['last_attempted'] = time.monotonic()
        target['waiting_event'] = True
        target['rearm_after_burst'] = False
        self._remove_proactive_from_queue(call)
    self.band_hopper.note_attempt_abandoned(call)
    self.clear_current(reason, delete_candidate=(source != 'direct'))
    self.decision_due_at = time.monotonic() + self.decision_settle_time
    return True

  def sendto_log(self, packet):
    if not self.logger_ip or not self.logger_port:
      return
    packet.TXPower = str(self.tx_power or packet.TXPower)
    packet.Comments = '[ft8ctrl] ' + (packet.Comments or '')
    if not self.logger_socket:
      self.logger_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.logger_socket.sendto(packet.raw(), (self.logger_ip, self.logger_port))

  # ------------------------------------------------------------------------
  # QSO state machine
  # ------------------------------------------------------------------------

  def clear_current(self, reason, delete_candidate=False):
    # V10.7.4 base clear hook: captured by v60_runtime original_clear
    try:
      import v107_policy
      v107_policy.before_clear(self, reason)
    except Exception as err:
      LOG.exception('V10.7.4 before_clear failed: %s', err)

    if self.current:
      LOG.info('State %s -> IDLE: %s (%s)', self.state.value,
               self.current.get('call'), reason)
      if delete_candidate:
        self.queue.put((DBCommand.DELETE, {
          'call': self.current['call'],
          'band': self.current.get('band', self.band),
        }))
    self.current = None
    self.state = QSOState.IDLE
    self.current_tx_attempts = 0
    self.current_unanswered_cycles = 0
    self.current_tx_seen_since_decode = False
    self.current_accepted = False
    self.current_working_other = False
    self.current_terminal_seen = False
    self.current_started_at = 0.0
    self.current_last_tx_at = 0.0
    self.current_rearm_sent_at = 0.0
    self.current_rearm_count = 0
    self.engaged_at = 0.0
    self.engaged_rx_stage = 0
    self.engaged_tx_since_progress = 0

  def start_candidate(self, data, reason):
    if not data:
      return False
    if self.current and self.current.get('call') == data.get('call'):
      return False

    old = self.current
    old_is_proactive = bool(old and old.get('proactive'))

    if old:
      LOG.info('Switch %s -> %s (%s)', old.get('call'), data.get('call'), reason)
      # Decisions happen after a complete RX batch. Halt the old automatic
      # retry before selecting a different station.
      if self.tx_enabled:
        self.stop_transmit(self.last_ip_from, immediate=True)

    if not self.call_station(self.last_ip_from, data, reason):
      return False

    # Only consume a proactive queue entry after WSReply has actually been sent.
    if data.get('proactive'):
      self._remove_proactive_from_queue(data['call'])
      target = self.proactive_targets.get(data['call'])
      if target:
        target['waiting_event'] = False
        target['rearm_after_burst'] = False
    elif data.get('source') == 'direct':
      # A remembered rare Country can also call us directly. While the direct
      # QSO is active it must not simultaneously remain in the proactive queue.
      self._remove_proactive_from_queue(data.get('call'))

      # If we were only listening after a completed four-TX burst, a direct
      # caller must not make us forget that rare Country. Re-arm the most
      # recently attempted waiting targets so they resume after the direct QSO.
      if not old:
        waiting = [
          target for call, target in self.proactive_targets.items()
          if call != data.get('call') and target.get('band') == self.band
          and target.get('waiting_event')
        ]
        waiting.sort(key=lambda target: target.get('last_attempted', 0.0), reverse=True)
        for target in reversed(waiting):
          self.queue_proactive_target(
            target['call'], front=True, reason='resume after direct caller'
          )

    # If a rare Country attempt is interrupted by a direct caller or by a new
    # rare target, put it at the FRONT so it is resumed immediately afterwards.
    if old_is_proactive and old.get('call') != data.get('call'):
      target = self.proactive_targets.get(old['call'])
      if target:
        target['last_attempted'] = time.monotonic()
        target['waiting_event'] = False
        self.queue_proactive_target(
          old['call'],
          front=True,
          reason=f'suspended for {data.get("call")}',
          allow_current=True,
        )

    if old:
      self.queue.put((DBCommand.DELETE, {
        'call': old['call'],
        'band': old.get('band', self.band),
      }))

    self.current = dict(data)
    self.current.setdefault('source', 'cq')
    self.state = QSOState.ATTEMPT
    self.current_tx_attempts = 0
    self.current_unanswered_cycles = 0
    self.current_tx_seen_since_decode = False
    self.current_accepted = False
    self.current_working_other = False
    self.current_terminal_seen = False
    self.current_started_at = time.monotonic()
    self.current_last_tx_at = 0.0
    self.current_rearm_sent_at = 0.0
    self.current_rearm_count = 0
    self.engaged_at = 0.0
    self.engaged_rx_stage = 0
    self.engaged_tx_since_progress = 0

    LOG.info('State IDLE -> ATTEMPT: %s priority=%d%s',
             self.current['call'], self.priority_class(self.current),
             ' proactive' if self.current.get('proactive') else '')
    return True

  def mark_engaged(self, call, payload):
    if not self.current or call != self.current.get('call'):
      return

    now = time.monotonic()
    stage = self.qso_stage(payload)

    if self.state != QSOState.ENGAGED:
      LOG.info('State ATTEMPT -> ENGAGED: %s answered us (%s)',
               call, ' '.join(payload) if payload else 'direct reply')
      self.state = QSOState.ENGAGED
      self.engaged_rx_stage = stage
      self.engaged_tx_since_progress = 0
      self.engaged_at = now
    elif stage > self.engaged_rx_stage:
      LOG.info('QSO progress with %s: stage %d -> %d (%s); retry counter reset',
               call, self.engaged_rx_stage, stage,
               ' '.join(payload) if payload else 'direct reply')
      self.engaged_rx_stage = stage
      self.engaged_tx_since_progress = 0
      self.engaged_at = now
    else:
      LOG.debug('QSO with %s heard again without stage progress (%s); retry %d/%d',
                call, ' '.join(payload) if payload else 'direct reply',
                self.engaged_tx_since_progress, self.engaged_retries)

    self.current_unanswered_cycles = 0
    self.current_working_other = False
    self.pending_direct_calls.pop(call, None)
    if self.is_terminal(payload):
      self.current_terminal_seen = True

  def best_available_candidate(self):
    if not self.band:
      return None

    # Direct callers always have absolute priority.
    direct = self.next_direct_call(self.band)
    if direct:
      return direct

    # Proactive Country targets are a persistent round-robin queue. They are
    # preferred over the short-lived CQ selector because the whole point is to
    # keep pursuing rare Countries even between their CQs.
    proactive = self.next_proactive_target(self.band)
    if proactive:
      return proactive

    cq = self.selector(self.band)
    if cq:
      cq = dict(cq)
      cq.setdefault('source', 'cq')
    return cq

  def evaluate_after_decode(self):
    """Make one decision after the complete WSJT-X decode batch."""
    if self.decoding or self.transmitting:
      return

    # Respect genuinely manual operation. In proactive mode our own targets are
    # tracked in self.current, so an IDLE+Enabled state remains foreign/manual.
    if self.state == QSOState.IDLE and self.tx_enabled and self.last_dx_call:
      LOG.debug('Manual/foreign WSJT-X QSO active with %s; automation waits', self.last_dx_call)
      return

    if self.state == QSOState.ENGAGED:
      # Once the other station has answered us, finish that QSO. New callers
      # and rare Countries stay queued and never pre-empt genuine progress.
      # However, if WSJT-X has repeated the current QSO stage engaged_retries
      # times without the remote station advancing, stop Auto Tx and listen /
      # rotate instead of running until the WSJT-X watchdog.
      if self.engaged_tx_since_progress >= self.engaged_retries:
        call = self.current['call']
        target = self.proactive_targets.get(call)
        other = self.next_proactive_target(self.band, exclude={call}) if target else None

        LOG.warning(
          'ENGAGED no-progress limit reached for %s: %d TX at QSO stage %d; stopping Auto Tx',
          call, self.engaged_tx_since_progress, self.engaged_rx_stage,
        )
        self.stop_transmit(self.last_ip_from, immediate=False)

        if target:
          target['last_attempted'] = time.monotonic()
          target['rearm_after_burst'] = False
          self._remove_proactive_from_queue(call)
          if other:
            target['waiting_event'] = False
            self.queue_proactive_target(call, reason='round-robin after stalled engaged QSO')
          else:
            target['waiting_event'] = True
            LOG.info(
              'Country proactive: %s stalled after QSO engagement; listening for fresh CQ/RRR/RR73/73 '
              'or another wanted Country', call,
            )

        self.clear_current('ENGAGED retry limit reached without QSO progress',
                           delete_candidate=True)
        best = self.best_available_candidate()
        if best and best.get('call') != call:
          self.start_candidate(best, 'next target after stalled engaged QSO')
      return

    if self.state == QSOState.ATTEMPT:
      # Only count a failed receive cycle if a transmission actually occurred
      # since the previous decode batch.
      if self.current_tx_seen_since_decode:
        self.current_unanswered_cycles += 1
        self.current_tx_seen_since_decode = False
        LOG.info('No reply yet from %s after TX %d (RX cycle %d)',
                 self.current['call'], self.current_tx_attempts,
                 self.current_unanswered_cycles)

      is_proactive = bool(self.current.get('proactive'))

      # The first transmitted call always gets its complete following RX cycle.
      # Before that, changing target risks cancelling a call that WSJT-X has
      # selected but not yet transmitted.
      if not self.current_accepted or self.current_tx_attempts == 0:
        self.current_working_other = False
        return

      # Direct callers can interrupt any unanswered attempt after one complete
      # RX cycle. start_candidate() remembers an interrupted proactive Country
      # at the front of the queue for immediate resumption after the direct QSO.
      direct = self.next_direct_call(self.band)
      if (direct and direct.get('call') != self.current.get('call')
          and self.current_unanswered_cycles >= 1):
        self.start_candidate(direct, 'direct caller after complete unanswered RX cycle')
        return

      if is_proactive:
        alternative = self.proactive_alternative()

        # A newly appearing rare Country gets a chance after the first complete
        # unanswered RX cycle. If the current target is visibly working another
        # station, we may switch as soon as another rare target is available.
        new_alternative = bool(
          alternative
          and alternative.get('last_seen', 0.0) >= self.current_started_at
        )
        if (alternative
            and alternative.get('call') != self.current.get('call')
            and (self.current_working_other
                 or (self.current_unanswered_cycles >= 1 and new_alternative))):
          reason = (
            'current rare Country working somebody else'
            if self.current_working_other
            else 'new rare Country after first unanswered RX cycle'
          )
          self.current_working_other = False
          self.start_candidate(alternative, reason)
          return

        # The proactive rule is a hard four-call burst. We deliberately stop
        # here, after the RX cycle following TX4, before WSJT-X can reach its
        # own watchdog. At the burst boundary, apply the cumulative short-term
        # DX success model as well: v5 tracked this history but the proactive
        # branch returned before the model could actually influence a decision.
        if (self.current_unanswered_cycles >= 1
            and self.current_tx_attempts >= self.tx_retries):
          abandon, success_score = self.band_hopper.should_abandon_target(
            self.current,
            self.is_dx(self.current),
            self.current_tx_attempts,
          )
          if success_score is not None:
            LOG.debug(
              'Proactive DX success score %s: %.1f/100 burst=%d',
              self.current['call'], success_score,
              self.band_hopper.target.bursts if self.band_hopper.target else 0,
            )
          if abandon:
            call = self.current['call']
            target = self.proactive_targets.get(call)
            LOG.info(
              'Country proactive: pause %s; short-term success score %.1f/100',
              call, success_score,
            )
            self.stop_transmit(self.last_ip_from, immediate=False)
            if target:
              target['last_attempted'] = time.monotonic()
              target['waiting_event'] = True
              target['rearm_after_burst'] = False
              self._remove_proactive_from_queue(call)
            self.band_hopper.note_attempt_abandoned(call)
            self.clear_current(
              'proactive DX short-term success probability too low',
              delete_candidate=True,
            )
            best = self.best_available_candidate()
            if best and best.get('call') != call:
              self.start_candidate(best, 'next target after low proactive DX success score')
            else:
              self.maybe_band_hop(best=best)
            return

          next_candidate = self.finish_proactive_burst()
          if next_candidate:
            self.start_candidate(next_candidate, 'next target after proactive burst')
          return

        # Hearing the target work somebody else is useful propagation evidence,
        # not a reason to abandon it when no alternative rare target exists.
        if self.current_working_other:
          LOG.info(
            'Country proactive: %s is working somebody else; continuing burst (%d/%d)',
            self.current['call'], self.current_tx_attempts, self.tx_retries,
          )
          self.current_working_other = False

        # WSJT-X Improved may turn Auto Tx off during an unanswered proactive
        # burst. If the target was just heard, re-send the freshest Reply while
        # preserving TX1/TX2/... rather than silently freezing in ATTEMPT.
        if (not self.tx_enabled and not self.transmitting
            and self.current_tx_attempts < self.tx_retries):
          self.rearm_current_attempt(
            'resume proactive burst after unexpected Auto Tx disable'
          )
        return

      # Standard/non-proactive sequencing retained for other selectors.
      if self.current_working_other:
        if self.is_dx(self.current):
          # For a DX, hearing it work somebody else is propagation evidence,
          # not an unconditional reason to abandon it. The success model will
          # decide later whether continued calling is still worthwhile.
          LOG.info(
            'DX %s is working somebody else; keeping target for success-score evaluation',
            self.current['call']
          )
          self.current_working_other = False
        else:
          old = self.current['call']
          best = self.best_available_candidate()
          self.stop_transmit(self.last_ip_from, immediate=True)
          self.clear_current('station is working somebody else', delete_candidate=True)
          if best and best.get('call') != old:
            self.start_candidate(best, 'replacement after busy station')
          return

      # For a DX that remains unanswered, estimate whether continuing is still
      # worthwhile. This never overrides the existing first-RX-cycle protection
      # and is inactive until the configured evaluation age is reached.
      abandon, success_score = self.band_hopper.should_abandon_target(
        self.current,
        self.is_dx(self.current),
        self.current_tx_attempts,
      )
      if success_score is not None:
        LOG.debug('DX success score %s: %.1f/100 after %d TX',
                  self.current['call'], success_score, self.current_tx_attempts)
      if abandon:
        old = self.current['call']
        LOG.info('Abandon DX %s: short-term success score %.1f/100',
                 old, success_score)
        self.stop_transmit(self.last_ip_from, immediate=True)
        self.clear_current('DX short-term success probability too low', delete_candidate=True)
        self.band_hopper.note_attempt_abandoned(old)
        best = self.best_available_candidate()
        if best and best.get('call') != old:
          self.start_candidate(best, 'replacement after low DX success score')
        else:
          self.maybe_band_hop(best=best)
        return

      best = self.best_available_candidate()
      current_priority = self.priority_class(self.current)
      best_priority = self.priority_class(best) if best else 0

      if (self.current_unanswered_cycles >= 1 and best
          and best.get('call') != self.current.get('call')
          and best_priority > current_priority):
        self.start_candidate(best, 'higher priority after complete unanswered RX cycle')
        return

      if (best and best.get('call') != self.current.get('call')
          and best_priority == current_priority
          and self.current_tx_attempts >= self.same_priority_reselect_attempts
          and self.candidate_key(best) > self.candidate_key(self.current)):
        self.start_candidate(best, 'better candidate in same priority class')
        return

      if (self.current_unanswered_cycles >= 1
          and self.current_tx_attempts >= self.tx_retries):
        old = self.current['call']
        self.stop_transmit(self.last_ip_from, immediate=False)
        self.clear_current(
          'retry limit reached after final RX cycle',
          delete_candidate=True,
        )
        if best and best.get('call') != old:
          self.start_candidate(best, 'next candidate after retry limit')
        return

      self.current_working_other = False
      return

    # IDLE: two genuinely silent FT8 periods mean the current band is
    # probably closed.  This overrides stale database candidates.
    if not self.tx_enabled:
      if self.maybe_band_hop(silent_only=True):
        return

      best = self.best_available_candidate()
      # Post-QSO and active-band hopping only treat a recently heard candidate
      # as "interesting"; old DB rows must not pin us to one band forever.
      if self.maybe_band_hop(best=best):
        return
      if best:
        self.start_candidate(best, 'idle selection after complete decode cycle')

  def log_call(self, packet):
    self.sendto_log(packet)
    frequency = packet.DialFrequency
    band = get_band(frequency)
    call = packet.DXCall

    # A proactive-only station may never have called CQ and therefore may not
    # yet exist in SQLite. Persist its last decode first so status=2 survives a
    # service restart and prevents another call to the same station/band.
    remembered = self.proactive_targets.get(call)
    if remembered and not get_call(self.db_name, call, band):
      self.queue.put((DBCommand.INSERT, dict(remembered)))

    self.queue.put((DBCommand.STATUS, {
      'call': call,
      'status': 2,
      'band': band,
    }))
    self.pending_direct_calls.pop(call, None)
    self.drop_proactive_target(call, 'QSO logged')

    LOG.info('** Logged call: %s, Grid: %s, Mode: %s',
             call, packet.DXGrid, packet.Mode)

    if self.current and call == self.current.get('call'):
      self.band_hopper.note_qso_completed(call)
      self.clear_current('QSO logged', delete_candidate=False)
      self.decision_due_at = time.monotonic() + self.decision_settle_time
    elif self.current:
      LOG.info('Logged %s while automation is on %s; current state preserved',
               call, self.current.get('call'))
    else:
      # A manually completed QSO should also receive the 2-minute post-QSO
      # observation period, but it must not disturb an unrelated automation QSO.
      self.band_hopper.note_qso_completed(call)
      self.decision_due_at = time.monotonic() + self.decision_settle_time

  def process_decode(self, packet):
    # Replayed Band Activity decodes are historical and must never initiate or
    # pre-empt a QSO. Low-confidence/off-air decodes are also excluded.
    LOG.debug(
        'WSDECODE RAW New=%s LowConf=%s OffAir=%s DF=%s Message=%r',
        packet.New,
        packet.LowConfidence,
        packet.OffAir,
        packet.DeltaFrequency,
        packet.Message
    )

    if not packet.New:
      LOG.debug('Ignore replayed decode: %s', packet.Message)
      return
    if packet.LowConfidence or packet.OffAir:
      LOG.debug('Ignore low-confidence/off-air decode: %s', packet.Message)
      return

    # Count every valid decode, even when its text is not useful to a call
    # selector.  Zero valid decodes over two complete FT8 periods is therefore
    # a real "silent band" indication rather than "no interesting CQ".
    self.band_hopper.record_decode(packet.Time, packet.SNR)

    parsed_any = False
    for segment in (part.strip() for part in packet.Message.split(';')):
      kind, match = self.parse_segment(segment)
      if not kind:
        continue
      parsed_any = True

      if self.band_hopper.enabled:
        station_call = match.get('call')
        station_info = self.lookup_candidate(
          station_call,
          match.get('grid'),
          self.band,
        ) if station_call else {}
        if station_call:
          self.band_hopper.record_station(
            station_call,
            packet.SNR,
            station_info,
          )

        # When our current DX is working somebody else, note whether that
        # correspondent is from our own geographic region.  A DX successfully
        # working other Europeans is a positive path indicator for F4EGM.
        if (kind == 'REPLY' and self.current and station_call
            and station_call == self.current.get('call')
            and match.get('to') and match.get('to') != self.mycall):
          peer_info = self.lookup_candidate(match['to'], None, self.band)
          self.band_hopper.record_target_peer(station_call, peer_info)

      if kind == 'CQ':
        if match['call'] == self.mycall:
          continue

        # The ordinary selector still receives CQ callers in SQLite.
        match['frequency'] = self.frequency
        match['band'] = self.band
        match['packet'] = packet.as_dict().copy()
        self.queue.put((DBCommand.INSERT, match))

        # Proactive mode also remembers the exact decode independently of the
        # selector's short freshness window.
        self.remember_proactive_target(packet, match, 'CQ')
        continue

      if kind == 'REPLY':
        call = match['call']
        to_call = match['to']
        payload = match['payload']
        terminal = payload[-1] if self.is_terminal(payload) else None

        # Crucial proactive extension: the TRANSMITTING station is match['call'].
        # It becomes a candidate even when it is talking to somebody else.
        self.remember_proactive_target(
          packet,
          match,
          terminal or ('DIRECT' if to_call == self.mycall else 'QSO'),
        )

        if to_call == self.mycall:
          if self.current and call == self.current.get('call'):
            # Any direct message from our selected station proves two-way copy.
            self.mark_engaged(call, payload)
          elif not self.is_terminal(payload):
            # Tail-ender/direct caller. Queue now but never switch in the middle
            # of the decode batch; our current station might answer later in it.
            self.queue_direct_call(packet, match)
          continue

        # A station we are merely ATTEMPTING may be heard working somebody
        # else. Proactive mode uses this as a reason to try another rare target
        # if one is available, but otherwise keeps the four-call burst going.
        if (self.state == QSOState.ATTEMPT and self.current
            and call == self.current.get('call')
            and not self.is_terminal(payload)):
          self.current_working_other = True

    if not parsed_any:
      LOG.debug('Unmatched: %s', packet.Message)

    now = time.monotonic()
    self.db_barrier = Event()
    self.db_barrier_started_at = now
    self.queue.put((DBCommand.SYNC, self.db_barrier))
    self.decision_due_at = now + self.decision_settle_time
    LOG.debug('Decode received; quiet-time decision scheduled in %.2fs',
              self.decision_settle_time)

  def process_status(self, packet):
    previous_decoding = self.decoding
    previous_transmitting = self.transmitting
    previous_watchdog = self.tx_watchdog
    old_band = self.band

    self.frequency = packet.Frequency
    self.band = get_band(self.frequency)
    self.tx_enabled = packet.TXEnabled
    self.transmitting = packet.Transmitting
    self.decoding = packet.Decoding
    self.last_dx_call = packet.DXCall
    self.tx_watchdog = packet.TXWatchdog
    self.config_name = packet.ConfigName

    if (self.band_hopper.enabled and self.band
        and (self.band != old_band or self.band_hopper.current_band is None)):
      self.band_hopper.on_band_change(
        self.band, self.config_name, now_utc=datetime.utcnow()
      )

    if old_band and self.band and self.band != old_band:
      LOG.info('Band change %dm -> %dm', old_band, self.band)
      self.proactive_decode_guard.clear_band(old_band)

      if self.proactive_targets:
        LOG.info('Country proactive: dropping remembered targets after band change')
        self.proactive_targets.clear()
        self.proactive_queue.clear()

      if self.current:
        self.queue.put((DBCommand.DELETE, {
          'call': self.current['call'],
          'band': old_band,
        }))
      self.clear_current('band changed', delete_candidate=False)

    # Proactive mode is designed to stop at tx_retries before this can happen.
    # Do not build any retry logic around WSJT-X's watchdog anymore; just make
    # an unexpected hit very visible in the log for diagnosis.
    if packet.TXWatchdog and not previous_watchdog:
      LOG.error(
        'WSJT-X watchdog unexpectedly reached. FT8Commander should have stopped '
        'after %d TX; manual Enable Tx may be required.',
        self.tx_retries,
      )

    # Count actual transmissions using the False -> True edge.
    if self.transmitting and not previous_transmitting:
      # Band hopping v5: TX slots are never counted as silent RX periods.
      self.band_hopper.note_tx_period()
      if self.current and packet.DXCall == self.current.get('call'):
        self.current_accepted = True
        self.current_tx_attempts += 1
        self.current_tx_seen_since_decode = True
        self.current_last_tx_at = time.monotonic()
        self.current_rearm_sent_at = 0.0
        if self.current_tx_attempts == 1:
          self.band_hopper.note_attempt_started(self.current)
        # Keep a cumulative TX count across repeated four-call bursts so the
        # short-term DX success score can decay realistically over 10-15 min.
        self.band_hopper.note_target_tx(self.current.get('call'))
        if self.current.get('source') == 'direct':
          self.pending_direct_calls.pop(self.current['call'], None)
        else:
          # Exclude the active CQ from the ordinary selector while the state
          # machine owns it. Proactive memory is independent from this DB flag.
          self.queue.put((DBCommand.STATUS, {
            'call': self.current['call'],
            'status': 1,
            'band': self.current.get('band', self.band),
          }))
        if self.state == QSOState.ENGAGED:
          self.engaged_tx_since_progress += 1
          LOG.info('TX start: %s QSO no-progress retry %d/%d total_tx=%d message=%s',
                   self.current['call'], self.engaged_tx_since_progress,
                   self.engaged_retries, self.current_tx_attempts, packet.TxMessage)
        else:
          LOG.info('TX start: %s attempt %d/%d message=%s',
                   self.current['call'], self.current_tx_attempts,
                   self.tx_retries, packet.TxMessage)

    if (self.current and packet.DXCall == self.current.get('call')
        and packet.TXEnabled):
      self.current_accepted = True
      if self.current_rearm_sent_at:
        LOG.info(
          'ATTEMPT rearm accepted by WSJT-X for %s; TX counter remains %d/%d',
          self.current.get('call'), self.current_tx_attempts, self.tx_retries,
        )
        self.current_rearm_sent_at = 0.0

    if previous_decoding and not self.decoding:
      now = time.monotonic()
      if self.state == QSOState.IDLE and not self.tx_enabled:
        self.band_hopper.complete_period()
      self.db_barrier = Event()
      self.db_barrier_started_at = now
      self.queue.put((DBCommand.SYNC, self.db_barrier))
      self.decision_due_at = now + self.decision_settle_time
      LOG.debug('Decode cycle complete; decision scheduled in %.2fs',
                self.decision_settle_time)

    if packet.DXCall:
      LOG.debug('%s => TX:%s Enabled:%s Decoding:%s Watchdog:%s state=%s',
                packet.DXCall, packet.Transmitting, packet.TXEnabled,
                packet.Decoding, packet.TXWatchdog, self.state.value)

  def clear_decode_candidates(self, all_bands=False):
    """Mirror WSJT-X Band Activity clearing in transient candidate caches."""
    band = None if all_bands else self.band
    self.queue.put((DBCommand.CLEAR, {'band': band}))
    if all_bands:
      self.pending_direct_calls.clear()
      self.proactive_targets.clear()
      self.proactive_queue.clear()
    else:
      self.pending_direct_calls = {
        call: data for call, data in self.pending_direct_calls.items()
        if data.get('band') != band
      }
      for call, target in list(self.proactive_targets.items()):
        if target.get('band') == band:
          self.drop_proactive_target(call, 'WSJT-X Band Activity cleared')

  def process_clear(self, packet):
    # Window None/0 = Band Activity, 1 = Rx Frequency, 2 = both. A Reply must
    # describe a decode still retained by WSJT-X, so stale Band Activity
    # candidates must be discarded here as well.
    if packet.Window in (None, 0, 2):
      LOG.info('WSJT-X cleared Band Activity; dropping unworked candidates')
      self.clear_decode_candidates(all_bands=False)

  def process_close(self):
    LOG.info('WSJT-X closed; clearing transient automation state')
    self.clear_decode_candidates(all_bands=True)
    self.tx_watchdog = False
    self.clear_current('WSJT-X closed', delete_candidate=False)
    self.wsjt_id = None
    self.tx_enabled = False
    self.transmitting = False
    self.decoding = False
    self.last_dx_call = None
    self.decision_due_at = None

  def process_packet(self, rawdata, ip_from):
    self.last_ip_from = ip_from
    packet = wsjtx.ft8_decode(rawdata)
    packet_id = packet.ClientId
    if packet_id and packet_id != self.wsjt_id:
      self.wsjt_id = packet_id
      LOG.info('WSJT-X UDP client id: %s', self.wsjt_id)
    match packet:
      case wsjtx.WSHeartbeat() | wsjtx.WSADIF():
        pass
      case wsjtx.WSLogged():
        self.log_call(packet)
      case wsjtx.WSDecode():
        self.process_decode(packet)
      case wsjtx.WSStatus():
        self.process_status(packet)
      case wsjtx.WSClear():
        self.process_clear(packet)
      case wsjtx.WSClose():
        self.process_close()
      case _:
        LOG.debug('Packet type "%r" not processed', packet)

  def check_timeouts(self):
    now = time.monotonic()
    self.expire_proactive_targets(now)

    # Fallback for totally quiet bands: Improved may omit Decoding edges.
    # Count only complete IDLE receive slots, never our own TX periods.
    if (self.state == QSOState.IDLE and not self.tx_enabled
        and not self.transmitting):
      finalized = self.band_hopper.poll_period()
      if finalized is not None:
        self.maybe_band_hop(silent_only=True)

    if self.state == QSOState.ATTEMPT and self.current:
      # Hard deadman first: no automation-owned ATTEMPT can pin the radio
      # indefinitely, regardless of WSJT-X status edge behaviour.
      if (self.current_started_at
          and now - self.current_started_at > self.attempt_deadman_timeout):
        self.abort_stuck_attempt(
          f'ATTEMPT deadman timeout {self.attempt_deadman_timeout:.0f}s'
        )
        return

      # A re-sent WSReply must be acknowledged quickly. If it is not, release
      # the state instead of retrying the Reply forever.
      if (self.current_rearm_sent_at and not self.tx_enabled
          and now - self.current_rearm_sent_at > self.attempt_rearm_timeout):
        self.abort_stuck_attempt(
          f'WSReply rearm not accepted within {self.attempt_rearm_timeout:.0f}s'
        )
        return

      # If Auto Tx vanished after at least one real transmission and there was
      # no sufficiently fresh decode to justify a re-arm, release after a short
      # bounded grace period. This is the final anti-deadlock path for band hop.
      if (self.current_accepted and self.current_tx_attempts > 0
          and not self.tx_enabled and not self.transmitting
          and not self.current_rearm_sent_at and self.current_last_tx_at
          and now - self.current_last_tx_at > self.attempt_disabled_timeout):
        if not self.rearm_current_attempt(
            'late recovery before ATTEMPT safety release'):
          self.abort_stuck_attempt(
            f'Auto Tx disabled for {now - self.current_last_tx_at:.0f}s during ATTEMPT'
          )
          return

      if (not self.current_accepted
          and now - self.current_started_at > self.selection_timeout):
        call = self.current['call']
        source = self.current.get('source')
        proactive = bool(self.current.get('proactive'))
        LOG.warning('WSJT-X did not enable/select %s within %.1fs',
                    call, self.selection_timeout)
        if source == 'direct':
          LOG.warning('For UDP replies to non-CQ messages in WSJT-X Improved, '
                      'enable "Hold Tx Freq".')
          self.pending_direct_calls.pop(call, None)
        if proactive:
          target = self.proactive_targets.get(call)
          if target:
            # A stale WSReply should not be hammered repeatedly. Keep hearing
            # the station for timeout purposes but require a fresh CQ/terminal
            # (or a future new-target rotation) before another attempt.
            target['waiting_event'] = True
            target['rearm_after_burst'] = False
            self._remove_proactive_from_queue(call)
        self.clear_current(
          'WSReply selection timeout',
          delete_candidate=(source != 'direct'),
        )
        self.decision_due_at = now + self.decision_settle_time

    if (self.state == QSOState.ENGAGED and self.current
        and self.engaged_at and now - self.engaged_at > self.qso_timeout):
      call = self.current['call']
      LOG.warning('QSO timeout with %s after %.0fs', call, now - self.engaged_at)
      self.stop_transmit(self.last_ip_from, immediate=False)
      if call in self.proactive_targets:
        self.queue_proactive_target(
          call, front=True, reason='engaged QSO timeout', allow_current=True
        )
      self.clear_current('QSO timeout', delete_candidate=True)
      self.decision_due_at = now + self.decision_settle_time

    # A terminal message plus Tx disabled means the exchange is complete even
    # if WSJT-X automatic logging is off. Do not proactively call it again.
    if (self.state == QSOState.ENGAGED and self.current_terminal_seen
        and not self.transmitting and not self.tx_enabled):
      call = self.current['call']
      self.drop_proactive_target(call, 'terminal QSO completed')
      self.band_hopper.note_qso_completed(call, now)
      self.clear_current('terminal message completed; Tx disabled', delete_candidate=False)
      self.decision_due_at = now + self.decision_settle_time

  def maybe_decide(self):
    if self.decision_due_at is None:
      return
    now = time.monotonic()
    if now < self.decision_due_at:
      return

    # Prefer to decide only after every DB update from the completed decode
    # batch is visible. Never wait indefinitely: on a slow/faulted DB worker,
    # FT8 timing is more important than blocking the sequencer forever.
    if self.db_barrier is not None and not self.db_barrier.is_set():
      if now - self.db_barrier_started_at < self.db_settle_timeout:
        self.decision_due_at = now + 0.03
        return
      LOG.warning('DB decode-batch barrier timed out after %.2fs; deciding with available data',
                  self.db_settle_timeout)

    self.db_barrier = None
    self.decision_due_at = None
    self.evaluate_after_decode()

  def run(self):
    LOG.info('ft8ctrl running...')
    LOG.info('Priority: tail-DX > tail-EU > Country proactive > CQ; engaged QSO is never pre-empted')
    if self.proactive_enabled:
      LOG.info('Country proactive enabled: %d TX/burst, target expiry %.0fs',
               self.tx_retries, self.proactive_timeout)

    while True:
      fds, _, _ = select.select([self.sock], [], [], 0.1)
      if fds:
        # Drain every queued UDP datagram before making any selection. This is
        # essential: a station's reply may be later in the same decode batch
        # than one or more attractive CQs.
        while True:
          try:
            rawdata, ip_from = self.sock.recvfrom(4096)
          except BlockingIOError:
            break
          try:
            self.process_packet(rawdata, ip_from)
          except (OSError, ValueError, KeyError, RuntimeError) as err:
            LOG.exception('Packet processing error: %s', err)

      self.check_timeouts()
      self.maybe_decide()


class LoadPlugins:

  def __init__(self, plugins):
    """Load and initialize plugins"""
    self.call_select = []
    if isinstance(plugins, str):
      plugins = [plugins]

    LOG.info('Call selector: %s', ', '.join(plugins))
    for plugin in plugins:
      *module_name, class_name = plugin.split('.')
      module_name = '.'.join(['plugins'] + module_name)
      module = import_module(module_name)
      try:
        klass = getattr(module, class_name)
      except AttributeError:
        LOG.error('Call selector plugin %s not found', class_name)
        raise SystemExit(f'"{class_name}" not found') from None
      self.call_select.append(klass())

  def __call__(self, band):
    for selector in self.call_select:
      data = selector.get(band)
      if not data:
        continue
      data['selector'] = selector.__class__.__name__
      data.setdefault('source', 'cq')
      LOG.debug('Select: %s, From:%s, SNR:%s, Distance:%sKm, Band:%dm, Selector:%s',
                data.get('call'), data.get('country'), data.get('snr'),
                data.get('distance'), data.get('band'), data.get('selector'))
      return data
    return None

  def __repr__(self):
    return '<LoadPlugins> ' + ', '.join(p.__class__.__name__ for p in self.call_select)


def get_log_level():
  loglevel = os.getenv('LOG_LEVEL', 'INFO').upper()
  if loglevel not in logging._nameToLevel:  # pylint: disable=protected-access
    logging.error('Log level "%s" does not exist, defaulting to INFO', loglevel)
    loglevel = logging.INFO
  return loglevel


# FT8Commander v6.0 runtime integration.
# The implementation is isolated in v60_runtime.py for safe rollback.
install_v60_runtime(Sequencer, QSOState, LOG)


def main():
  # pylint: disable=global-statement
  global LOG
  parser = ArgumentParser(description='ft8ctl wsjt-x automation')
  parser.add_argument('-c', '--config', help='Name of the configuration file')
  opts = parser.parse_args()

  config = Config(opts.config)
  config = config['ft8ctrl']

  formatter = logging.Formatter(
    fmt='%(asctime)s - %(levelname)-7s %(lineno)3d:%(module)-8s - %(message)s',
    datefmt='%H:%M:%S',
  )
  LOG = logging.getLogger()
  LOG.setLevel(logging.DEBUG)

  console_handler = logging.StreamHandler()
  console_handler.setLevel(get_log_level())
  console_handler.setFormatter(formatter)
  LOG.addHandler(console_handler)

  logfile_name = Path(getattr(config, 'logfile_name', LOGFILE_NAME)).expanduser()
  logfile_name.parent.mkdir(parents=True, exist_ok=True)
  file_handler = RotatingFileHandler(logfile_name, maxBytes=LOGFILE_SIZE, backupCount=5)
  file_handler.setLevel(logging.DEBUG)
  file_handler.setFormatter(formatter)
  LOG.addHandler(file_handler)

  db_name = Path(config.db_name).expanduser()
  db_name.parent.mkdir(parents=True, exist_ok=True)
  create_db(db_name)

  queue = Queue()
  try:
    db_thread = DBInsert(db_name, queue, config.my_grid)
    db_thread.daemon = True
    db_thread.start()
  except RuntimeError as err:
    LOG.error('Configuration error: %s', err)
    raise SystemExit('Configuration Error') from None

  db_purge = Purge(db_name, config.retry_time)
  db_purge.daemon = True
  db_purge.start()

  call_select = LoadPlugins(config.call_selector)
  try:
    # V10.7.4 post-runtime policy install
    import v107_policy
    v107_policy.install(Sequencer)
    # V10.7.6 terminal-repeat mandatory-revisit install
    import v1076_terminal_revisit
    v1076_terminal_revisit.install(Sequencer)
    main_loop = Sequencer(config, queue, call_select)
    main_loop.run()
  except OSError as err:
    LOG.error('%s - %s', config.wsjt_ip, err.strerror)
  except KeyboardInterrupt:
    LOG.info('^C pressed exiting')


if __name__ == '__main__':
  main()
