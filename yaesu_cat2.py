#!/usr/bin/env python3
"""Minimal Yaesu CAT-2 transport used only for safe MAIN-VFO QSY.

No pyserial dependency.  The port is opened only for the duration of a command.
The implementation deliberately exposes only FA frequency read/write operations.
"""

from __future__ import annotations

import logging
import os
import re
import select
import termios
import time

LOG = logging.getLogger(__name__)

_FA_RE = re.compile(rb'FA([0-9]{9});')
_SUPPORTED_BAUDS = {
  4800: termios.B4800,
  9600: termios.B9600,
  19200: termios.B19200,
  38400: termios.B38400,
  57600: termios.B57600,
  115200: termios.B115200,
}


def _as_bool(value):
  if isinstance(value, bool):
    return value
  if isinstance(value, str):
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}
  return bool(value)


class YaesuCAT2:
  """Small, intentionally restricted CAT-2 controller for the FTX-1."""

  def __init__(self, config):
    self.port = str(getattr(
      config,
      'band_hop_cat_port',
      '/dev/ttyUSB1',
    ))
    self.baud = int(getattr(config, 'band_hop_cat_baud', 4800))
    self.verify = _as_bool(getattr(config, 'band_hop_cat_verify', True))
    self.timeout = max(0.2, float(getattr(config, 'band_hop_cat_timeout', 0.8)))

    if self.baud not in _SUPPORTED_BAUDS:
      raise ValueError(f'Unsupported CAT-2 baud rate: {self.baud}')

  @staticmethod
  def _validate_frequency(frequency_hz):
    frequency_hz = int(frequency_hz)
    # The band hopper is HF/6m-only.  Reject obviously malformed values before
    # anything is sent to the radio.
    if not 1_000_000 <= frequency_hz <= 54_000_000:
      raise ValueError(f'Unsafe/invalid CAT QSY frequency: {frequency_hz} Hz')
    return frequency_hz

  def _configure(self, fd):
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
    attrs[2] &= ~termios.PARENB
    attrs[2] &= ~termios.CSTOPB
    if hasattr(termios, 'CRTSCTS'):
      attrs[2] &= ~termios.CRTSCTS
    attrs[3] = 0
    speed = _SUPPORTED_BAUDS[self.baud]
    attrs[4] = speed
    attrs[5] = speed
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)

  def _open(self):
    fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    self._configure(fd)
    return fd

  def _read_until_semicolon(self, fd, timeout=None):
    deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
    data = b''
    while time.monotonic() < deadline:
      remaining = max(0.0, deadline - time.monotonic())
      ready, _, _ = select.select([fd], [], [], remaining)
      if not ready:
        break
      try:
        chunk = os.read(fd, 256)
      except BlockingIOError:
        continue
      if chunk:
        data += chunk
        if b';' in data:
          break
    return data

  def _query_frequency_fd(self, fd):
    termios.tcflush(fd, termios.TCIFLUSH)
    os.write(fd, b'FA;')
    termios.tcdrain(fd)
    data = self._read_until_semicolon(fd)
    match = _FA_RE.search(data)
    if not match:
      raise RuntimeError(f'No valid FA response on CAT-2: {data!r}')
    return int(match.group(1))

  def get_frequency(self):
    fd = self._open()
    try:
      return self._query_frequency_fd(fd)
    finally:
      os.close(fd)

  def set_frequency(self, frequency_hz):
    frequency_hz = self._validate_frequency(frequency_hz)
    fd = self._open()
    try:
      command = f'FA{frequency_hz:09d};'.encode('ascii')
      termios.tcflush(fd, termios.TCIFLUSH)
      os.write(fd, command)
      termios.tcdrain(fd)
      if not self.verify:
        return frequency_hz

      # At 4800 baud a CAT frame itself takes a few tens of milliseconds.
      time.sleep(0.10)
      actual = self._query_frequency_fd(fd)
      if actual != frequency_hz:
        raise RuntimeError(
          f'CAT-2 QSY verification failed: requested {frequency_hz}, radio reports {actual}'
        )
      return actual
    finally:
      os.close(fd)
