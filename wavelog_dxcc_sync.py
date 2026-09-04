#!/home/pi/venv/bin/python
"""Synchronize worked/confirmed DXCC-by-band memory from Wavelog API v2.

Network/API failure is fail-safe: the existing cache is never replaced unless
both the worked-QSO and confirmation passes complete successfully.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from DXEntity import DXCC

from v60_dxcc import DXCCBandMemory

ADIF_TAG = re.compile(r'<([^:>]+):(\d+)(?::[^>]*)?>', re.I)


def parse_adif_records(adif: str):
  records = []
  for raw in re.split(r'<eor>', adif, flags=re.I):
    fields = {}
    pos = 0
    while True:
      match = ADIF_TAG.search(raw, pos)
      if not match:
        break
      name = match.group(1).upper()
      length = int(match.group(2))
      start = match.end()
      fields[name] = raw[start:start + length].strip()
      pos = start + length
    if fields.get('CALL'):
      records.append(fields)
  return records


def request_json(url, token, timeout=30):
  req = urllib.request.Request(url, headers={
    'Authorization': f'Bearer {token}',
    'Accept': 'application/json',
    'User-Agent': 'FT8Commander-V6-DXCC-Sync/1.0',
  })
  with urllib.request.urlopen(req, timeout=timeout) as response:
    return json.loads(response.read().decode('utf-8', errors='replace'))


def endpoint(base, resource, params):
  base = base.rstrip('/')
  if not base.endswith('/index.php'):
    base += '/index.php'
  return f'{base}/api/v2/{resource}?' + urllib.parse.urlencode(params)


def fetch_worked(base, token, station_id=None, mode='FT8'):
  memory = {}
  since_id = 0
  while True:
    params = {'format': 'adif', 'since_id': since_id, 'per_page': 5000, 'mode': mode}
    if station_id:
      params['station_id'] = station_id
    payload = request_json(endpoint(base, 'qso', params), token)
    data = payload.get('data') or {}
    adif = data.get('adif') or ''
    records = parse_adif_records(adif)
    for record in records:
      try:
        dxcc = int(record.get('DXCC', ''))
      except ValueError:
        continue
      band = str(record.get('BAND', '')).lower()
      if band:
        memory.setdefault(band, set()).add(dxcc)
    next_id = int(data.get('lastfetchedid') or since_id)
    meta = payload.get('meta') or {}
    if not meta.get('has_more') or next_id <= since_id:
      break
    since_id = next_id
  return memory


def fetch_confirmed(base, token, station_id=None, mode='FT8', types='lotw,qrz'):
  result = {}
  lookup = DXCC().lookup
  page = 1
  while True:
    params = {'type': types, 'mode': mode, 'page': page, 'per_page': 1000}
    if station_id:
      params['station_id'] = station_id
    payload = request_json(endpoint(base, 'confirmation', params), token)
    for row in payload.get('data') or []:
      call = str(row.get('callsign') or '').upper()
      band = str(row.get('band') or '').lower()
      if not call or not band:
        continue
      try:
        info = lookup(call)
      except Exception:
        info = None
      dxcc = None
      if info is not None:
        for attr in ('adif', 'dxcc', 'id'):
          try:
            value = getattr(info, attr)
          except Exception:
            continue
          try:
            dxcc = int(value)
            break
          except (TypeError, ValueError):
            continue
      if dxcc is not None:
        result.setdefault(band, set()).add(dxcc)
    meta = payload.get('meta') or {}
    if not meta.get('has_more'):
      break
    page += 1
  return result


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('--url', required=True, help='Wavelog base URL')
  parser.add_argument('--token-file', required=True)
  parser.add_argument('--station-id')
  parser.add_argument('--cache', default='/run/ft8commander/dxcc-memory.json')
  parser.add_argument('--persistent-cache', default='/var/lib/wavelogstoat/ft8commander/dxcc-memory.json')
  parser.add_argument('--mode', default='FT8')
  parser.add_argument('--confirmations', default='lotw,qrz')
  args = parser.parse_args()

  token = Path(args.token_file).read_text(encoding='utf-8').strip()
  if not token:
    raise SystemExit('Empty Wavelog token')

  # Fetch everything before replacing either cache.
  worked = fetch_worked(args.url, token, args.station_id, args.mode)
  confirmed = fetch_confirmed(args.url, token, args.station_id, args.mode, args.confirmations)

  payload = {
    'schema': 1,
    'source': 'wavelog-api-v2',
    'worked': {band: sorted(values) for band, values in sorted(worked.items())},
    'confirmed': {band: sorted(values) for band, values in sorted(confirmed.items())},
  }
  # Re-use the atomic writer, then mirror to persistent storage for offline boot.
  memory = DXCCBandMemory(args.cache)
  memory.worked = {band: set(values) for band, values in worked.items()}
  memory.confirmed = {band: set(values) for band, values in confirmed.items()}
  memory.save()

  persistent = Path(args.persistent_cache)
  persistent.parent.mkdir(parents=True, exist_ok=True)
  persistent.write_text(Path(args.cache).read_text(encoding='utf-8'), encoding='utf-8')

  print(json.dumps({
    'worked_bands': {band: len(values) for band, values in sorted(worked.items())},
    'confirmed_bands': {band: len(values) for band, values in sorted(confirmed.items())},
    'cache': args.cache,
    'persistent_cache': str(persistent),
  }, sort_keys=True))


if __name__ == '__main__':
  main()
