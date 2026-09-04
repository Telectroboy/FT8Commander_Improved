#!/home/pi/venv/bin/python
"""Run Wavelog DXCC sync near local sunrise/sunset, with 07:00/19:00 fallback.

Designed to be invoked by a 10-minute systemd timer. The state file prevents
multiple runs around the same greyline. No Internet is required for the solar
calculation itself.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import subprocess
import sys
from pathlib import Path

import yaml

import geo


def _sun_event_utc(date, lat, lon, sunrise=True, zenith=90.833):
  # NOAA-style sunrise/sunset approximation, accurate enough for scheduling a
  # twice-daily non-critical log synchronization.
  n = date.timetuple().tm_yday
  lng_hour = lon / 15.0
  t = n + ((6 - lng_hour) / 24.0 if sunrise else (18 - lng_hour) / 24.0)
  m = (0.9856 * t) - 3.289
  l = m + (1.916 * math.sin(math.radians(m))) + (0.020 * math.sin(math.radians(2 * m))) + 282.634
  l %= 360.0
  ra = math.degrees(math.atan(0.91764 * math.tan(math.radians(l)))) % 360.0
  lq = math.floor(l / 90.0) * 90.0
  raq = math.floor(ra / 90.0) * 90.0
  ra = (ra + (lq - raq)) / 15.0
  sin_dec = 0.39782 * math.sin(math.radians(l))
  cos_dec = math.cos(math.asin(sin_dec))
  cos_h = (math.cos(math.radians(zenith)) - (sin_dec * math.sin(math.radians(lat)))) / (cos_dec * math.cos(math.radians(lat)))
  if cos_h > 1 or cos_h < -1:
    return None
  h = (360.0 - math.degrees(math.acos(cos_h))) if sunrise else math.degrees(math.acos(cos_h))
  h /= 15.0
  local_mean = h + ra - (0.06571 * t) - 6.622
  utc_hour = (local_mean - lng_hour) % 24.0
  hour = int(utc_hour)
  minute = int((utc_hour - hour) * 60)
  second = int(round((((utc_hour - hour) * 60) - minute) * 60))
  if second >= 60:
    minute += 1; second -= 60
  if minute >= 60:
    hour = (hour + 1) % 24; minute -= 60
  return dt.datetime(date.year, date.month, date.day, hour, minute, second, tzinfo=dt.timezone.utc)


def _load_station_grid(config_path):
  payload = yaml.safe_load(Path(config_path).read_text(encoding='utf-8')) or {}
  return str((payload.get('ft8ctrl') or {}).get('my_grid') or '').strip().upper()


def _near(now, event, minutes):
  return event is not None and abs((now - event).total_seconds()) <= minutes * 60


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument('--config', default='/home/pi/FT8Commander/ft8ctrl.yaml')
  ap.add_argument('--wavelog-config', default='/etc/ft8commander/wavelog-v6.json')
  ap.add_argument('--state', default='/var/lib/wavelogstoat/ft8commander/dxcc-sync-state.json')
  ap.add_argument('--window-min', type=int, default=12)
  args = ap.parse_args()

  wcfg_path = Path(args.wavelog_config)
  if not wcfg_path.exists():
    print('Wavelog V6 not configured; greyline sync skipped')
    return 0
  wcfg = json.loads(wcfg_path.read_text(encoding='utf-8'))
  grid = _load_station_grid(args.config)
  try:
    lat, lon = geo.grid2latlon(grid)
  except Exception:
    lat = lon = None

  now = dt.datetime.now(dt.timezone.utc)
  events = []
  if lat is not None:
    events = [_sun_event_utc(now.date(), lat, lon, True), _sun_event_utc(now.date(), lat, lon, False)]
  events = [e for e in events if e is not None]
  if not events:
    # Offline/local fallback baseline requested by the operator: 07:00/19:00
    # local system time. Convert those local instants to UTC using astimezone.
    local_now = dt.datetime.now().astimezone()
    for hour in (7, 19):
      local_event = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
      events.append(local_event.astimezone(dt.timezone.utc))

  event = next((e for e in events if _near(now, e, args.window_min)), None)
  if event is None:
    return 0

  state_path = Path(args.state)
  try:
    state = json.loads(state_path.read_text(encoding='utf-8'))
  except Exception:
    state = {}
  key = event.strftime('%Y-%m-%dT%H:%MZ')
  if state.get('last_event') == key:
    return 0

  command = [
    sys.executable, '/home/pi/FT8Commander/wavelog_dxcc_sync.py',
    '--url', str(wcfg['url']), '--token-file', str(wcfg['token_file']),
  ]
  if wcfg.get('station_id') not in (None, ''):
    command += ['--station-id', str(wcfg['station_id'])]
  completed = subprocess.run(command, check=False)
  if completed.returncode != 0:
    return completed.returncode
  state_path.parent.mkdir(parents=True, exist_ok=True)
  state_path.write_text(json.dumps({'last_event': key, 'last_sync_utc': now.isoformat()}) + '\n', encoding='utf-8')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())
