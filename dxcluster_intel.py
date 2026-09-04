#!/usr/bin/env python3
"""Optional classic DX Cluster feed for FT8Commander v6 band hints.

This client is intentionally advisory: spots can increase a band's exploration
utility, but they never directly create a TX candidate. FT8Commander still
requires a local WSJT-X decode before transmitting.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

LOG = logging.getLogger(__name__)
_DX_RE = re.compile(r'^DX de\s+\S+:\s+([0-9.]+)\s+([A-Z0-9/]+)\s*(.*)$', re.I)


@dataclass
class DXSpot:
  frequency_hz: int
  call: str
  comment: str
  ts: float

  @property
  def band(self):
    mhz = self.frequency_hz / 1e6
    ranges = ((7.0, 7.3, 40), (10.1, 10.15, 30), (14.0, 14.35, 20),
              (18.068, 18.168, 17), (21.0, 21.45, 15), (24.89, 24.99, 12),
              (28.0, 29.7, 10))
    for lo, hi, band in ranges:
      if lo <= mhz <= hi:
        return band
    return None


class DXClusterIntel:
  def __init__(self, config: Any, my_call: str):
    self.enabled = str(getattr(config, 'dxcluster_enabled', 'false')).lower() in {'1','true','yes','on'}
    self.host = str(getattr(config, 'dxcluster_host', '') or '')
    self.port = int(getattr(config, 'dxcluster_port', 7300))
    self.login = str(getattr(config, 'dxcluster_login', my_call) or my_call).upper()
    self.ttl = max(60.0, float(getattr(config, 'dxcluster_ttl', 900)))
    self.spots = deque(maxlen=5000)
    self._lock = threading.Lock()
    self._stop = threading.Event()
    self._thread = None
    self.connected = False

  def ingest_line(self, line: str):
    match = _DX_RE.match(line.strip())
    if not match:
      return None
    try:
      # Classic clusters conventionally report kHz.
      hz = int(round(float(match.group(1)) * 1000.0))
    except ValueError:
      return None
    spot = DXSpot(hz, match.group(2).upper(), match.group(3).strip(), time.monotonic())
    if spot.band is None:
      return None
    with self._lock:
      self.spots.append(spot)
    return spot

  def start(self):
    if not self.enabled or not self.host:
      return False
    if self._thread and self._thread.is_alive():
      return True
    self._stop.clear()
    self._thread = threading.Thread(target=self._run, name='dxcluster-v6', daemon=True)
    self._thread.start()
    return True

  def _run(self):
    while not self._stop.is_set():
      try:
        with socket.create_connection((self.host, self.port), timeout=10) as sock:
          sock.settimeout(2)
          self.connected = True
          try:
            sock.sendall((self.login + '\n').encode('ascii', errors='ignore'))
          except OSError:
            pass
          buffer = b''
          while not self._stop.is_set():
            try:
              chunk = sock.recv(4096)
            except socket.timeout:
              continue
            if not chunk:
              break
            buffer += chunk
            while b'\n' in buffer:
              raw, buffer = buffer.split(b'\n', 1)
              self.ingest_line(raw.decode('utf-8', errors='replace'))
      except OSError as err:
        LOG.debug('DX Cluster unavailable: %s', err)
      finally:
        self.connected = False
      self._stop.wait(30)

  def recent(self, now=None):
    now = time.monotonic() if now is None else float(now)
    with self._lock:
      return [spot for spot in self.spots if now - spot.ts <= self.ttl]

  def stop(self):
    self._stop.set()
