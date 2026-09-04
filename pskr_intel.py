#!/usr/bin/env python3
"""Optional PSK Reporter MQTT intelligence for FT8Commander v6.

The module is explicitly optional. If paho-mqtt or Internet is unavailable,
start() returns False and the local automation continues unchanged.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

LOG = logging.getLogger(__name__)


def grid_center(grid: str | None):
  """Return approximate (lat, lon) from a 4/6-char Maidenhead locator."""
  if not grid:
    return None
  g = str(grid).strip().upper()
  if len(g) < 4:
    return None
  try:
    lon = (ord(g[0]) - 65) * 20 - 180 + int(g[2]) * 2 + 1.0
    lat = (ord(g[1]) - 65) * 10 - 90 + int(g[3]) + 0.5
    if len(g) >= 6:
      lon += (ord(g[4]) - 65) * (2 / 24) + (1 / 24)
      lat += (ord(g[5]) - 65) * (1 / 24) + (1 / 48)
    return lat, lon
  except (ValueError, IndexError):
    return None


def distance_km(a, b):
  if not a or not b:
    return None
  lat1, lon1 = map(math.radians, a)
  lat2, lon2 = map(math.radians, b)
  dlat, dlon = lat2 - lat1, lon2 - lon1
  h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
  return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


@dataclass
class Spot:
  sender: str
  receiver: str
  frequency: int
  snr: int | None
  tx_time: int
  sender_grid: str | None
  receiver_grid: str | None
  band: str
  sender_dxcc: int | None
  receiver_dxcc: int | None
  received_monotonic: float

  @property
  def df(self):
    return None


class PSKReporterIntel:
  def __init__(self, config: Any, my_call: str):
    self.enabled = str(getattr(config, 'pskr_enabled', 'false')).lower() in {'1', 'true', 'yes', 'on'}
    self.host = str(getattr(config, 'pskr_host', 'mqtt.pskreporter.info'))
    self.port = int(getattr(config, 'pskr_port', 1883))
    self.ttl = max(60.0, float(getattr(config, 'pskr_ttl', 600)))
    self.proxy_max_km = max(10.0, float(getattr(config, 'pskr_proxy_max_km', 250)))
    self.my_call = str(my_call or '').upper()
    self._spots_by_rx = defaultdict(lambda: deque(maxlen=5000))
    self._spots_by_tx = defaultdict(lambda: deque(maxlen=3000))
    self._lock = threading.Lock()
    self._client = None
    self.connected = False
    self._subscriptions = set()

  @staticmethod
  def topic_for_target(target: str, band: str):
    return f'pskr/filter/v2/{band}/FT8/+/{str(target).upper()}/#'

  @staticmethod
  def topic_for_rx_field(grid: str, band: str):
    field = str(grid or '').upper()[:4]
    return f'pskr/filter/v2/{band}/FT8/+/+/+/{field}/#' if len(field) == 4 else None

  def ingest_payload(self, payload: bytes | str):
    try:
      row = json.loads(payload.decode('utf-8') if isinstance(payload, bytes) else payload)
      spot = Spot(
        sender=str(row.get('sc') or '').upper(), receiver=str(row.get('rc') or '').upper(),
        frequency=int(row.get('f') or 0), snr=None if row.get('rp') is None else int(row.get('rp')),
        tx_time=int(row.get('t_tx') or row.get('t') or 0),
        sender_grid=row.get('sl'), receiver_grid=row.get('rl'), band=str(row.get('b') or ''),
        sender_dxcc=row.get('sa'), receiver_dxcc=row.get('ra'), received_monotonic=time.monotonic(),
      )
    except (ValueError, TypeError, json.JSONDecodeError):
      return None
    if not spot.sender or not spot.receiver or not spot.frequency:
      return None
    with self._lock:
      self._spots_by_rx[spot.receiver].append(spot)
      self._spots_by_tx[spot.sender].append(spot)
    return spot

  def start(self, bands=None):
    if not self.enabled:
      return False
    try:
      import paho.mqtt.client as mqtt  # type: ignore
    except ImportError:
      LOG.warning('PSK Reporter disabled: optional paho-mqtt package is not installed')
      return False

    def on_connect(client, userdata, flags, reason_code, properties=None):
      self.connected = True
      for topic in list(self._subscriptions):
        client.subscribe(topic, qos=0)
      LOG.info('V6 PSK Reporter MQTT connected (%s:%d)', self.host, self.port)

    def on_disconnect(client, userdata, disconnect_flags=None, reason_code=None, properties=None):
      self.connected = False
      LOG.warning('V6 PSK Reporter MQTT disconnected; local/offline logic remains active')

    def on_message(client, userdata, message):
      self.ingest_payload(message.payload)

    try:
      try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
      except AttributeError:
        client = mqtt.Client()
      client.on_connect = on_connect
      client.on_disconnect = on_disconnect
      client.on_message = on_message
      client.connect_async(self.host, self.port, keepalive=60)
      client.loop_start()
      self._client = client
      return True
    except Exception as err:
      LOG.warning('PSK Reporter unavailable: %s', err)
      return False

  def watch_target(self, target: str, band: str, target_grid: str | None = None):
    if not self.enabled:
      return
    topics = {self.topic_for_target(target, band)}
    proxy_topic = self.topic_for_rx_field(target_grid or '', band)
    if proxy_topic:
      topics.add(proxy_topic)
    for topic in topics:
      if topic in self._subscriptions:
        continue
      self._subscriptions.add(topic)
      if self.connected and self._client is not None:
        try:
          self._client.subscribe(topic, qos=0)
        except Exception as err:
          LOG.debug('PSK Reporter subscribe failed for %s: %s', topic, err)

  def stop(self):
    if self._client is not None:
      try:
        self._client.loop_stop()
        self._client.disconnect()
      except Exception:
        pass
    self._client = None
    self.connected = False

  def _fresh(self, queue, now=None):
    now = time.monotonic() if now is None else float(now)
    return [spot for spot in queue if now - spot.received_monotonic <= self.ttl]

  def target_hears_us(self, target: str, band: str, now=None):
    target = str(target or '').upper()
    with self._lock:
      spots = self._fresh(self._spots_by_rx[target], now)
    return [spot for spot in spots if spot.sender == self.my_call and spot.band == band]

  def target_remote_map(self, target: str, band: str, now=None):
    target = str(target or '').upper()
    with self._lock:
      spots = self._fresh(self._spots_by_rx[target], now)
    return [spot for spot in spots if spot.band == band]

  def best_proxy_remote_map(self, target_grid: str, band: str, now=None):
    target_pos = grid_center(target_grid)
    if target_pos is None:
      return None, []
    now = time.monotonic() if now is None else float(now)
    candidates = []
    with self._lock:
      rx_items = list(self._spots_by_rx.items())
    for receiver, queue in rx_items:
      spots = [spot for spot in self._fresh(queue, now) if spot.band == band and spot.receiver_grid]
      if not spots:
        continue
      pos = grid_center(spots[-1].receiver_grid)
      dist = distance_km(target_pos, pos)
      if dist is None or dist > self.proxy_max_km:
        continue
      # Prefer activity and recency, not just the geometrically closest station.
      recent_count = sum(1 for spot in spots if now - spot.received_monotonic <= 300)
      age = max(0.0, now - spots[-1].received_monotonic)
      score = recent_count * 2.0 - dist * 0.05 - age * 0.02
      candidates.append((score, receiver, dist, spots))
    if not candidates:
      return None, []
    candidates.sort(reverse=True, key=lambda item: item[0])
    _, receiver, dist, spots = candidates[0]
    return {'receiver': receiver, 'distance_km': dist, 'confidence': max(0.25, 1.0 - dist / self.proxy_max_km)}, spots
