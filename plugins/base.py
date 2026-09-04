#
# BSD 3-Clause License
#
# Copyright (c) 2023, Fred W6BSD
# All rights reserved.
#
# Modified by F4EGM
#

import dbm
import logging
import marshal
import operator
import os
import ssl
import time
import warnings
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib import request

from config import Config
from dbutils import connect_db, normalize_dxcc

# Silence Python 3.12 deprecation warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)

LOTW_URL = 'https://lotw.arrl.org/lotw-user-activity.csv'
LOTW_CACHE = Path('/var/lib/wavelogstoat/ft8commander/lotw_cache.dat')
LOTW_EXPIRE = 7 * 86400
LOTW_LASTSEEN = 270

MIN_SNR = -50
MAX_SNR = +50
CONTINENTS = {'AF', 'AS', 'EU', 'NA', 'OC', 'SA'}

ZERO = marshal.dumps(0)


class BlackList:
  # Singleton class

  def __new__(cls):
    if hasattr(cls, '_instance') and isinstance(cls._instance, cls):
      return cls._instance

    cls.blacklist = []
    cls._instance = super(BlackList, cls).__new__(cls)
    cls.log = logging.getLogger(f'ft8ctrl.{cls.__name__}')
    config = Config()
    try:
      configured = config.get('BlackList', []) or []
      cls.blacklist = [c.upper() for c in configured]
    except KeyError:
      pass

    cls.log.info('Blacklist: %d callsigns blacklisted.', len(cls.blacklist))
    return cls._instance

  def check(self, call):
    call = call.upper()
    return call in self.blacklist

  def __contains__(self, call):
    return self.check(call)


class CallSelector(ABC):
  # pylint: disable=too-many-instance-attributes

  REQ = ('SELECT * FROM cqcalls WHERE '
         'status = 0 AND band = ? AND time > ?')

  def __init__(self):
    config = Config()
    self.config = config.get(self.__class__.__name__)
    if self.config is None:
      raise RuntimeError(f'Missing configuration section: {self.__class__.__name__}')

    self.log = logging.getLogger(f'ft8ctrl.{self.__class__.__name__}')

    self.debug = getattr(self.config, 'debug', False)
    if self.debug:
      self.log.setLevel(logging.DEBUG)

    self.blacklist = BlackList()
    self.db_name = Path(config['ft8ctrl.db_name']).expanduser()
    self.min_snr = getattr(self.config, 'min_snr', MIN_SNR)
    self.max_snr = getattr(self.config, 'max_snr', MAX_SNR)
    self.delta = getattr(self.config, 'delta', 35)
    ft8_config = config['ft8ctrl']
    self.continent = getattr(
      self.config, 'my_continent', getattr(ft8_config, 'my_continent', 'NA')
    ).upper()
    self.log.debug('My continent %s', self.continent)

    if getattr(self.config, 'lotw_users_only', False):
      self.lotw = LOTW()
      self.log.info('%s reply to LOTW users only', self.__class__.__name__)
    else:
      self.lotw = Nothing()

  @abstractmethod
  def get(self, band):
    return self._get(band)

  def _get(self, band):
    """Return fresh candidates for one band.

    The historical implementation cached this method globally for three
    seconds. That cache was not keyed by band and could also hide a newly
    decoded DX during the exact interval where we want to react quickly.
    SQLite is local and the query is small, so correctness is preferable here.
    """
    records = []
    start = datetime.utcnow() - timedelta(seconds=self.delta)
    with connect_db(self.db_name) as conn:
      curs = conn.cursor()
      curs.execute(self.REQ, (band, start))
      for record in (dict(r) for r in curs):
        (record['country'], record['continent'], record['cqzone'], record['ituzone']) = normalize_dxcc(
          record.get('call'), record.get('country'), record.get('continent'),
          record.get('cqzone'), record.get('ituzone')
        )
        extra = (record.get('extra') or '').upper()
        continent = (record.get('continent') or '').upper()

        # Respect explicit continent-directed CQs (CQ NA, CQ AS, ...).
        # Do not reinterpret the informal "CQ DX" token as a hard continent
        # exclusion: its meaning is operator-dependent, so ranking is based on
        # the caller's actual continent/distance instead.
        if extra in CONTINENTS and extra != self.continent:
          self.log.debug('Ignore directed CQ: %s (%s) calling %s',
                         record['call'], continent or '?', extra)
          continue

        record['coef'] = self.coefficient(record.get('distance'), record.get('snr'))
        records.append(record)
    return records

  def select_record(self, records):
    records = self.sort(records)
    for record in records:
      snr = record.get('snr')
      if snr is None or not self.min_snr <= snr <= self.max_snr:
        continue
      if record['call'] in self.blacklist:
        self.log.debug('%s is blacklisted', record['call'])
        continue
      if record['call'] not in self.lotw:
        self.log.debug('%s is not an LoTW user', record['call'])
        continue
      return record
    return None

  @staticmethod
  def coefficient(dist, snr):
    if dist is None or snr is None:
      return 0
    return dist * 10**(snr / 10)

  @staticmethod
  def sort(records):
    return sorted(records, key=operator.itemgetter('snr'), reverse=True)


class Nothing:
  # pylint: disable=too-few-public-methods
  def __contains__(self, call):
    return True


class LOTW:
  # Singleton class

  def __new__(cls):
    if hasattr(cls, '_instance') and isinstance(cls._instance, cls):
      return cls._instance

    cls.log = logging.getLogger(f'ft8ctrl.{cls.__name__}')
    cls.log.info('LOTW database: %s (%d days)', LOTW_CACHE, LOTW_LASTSEEN)

    if not LOTW_CACHE.parent.exists():
      LOTW_CACHE.parent.mkdir(parents=True)

    try:
      with dbm.open(str(LOTW_CACHE), 'r') as fdb:
        age = marshal.loads(fdb.get('__age__', ZERO))
    except dbm.error:
      age = 0

    if time.time() > age + LOTW_EXPIRE:
      cls.log.info('LOTW cache expired. Reload...')
      try:
        context = ssl._create_unverified_context()
        with request.urlopen(LOTW_URL, context=context, timeout=20) as response:
          if response.status != 200:
            raise OSError(f'Download error: HTTP {response.status}')
          LOTW.store_lotw(response)
      except OSError as err:
        if age:
          cls.log.warning('LOTW update unavailable; using existing cache: %s', err)
        else:
          cls.log.error('LOTW cache unavailable and download failed: %s', err)
          raise

    cls.log.info('LOTW lookup database ready')
    cls.__contains__ = lru_cache(maxsize=512)(cls.__contains__)

    cls._instance = super(LOTW, cls).__new__(cls)
    return cls._instance

  @staticmethod
  def store_lotw(response):
    start_date = datetime.now() - timedelta(days=LOTW_LASTSEEN)
    charset = response.info().get_content_charset('utf-8')
    try:
      with dbm.open(str(LOTW_CACHE), 'c') as fdb:
        for line in (r.decode(charset) for r in response):
          fields = list(line.rstrip().split(','))
          if len(fields) < 2:
            continue
          try:
            if datetime.strptime(fields[1], '%Y-%m-%d') > start_date:
              fdb[fields[0].upper()] = marshal.dumps(fields[1])
          except ValueError:
            continue
        fdb['__age__'] = marshal.dumps(int(time.time()))
    except dbm.error as err:
      raise IOError from err

  def __contains__(self, key):
    try:
      with dbm.open(str(LOTW_CACHE), 'r') as fdb:
        return key.upper() in fdb
    except dbm.error as err:
      logging.error(err)
      raise SystemError(err) from None

  def __repr__(self):
    try:
      _st = os.stat(LOTW_CACHE)
      fdate = float(_st.st_mtime)
      expire = LOTW_EXPIRE - int(time.time() - fdate)
      if expire < 1:
        raise IOError
    except IOError:
      return f'<LOTW id:{id(self)}> LOTW cache "Expired"'

    return f'<LOTW id:{id(self)}> LOTW cache expire in: {expire} seconds'
