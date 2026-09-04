#!/usr/bin/env python3

import getpass
import html
import re
import urllib.parse
import urllib.request
from collections import Counter

URL = "https://logbook.qrz.com/api"
MAX = 250

key = getpass.getpass("QRZ API key: ")

def parse_adif_record(record):
    fields = {}
    pos = 0

    tag_re = re.compile(r"<([^:>]+):(\d+)(?::[^>]*)?>", re.I)

    while True:
        m = tag_re.search(record, pos)
        if not m:
            break

        name = m.group(1).lower()
        length = int(m.group(2))
        start = m.end()
        value = record[start:start + length]

        fields[name] = value
        pos = start + length

    return fields


after = 0
records = []
page = 0

while True:
    page += 1

    options = (
        f"STATUS:CONFIRMED,"
        f"MAX:{MAX},"
        f"AFTERLOGID:{after},"
        f"TYPE:ADIF"
    )

    data = urllib.parse.urlencode({
        "KEY": key,
        "ACTION": "FETCH",
        "OPTION": options
    }).encode()

    req = urllib.request.Request(
        URL,
        data=data,
        headers={
            "User-Agent": "FT8Commander-DXCC/0.1 (F4EGM)"
        }
    )

    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8", errors="replace")

    if not re.search(r"(?:^|&)RESULT=OK(?:&|$)", raw.split("&ADIF=", 1)[0]):
        raise RuntimeError(raw[:500])

    if "&ADIF=" not in raw:
        raise RuntimeError("Réponse QRZ sans champ ADIF")

    header, adif_encoded = raw.split("&ADIF=", 1)

    m = re.search(r"(?:^|&)COUNT=(\d+)", header)
    if not m:
        raise RuntimeError("COUNT absent de la réponse QRZ")

    count = int(m.group(1))

    adif = html.unescape(adif_encoded)

    page_records = []

    for chunk in re.split(r"<eor>", adif, flags=re.I):
        fields = parse_adif_record(chunk)

        if "app_qrzlog_logid" in fields:
            page_records.append(fields)

    print(
        f"Page {page}: QRZ={count}, "
        f"lus={len(page_records)}, "
        f"AFTERLOGID={after}"
    )

    if not page_records:
        break

    records.extend(page_records)

    logids = [
        int(r["app_qrzlog_logid"])
        for r in page_records
        if r.get("app_qrzlog_logid", "").isdigit()
    ]

    if not logids:
        raise RuntimeError("Aucun LOGID exploitable")

    after = max(logids) + 1

    if count < MAX:
        break


print()
print(f"QSO confirmés récupérés : {len(records)}")

dxcc = Counter()
countries = Counter()

for r in records:
    dxcc_id = r.get("dxcc", "").strip()
    country = r.get("country", "").strip()

    if dxcc_id:
        dxcc[dxcc_id] += 1

    if country:
        countries[country] += 1


with open("/tmp/qrz-confirmed-dxcc.tsv", "w", encoding="utf-8") as f:
    f.write("DXCC\tCOUNTRY\tQSO_CONFIRMED\n")

    pairs = {}

    for r in records:
        d = r.get("dxcc", "").strip()
        c = r.get("country", "").strip()

        if d:
            pairs.setdefault(d, c)

    for d in sorted(pairs, key=lambda x: int(x) if x.isdigit() else 999999):
        f.write(f"{d}\t{pairs[d]}\t{dxcc[d]}\n")


print(f"DXCC uniques confirmés : {len(dxcc)}")
print("Fichier : /tmp/qrz-confirmed-dxcc.tsv")
