#!/usr/bin/env python3

import os
import sqlite3
import sys
from pathlib import Path

PERSISTENT = Path("/var/lib/wavelogstoat/ft8commander/ft8ctl.sql")
RAM = Path("/run/ft8commander/ft8ctl.sql")

def cleanup_sidecars(path):
    for suffix in ("-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()

def sqlite_backup(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)

    tmp = Path(str(dst) + ".new")

    for p in (tmp, Path(str(tmp) + "-wal"), Path(str(tmp) + "-shm")):
        if p.exists():
            p.unlink()

    src_db = sqlite3.connect(src)
    dst_db = sqlite3.connect(tmp)

    try:
        src_db.backup(dst_db)
        dst_db.commit()
    finally:
        dst_db.close()
        src_db.close()

    os.replace(tmp, dst)
    cleanup_sidecars(dst)

if len(sys.argv) != 2 or sys.argv[1] not in ("restore", "save"):
    raise SystemExit("Usage: db_ram_sync.py restore|save")

if sys.argv[1] == "restore":
    if not PERSISTENT.exists():
        raise SystemExit(f"Base persistante absente: {PERSISTENT}")
    sqlite_backup(PERSISTENT, RAM)
    print(f"RESTORE OK: {PERSISTENT} -> {RAM}")

else:
    if not RAM.exists():
        raise SystemExit(f"Base RAM absente: {RAM}")
    sqlite_backup(RAM, PERSISTENT)
    print(f"SAVE OK: {RAM} -> {PERSISTENT}")
