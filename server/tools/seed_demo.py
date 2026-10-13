#!/usr/bin/env python3
"""Fill a data directory with N days of synthetic measurements (development / UI demo).

    RETENCKA_DATA=/tmp/rtdata python3 tools/seed_demo.py 30
    python3 tools/seed_demo.py 90 BEEF --backup demo.db.gz   # file for "Nahrát zálohu" in the web UI
"""

import gzip
import math
import os
import shutil
import tempfile
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retencka import model  # noqa: E402
from retencka.core import Core  # noqa: E402
from retencka.db import Database  # noqa: E402
from retencka.settings import Nodes, Settings  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    backup = None
    if "--backup" in args:
        i = args.index("--backup")
        backup = args[i + 1]
        del args[i:i + 2]
    sys.argv[1:] = args
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    tmp = tempfile.mkdtemp() if backup else None
    data = tmp or os.environ.get("RETENCKA_DATA", "/tmp/rtdata")
    os.makedirs(data, exist_ok=True)
    db = Database(os.path.join(data, "retencka.db"))
    s = Settings(db)
    nodes = Nodes(db)
    nid = int(sys.argv[2], 16) if len(sys.argv) > 2 else 0xBEEF
    if not nodes.get(nid):
        nodes.add(nid, "Demo nádrž" if nid == 0xBEEF else f"Demo {nid:04X}")
    st = nodes.get(nid)
    now = int(time.time())
    t = now - days * 86400
    probe = 900.0
    seq = 0
    rows = []
    rnd = random.Random(1)
    while t < now:
        hour = time.localtime(t).tm_hour
        # daytime garden use, occasional rain, refill when low
        if 6 <= hour <= 21:
            probe -= rnd.random() * 1.2
        if rnd.random() < 0.004:
            probe += rnd.uniform(40, 160)
        refilling = 0
        if probe < 350:
            refilling = 1
            probe += 25
        probe = max(130.0, min(1250.0, probe))
        p100 = int((probe + rnd.gauss(0, 4.0)) * 10)     # noisy probe
        lv = model.compute_level(p100, st)
        temp = int(1100 + 400 * math.sin((t % 86400) / 86400 * 2 * math.pi) + rnd.gauss(0, 20))
        hum = int(min(10000, 8800 + 700 * math.cos((t % 86400) / 86400 * 2 * math.pi)))
        vbat = int(5150 - (now - t) / 86400 * -0.4)
        rows.append((t, nid, seq & 0xFFFF, 0, vbat, temp, hum, p100, int(lv.level_cm * 10),
                     int(lv.volume_l), rnd.randint(-170, -150), 30, refilling, 1))
        seq += 1
        t += 300 if not refilling else 60
    with db.transaction() as c:
        c.executemany("INSERT INTO measurements(ts,node_id,seq,flags,vbat_mv,temp_x100,hum_x100,probe_100uv,"
                      "level_mm,volume_l,rssi_x2,lqi,refilling,time_ok) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    core = Core(db, s, data)
    core.recompute_levels(nid)                       # calibration + filter + hourly
    refills = add_refills(db, nid)
    print(f"Inserted {len(rows)} measurements and {refills} refills into {data}")
    if backup:
        snap = os.path.join(data, "snapshot.db")
        core.snapshot(snap)
        db.close_thread()
        with open(snap, "rb") as fi, gzip.open(backup, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        shutil.rmtree(data, ignore_errors=True)
        print(f"Backup for the web import: {backup} ({os.path.getsize(backup) // 1024} kB)")


def add_refills(db: Database, nid: int) -> int:
    """Refill log rows for the synthetic refilling stretches (green bands in the chart)."""
    out, cur = [], None
    for r in db.query("SELECT ts, volume_l, refilling FROM measurements WHERE node_id=? ORDER BY ts", (nid,)):
        if r["refilling"] and cur is None:
            cur = [r["ts"], r["volume_l"], r["ts"], r["volume_l"]]
        elif r["refilling"]:
            cur[2:] = [r["ts"], r["volume_l"]]
        elif cur is not None:
            out.append(cur)
            cur = None
    with db.transaction() as c:
        c.executemany("INSERT INTO refills(start_ts, end_ts, mode, target_pct, start_l, end_l, planned_s, result, "
                      "username, node_id) VALUES(?,?,'auto',30,?,?,?,'Dosaženo cíle (ukázková data)','system',?)",
                      [(a, b, sl, el, b - a + 120, nid) for a, sl, b, el in out])
    return len(out)


if __name__ == "__main__":
    main()
