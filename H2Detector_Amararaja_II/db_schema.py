"""
db_schema.py
------------
Schema definitions and initialisation for the H2 Gas Detector Dashboard.

Call `initialise_schema()` once at application startup (before any
repository calls).  The function is idempotent — running it multiple
times on an existing database is safe.

Tables
------
plant_master        — The industrial plant / company where detectors are installed.
device_master       — One row per detected gas-detector device.
reading_transactions— One row per periodic reading captured from a device.
"""

import logging
from db_connection import get_connection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL statements
# ---------------------------------------------------------------------------

_DDL_PLANT_MASTER = """
CREATE TABLE IF NOT EXISTS plant_master (
    id           INTEGER  PRIMARY KEY AUTOINCREMENT,
    plant_name   TEXT     NOT NULL,
    company_name TEXT     NOT NULL,
    location     TEXT,
    created_at   DATETIME NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at   DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

_DDL_DEVICE_MASTER = """
CREATE TABLE IF NOT EXISTS device_master (
    id                 INTEGER  PRIMARY KEY AUTOINCREMENT,
    plant_id           INTEGER  NOT NULL
                                 REFERENCES plant_master(id) ON DELETE CASCADE,
    device_address     INTEGER  NOT NULL UNIQUE,   -- Modbus address (1-247)
    device_name        TEXT     NOT NULL,           -- e.g. "Device 001"
    gas_type           TEXT,                        -- e.g. "H2", "CO", "VOC"
    gas_unit           TEXT,                        -- e.g. "ppm", "%LEL"
    device_range       REAL,                        -- full-scale range value
    device_status_flag INTEGER  NOT NULL DEFAULT 0, -- 1=online 0=offline
    created_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

_DDL_READING_TRANSACTIONS = """
CREATE TABLE IF NOT EXISTS reading_transactions (
    id                  INTEGER  PRIMARY KEY AUTOINCREMENT,
    device_id           INTEGER  NOT NULL
                                  REFERENCES device_master(id) ON DELETE CASCADE,
    concentration_value REAL     NOT NULL,
    low_alarm           REAL,
    high_alarm          REAL,
    alarm_status        INTEGER,               -- 1=Normal 2=Low 3=High 0=Invalid
    recorded_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

_DDL_LIVE_READINGS = """
CREATE TABLE IF NOT EXISTS live_readings (
    device_id           INTEGER  NOT NULL PRIMARY KEY
                                  REFERENCES device_master(id) ON DELETE CASCADE,
    concentration_value REAL     NOT NULL,
    low_alarm           REAL,
    high_alarm          REAL,
    alarm_status        INTEGER,
    polled_at           DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

_DDL_LIVE_CACHE = """
CREATE TABLE IF NOT EXISTS live_cache (
    id                  INTEGER  PRIMARY KEY AUTOINCREMENT,
    device_id           INTEGER  NOT NULL
                                  REFERENCES device_master(id) ON DELETE CASCADE,
    concentration_value REAL     NOT NULL,
    low_alarm           REAL,
    high_alarm          REAL,
    alarm_status        INTEGER,
    polled_at           DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

_DDL_K_FACTOR_RULES = """
CREATE TABLE IF NOT EXISTS k_factor_rules (
    id          INTEGER  PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER  NOT NULL UNIQUE
                          REFERENCES device_master(id) ON DELETE CASCADE,
    k_factor    REAL     NOT NULL DEFAULT 1.0,
    is_enabled  INTEGER  NOT NULL DEFAULT 1,
    created_at  DATETIME NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

# Index to speed up time-range queries per device
_IDX_READINGS_DEVICE_TIME = """
CREATE INDEX IF NOT EXISTS idx_readings_device_time
    ON reading_transactions (device_id, recorded_at DESC);
"""

# Index on recorded_at alone — covers cross-device date-range scans
_IDX_READINGS_TIME = """
CREATE INDEX IF NOT EXISTS idx_readings_time
    ON reading_transactions (recorded_at DESC);
"""

_IDX_LIVE_CACHE_DEVICE_TIME = """
CREATE INDEX IF NOT EXISTS idx_live_cache_device_time
    ON live_cache (device_id, polled_at DESC, id DESC);
"""

# ---------------------------------------------------------------------------
# Schema version tracking (simple migrations support)
# ---------------------------------------------------------------------------

_DDL_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

_DDL_OFFLINE_ALERTS = """
CREATE TABLE IF NOT EXISTS device_offline_alerts (
    id                 INTEGER  PRIMARY KEY,
    enabled            INTEGER  NOT NULL DEFAULT 1,
    cooldown_minutes   INTEGER  NOT NULL DEFAULT 30,
    last_alert_at      DATETIME,
    updated_at         DATETIME NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

# Increment this whenever a breaking schema change is added below
CURRENT_SCHEMA_VERSION = 4


def _get_applied_version(conn) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM schema_version"
    ).fetchone()
    return row["v"] if row["v"] is not None else 0


def _mark_version(conn, version: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (version,)
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def initialise_schema() -> None:
    """
    Create all tables and indexes if they do not exist, then run any
    pending migrations.  Safe to call on every application startup.
    """
    conn = get_connection()
    try:
        with conn:   # automatic commit / rollback
            # Core tables
            conn.execute(_DDL_SCHEMA_VERSION)
            conn.execute(_DDL_PLANT_MASTER)
            conn.execute(_DDL_DEVICE_MASTER)
            conn.execute(_DDL_READING_TRANSACTIONS)
            conn.execute(_DDL_K_FACTOR_RULES)
            conn.execute(_IDX_READINGS_DEVICE_TIME)
            conn.execute(_IDX_READINGS_TIME)
            conn.execute(_DDL_OFFLINE_ALERTS)
            conn.execute(
                "INSERT OR IGNORE INTO device_offline_alerts (id, enabled, cooldown_minutes) VALUES (1, 1, 30)"
            )

            applied = _get_applied_version(conn)

            # -----------------------------------------------------------------
            # v1 — seed a default plant row so every device has a foreign key
            # -----------------------------------------------------------------
            if applied < 1:
                _seed_default_plant(conn)
                _mark_version(conn, 1)
                logger.info("Schema migration v1 applied.")

            # -----------------------------------------------------------------
            # v2 — add persistent live_cache table for last-N live samples
            # -----------------------------------------------------------------
            if applied < 2:
                conn.execute(_DDL_LIVE_CACHE)
                conn.execute(_IDX_LIVE_CACHE_DEVICE_TIME)
                _mark_version(conn, 2)
                logger.info("Schema migration v2 applied.")

            # -----------------------------------------------------------------
            # v3 — add persistent device_status_flag in device_master
            # -----------------------------------------------------------------
            if applied < 3:
                cols = [r["name"] for r in conn.execute("PRAGMA table_info(device_master)").fetchall()]
                if "device_status_flag" not in cols:
                    conn.execute(
                        "ALTER TABLE device_master ADD COLUMN device_status_flag INTEGER NOT NULL DEFAULT 0"
                    )
                _mark_version(conn, 3)
                logger.info("Schema migration v3 applied.")

            # -----------------------------------------------------------------
            # v4 — add device_offline_alerts table for global offline alert config
            # -----------------------------------------------------------------
            if applied < 4:
                conn.execute(_DDL_OFFLINE_ALERTS)
                # Seed single row with defaults
                conn.execute(
                    "INSERT OR IGNORE INTO device_offline_alerts (id, enabled, cooldown_minutes) VALUES (1, 1, 30)"
                )
                _mark_version(conn, 4)
                logger.info("Schema migration v4 applied.")

        logger.info(
            "Database schema is up to date (version %d).", CURRENT_SCHEMA_VERSION
        )
    except Exception:
        logger.exception("Failed to initialise database schema.")
        raise


def _seed_default_plant(conn) -> None:
    """Insert the default plant row if plant_master is empty."""
    count = conn.execute("SELECT COUNT(*) FROM plant_master").fetchone()[0]
    if count == 0:
        conn.execute(
            """
            INSERT INTO plant_master (plant_name, company_name, location)
            VALUES (?, ?, ?)
            """,
            ("Default Plant", "GreenEnv Technologies", "Site A"),
        )
        logger.info("Default plant record seeded into plant_master.")
