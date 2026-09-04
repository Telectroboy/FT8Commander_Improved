#!/usr/bin/env python3
"""DXCC-by-band memory for FT8Commander v6.

The runtime reader is offline-first: it consumes a JSON cache under /run and
continues with the last known data if Wavelog is unavailable.  The synchronizer
is a separate executable so network failures never enter the QSO state machine.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _norm_band(value: Any) -> str:
  text = str(value or '').strip().lower()
  if text and not text.endswith('m') and text.isdigit():
    text += 'm'
  return text


@dataclass
class DXCCStatus:
  dxcc: int | None
  band: str
  worked: bool
  confirmed: bool


class DXCCBandMemory:
  def __init__(self, path='/run/ft8commander/dxcc-memory.json', fallback='/var/lib/wavelogstoat/ft8commander/dxcc-memory.json'):
    self.path = Path(path)
    self.fallback = Path(fallback) if fallback else None
    self.mtime_ns = 0
    self.worked: dict[str, set[int]] = {}
    self.confirmed: dict[str, set[int]] = {}
    self.updated_at = None
    self.load(force=True)

  @staticmethod
  def _decode_section(section):
    result = {}
    if not isinstance(section, dict):
      return result
    for band, values in section.items():
      parsed = set()
      for value in values or []:
        try:
          parsed.add(int(value))
        except (TypeError, ValueError):
          continue
      result[_norm_band(band)] = parsed
    return result

  def load(self, force=False):
    source = self.path
    try:
      stat = source.stat()
    except FileNotFoundError:
      if self.fallback is None:
        return False
      source = self.fallback
      try:
        stat = source.stat()
      except FileNotFoundError:
        return False
    source_key = hash((str(source), stat.st_mtime_ns))
    if not force and source_key == self.mtime_ns:
      return False
    try:
      payload = json.loads(source.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
      return False
    if not isinstance(payload, dict):
      return False
    self.worked = self._decode_section(payload.get('worked'))
    self.confirmed = self._decode_section(payload.get('confirmed'))
    self.updated_at = payload.get('updated_at')
    self.mtime_ns = source_key
    return True

  def status(self, dxcc, band) -> DXCCStatus:
    try:
      dxcc_num = int(dxcc) if dxcc not in (None, '') else None
    except (TypeError, ValueError):
      dxcc_num = None
    band = _norm_band(band)
    return DXCCStatus(
      dxcc=dxcc_num,
      band=band,
      worked=bool(dxcc_num is not None and dxcc_num in self.worked.get(band, set())),
      confirmed=bool(dxcc_num is not None and dxcc_num in self.confirmed.get(band, set())),
    )

  def add_worked(self, dxcc, band):
    try:
      dxcc = int(dxcc)
    except (TypeError, ValueError):
      return False
    band = _norm_band(band)
    if not band:
      return False
    before = len(self.worked.setdefault(band, set()))
    self.worked[band].add(dxcc)
    return len(self.worked[band]) != before

  def eligible(self, dxcc, band, *, exclude_worked=True, exclude_confirmed=True):
    status = self.status(dxcc, band)
    if status.dxcc is None:
      return True, 'dxcc-unknown'
    if exclude_confirmed and status.confirmed:
      return False, 'confirmed-dxcc-band'
    if exclude_worked and status.worked:
      return False, 'worked-dxcc-band'
    return True, 'missing-dxcc-band'

  def to_payload(self):
    return {
      'schema': 1,
      'updated_at': self.updated_at,
      'worked': {band: sorted(values) for band, values in sorted(self.worked.items())},
      'confirmed': {band: sorted(values) for band, values in sorted(self.confirmed.items())},
    }

  def save(self):
    self.path.parent.mkdir(parents=True, exist_ok=True)
    payload = self.to_payload()
    payload['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    fd, temp = tempfile.mkstemp(prefix=self.path.name + '.', dir=str(self.path.parent))
    try:
      with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, sort_keys=True, separators=(',', ':'))
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
      os.replace(temp, self.path)
      self.load(force=True)
    finally:
      try:
        os.unlink(temp)
      except FileNotFoundError:
        pass
