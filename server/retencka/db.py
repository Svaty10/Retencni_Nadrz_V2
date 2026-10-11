"""SQLite storage: one file, WAL, per-thread connections, versioned migrations.

All timestamps are UTC epoch seconds (INTEGER). Raw sensor values are stored next to
derived ones so that a calibration change can be re-applied to the whole history.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Any, Iterable, Optional

log = logging.getLogger("retencka.db")

MIGRATIONS: list[str] = [
    # --- v1 ---------------------------------------------------------------
    """
    CREATE TABLE measurements (
        id          INTEGER PRIMARY KEY,
        ts          INTEGER NOT NULL,
        node_id     INTEGER NOT NULL,
        seq         INTEGER NOT NULL,
        flags       INTEGER NOT NULL,
        vbat_mv     INTEGER,
        temp_x100   INTEGER,
        hum_x100    INTEGER,
        probe_100uv INTEGER,
        level_mm    INTEGER,
        volume_l    INTEGER,
        rssi_x2     INTEGER,
        lqi         INTEGER,
        refilling   INTEGER NOT NULL DEFAULT 0,
        time_ok     INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX idx_meas_ts ON measurements(ts);

    CREATE TABLE hourly (
        hour      INTEGER PRIMARY KEY,
        n         INTEGER NOT NULL,
        level_mm  REAL,
        level_min INTEGER,
        level_max INTEGER,
        volume_l  REAL,
        temp      REAL,
        hum       REAL,
        vbat      REAL,
        refilling INTEGER
    ) WITHOUT ROWID;

    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) WITHOUT ROWID;

    CREATE TABLE users (
        username     TEXT PRIMARY KEY COLLATE NOCASE,
        pw_hash      TEXT NOT NULL,
        role         TEXT NOT NULL CHECK (role IN ('admin', 'user')),
        created      INTEGER NOT NULL,
        pw_changed   INTEGER NOT NULL,
        failed       INTEGER NOT NULL DEFAULT 0,
        locked_until INTEGER NOT NULL DEFAULT 0,
        last_login   INTEGER
    );

    CREATE TABLE sessions (
        id_hash   TEXT PRIMARY KEY,
        username  TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE ON UPDATE CASCADE,
        csrf      TEXT NOT NULL,
        created   INTEGER NOT NULL,
        last_seen INTEGER NOT NULL,
        ip        TEXT,
        ua        TEXT
    ) WITHOUT ROWID;
    CREATE INDEX idx_sessions_user ON sessions(username);

    CREATE TABLE audit (
        id       INTEGER PRIMARY KEY,
        ts       INTEGER NOT NULL,
        username TEXT,
        ip       TEXT,
        action   TEXT NOT NULL,
        detail   TEXT
    );
    CREATE INDEX idx_audit_ts ON audit(ts);
    CREATE INDEX idx_audit_action ON audit(action, ts);

    CREATE TABLE refills (
        id         INTEGER PRIMARY KEY,
        start_ts   INTEGER NOT NULL,
        end_ts     INTEGER,
        mode       TEXT NOT NULL,
        target_pct REAL NOT NULL,
        start_l    INTEGER,
        end_l      INTEGER,
        planned_s  INTEGER,
        result     TEXT,
        username   TEXT
    );
    CREATE INDEX idx_refills_start ON refills(start_ts);
    """,
    # --- v2: several sensor nodes per hub, filtered level ---------------------
    """
    CREATE TABLE nodes (
        node_id INTEGER PRIMARY KEY,
        name    TEXT NOT NULL,
        config  TEXT NOT NULL,
        created INTEGER NOT NULL,
        sort    INTEGER NOT NULL DEFAULT 0
    );
    ALTER TABLE measurements ADD COLUMN level_raw_mm INTEGER;
    CREATE INDEX idx_meas_node_ts ON measurements(node_id, ts);
    DROP TABLE hourly;
    CREATE TABLE hourly (
        node_id   INTEGER NOT NULL,
        hour      INTEGER NOT NULL,
        n         INTEGER NOT NULL,
        level_mm  REAL,
        level_min INTEGER,
        level_max INTEGER,
        volume_l  REAL,
        temp      REAL,
        hum       REAL,
        vbat      REAL,
        refilling INTEGER,
        PRIMARY KEY (node_id, hour)
    ) WITHOUT ROWID;
    ALTER TABLE refills ADD COLUMN node_id INTEGER;
    """,
    # --- v3: encrypted radio link (per-node keys from pairing) ----------------
    """
    ALTER TABLE nodes ADD COLUMN secret TEXT;
    ALTER TABLE nodes ADD COLUMN paired_at INTEGER;
    """,
    # --- v4: two-step unpairing (hub request + confirmation from the sensor) --
    """
    ALTER TABLE nodes ADD COLUMN unpair_at INTEGER;
    """,
]


class Database:
    """Thread-safe access through one connection per thread."""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._migrate()

    # ------------------------------------------------------------------ core
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10, isolation_level=None,
                                check_same_thread=True)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")      # durable across power loss in WAL
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=5000")
            c.execute("PRAGMA temp_store=MEMORY")
            c.execute("PRAGMA cache_size=-1024")        # 1 MB page cache per connection
            self._local.conn = c
        return c

    def close_thread(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        return self.conn().execute(sql, params if isinstance(params, dict) else tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        return self.conn().execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.one(sql, params)
        return default if row is None or row[0] is None else row[0]

    def transaction(self):
        return _Tx(self.conn())

    # ------------------------------------------------------------ migrations
    def _migrate(self) -> None:
        c = self.conn()
        ver = c.execute("PRAGMA user_version").fetchone()[0]
        for i in range(ver, len(MIGRATIONS)):
            log.info("Applying DB migration v%d", i + 1)
            c.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _split_sql(MIGRATIONS[i]):
                    c.execute(stmt)
                c.execute(f"PRAGMA user_version={i + 1}")
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    def integrity_ok(self) -> bool:
        try:
            return self.scalar("PRAGMA quick_check") == "ok"
        except sqlite3.DatabaseError:
            return False


class _Tx:
    def __init__(self, conn: sqlite3.Connection):
        self.c = conn

    def __enter__(self) -> sqlite3.Connection:
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.c.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def _split_sql(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]
