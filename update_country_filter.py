#!/var/lib/wavelogstoat/ft8commander/venv/bin/python

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request

from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml
from DXEntity import DXCC


QRZ_URL = "https://logbook.qrz.com/api"
PAGE_SIZE = 250

KEYFILE = Path(
    "/var/lib/wavelogstoat/ft8commander/qrz-api-key"
)

YAMLFILE = Path(
    "/home/pi/FT8Commander/ft8ctrl.yaml"
)

REPORTFILE = Path(
    "/var/lib/wavelogstoat/ft8commander/qrz-confirmed-dxcc.tsv"
)

SERVICE = "ft8commander.service"


ADIF_TAG = re.compile(
    r"<([^:>]+):(\d+)(?::[^>]*)?>",
    re.I
)


def parse_adif_record(record):
    fields = {}
    pos = 0

    while True:
        m = ADIF_TAG.search(record, pos)

        if not m:
            break

        name = m.group(1).lower()
        length = int(m.group(2))
        start = m.end()
        value = record[start:start + length]

        fields[name] = value
        pos = start + length

    return fields


def fetch_qrz_confirmed(key):
    after = 0
    page = 0
    records = []
    seen_logids = set()

    while True:
        page += 1

        option = (
            f"STATUS:CONFIRMED,"
            f"MAX:{PAGE_SIZE},"
            f"AFTERLOGID:{after},"
            f"TYPE:ADIF"
        )

        data = urllib.parse.urlencode({
            "KEY": key,
            "ACTION": "FETCH",
            "OPTION": option,
        }).encode()

        request = urllib.request.Request(
            QRZ_URL,
            data=data,
            headers={
                "User-Agent":
                    "FT8Commander-DXCC-Updater/1.0 (F4EGM)"
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=60
        ) as response:
            raw = response.read().decode(
                "utf-8",
                errors="replace"
            )

        header, separator, encoded_adif = raw.partition(
            "&ADIF="
        )

        params = urllib.parse.parse_qs(
            header,
            keep_blank_values=True
        )

        result = params.get("RESULT", [""])[0]

        if result != "OK":
            raise RuntimeError(
                "Erreur API QRZ:\n" + raw[:1000]
            )

        try:
            count = int(
                params.get("COUNT", ["0"])[0] or "0"
            )
        except ValueError:
            raise RuntimeError(
                "COUNT QRZ invalide:\n" + header
            )

        adif = html.unescape(
            encoded_adif if separator else ""
        )

        page_records = []

        for chunk in re.split(
            r"<eor>",
            adif,
            flags=re.I
        ):
            fields = parse_adif_record(chunk)

            logid = fields.get(
                "app_qrzlog_logid",
                ""
            ).strip()

            if not logid:
                continue

            if logid in seen_logids:
                continue

            seen_logids.add(logid)
            page_records.append(fields)

        print(
            f"QRZ page {page}: "
            f"{count} QSO, "
            f"{len(page_records)} lus"
        )

        if count != len(page_records):
            raise RuntimeError(
                "Le nombre de QSO parses ne correspond "
                f"pas a COUNT: {len(page_records)} != {count}"
            )

        records.extend(page_records)

        if count < PAGE_SIZE:
            break

        logids = [
            int(r["app_qrzlog_logid"])
            for r in page_records
            if r.get(
                "app_qrzlog_logid",
                ""
            ).isdigit()
        ]

        if not logids:
            raise RuntimeError(
                "Aucun LOGID exploitable pour la pagination"
            )

        new_after = max(logids) + 1

        if new_after <= after:
            raise RuntimeError(
                "Pagination QRZ bloquee"
            )

        after = new_after

    return records


def extract_confirmed_dxcc(records):
    confirmed = set()
    counts = Counter()
    qrz_country = {}

    for record in records:
        value = record.get("dxcc", "").strip()

        if not value.isdigit():
            continue

        dxcc = int(value)

        if dxcc <= 0:
            continue

        confirmed.add(dxcc)
        counts[dxcc] += 1

        country = record.get(
            "country",
            ""
        ).strip()

        if country and dxcc not in qrz_country:
            qrz_country[dxcc] = country

    return confirmed, counts, qrz_country


def build_dxentity_mapping(target_dxcc):
    dx = DXCC()
    mapping = {}

    for prefixes in dx.entities.values():
        for prefix in prefixes:
            try:
                result = dx.lookup(prefix)
            except Exception:
                continue

            try:
                adif = int(result.adif)
            except Exception:
                continue

            if adif not in target_dxcc:
                continue

            mapping.setdefault(
                adif,
                set()
            ).add(result.country)

    return mapping


def write_report(
    confirmed,
    counts,
    qrz_country,
    mapping
):
    REPORTFILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with REPORTFILE.open(
        "w",
        encoding="utf-8"
    ) as f:
        f.write(
            "DXCC\tQRZ_COUNTRY\tQSO_CONFIRMED\t"
            "DXENTITY_COUNTRY\n"
        )

        for dxcc in sorted(confirmed):
            names = " | ".join(
                sorted(mapping.get(dxcc, []))
            )

            f.write(
                f"{dxcc}\t"
                f"{qrz_country.get(dxcc, '')}\t"
                f"{counts[dxcc]}\t"
                f"{names}\n"
            )


def replace_country_list(names):
    original = YAMLFILE.read_text(
        encoding="utf-8"
    )

    config = yaml.safe_load(original)

    if not isinstance(config, dict):
        raise RuntimeError(
            "ft8ctrl.yaml invalide"
        )

    if "Country" not in config:
        raise RuntimeError(
            "Bloc Country absent de ft8ctrl.yaml"
        )

    lines = original.splitlines(
        keepends=True
    )

    country_start = None

    for i, line in enumerate(lines):
        if line.rstrip("\r\n") == "Country:":
            country_start = i
            break

    if country_start is None:
        raise RuntimeError(
            "Bloc Country introuvable dans le texte YAML"
        )

    country_end = len(lines)

    for i in range(
        country_start + 1,
        len(lines)
    ):
        stripped = lines[i].strip()

        if not stripped:
            continue

        if stripped.startswith("#"):
            continue

        if not lines[i][0].isspace():
            country_end = i
            break

    reverse_index = None
    list_index = None

    for i in range(
        country_start + 1,
        country_end
    ):
        if re.match(
            r"^  reverse\s*:",
            lines[i]
        ):
            reverse_index = i

        if re.match(
            r"^  list\s*:",
            lines[i]
        ):
            list_index = i
            break

    if list_index is None:
        raise RuntimeError(
            "Country.list introuvable"
        )

    if reverse_index is None:
        lines.insert(
            list_index,
            "  reverse: True\n"
        )
        list_index += 1
        country_end += 1
    else:
        newline = (
            "\r\n"
            if lines[reverse_index].endswith(
                "\r\n"
            )
            else "\n"
        )

        lines[reverse_index] = (
            "  reverse: True" + newline
        )

    list_end = country_end

    for i in range(
        list_index + 1,
        country_end
    ):
        line = lines[i]

        if not line.strip():
            continue

        spaces = (
            len(line)
            - len(line.lstrip(" "))
        )

        if spaces == 2:
            list_end = i
            break

    newline = (
        "\r\n"
        if lines[list_index].endswith("\r\n")
        else "\n"
    )

    items = [
        "    - "
        + json.dumps(
            name,
            ensure_ascii=False
        )
        + newline
        for name in sorted(names)
    ]

    new_lines = (
        lines[:list_index + 1]
        + items
        + lines[list_end:]
    )

    updated = "".join(new_lines)

    check = yaml.safe_load(updated)

    country = check.get("Country", {})

    if country.get("reverse") is not True:
        raise RuntimeError(
            "Validation echouee: Country.reverse != True"
        )

    actual = country.get("list", [])

    if set(actual) != set(names):
        raise RuntimeError(
            "Validation echouee: Country.list "
            "ne correspond pas a la liste generee"
        )

    if len(actual) != len(names):
        raise RuntimeError(
            "Validation echouee: doublons dans Country.list"
        )

    if updated == original:
        return False, None

    timestamp = datetime.now().strftime(
        "%Y%m%d-%H%M%S"
    )

    backup = YAMLFILE.with_name(
        YAMLFILE.name
        + ".bak-qrz-"
        + timestamp
    )

    shutil.copy2(
        YAMLFILE,
        backup
    )

    mode = YAMLFILE.stat().st_mode

    temporary = YAMLFILE.with_name(
        YAMLFILE.name + ".new"
    )

    temporary.write_text(
        updated,
        encoding="utf-8"
    )

    os.chmod(
        temporary,
        mode
    )

    os.replace(
        temporary,
        YAMLFILE
    )

    # Relire le fichier reel apres remplacement.
    final_check = yaml.safe_load(
        YAMLFILE.read_text(
            encoding="utf-8"
        )
    )

    final_country = final_check["Country"]

    if (
        final_country.get("reverse") is not True
        or set(final_country.get("list", []))
        != set(names)
    ):
        shutil.copy2(
            backup,
            YAMLFILE
        )
        raise RuntimeError(
            "Validation finale echouee; "
            "ancienne configuration restauree"
        )

    return True, backup


def restart_ft8commander():
    print(
        "Redemarrage de FT8Commander..."
    )

    subprocess.run(
        [
            "sudo",
            "systemctl",
            "restart",
            SERVICE,
        ],
        check=True,
    )

    result = subprocess.run(
        [
            "systemctl",
            "is-active",
            "--quiet",
            SERVICE,
        ]
    )

    if result.returncode != 0:
        raise RuntimeError(
            "FT8Commander n'est pas actif "
            "apres le redemarrage"
        )

    print(
        "FT8Commander : active"
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Actualise Country.list depuis "
            "les DXCC confirmes QRZ."
        )
    )

    parser.add_argument(
        "--no-restart",
        action="store_true",
        help=(
            "Met a jour le YAML sans "
            "redemarrer FT8Commander"
        ),
    )

    args = parser.parse_args()

    if not KEYFILE.exists():
        raise RuntimeError(
            f"Cle QRZ absente: {KEYFILE}"
        )

    key = KEYFILE.read_text(
        encoding="utf-8"
    ).strip()

    if not key:
        raise RuntimeError(
            "Le fichier de cle QRZ est vide"
        )

    print(
        "Recuperation des confirmations QRZ..."
    )

    records = fetch_qrz_confirmed(key)

    confirmed, counts, qrz_country = (
        extract_confirmed_dxcc(records)
    )

    if not confirmed:
        raise RuntimeError(
            "Aucun DXCC confirme retourne par QRZ; "
            "mise a jour annulee"
        )

    print()
    print(
        f"QSO confirmes QRZ : {len(records)}"
    )
    print(
        f"DXCC confirmes QRZ : {len(confirmed)}"
    )

    mapping = build_dxentity_mapping(
        confirmed
    )

    mapped_dxcc = set(mapping)
    missing = sorted(
        confirmed - mapped_dxcc
    )

    names = {
        name
        for dxcc_names in mapping.values()
        for name in dxcc_names
    }

    print(
        f"DXCC reconnus par DXEntity : "
        f"{len(mapped_dxcc)}"
    )
    print(
        f"Noms Country generes : {len(names)}"
    )

    if missing:
        print(
            "ATTENTION - DXCC sans correspondance "
            "DXEntity : "
            + ", ".join(map(str, missing))
        )

    write_report(
        confirmed,
        counts,
        qrz_country,
        mapping
    )

    changed, backup = replace_country_list(
        names
    )

    print()
    print(
        f"Rapport : {REPORTFILE}"
    )

    if changed:
        print(
            f"Country.list mis a jour : "
            f"{len(names)} noms"
        )
        print(
            f"Sauvegarde YAML : {backup}"
        )

        if not args.no_restart:
            restart_ft8commander()
        else:
            print(
                "Redemarrage non effectue "
                "(--no-restart)"
            )
    else:
        print(
            "Country.list est deja a jour."
        )

    print()
    print("Termine.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nInterrompu.",
            file=sys.stderr
        )
        sys.exit(130)
    except Exception as exc:
        print(
            f"\nERREUR: {exc}",
            file=sys.stderr
        )
        sys.exit(1)
