#
# BSD 3-Clause License
#
# Copyright (c) 2023, Fred W6BSD
# All rights reserved.
#
# Modified by F4EGM
#

from DXEntity import DXCC

from .base import CallSelector


class Continent(CallSelector):

  CONTINENTS = ["AF", "AS", "EU", "NA", "OC", "SA"]

  def __init__(self):
    super().__init__()
    self.c_list = set([])
    self.reverse = getattr(self.config, 'reverse', False)
    continents = getattr(self.config, 'list', [])
    continents = [continents] if isinstance(continents, str) else continents

    for cnt in continents:
      if cnt in self.CONTINENTS:
        self.c_list.add(cnt)
      else:
        self.log.warning('Ignoring continent: "%s" is not valid', cnt)

  def get(self, band):
    records = []
    for record in super().get(band):
      if (record['continent'] in self.c_list) ^ self.reverse:
        records.append(record)
    return self.select_record(records)


class Country(CallSelector):

  def __init__(self):
    super().__init__()
    dxcc = DXCC()
    self.c_list = set([])
    self.reverse = getattr(self.config, 'reverse', False)
    self.band_memory = getattr(self.config, 'band_memory', False)
    entities = getattr(self.config, 'list', [])
    entities = [entities] if isinstance(entities, str) else entities

    for country in entities:
      if dxcc.isentity(country):
        self.c_list.add(country)
      else:
        self.log.warning('Ignoring country: "%s" is not a valid entity', country)

  def get(self, band):
    records = []
    for record in super().get(band):
      # V6 band_memory delegates wanted/missing DXCC decisions to the central
      # DXCC+band cache. Keep the old list intact for rollback/manual policy,
      # but do not use a global country exclusion to hide missing bands.
      if self.band_memory or ((record['country'] in self.c_list) ^ self.reverse):
        records.append(record)
    return self.select_record(records)
