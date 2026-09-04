#
# BSD 3-Clause License
#
# Copyright (c) 2023, Fred W6BSD
# All rights reserved.
#
# Modified by F4EGM
#

import json
import logging
import re
import sqlite3
import time
from datetime import datetime
from enum import Enum
from threading import Thread

import DXEntity

import geo


class DBCommand(Enum):
  INSERT = 1
  STATUS = 2
  DELETE = 3
  SYNC = 4
  CLEAR = 5


SQL_TABLE = """
CREATE TABLE IF NOT EXISTS cqcalls
(
  call TEXT,
  extra TEXT,
  time TIMESTAMP,
  status INTEGER,
  snr INTEGER,
  grid TEXT,
  lat REAL,
  lon REAL,
  distance REAL,
  azimuth REAL,
  country TEXT,
  continent TEXT,
  cqzone INTEGER,
  ituzone INTEGER,
  frequency INTEGER,
  band INTEGER,
  packet JSON
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_call on cqcalls (call, band);
CREATE INDEX IF NOT EXISTS idx_time on cqcalls (time DESC);
CREATE INDEX IF NOT EXISTS idx_grid on cqcalls (grid ASC);
CREATE INDEX IF NOT EXISTS idx_candidate on cqcalls (band, status, time DESC);
"""


logger = logging.getLogger('ft8ctrl.dbutils')


# Guantanamo Bay uses KG4 followed by a TWO-letter suffix (KG4AA-KG4ZZ).
# Ordinary US FCC calls can use KG4 followed by THREE suffix letters. Prefix-
# only DXCC databases may therefore misclassify e.g. KG4ILP as Guantanamo.
KG4_US_CALL_RE = re.compile(r'^KG4[A-Z]{3}$', re.IGNORECASE)


def normalize_dxcc(call, country, continent, cqzone=None, ituzone=None):
  """Apply narrow DXCC corrections that cannot be expressed by prefix alone."""
  base_call = (call or '').upper().split('/', 1)[0]
  if KG4_US_CALL_RE.fullmatch(base_call):
    if country != 'United States':
      logger.debug('DXCC override: %s %s -> United States', call, country)
    # The exact CQ/ITU zone depends on the operator location; do not retain
    # Guantanamo's zones when correcting a regular US KG4xxx callsign.
    return 'United States', 'NA', None, None
  return country, continent, cqzone, ituzone


def get_band(key):
  _bands = {
    1: 160,
    3: 80,
    7: 40,
    10: 30,
    14: 20,
    18: 17,
    21: 15,
    24: 12,
    28: 10,
    50: 6,
  }

  key = int(key / 10**6)
  if key not in _bands:
    return 0
  return _bands[key]


class DBJSONEncoder(json.JSONEncoder):
  """Special JSON encoder capable of encoding sets and datetimes."""
  def default(self, o):
    if isinstance(o, set):
      return {'__type__': 'set', 'value': list(o)}
    if isinstance(o, datetime):
      return {'__type__': 'datetime', 'value': o.timestamp()}
    return super().default(o)


class DBJSONDecoder(json.JSONDecoder):
  """Special JSON decoder matching DBJSONEncoder."""
  def __init__(self):
    super().__init__(object_hook=self.dict_to_object)

  def dict_to_object(self, json_obj):
    if '__type__' not in json_obj:
      return json_obj
    if json_obj['__type__'] == 'set':
      return set(json_obj['value'])
    if json_obj['__type__'] == 'datetime':
      return datetime.fromtimestamp(json_obj['value'])
    return json_obj


sqlite3.register_adapter(dict, DBJSONEncoder().encode)
sqlite3.register_converter('JSON', lambda x: DBJSONDecoder().decode(x.decode('utf-8')))


def connect_db(db_name):
  try:
    conn = sqlite3.connect(
      db_name,
      timeout=15,
      detect_types=sqlite3.PARSE_DECLTYPES,
      isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
  except sqlite3.OperationalError as err:
    logger.error('Database: %s - %s', db_name, err)
    raise SystemExit('Database Error') from None
  return conn


def create_db(db_name):
  logger.info('Database: %s', db_name)
  with connect_db(db_name) as conn:
    curs = conn.cursor()
    # Reads (selector) and writes (decode worker) happen concurrently. WAL
    # avoids most reader/writer blocking on the Raspberry Pi while NORMAL is
    # sufficient for this rebuildable candidate cache.
    curs.execute('PRAGMA journal_mode=WAL')
    curs.execute('PRAGMA synchronous=NORMAL')
    curs.executescript(SQL_TABLE)


def get_call(db_name, call, band=None):
  """Return the most recent record for call, optionally on one band."""
  if band is None:
    req = 'SELECT * FROM cqcalls WHERE call = ? ORDER BY time DESC LIMIT 1'
    args = (call,)
  else:
    req = 'SELECT * FROM cqcalls WHERE call = ? AND band = ? ORDER BY time DESC LIMIT 1'
    args = (call, band)
  with connect_db(db_name) as conn:
    curs = conn.cursor()
    curs.execute(req, args)
    record = curs.fetchone()
  return dict(record) if record else {}


class DBInsert(Thread):

  # Refresh all volatile decode fields on every new decode. The original code
  # only refreshed snr/packet, which left the timestamp stale; a station that
  # kept calling CQ could therefore age out of the selector delta window.
  # status is intentionally preserved so an active/finished station is not
  # accidentally returned to status=0 by a later decode.
  INSERT = """
  INSERT INTO cqcalls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  ON CONFLICT(call, band) DO UPDATE SET
    extra = excluded.extra,
    time = excluded.time,
    snr = excluded.snr,
    grid = excluded.grid,
    lat = excluded.lat,
    lon = excluded.lon,
    distance = excluded.distance,
    azimuth = excluded.azimuth,
    country = excluded.country,
    continent = excluded.continent,
    cqzone = excluded.cqzone,
    ituzone = excluded.ituzone,
    frequency = excluded.frequency,
    packet = excluded.packet
  WHERE status <> 2
  """
  UPDATE = 'UPDATE cqcalls SET status=? WHERE status <> 2 and call = ? and band = ?'
  # Delete any unworked candidate, whether WSJT-X had already emitted a TX
  # status or the attempt was pre-empted just before the first transmission.
  DELETE = 'DELETE FROM cqcalls WHERE status < 2 AND call = ? and band = ?'
  CLEAR_BAND = 'DELETE FROM cqcalls WHERE status < 2 AND band = ?'
  CLEAR_ALL = 'DELETE FROM cqcalls WHERE status < 2'

  def __init__(self, db_name, queue, grid):
    super().__init__()
    self.db_name = db_name
    self.queue = queue
    self.origin = geo.grid2latlon(grid)
    self.dxe_lookup = DXEntity.DXCC().lookup

  def enrich(self, data):
    grid = data.get('grid')
    if grid:
      try:
        lat, lon = geo.grid2latlon(grid)
        data['lat'], data['lon'] = lat, lon
        data['distance'] = geo.distance(self.origin, (lat, lon))
        data['azimuth'] = geo.azimuth(self.origin, (lat, lon))
      except (RuntimeError, ValueError, TypeError) as err:
        logger.warning('Invalid locator %s for %s: %s', grid, data.get('call'), err)
        data['grid'] = None
        data['lat'] = data['lon'] = None
        data['distance'] = data['azimuth'] = None
    else:
      data['grid'] = None
      data['lat'] = data['lon'] = None
      data['distance'] = data['azimuth'] = None

    try:
      dxentity = self.dxe_lookup(data['call'])
      data['country'] = dxentity.country
      data['continent'] = dxentity.continent
      data['cqzone'] = dxentity.cqzone
      data['ituzone'] = dxentity.ituzone
      (data['country'], data['continent'], data['cqzone'], data['ituzone']) = normalize_dxcc(
        data['call'], data['country'], data['continent'],
        data['cqzone'], data['ituzone']
      )
    except KeyError:
      logger.error('DXEntity for %s not found; ignoring candidate', data.get('call'))
      return False
    return True

  def run(self):
    # pylint: disable=no-member
    logger.info('Database Insert thread started')
    conn = connect_db(self.db_name)
    while True:
      cmd, data = self.queue.get()
      try:
        if cmd == DBCommand.INSERT:
          if not self.enrich(data):
            continue
          DBInsert.write(conn, data)
        elif cmd == DBCommand.STATUS:
          DBInsert.status(conn, data)
        elif cmd == DBCommand.DELETE:
          DBInsert.delete(conn, data)
        elif cmd == DBCommand.SYNC:
          # Queue barrier used by the sequencer: when this Event is set, every
          # DB command queued before it has been processed.
          data.set()
        elif cmd == DBCommand.CLEAR:
          DBInsert.clear(conn, data)
        else:
          logger.warning('Unknown DB command: %r', cmd)
      except sqlite3.OperationalError as err:
        logger.warning('Queue len: %d - Error: %s', self.queue.qsize(), err)
      except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as err:
        # Never let a malformed decode kill the DB worker thread.
        logger.exception('DB worker error: %s; data=%r', err, data)
      finally:
        self.queue.task_done()

  @staticmethod
  def write(conn, call_info):
    # pylint: disable=no-member
    data = type('CallInfo', (object,), call_info)
    with conn:
      curs = conn.cursor()
      curs.execute(DBInsert.INSERT, (
        data.call,
        getattr(data, 'extra', None),
        data.packet['Time'],
        0,
        data.packet['SNR'],
        data.grid,
        data.lat,
        data.lon,
        data.distance,
        data.azimuth,
        data.country,
        data.continent,
        data.cqzone,
        data.ituzone,
        data.frequency,
        data.band,
        data.packet,
      ))
      if not curs.rowcount:
        logger.debug('DB Write: already worked %s on %d band', data.call, data.band)
      else:
        logger.debug('DB Write: %s, %s, %s, %s', data.call, data.continent,
                     data.grid, data.country)

  @staticmethod
  def status(conn, data):
    with conn:
      curs = conn.cursor()
      curs.execute(DBInsert.UPDATE, (data['status'], data['call'], data['band']))
      logger.debug('%s (%s, %s, %d)', DBInsert.UPDATE, data['status'],
                   data['call'], data['band'])

  @staticmethod
  def delete(conn, data):
    with conn:
      curs = conn.cursor()
      curs.execute(DBInsert.DELETE, (data['call'], data['band']))
      logger.debug('%s (%s:%s)', DBInsert.DELETE, data['call'], data['band'])

  @staticmethod
  def clear(conn, data):
    """Discard unworked decode candidates after WSJT-X clears activity."""
    band = data.get('band') if data else None
    with conn:
      curs = conn.cursor()
      if band:
        curs.execute(DBInsert.CLEAR_BAND, (band,))
        logger.debug('Cleared unworked candidates on %dm', band)
      else:
        curs.execute(DBInsert.CLEAR_ALL)
        logger.debug('Cleared all unworked candidates')


class Purge(Thread):
  REQ = "DELETE FROM cqcalls WHERE status < 2 AND time < datetime('now','{} minute');"

  def __init__(self, db_name, purge_time):
    super().__init__()
    self.db_name = db_name
    self.purge_time = abs(purge_time) * -1
    self.req = self.REQ.format(self.purge_time)
    logger.debug(self.req)

  def run(self):
    count = 0
    logger.info('Purge thread started (retry_time %d minutes)', abs(self.purge_time))
    conn = connect_db(self.db_name)
    while True:
      with conn:
        try:
          curs = conn.cursor()
          curs.execute(self.req)
          count = curs.rowcount
        except sqlite3.OperationalError as err:
          logger.error(err)
      logger.debug('Purge %d Records', count)
      time.sleep(60)
