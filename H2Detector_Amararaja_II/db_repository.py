"""
db_repository.py
----------------
All database reads and writes for the H2 Gas Detector Dashboard.

Design principles
-----------------
* Every public function opens/uses the shared connection from db_connection.
* All writes use `with conn:` blocks so SQLite handles commit/rollback.
* Errors are logged and re-raised so callers can decide how to handle them.
* Functions are intentionally fine-grained so they can be unit-tested
  independently and composed by the UI layer as needed.
"""

import logging
import sqlite3
from datetime import datetime
from typing import Optional

from db_connection import get_connection

logger = logging.getLogger(__name__)


def _ensure_offline_alert_table() -> None:
    """Ensure the offline alert config table exists before using it."""
    conn = get_connection()
    try:
        conn.execute(
            "SELECT 1 FROM device_offline_alerts WHERE id = 1 LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        logger.warning(
            "device_offline_alerts table missing; applying pending schema migrations."
        )
        from db_schema import initialise_schema
        initialise_schema()


# =============================================================================
# plant_master
# =============================================================================

def get_default_plant_id() -> int:
    """
    Return the id of the first plant in plant_master.
    Raises RuntimeError if no plant row exists (schema not initialised).
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM plant_master ORDER BY id LIMIT 1"
    ).fetchone()
    if row is None:
        raise RuntimeError(
            "plant_master is empty — call db_schema.initialise_schema() first."
        )
    return row["id"]


def get_all_plants() -> list[dict]:
    """Return all rows from plant_master as a list of dicts."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM plant_master ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_plant(plant_name: str, company_name: str,
                 location: str = "") -> int:
    """
    Insert a new plant or return the id if one with the same name already
    exists.  Returns the plant id.
    """
    conn = get_connection()
    try:
        with conn:
            existing = conn.execute(
                "SELECT id FROM plant_master WHERE plant_name = ?",
                (plant_name,),
            ).fetchone()
            if existing:
                return existing["id"]
            cursor = conn.execute(
                """
                INSERT INTO plant_master (plant_name, company_name, location)
                VALUES (?, ?, ?)
                """,
                (plant_name, company_name, location),
            )
            logger.info("Inserted plant '%s' (id=%d).", plant_name,
                        cursor.lastrowid)
            return cursor.lastrowid
    except Exception:
        logger.exception("upsert_plant failed for '%s'.", plant_name)
        raise


# =============================================================================
# device_master
# =============================================================================

def upsert_device(plant_id: int, device_address: int, device_name: str,
                  gas_type: str, gas_unit: str,
                  device_range: float,
                  device_status_flag: Optional[int] = None) -> int:
    """
    Insert a new device row or update it if the Modbus address already exists.
    Returns the device_master.id for the (now current) row.
    """
    conn = get_connection()
    try:
        with conn:
            existing = conn.execute(
                "SELECT id FROM device_master WHERE device_address = ?",
                (device_address,),
            ).fetchone()

            if existing:
                if device_status_flag is None:
                    conn.execute(
                        """
                        UPDATE device_master
                           SET plant_id     = ?,
                               device_name  = ?,
                               gas_type     = ?,
                               gas_unit     = ?,
                               device_range = ?,
                               updated_at   = datetime('now','localtime')
                         WHERE device_address = ?
                        """,
                        (plant_id, device_name, gas_type, gas_unit,
                         device_range, device_address),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE device_master
                           SET plant_id           = ?,
                               device_name        = ?,
                               gas_type           = ?,
                               gas_unit           = ?,
                               device_range       = ?,
                               device_status_flag = ?,
                               updated_at         = datetime('now','localtime')
                         WHERE device_address = ?
                        """,
                        (plant_id, device_name, gas_type, gas_unit,
                         device_range, int(device_status_flag), device_address),
                    )
                logger.debug(
                    "Updated device addr=%d name='%s'.",
                    device_address, device_name,
                )
                return existing["id"]
            else:
                status_flag = int(device_status_flag) if device_status_flag is not None else 0
                cursor = conn.execute(
                    """
                    INSERT INTO device_master
                        (plant_id, device_address, device_name,
                         gas_type, gas_unit, device_range, device_status_flag)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (plant_id, device_address, device_name,
                     gas_type, gas_unit, device_range, status_flag),
                )
                logger.info(
                    "Inserted device addr=%d name='%s' (id=%d).",
                    device_address, device_name, cursor.lastrowid,
                )
                return cursor.lastrowid
    except Exception:
        logger.exception(
            "upsert_device failed for address %d.", device_address
        )
        raise


def get_device_name(address: int):
    """Return the stored device_name for a given Modbus address, or None."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT device_name FROM device_master WHERE device_address = ?",
            (address,),
        ).fetchone()
        return row["device_name"] if row else None
    except Exception:
        logger.exception("get_device_name failed for address %d.", address)
        return None


def rename_device(address: int, new_name: str) -> None:
    """Update the custom device_name for a given Modbus address.
    Raises ValueError if the name is already used by another device.
    """
    name = new_name.strip()
    conn = get_connection()
    try:
        clash = conn.execute(
            "SELECT device_address FROM device_master "
            "WHERE LOWER(device_name) = LOWER(?) AND device_address != ?",
            (name, address),
        ).fetchone()
        if clash:
            raise ValueError(
                f"Name '{name}' is already used by Device {clash['device_address']:03d}."
            )
        with conn:
            conn.execute(
                """
                UPDATE device_master
                   SET device_name = ?,
                       updated_at  = datetime('now','localtime')
                 WHERE device_address = ?
                """,
                (name, address),
            )
        logger.debug("Renamed device addr=%d to '%s'.", address, name)
    except ValueError:
        raise
    except Exception:
        logger.exception("rename_device failed for address %d.", address)
        raise


def get_all_devices(plant_id: Optional[int] = None) -> list[dict]:
    """
    Return all device rows.  Optionally filter by plant_id.
    Each row includes the joined plant_name for convenience.
    """
    conn = get_connection()
    if plant_id is not None:
        rows = conn.execute(
            """
            SELECT d.*, p.plant_name
              FROM device_master d
              JOIN plant_master  p ON p.id = d.plant_id
             WHERE d.plant_id = ?
             ORDER BY d.device_address
            """,
            (plant_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT d.*, p.plant_name
              FROM device_master d
              JOIN plant_master  p ON p.id = d.plant_id
             ORDER BY d.device_address
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_device_by_address(device_address: int) -> Optional[dict]:
    """Return the device_master row for the given Modbus address, or None."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM device_master WHERE device_address = ?",
        (device_address,),
    ).fetchone()
    return dict(row) if row else None


def upsert_k_factor_rule(device_id: int,
                         k_factor: float,
                         is_enabled: bool = True) -> int:
    """Insert or update one K-factor rule for a device. Returns rule id."""
    conn = get_connection()
    factor = float(k_factor)
    enabled_i = 1 if bool(is_enabled) else 0

    with conn:
        existing = conn.execute(
            "SELECT id FROM k_factor_rules WHERE device_id = ?",
            (device_id,),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE k_factor_rules
                   SET k_factor   = ?,
                       is_enabled = ?,
                       updated_at = datetime('now','localtime')
                 WHERE id = ?
                """,
                (factor, enabled_i, existing["id"]),
            )
            return existing["id"]

        cursor = conn.execute(
            """
            INSERT INTO k_factor_rules (device_id, k_factor, is_enabled)
            VALUES (?, ?, ?)
            """,
            (device_id, factor, enabled_i),
        )
        return cursor.lastrowid


def get_k_factor_rules() -> list[dict]:
    """Return all K-factor rules with joined device metadata."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT kr.id,
               kr.device_id,
               kr.k_factor,
               kr.is_enabled,
               kr.created_at,
               kr.updated_at,
               dm.device_address,
               dm.device_name,
               dm.gas_unit
          FROM k_factor_rules kr
          JOIN device_master dm ON dm.id = kr.device_id
         ORDER BY dm.device_address ASC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def delete_k_factor_rule(rule_id: int) -> None:
    """Delete one K-factor rule by id."""
    conn = get_connection()
    with conn:
        conn.execute("DELETE FROM k_factor_rules WHERE id = ?", (rule_id,))


def get_k_factor_for_device_address(device_address: int) -> float:
    """Resolve active K-factor for a device address; defaults to 1.0."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT kr.k_factor,
               kr.is_enabled
          FROM device_master dm
          LEFT JOIN k_factor_rules kr ON kr.device_id = dm.id
         WHERE dm.device_address = ?
         LIMIT 1
        """,
        (device_address,),
    ).fetchone()

    if not row:
        return 1.0
    if int(row["is_enabled"] or 0) != 1:
        return 1.0
    try:
        return float(row["k_factor"])
    except Exception:
        return 1.0


def set_device_status_flag(device_address: int, online: bool) -> None:
    """Set online/offline status for one device by Modbus address."""
    conn = get_connection()
    with conn:
        conn.execute(
            """
            UPDATE device_master
               SET device_status_flag = ?,
                   updated_at         = datetime('now','localtime')
             WHERE device_address = ?
            """,
            (1 if online else 0, device_address),
        )


def apply_scan_status_flags(plant_id: int, online_addresses: set[int]) -> None:
    """Mark scanned devices online and all other devices in plant offline."""
    conn = get_connection()
    with conn:
        conn.execute(
            """
            UPDATE device_master
               SET device_status_flag = 0,
                   updated_at         = datetime('now','localtime')
             WHERE plant_id = ?
            """,
            (plant_id,),
        )

        if online_addresses:
            placeholders = ",".join(["?"] * len(online_addresses))
            conn.execute(
                f"""
                UPDATE device_master
                   SET device_status_flag = 1,
                       updated_at         = datetime('now','localtime')
                 WHERE plant_id = ?
                   AND device_address IN ({placeholders})
                """,
                (plant_id, *sorted(online_addresses)),
            )


def reset_all_devices_offline(plant_id: int) -> None:
    """Reset all devices in plant to offline status (used on service shutdown)."""
    conn = get_connection()
    with conn:
        conn.execute(
            """
            UPDATE device_master
               SET device_status_flag = 0,
                   updated_at         = datetime('now','localtime')
             WHERE plant_id = ?
            """,
            (plant_id,),
        )


# =============================================================================
# reading_transactions
# =============================================================================

def insert_reading(device_id: int, concentration_value: float,
                   low_alarm: float, high_alarm: float,
                   alarm_status: int,
                   recorded_at: Optional[str] = None) -> int:
    """
    Append one reading row for a device.  Returns the new row id.
    This is the hot-path called every polling minute; it is kept
    as lightweight as possible.
    """
    conn = get_connection()
    try:
        with conn:
            if recorded_at:
                cursor = conn.execute(
                    """
                    INSERT INTO reading_transactions
                        (device_id, concentration_value,
                         low_alarm, high_alarm, alarm_status, recorded_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (device_id, concentration_value,
                     low_alarm, high_alarm, alarm_status, recorded_at),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO reading_transactions
                        (device_id, concentration_value,
                         low_alarm, high_alarm, alarm_status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (device_id, concentration_value,
                     low_alarm, high_alarm, alarm_status),
                )
            logger.debug(
                "Reading inserted: device_id=%d conc=%.2f alarm=%d",
                device_id, concentration_value, alarm_status,
            )
            return cursor.lastrowid
    except Exception:
        logger.exception(
            "insert_reading failed for device_id=%d.", device_id
        )
        raise


def get_readings_for_device(device_id: int,
                             limit: int = 100) -> list[dict]:
    """
    Return the most recent `limit` readings for a device, newest first.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT *
          FROM reading_transactions
         WHERE device_id = ?
         ORDER BY recorded_at DESC
         LIMIT ?
        """,
        (device_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_readings_in_range(device_id: int,
                          start: datetime,
                          end: datetime) -> list[dict]:
    """
    Return all readings for a device within [start, end] (inclusive).
    `start` and `end` are Python datetime objects.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT *
          FROM reading_transactions
         WHERE device_id  = ?
           AND recorded_at >= ?
           AND recorded_at <= ?
         ORDER BY recorded_at ASC
        """,
        (device_id,
         start.strftime("%Y-%m-%d %H:%M:%S"),
         end.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_readings_in_range(start: datetime, end: datetime) -> list[dict]:
    """
    Return all readings across every device in [start, end], joined with
    device_master so each row includes ``_dev_name``.
    Uses the idx_readings_time index for a fast single-pass scan instead
    of issuing N per-device queries.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT rt.*,
               dm.device_name
          FROM reading_transactions rt
          JOIN device_master dm ON dm.id = rt.device_id
         WHERE rt.recorded_at >= ?
           AND rt.recorded_at <= ?
         ORDER BY rt.recorded_at ASC
        """,
        (start.strftime("%Y-%m-%d %H:%M:%S"),
         end.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["_dev_name"] = d.pop("device_name")
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Paginated / count variants  (used by the Device Logs pagination UI)
# ---------------------------------------------------------------------------

def count_readings_in_range(device_id: int,
                             start: datetime,
                             end: datetime) -> int:
    """Return total number of readings for a single device within [start, end]."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
          FROM reading_transactions
         WHERE device_id  = ?
           AND recorded_at >= ?
           AND recorded_at <= ?
        """,
        (device_id,
         start.strftime("%Y-%m-%d %H:%M:%S"),
         end.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    return row["n"] if row else 0


def count_all_readings_in_range(start: datetime, end: datetime) -> int:
    """Return total number of readings across all devices within [start, end]."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT COUNT(*) AS n
          FROM reading_transactions
         WHERE recorded_at >= ?
           AND recorded_at <= ?
        """,
        (start.strftime("%Y-%m-%d %H:%M:%S"),
         end.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchone()
    return row["n"] if row else 0


def count_readings_for_devices_in_range(device_ids: list[int],
                                                                                start: datetime,
                                                                                end: datetime) -> int:
        """Return total readings for a chosen set of device_ids within [start, end]."""
        if not device_ids:
                return 0
        conn = get_connection()
        placeholders = ",".join(["?"] * len(device_ids))
        row = conn.execute(
                f"""
                SELECT COUNT(*) AS n
                    FROM reading_transactions
                 WHERE device_id IN ({placeholders})
                     AND recorded_at >= ?
                     AND recorded_at <= ?
                """,
                (*device_ids,
                 start.strftime("%Y-%m-%d %H:%M:%S"),
                 end.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchone()
        return row["n"] if row else 0


def count_distinct_recorded_at_in_range(start: datetime,
                                        end: datetime,
                                        device_ids: Optional[list[int]] = None) -> int:
    """Return count of distinct recorded_at timestamps within [start, end]."""
    conn = get_connection()
    start_s = start.strftime("%Y-%m-%d %H:%M:%S")
    end_s = end.strftime("%Y-%m-%d %H:%M:%S")

    if device_ids is None:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT recorded_at) AS n
              FROM reading_transactions
             WHERE recorded_at >= ?
               AND recorded_at <= ?
            """,
            (start_s, end_s),
        ).fetchone()
        return row["n"] if row else 0

    if not device_ids:
        return 0

    placeholders = ",".join(["?"] * len(device_ids))
    row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT recorded_at) AS n
          FROM reading_transactions
         WHERE device_id IN ({placeholders})
           AND recorded_at >= ?
           AND recorded_at <= ?
        """,
        (*device_ids, start_s, end_s),
    ).fetchone()
    return row["n"] if row else 0


def get_device_logs_matrix_page(start: datetime,
                                end: datetime,
                                limit: int,
                                offset: int,
                                device_ids: Optional[list[int]] = None) -> tuple[list[str], list[dict]]:
    """
    Return a page of distinct timestamps and all device readings for those
    timestamps (for pivot/matrix UI rendering).
    """
    conn = get_connection()
    start_s = start.strftime("%Y-%m-%d %H:%M:%S")
    end_s = end.strftime("%Y-%m-%d %H:%M:%S")

    if device_ids is not None and not device_ids:
        return [], []

    if device_ids is None:
        ts_rows = conn.execute(
            """
            SELECT DISTINCT recorded_at
              FROM reading_transactions
             WHERE recorded_at >= ?
               AND recorded_at <= ?
                         ORDER BY recorded_at DESC
             LIMIT ? OFFSET ?
            """,
            (start_s, end_s, limit, offset),
        ).fetchall()
    else:
        dev_placeholders = ",".join(["?"] * len(device_ids))
        ts_rows = conn.execute(
            f"""
            SELECT DISTINCT recorded_at
              FROM reading_transactions
             WHERE device_id IN ({dev_placeholders})
               AND recorded_at >= ?
               AND recorded_at <= ?
                         ORDER BY recorded_at DESC
             LIMIT ? OFFSET ?
            """,
            (*device_ids, start_s, end_s, limit, offset),
        ).fetchall()

    timestamps = [str(r["recorded_at"]) for r in ts_rows]
    if not timestamps:
        return [], []

    ts_placeholders = ",".join(["?"] * len(timestamps))
    if device_ids is None:
        rows = conn.execute(
            f"""
            SELECT rt.recorded_at,
                   rt.device_id,
                   dm.device_address,
                   dm.device_name,
                   rt.concentration_value,
                   rt.low_alarm,
                   rt.high_alarm,
                   rt.alarm_status
              FROM reading_transactions rt
              JOIN device_master dm ON dm.id = rt.device_id
             WHERE rt.recorded_at IN ({ts_placeholders})
             ORDER BY rt.recorded_at DESC, dm.device_address ASC
            """,
            (*timestamps,),
        ).fetchall()
    else:
        dev_placeholders = ",".join(["?"] * len(device_ids))
        rows = conn.execute(
            f"""
            SELECT rt.recorded_at,
                   rt.device_id,
                   dm.device_address,
                   dm.device_name,
                   rt.concentration_value,
                   rt.low_alarm,
                   rt.high_alarm,
                   rt.alarm_status
              FROM reading_transactions rt
              JOIN device_master dm ON dm.id = rt.device_id
             WHERE rt.recorded_at IN ({ts_placeholders})
               AND rt.device_id IN ({dev_placeholders})
                         ORDER BY rt.recorded_at DESC, dm.device_address ASC
            """,
            (*timestamps, *device_ids),
        ).fetchall()

    return timestamps, [dict(r) for r in rows]


def get_readings_in_range_paged(device_id: int,
                                 start: datetime,
                                 end: datetime,
                                 limit: int,
                                 offset: int) -> list[dict]:
    """Return one page of readings for a single device, ordered by time."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT *
          FROM reading_transactions
         WHERE device_id  = ?
           AND recorded_at >= ?
           AND recorded_at <= ?
         ORDER BY recorded_at ASC
         LIMIT ? OFFSET ?
        """,
        (device_id,
         start.strftime("%Y-%m-%d %H:%M:%S"),
         end.strftime("%Y-%m-%d %H:%M:%S"),
         limit, offset),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_readings_in_range_paged(start: datetime,
                                     end: datetime,
                                     limit: int,
                                     offset: int) -> list[dict]:
    """
    Return one page of readings across all devices, joined with device_master.
    Each row includes ``_dev_name``.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT rt.*,
               dm.device_name
          FROM reading_transactions rt
          JOIN device_master dm ON dm.id = rt.device_id
         WHERE rt.recorded_at >= ?
           AND rt.recorded_at <= ?
         ORDER BY rt.recorded_at ASC
         LIMIT ? OFFSET ?
        """,
        (start.strftime("%Y-%m-%d %H:%M:%S"),
         end.strftime("%Y-%m-%d %H:%M:%S"),
         limit, offset),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["_dev_name"] = d.pop("device_name")
        result.append(d)
    return result


def get_readings_for_devices_in_range(start: datetime,
                                      end: datetime,
                                      device_ids: list[int]) -> list[dict]:
    """Return all readings for chosen devices in [start, end], with _dev_name."""
    if not device_ids:
        return []
    conn = get_connection()
    placeholders = ",".join(["?"] * len(device_ids))
    rows = conn.execute(
        f"""
        SELECT rt.*,
               dm.device_name
          FROM reading_transactions rt
          JOIN device_master dm ON dm.id = rt.device_id
         WHERE rt.device_id IN ({placeholders})
           AND rt.recorded_at >= ?
           AND rt.recorded_at <= ?
         ORDER BY rt.recorded_at ASC
        """,
        (*device_ids,
         start.strftime("%Y-%m-%d %H:%M:%S"),
         end.strftime("%Y-%m-%d %H:%M:%S")),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["_dev_name"] = d.pop("device_name")
        result.append(d)
    return result


def get_readings_for_devices_in_range_paged(start: datetime,
                                            end: datetime,
                                            device_ids: list[int],
                                            limit: int,
                                            offset: int) -> list[dict]:
    """Return one page of readings for chosen devices in [start, end], with _dev_name."""
    if not device_ids:
        return []
    conn = get_connection()
    placeholders = ",".join(["?"] * len(device_ids))
    rows = conn.execute(
        f"""
        SELECT rt.*,
               dm.device_name
          FROM reading_transactions rt
          JOIN device_master dm ON dm.id = rt.device_id
         WHERE rt.device_id IN ({placeholders})
           AND rt.recorded_at >= ?
           AND rt.recorded_at <= ?
         ORDER BY rt.recorded_at ASC
         LIMIT ? OFFSET ?
        """,
        (*device_ids,
         start.strftime("%Y-%m-%d %H:%M:%S"),
         end.strftime("%Y-%m-%d %H:%M:%S"),
         limit, offset),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["_dev_name"] = d.pop("device_name")
        result.append(d)
    return result


def get_latest_reading(device_id: int) -> Optional[dict]:
    """Return the single most recent reading row for a device, or None."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT *
          FROM reading_transactions
         WHERE device_id = ?
         ORDER BY recorded_at DESC
         LIMIT 1
        """,
        (device_id,),
    ).fetchone()
    return dict(row) if row else None


def get_all_latest_transaction_readings() -> list[dict]:
    """
    Return latest transaction row per device, joined with device metadata.
    Includes devices with no transaction rows (transaction fields will be NULL).
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT d.id AS device_id,
               d.device_address,
               d.device_name,
               d.gas_type,
               d.gas_unit,
               d.device_range,
               rt.concentration_value,
               rt.low_alarm,
               rt.high_alarm,
               rt.alarm_status,
               rt.recorded_at
          FROM device_master d
          LEFT JOIN reading_transactions rt
                 ON rt.id = (
                    SELECT r2.id
                      FROM reading_transactions r2
                     WHERE r2.device_id = d.id
                     ORDER BY r2.recorded_at DESC, r2.id DESC
                     LIMIT 1
                 )
         ORDER BY d.device_address
        """
    ).fetchall()
    return [dict(r) for r in rows]


def purge_old_readings(days_to_keep: int = 30) -> int:
    """
    Delete readings older than `days_to_keep` days.
    Returns the number of rows deleted.
    Called periodically to prevent unbounded database growth.
    """
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
                """
                DELETE FROM reading_transactions
                 WHERE recorded_at < datetime('now', ?, 'localtime')
                """,
                (f"-{days_to_keep} days",),
            )
            deleted = cursor.rowcount
            if deleted:
                logger.info(
                    "Purged %d reading(s) older than %d days.",
                    deleted, days_to_keep,
                )
            return deleted
    except Exception:
        logger.exception("purge_old_readings failed.")
        raise


# =============================================================================
# live_cache (persistent last-N live samples for per-second stats)
# =============================================================================

def insert_live_cache_reading(device_id: int, concentration_value: float,
                              low_alarm: float, high_alarm: float,
                              alarm_status: int,
                              polled_at: Optional[str] = None,
                              keep_last_n: int = 60) -> int:
    """
    Insert one live sample and trim older rows for the same device to keep only
    `keep_last_n` most recent entries.
    """
    conn = get_connection()
    try:
        with conn:
            if polled_at:
                cursor = conn.execute(
                    """
                    INSERT INTO live_cache
                        (device_id, concentration_value, low_alarm,
                         high_alarm, alarm_status, polled_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (device_id, concentration_value, low_alarm,
                     high_alarm, alarm_status, polled_at),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO live_cache
                        (device_id, concentration_value, low_alarm,
                         high_alarm, alarm_status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (device_id, concentration_value, low_alarm,
                     high_alarm, alarm_status),
                )

            conn.execute(
                """
                DELETE FROM live_cache
                 WHERE device_id = ?
                   AND id NOT IN (
                       SELECT id
                         FROM live_cache
                        WHERE device_id = ?
                                                ORDER BY id DESC
                        LIMIT ?
                   )
                """,
                (device_id, device_id, keep_last_n),
            )
            return cursor.lastrowid
    except Exception:
        logger.exception("insert_live_cache_reading failed for device_id=%d", device_id)
        raise


def get_live_cache_recent(device_id: int, limit: int = 60) -> list[dict]:
    """Return up to `limit` recent live samples for one device (newest first)."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT *
          FROM live_cache
         WHERE device_id = ?
                 ORDER BY id DESC
         LIMIT ?
        """,
        (device_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_live_cache_latest() -> list[dict]:
    """Return latest live_cache row per device with device metadata."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT d.id AS device_id,
               d.device_address,
               d.device_name,
               d.gas_type,
               d.gas_unit,
               d.device_range,
               lc.concentration_value,
               lc.low_alarm,
               lc.high_alarm,
               lc.alarm_status,
               lc.polled_at
          FROM device_master d
          LEFT JOIN live_cache lc
                 ON lc.id = (
                    SELECT l2.id
                      FROM live_cache l2
                     WHERE l2.device_id = d.id
                                         ORDER BY l2.id DESC
                     LIMIT 1
                 )
         ORDER BY d.device_address
        """
    ).fetchall()
    return [dict(r) for r in rows]


# Backward-compatible aliases used by some UI/report paths.
def upsert_live_reading(device_id: int, concentration_value: float,
                        low_alarm: float, high_alarm: float,
                        alarm_status: int, last_updated: str) -> None:
    insert_live_cache_reading(
        device_id=device_id,
        concentration_value=concentration_value,
        low_alarm=low_alarm,
        high_alarm=high_alarm,
        alarm_status=alarm_status,
        polled_at=last_updated,
        keep_last_n=60,
    )


def get_all_live_readings() -> list[dict]:
    return get_all_live_cache_latest()


def get_live_reading(device_id: int) -> Optional[dict]:
    rows = get_live_cache_recent(device_id, limit=1)
    return rows[0] if rows else None

# =============================================================================
# device_offline_alerts (global configuration)
# =============================================================================

def get_offline_alert_config() -> dict:
    """Return the global offline alert configuration."""
    _ensure_offline_alert_table()
    conn = get_connection()
    row = conn.execute(
        "SELECT id, enabled, cooldown_minutes, last_alert_at FROM device_offline_alerts WHERE id = 1"
    ).fetchone()
    if row:
        return dict(row)
    # Return defaults if table is empty
    return {
        "id": 1,
        "enabled": 1,
        "cooldown_minutes": 30,
        "last_alert_at": None,
    }


def update_offline_alert_config(enabled: int, cooldown_minutes: int) -> None:
    """Update the global offline alert configuration."""
    _ensure_offline_alert_table()
    conn = get_connection()
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO device_offline_alerts 
            (id, enabled, cooldown_minutes, updated_at)
            VALUES (1, ?, ?, datetime('now','localtime'))
            """,
            (enabled, cooldown_minutes),
        )


def update_offline_alert_timestamp(device_addr: int) -> None:
    """Record the current time as the last offline alert for a device."""
    _ensure_offline_alert_table()
    conn = get_connection()
    with conn:
        conn.execute(
            """
            UPDATE device_offline_alerts 
            SET last_alert_at = datetime('now','localtime')
            WHERE id = 1
            """,
        )