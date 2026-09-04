#!/usr/bin/env python3
"""FTX-1 CAT-2 MAIN/SUB manager for FT8Commander v6 TX-DF.

Field-validated on FTX-1 MAIN 01-12 with WSJT-X Split=None:
  - FB prepares the SUB dial frequency while MAIN remains the receiver;
  - VS1 makes SUB the TX/RX VFO and automatically reports FT1;
  - VS0 returns MAIN as TX/RX and automatically reports FT0;
  - ST remains 0 throughout.

The runtime deliberately keeps VS0 during normal RX, arms VS1 only just before
an FT8 TX slot, and returns to VS0 immediately after the real TX ends.
"""
from __future__ import annotations

import logging
import os
import re
import termios
import time
from dataclasses import dataclass

from yaesu_cat2 import YaesuCAT2

LOG = logging.getLogger(__name__)
V60_TXDF_VS_RADIO = '2026-09-02-v5'

_FA_RE = re.compile(rb'FA([0-9]{9});')
_FB_RE = re.compile(rb'FB([0-9]{9});')
_FR_RE = re.compile(rb'FR([0-9]{2});')
_FT_RE = re.compile(rb'FT([01]);')
_ST_RE = re.compile(rb'ST([01]);')
_VS_RE = re.compile(rb'VS([01]);')
_TX_RE = re.compile(rb'TX([01]);')
_HF_ANT_RE = re.compile(rb'EX030704([01]);')


@dataclass
class RadioState:
  fa: int
  fb: int
  fr: int
  ft: int
  st: int
  vs: int
  tx: int
  hf_ant: int


class FTX1SplitManager(YaesuCAT2):
  """FTX-1 TX-DF controller using FB + VS, never ST split."""

  def _query_fd(self, fd, command: bytes, regex, label: str):
    termios.tcflush(fd, termios.TCIFLUSH)
    os.write(fd, command)
    termios.tcdrain(fd)
    data = self._read_until_semicolon(fd)
    match = regex.search(data)
    if not match:
      raise RuntimeError(f'No valid {label} response on CAT-2: {data!r}')
    return int(match.group(1))

  def get_sub_frequency(self):
    fd = self._open()
    try:
      return self._query_fd(fd, b'FB;', _FB_RE, 'FB')
    finally:
      os.close(fd)

  def get_receive_mode(self):
    fd = self._open()
    try:
      return self._query_fd(fd, b'FR;', _FR_RE, 'FR')
    finally:
      os.close(fd)

  def get_tx_vfo(self):
    fd = self._open()
    try:
      return self._query_fd(fd, b'FT;', _FT_RE, 'FT')
    finally:
      os.close(fd)

  def get_split(self):
    fd = self._open()
    try:
      return self._query_fd(fd, b'ST;', _ST_RE, 'ST')
    finally:
      os.close(fd)

  def get_vfo_select(self):
    fd = self._open()
    try:
      return self._query_fd(fd, b'VS;', _VS_RE, 'VS')
    finally:
      os.close(fd)

  def get_tx_state(self):
    fd = self._open()
    try:
      return self._query_fd(fd, b'TX;', _TX_RE, 'TX')
    finally:
      os.close(fd)

  def get_hf_ant_select(self):
    fd = self._open()
    try:
      return self._query_fd(fd, b'EX030704;', _HF_ANT_RE, 'HF ANT SELECT')
    finally:
      os.close(fd)

  def snapshot(self):
    fd = self._open()
    try:
      return RadioState(
        fa=self._query_fd(fd, b'FA;', _FA_RE, 'FA'),
        fb=self._query_fd(fd, b'FB;', _FB_RE, 'FB'),
        fr=self._query_fd(fd, b'FR;', _FR_RE, 'FR'),
        ft=self._query_fd(fd, b'FT;', _FT_RE, 'FT'),
        st=self._query_fd(fd, b'ST;', _ST_RE, 'ST'),
        vs=self._query_fd(fd, b'VS;', _VS_RE, 'VS'),
        tx=self._query_fd(fd, b'TX;', _TX_RE, 'TX'),
        hf_ant=self._query_fd(fd, b'EX030704;', _HF_ANT_RE, 'HF ANT SELECT'),
      )
    finally:
      os.close(fd)

  def _set_and_verify(self, command: bytes, query: bytes, regex, expected: int, label: str):
    fd = self._open()
    try:
      termios.tcflush(fd, termios.TCIFLUSH)
      os.write(fd, command)
      termios.tcdrain(fd)
      if not self.verify:
        return expected
      time.sleep(0.10)
      actual = self._query_fd(fd, query, regex, label)
      if actual != expected:
        raise RuntimeError(f'CAT-2 {label} verification failed: wanted {expected}, got {actual}')
      return actual
    finally:
      os.close(fd)

  def set_sub_frequency(self, hz: int):
    hz = self._validate_frequency(hz)
    return self._set_and_verify(f'FB{hz:09d};'.encode('ascii'), b'FB;', _FB_RE, hz, 'FB')

  def set_tx_vfo(self, sub: bool):
    """Legacy helper retained for diagnostics; TX-DF runtime does not call it."""
    expected = 1 if sub else 0
    return self._set_and_verify(f'FT{expected};'.encode('ascii'), b'FT;', _FT_RE, expected, 'FT')

  def set_split(self, enabled: bool):
    """Legacy helper retained for diagnostics; TX-DF runtime keeps ST0."""
    expected = 1 if enabled else 0
    return self._set_and_verify(f'ST{expected};'.encode('ascii'), b'ST;', _ST_RE, expected, 'ST')

  def set_vfo_select(self, sub: bool):
    expected = 1 if sub else 0
    return self._set_and_verify(f'VS{expected};'.encode('ascii'), b'VS;', _VS_RE, expected, 'VS')

  @staticmethod
  def validate_txdf_baseline(state: RadioState, dial_rx_hz: int | None = None):
    """Require the field-validated normal state before FT8Commander owns TX-DF."""
    if state.tx != 0:
      raise RuntimeError('CAT-2 TX-DF baseline rejected: radio is transmitting')
    if state.st != 0 or state.vs != 0 or state.ft != 0:
      raise RuntimeError(
        f'CAT-2 TX-DF baseline rejected: FT={state.ft} ST={state.st} VS={state.vs}; '
        'expected FT0/ST0/VS0'
      )
    if state.fr != 1:
      raise RuntimeError(
        f'CAT-2 TX-DF baseline rejected: FR={state.fr:02d}; expected FR01 single receive '
        '(the field-validated mode)'
      )
    if dial_rx_hz is not None and int(state.fa) != int(dial_rx_hz):
      raise RuntimeError(
        f'CAT-2 TX-DF baseline rejected: FA={state.fa}, WSJT-X dial={int(dial_rx_hz)}'
      )
    return True

  def prepare_tx_df(self, dial_rx_hz: int, wanted_df: int, audio_df: int = 1500,
                    base_state: RadioState | None = None):
    """Prepare FB only. MAIN remains selected for RX until arm_tx_df()."""
    base = base_state or self.snapshot()
    self.validate_txdf_baseline(base, dial_rx_hz)
    sub_hz = int(dial_rx_hz) + int(wanted_df) - int(audio_df)
    self.set_sub_frequency(sub_hz)
    state = self.snapshot()
    if (state.fa != int(dial_rx_hz) or state.fb != sub_hz
        or state.fr != base.fr or state.ft != 0 or state.st != 0 or state.vs != 0
        or state.tx != 0 or state.hf_ant != base.hf_ant):
      raise RuntimeError(
        'CAT-2 TX-DF FB preparation mismatch: '
        f'FA={state.fa} FB={state.fb} FR={state.fr} FT={state.ft} ST={state.st} '
        f'VS={state.vs} TX={state.tx} ANT={state.hf_ant}; '
        f'expected FA={int(dial_rx_hz)} FB={sub_hz} FR={base.fr} FT0 ST0 VS0 TX0 ANT={base.hf_ant}'
      )
    return sub_hz

  def arm_tx_df(self, sub_hz: int, expected_hf_ant: int):
    """Select SUB immediately before TX; VS1 automatically makes FT report 1."""
    before = self.snapshot()
    self.validate_txdf_baseline(before)
    if before.fb != int(sub_hz):
      raise RuntimeError(f'CAT-2 TX-DF arm rejected: FB={before.fb}, expected {int(sub_hz)}')
    if before.hf_ant != int(expected_hf_ant):
      raise RuntimeError(
        f'CAT-2 TX-DF arm rejected: MAIN antenna={before.hf_ant}, expected {int(expected_hf_ant)}'
      )
    self.set_vfo_select(True)
    try:
      state = self.snapshot()
      if (state.fa != before.fa or state.fb != int(sub_hz) or state.fr != before.fr
          or state.ft != 1 or state.st != 0 or state.vs != 1 or state.tx != 0
          or state.hf_ant != int(expected_hf_ant)):
        raise RuntimeError(
          'CAT-2 TX-DF VS1 arm mismatch: '
          f'FA={state.fa} FB={state.fb} FR={state.fr} FT={state.ft} ST={state.st} '
          f'VS={state.vs} TX={state.tx} ANT={state.hf_ant}; '
          f'expected FA={before.fa} FB={int(sub_hz)} FR={before.fr} FT1 ST0 VS1 TX0 '
          f'ANT={int(expected_hf_ant)}'
        )
      return state
    except Exception:
      # Fail closed: put MAIN back in charge before propagating the error.
      try:
        self.set_vfo_select(False)
      except Exception as restore_err:
        LOG.error('CAT-2 TX-DF emergency VS0 rollback failed: %s', restore_err)
      raise

  def disarm_tx_df(self, expected_hf_ant: int):
    """Return to MAIN RX immediately after TX while keeping prepared FB."""
    before = self.snapshot()
    if before.tx != 0:
      raise RuntimeError('CAT-2 TX-DF disarm refused while radio still reports TX1')
    self.set_vfo_select(False)
    state = self.snapshot()
    if (state.fa != before.fa or state.fb != before.fb or state.fr != before.fr
        or state.ft != 0 or state.st != 0 or state.vs != 0 or state.tx != 0
        or state.hf_ant != int(expected_hf_ant)):
      raise RuntimeError(
        'CAT-2 TX-DF VS0 disarm mismatch: '
        f'FA={state.fa} FB={state.fb} FR={state.fr} FT={state.ft} ST={state.st} '
        f'VS={state.vs} TX={state.tx} ANT={state.hf_ant}; expected FT0 ST0 VS0 TX0 '
        f'ANT={int(expected_hf_ant)}'
      )
    return state

  def restore(self, state: RadioState):
    """Restore the exact validated pre-TXDF normal state."""
    self.validate_txdf_baseline(state)
    current = self.snapshot()
    if current.tx != 0:
      raise RuntimeError('CAT-2 TX-DF restore refused while radio still reports TX1')
    # VS0 first: return the receiver to MAIN before touching the parked SUB.
    if current.vs != 0:
      self.set_vfo_select(False)
    self.set_sub_frequency(state.fb)
    if self.get_frequency() != state.fa:
      self.set_frequency(state.fa)
    final = self.snapshot()
    if final != state:
      raise RuntimeError(f'CAT-2 TX-DF exact restore mismatch: final={final}, expected={state}')
    return final
