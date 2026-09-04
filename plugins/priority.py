#
# BSD 3-Clause License
#
# Copyright (c) 2023, Fred W6BSD
# All rights reserved.
#
# Modified by F4EGM
#

from config import Config

from .base import CallSelector


class Priority(CallSelector):
  """DX-oriented selector with a success bias.

  Ordering:
    1. transcontinental CQ before same-continent CQ;
    2. within that class, a distance/SNR score;
    3. raw distance, then SNR as tie breakers.

  ``snr_km_weight`` says how many kilometres one dB of SNR is worth in the
  score. The default 300 km/dB keeps distance important while giving a useful
  completion-probability bias to healthier decodes.
  """

  def __init__(self):
    super().__init__()
    root = Config()['ft8ctrl']
    self.snr_km_weight = float(
      getattr(self.config, 'snr_km_weight', getattr(root, 'snr_km_weight', 300))
    )

  def get(self, band):
    return self.select_record(list(super().get(band)))

  def sort(self, records):
    def key(record):
      continent = (record.get('continent') or '').upper()
      transcontinental = 1 if continent and continent != self.continent else 0
      distance = record.get('distance')
      snr = record.get('snr')
      distance = float(distance) if distance is not None else 0.0
      snr = float(snr) if snr is not None else -99.0
      success_distance_score = distance + self.snr_km_weight * snr
      return (transcontinental, success_distance_score, distance, snr)

    return sorted(records, key=key, reverse=True)
