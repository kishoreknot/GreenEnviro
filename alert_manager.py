"""
alert_manager.py
================
Central module for email-alert rules, SMTP configuration, alert firing
(with 10-min / hourly / bi-hourly throttle), and a Settings journal that records
every change made in the Settings section by any user.

Schema
------
  smtp_config      – single-row SMTP credentials
  alert_rules      – one row per unique threshold value
  alert_emails     – N emails per rule  (unique per rule)
    alert_fire_log   – every email dispatch event with alert type metadata
  settings_journal – audit trail for every Settings-section change
"""

import sqlite3
import os
import sys
import time
import smtplib
import threading
import logging
import calendar
import datetime as dt
from dataclasses import dataclass, field
from io import BytesIO
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.header import Header
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

def _resolve_alert_data_dir() -> str:
    """
    Return a stable writable directory for alerts DB.

    Priority:
    1) H2_DATA_DIR environment variable
    2) Frozen EXE: %PROGRAMDATA%\\H2GasDetector
    3) Source run: directory containing this file
    """
    env_dir = os.environ.get("H2_DATA_DIR", "").strip()
    if env_dir:
        return os.path.abspath(os.path.expanduser(env_dir))

    if getattr(sys, "frozen", False):
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return os.path.abspath(os.path.join(program_data, "H2GasDetector"))

    return os.path.dirname(os.path.abspath(__file__))


_DATA_DIR = _resolve_alert_data_dir()
os.makedirs(_DATA_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DATA_DIR, "alerts.db")
# RLock so the main thread can re-enter (e.g. log_journal called from within
# another locked function is safe).  Background threads still wait their turn.
_db_lock = threading.RLock()

# In-flight threshold sends avoid duplicate threads before the DB fire-log row exists.
_threshold_alert_inflight: set[tuple[int, int]] = set()
_threshold_alert_inflight_lock = threading.Lock()

# In-flight offline sends avoid duplicate threads before the DB fire-log row exists.
_offline_alert_inflight: set[int] = set()
_offline_alert_inflight_lock = threading.Lock()

# ── In-memory rules cache ────────────────────────────────────────────────────
# check_and_fire reads this cache so it NEVER touches the DB during polling.
# Any write function must call _invalidate_cache() to force a reload.
_rules_cache: list | None = None
_rules_cache_lock = threading.Lock()
_rules_cache_loaded_at: float = 0.0
_RULES_CACHE_TTL_SECONDS = 15.0


def _invalidate_cache() -> None:
    global _rules_cache, _rules_cache_loaded_at
    with _rules_cache_lock:
        _rules_cache = None
        _rules_cache_loaded_at = 0.0


def _get_cached_rules() -> list:
    """Return cached rules, loading from DB once if the cache is empty."""
    global _rules_cache, _rules_cache_loaded_at
    now = time.time()
    with _rules_cache_lock:
        if (_rules_cache is not None and
                (now - _rules_cache_loaded_at) < _RULES_CACHE_TTL_SECONDS):
            return list(_rules_cache)   # shallow copy
    # Cache miss — load from DB (outside the cache lock to avoid contention)
    rules = get_alert_rules()
    with _rules_cache_lock:
        _rules_cache = rules
        _rules_cache_loaded_at = now
    return list(rules)


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AlertRule:
    rule_id:    int
    threshold:  float
    occurrence: str          # "10-min" | "hourly" | "bi-hourly"
    created_at: str
    created_by: str
    emails:     list[str] = field(default_factory=list)


_ALLOWED_OCCURRENCES = ("10-min", "hourly", "bi-hourly")


@dataclass
class JournalEntry:
    entry_id:   int
    timestamp:  str
    username:   str
    section:    str          # "General" | "Alert Management" | "User Management"
    action:     str          # "ADD" | "UPDATE" | "DELETE"
    detail:     str


# ─────────────────────────────────────────────────────────────────────────────
# DB initialisation
# ─────────────────────────────────────────────────────────────────────────────

def init_alert_db() -> None:
    """Create all tables if they do not already exist."""
    logger.info("Alert DB path: %s", _DB_PATH)
    with _db_lock:
        con = _connect()
        try:
            con.executescript("""
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS smtp_config (
                    id         INTEGER PRIMARY KEY CHECK (id = 1),
                    host       TEXT    NOT NULL DEFAULT '',
                    port       INTEGER NOT NULL DEFAULT 587,
                    username   TEXT    NOT NULL DEFAULT '',
                    password   TEXT    NOT NULL DEFAULT '',
                    from_email TEXT    NOT NULL DEFAULT '',
                    use_tls    INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT    DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS alert_rules (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    threshold  REAL    NOT NULL UNIQUE,
                    occurrence TEXT    NOT NULL DEFAULT 'hourly'
                                       CHECK (occurrence IN ('10-min', 'hourly', 'bi-hourly')),
                    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
                    created_by TEXT    NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS alert_emails (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id    INTEGER NOT NULL
                               REFERENCES alert_rules(id) ON DELETE CASCADE,
                    email      TEXT    NOT NULL,
                    added_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (rule_id, email)
                );

                CREATE TABLE IF NOT EXISTS alert_fire_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id     INTEGER REFERENCES alert_rules(id) ON DELETE SET NULL,
                    alert_type  TEXT    NOT NULL DEFAULT 'threshold',
                    device_addr INTEGER NOT NULL,
                    concentration REAL  NOT NULL,
                    recipients  TEXT    NOT NULL,
                    fired_at    TEXT    NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS settings_journal (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp  TEXT    NOT NULL DEFAULT (datetime('now')),
                    username   TEXT    NOT NULL DEFAULT 'system',
                    section    TEXT    NOT NULL,
                    action     TEXT    NOT NULL,
                    detail     TEXT    NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS report_scheduler (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    frequency  TEXT    NOT NULL,
                    avg_at     TEXT    NOT NULL DEFAULT 'Raw',
                    scheduled_time TEXT NOT NULL DEFAULT '09:00',
                    last_sent_at TEXT,
                    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
                    created_by TEXT    NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS report_scheduler_emails (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    scheduler_id INTEGER NOT NULL
                                 REFERENCES report_scheduler(id) ON DELETE CASCADE,
                    email        TEXT    NOT NULL,
                    added_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (scheduler_id, email)
                );
            """)
            con.commit()

            _ensure_alert_fire_log_alert_type(con)
            # Migration: Add avg_at column if it doesn't exist
            _ensure_report_scheduler_avg_at(con)
            _ensure_report_scheduler_schedule_fields(con)
        finally:
            con.close()


# ─────────────────────────────────────────────────────────────────────────────
# Report Scheduler CRUD
# ─────────────────────────────────────────────────────────────────────────────

def get_report_schedulers() -> list[dict]:
    """Return all report scheduler events with their mail IDs."""
    try:
        with _db_lock:
            con = _connect()
            try:
                _ensure_report_scheduler_avg_at(con)
                _ensure_report_scheduler_schedule_fields(con)
                scheds = con.execute(
                    "SELECT id, frequency, avg_at, scheduled_time, last_sent_at, created_at, created_by "
                    "FROM report_scheduler ORDER BY id"
                ).fetchall()
                result = []
                for s in scheds:
                    emails = [
                        row["email"]
                        for row in con.execute(
                            "SELECT email FROM report_scheduler_emails "
                            "WHERE scheduler_id = ? ORDER BY added_at",
                            (s["id"],),
                        ).fetchall()
                    ]
                    result.append({
                        "id": s["id"],
                        "frequency": s["frequency"],
                        "avg_at": s["avg_at"],
                        "scheduled_time": s["scheduled_time"],
                        "last_sent_at": s["last_sent_at"],
                        "created_at": s["created_at"],
                        "created_by": s["created_by"],
                        "mail_ids": emails,
                    })
                return result
            finally:
                con.close()
    except Exception:
        logger.exception("get_report_schedulers failed")
        return []


def add_report_scheduler(
    frequency: str,
    mail: str,
    avg_at: str = "Raw",
    scheduled_time: str = "09:00",
    actor: str = "system",
) -> tuple[bool, str, int | None]:
    """Add a new report scheduler event with optional mail ID."""
    allowed = {"Daily", "Weekly", "Monthly"}
    avg_allowed = {"Raw", "5 min", "15 min", "30 min", "60 min"}
    if not frequency:
        return False, "Frequency required.", None
    if frequency not in allowed:
        return False, "Invalid frequency.", None
    if avg_at not in avg_allowed:
        return False, "Invalid averaging option.", None
    if not _parse_scheduled_time(scheduled_time):
        return False, "Invalid scheduled time (use HH:MM).", None
    if mail and not _valid_email(mail):
        return False, "Invalid email address.", None

    try:
        with _db_lock:
            con = _connect()
            try:
                _ensure_report_scheduler_avg_at(con)
                _ensure_report_scheduler_schedule_fields(con)
                existing = con.execute(
                    "SELECT id FROM report_scheduler WHERE frequency = ? AND avg_at = ?",
                    (frequency, avg_at),
                ).fetchone()
                if existing:
                    return False, f"Scheduler event for '{frequency} / {avg_at}' already exists.", None

                cur = con.execute(
                    "INSERT INTO report_scheduler (frequency, avg_at, scheduled_time, created_by) VALUES (?, ?, ?, ?)",
                    (frequency, avg_at, scheduled_time.strip(), actor),
                )
                scheduler_id = cur.lastrowid
                if mail:
                    con.execute(
                        "INSERT INTO report_scheduler_emails (scheduler_id, email) VALUES (?, ?)",
                        (scheduler_id, mail.strip().lower()),
                    )
                con.commit()
            finally:
                con.close()

        log_journal(
            actor,
            "Report Management",
            "ADD",
            f"Scheduler event created: {frequency} @ {scheduled_time}, mail={mail}",
        )
        return True, "Scheduler event added.", scheduler_id
    except Exception as e:
        logger.exception("add_report_scheduler failed")
        return False, str(e), None


def add_report_scheduler_email(
    scheduler_id: int,
    mail: str,
    actor: str = "system",
) -> tuple[bool, str]:
    if not _valid_email(mail):
        return False, "Invalid email address."

    try:
        with _db_lock:
            con = _connect()
            try:
                con.execute(
                    "INSERT INTO report_scheduler_emails (scheduler_id, email) VALUES (?, ?)",
                    (scheduler_id, mail.strip().lower()),
                )
                con.commit()
            finally:
                con.close()

        log_journal(
            actor,
            "Report Management",
            "ADD",
            f"Mail '{mail}' added to scheduler id={scheduler_id}",
        )
        return True, "Mail added."
    except Exception as e:
        logger.exception("add_report_scheduler_email failed")
        return False, str(e)


def delete_report_scheduler_email(
    scheduler_id: int,
    mail: str,
    actor: str = "system",
) -> tuple[bool, str]:
    try:
        with _db_lock:
            con = _connect()
            try:
                con.execute(
                    "DELETE FROM report_scheduler_emails WHERE scheduler_id = ? AND email = ?",
                    (scheduler_id, mail.strip().lower()),
                )
                con.commit()
            finally:
                con.close()

        log_journal(
            actor,
            "Report Management",
            "DELETE",
            f"Mail '{mail}' removed from scheduler id={scheduler_id}",
        )
        return True, "Mail removed."
    except Exception as e:
        logger.exception("delete_report_scheduler_email failed")
        return False, str(e)


def delete_report_scheduler(
    scheduler_id: int,
    actor: str = "system",
) -> tuple[bool, str]:
    try:
        with _db_lock:
            con = _connect()
            try:
                con.execute(
                    "DELETE FROM report_scheduler WHERE id = ?",
                    (scheduler_id,),
                )
                con.commit()
            finally:
                con.close()

        log_journal(
            actor,
            "Report Management",
            "DELETE",
            f"Scheduler event id={scheduler_id} deleted",
        )
        return True, "Scheduler event deleted."
    except Exception as e:
        logger.exception("delete_report_scheduler failed")
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _ensure_report_scheduler_avg_at(con: sqlite3.Connection) -> None:
    """Ensure report_scheduler has avg_at column for older DBs."""
    try:
        columns = [row[1] for row in con.execute("PRAGMA table_info(report_scheduler)").fetchall()]
        if columns and "avg_at" not in columns:
            logger.info("Migration: Adding avg_at column to report_scheduler")
            con.execute(
                "ALTER TABLE report_scheduler ADD COLUMN avg_at TEXT NOT NULL DEFAULT 'Raw'"
            )
            con.commit()
    except Exception:
        logger.exception("Failed ensuring report_scheduler.avg_at migration")


def _ensure_report_scheduler_schedule_fields(con: sqlite3.Connection) -> None:
    """Ensure report_scheduler has scheduled_time and last_sent_at columns."""
    try:
        columns = [row[1] for row in con.execute("PRAGMA table_info(report_scheduler)").fetchall()]
        if columns and "scheduled_time" not in columns:
            logger.info("Migration: Adding scheduled_time column to report_scheduler")
            con.execute(
                "ALTER TABLE report_scheduler ADD COLUMN scheduled_time TEXT NOT NULL DEFAULT '09:00'"
            )
            con.commit()
        if columns and "last_sent_at" not in columns:
            logger.info("Migration: Adding last_sent_at column to report_scheduler")
            con.execute(
                "ALTER TABLE report_scheduler ADD COLUMN last_sent_at TEXT"
            )
            con.commit()
    except Exception:
        logger.exception("Failed ensuring report_scheduler schedule field migration")


def _parse_scheduled_time(value: str) -> Optional[tuple[int, int]]:
    """Parse HH:MM time string and return (hour, minute)."""
    try:
        txt = str(value or "").strip()
        dt = datetime.strptime(txt, "%H:%M")
        return dt.hour, dt.minute
    except Exception:
        return None


def _parse_db_datetime(value: str) -> Optional[datetime]:
    txt = str(value or "").strip().replace("T", " ")
    if not txt:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(txt, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(txt)
    except Exception:
        return None


def _monthly_anchor(date_base: datetime, anchor_day: int, hour: int, minute: int) -> datetime:
    day = min(max(1, int(anchor_day)), calendar.monthrange(date_base.year, date_base.month)[1])
    return datetime(date_base.year, date_base.month, day, hour, minute, 0)


def _add_months(date_base: datetime, months: int, anchor_day: int, hour: int, minute: int) -> datetime:
    y = date_base.year + (date_base.month - 1 + months) // 12
    m = (date_base.month - 1 + months) % 12 + 1
    d = min(max(1, int(anchor_day)), calendar.monthrange(y, m)[1])
    return datetime(y, m, d, hour, minute, 0)


def _current_occurrence(now_dt: datetime, freq: str, hour: int, minute: int,
                        anchor_created: datetime) -> tuple[datetime, datetime]:
    """Return (previous_occurrence, current_occurrence<=now) for a scheduler."""
    f = str(freq or "").strip().lower()
    if f == "daily":
        curr = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if curr > now_dt:
            curr = curr - dt.timedelta(days=1)
        prev = curr - dt.timedelta(days=1)
        return prev, curr

    if f == "weekly":
        target_weekday = anchor_created.weekday()
        curr = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        back_days = (curr.weekday() - target_weekday) % 7
        curr = curr - dt.timedelta(days=back_days)
        if curr > now_dt:
            curr = curr - dt.timedelta(days=7)
        prev = curr - dt.timedelta(days=7)
        return prev, curr

    # monthly
    anchor_day = anchor_created.day
    curr = _monthly_anchor(now_dt, anchor_day, hour, minute)
    if curr > now_dt:
        curr = _add_months(curr, -1, anchor_day, hour, minute)
    prev = _add_months(curr, -1, anchor_day, hour, minute)
    return prev, curr


def _mark_report_scheduler_sent(scheduler_id: int, occurred_at: datetime) -> None:
    with _db_lock:
        con = _connect()
        try:
            con.execute(
                "UPDATE report_scheduler SET last_sent_at = ? WHERE id = ?",
                (occurred_at.strftime("%Y-%m-%d %H:%M:%S"), int(scheduler_id)),
            )
            con.commit()
        finally:
            con.close()


def run_pending_scheduled_reports(actor: str = "system", now_dt: Optional[datetime] = None) -> tuple[int, int]:
    """Run due scheduled reports and return (sent_count, due_count)."""
    now_ref = now_dt or datetime.now()
    sent_count = 0
    due_count = 0

    events = get_report_schedulers()
    if not events:
        return sent_count, due_count

    from db_repository import get_all_readings_in_range

    for event in events:
        freq = str(event.get("frequency", "")).strip()
        sched_time = str(event.get("scheduled_time", "09:00") or "09:00")
        hm = _parse_scheduled_time(sched_time)
        if not hm:
            continue
        hour, minute = hm

        created_at = _parse_db_datetime(str(event.get("created_at", ""))) or now_ref
        prev_occ, curr_occ = _current_occurrence(now_ref, freq, hour, minute, created_at)

        # Never trigger retroactively for an occurrence before the event was created.
        if created_at > curr_occ:
            continue

        last_sent = _parse_db_datetime(str(event.get("last_sent_at", "")))
        if last_sent is not None and last_sent >= curr_occ:
            continue

        due_count += 1
        window_start = prev_occ
        window_end = curr_occ - dt.timedelta(minutes=1)
        if window_end < window_start:
            window_end = window_start

        try:
            rows = get_all_readings_in_range(window_start, window_end)
            ok, _msg = send_report(
                int(event.get("id")),
                rows,
                actor=actor,
                report_range=(
                    window_start.strftime("%Y-%m-%d %H:%M:%S"),
                    window_end.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            if ok:
                _mark_report_scheduler_sent(int(event.get("id")), curr_occ)
                sent_count += 1
        except Exception:
            logger.exception("Failed running scheduled report id=%s", event.get("id"))

    return sent_count, due_count


def _ensure_alert_fire_log_alert_type(con: sqlite3.Connection) -> None:
    """Ensure alert_fire_log has alert_type column for older DBs."""
    try:
        columns = [row[1] for row in con.execute("PRAGMA table_info(alert_fire_log)").fetchall()]
        if columns and "alert_type" not in columns:
            logger.info("Migration: Adding alert_type column to alert_fire_log")
            con.execute(
                "ALTER TABLE alert_fire_log ADD COLUMN alert_type TEXT NOT NULL DEFAULT 'threshold'"
            )
            con.commit()
    except Exception:
        logger.exception("Failed ensuring alert_fire_log.alert_type migration")


def _insert_alert_fire_log(
    *,
    alert_type: str,
    device_addr: int,
    recipients: list[str],
    concentration: float,
    rule_id: Optional[int] = None,
) -> None:
    """Insert one successful alert send event into alert_fire_log."""
    con = _connect()
    try:
        _ensure_alert_fire_log_alert_type(con)
        con.execute(
            """
            INSERT INTO alert_fire_log
            (rule_id, alert_type, device_addr, concentration, recipients)
            VALUES (?, ?, ?, ?, ?)
            """,
            (rule_id, alert_type, device_addr, concentration, ", ".join(recipients)),
        )
        con.commit()
    finally:
        con.close()


def _get_last_alert_fire_epoch(
    alert_type: str,
    device_addr: int,
    rule_id: Optional[int] = None,
) -> float:
    """Return the latest fired_at epoch for an alert type/device[/rule], or 0.0."""
    try:
        with _db_lock:
            con = _connect()
            try:
                _ensure_alert_fire_log_alert_type(con)
                if rule_id is None:
                    row = con.execute(
                        """
                        SELECT CAST(strftime('%s', fired_at) AS INTEGER) AS fired_epoch
                          FROM alert_fire_log
                         WHERE alert_type = ?
                           AND device_addr = ?
                         ORDER BY fired_at DESC, id DESC
                         LIMIT 1
                        """,
                        (alert_type, int(device_addr)),
                    ).fetchone()
                else:
                    row = con.execute(
                        """
                        SELECT CAST(strftime('%s', fired_at) AS INTEGER) AS fired_epoch
                          FROM alert_fire_log
                         WHERE alert_type = ?
                           AND device_addr = ?
                           AND rule_id = ?
                         ORDER BY fired_at DESC, id DESC
                         LIMIT 1
                        """,
                        (alert_type, int(device_addr), int(rule_id)),
                    ).fetchone()
                if not row or row["fired_epoch"] is None:
                    return 0.0
                return float(row["fired_epoch"])
            finally:
                con.close()
    except Exception:
        logger.exception(
            "Failed reading last alert fire time for alert_type=%s device=%d rule_id=%s",
            alert_type,
            device_addr,
            rule_id,
        )
        return 0.0


def _valid_email(email: str) -> bool:
    """Very basic syntactic check — prevents obvious garbage."""
    email = email.strip()
    return "@" in email and "." in email.split("@")[-1] and len(email) <= 254


# ─────────────────────────────────────────────────────────────────────────────
# Settings Journal
# ─────────────────────────────────────────────────────────────────────────────

def log_journal(username: str, section: str, action: str, detail: str) -> None:
    """Append one row to settings_journal.  Never raises."""
    try:
        with _db_lock:
            con = _connect()
            try:
                con.execute(
                    "INSERT INTO settings_journal (username, section, action, detail) "
                    "VALUES (?, ?, ?, ?)",
                    (username or "system", section, action, detail),
                )

                con.commit()
            finally:
                con.close()
    except Exception:
        logger.exception("Journal write failed")


def get_journal(limit: int = 200) -> list[JournalEntry]:
    """Return the *limit* most-recent journal rows (newest first)."""
    try:
        with _db_lock:
            con = _connect()
            try:
                rows = con.execute(
                    "SELECT id, timestamp, username, section, action, detail "
                    "FROM settings_journal ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()

                return [JournalEntry(
                    entry_id  = r["id"],
                    timestamp = r["timestamp"],
                    username  = r["username"],
                    section   = r["section"],
                    action    = r["action"],
                    detail    = r["detail"],
                ) for r in rows]
            
            finally:
                con.close()
    except Exception:
        logger.exception("get_journal failed")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# SMTP configuration
# ─────────────────────────────────────────────────────────────────────────────

def get_smtp_config() -> dict:
    """Return the single SMTP config row (or empty defaults)."""
    try:
        with _db_lock:
            con = _connect()
            try:
                row = con.execute("SELECT * FROM smtp_config WHERE id = 1").fetchone()
                if row:
                    return dict(row)
            finally:
                con.close()
    except Exception:
        logger.exception("get_smtp_config failed")
    return {"host": "", "port": 587, "username": "", "password": "",
            "from_email": "", "use_tls": 1}


def save_smtp_config(host: str, port: int, username: str, password: str,
                     from_email: str, use_tls: bool,
                     actor: str = "system") -> tuple[bool, str]:
    """Upsert SMTP config. Returns (ok, message)."""
    try:
        with _db_lock:
            con = _connect()
            try:
                con.execute("""
                    INSERT INTO smtp_config (id, host, port, username, password,
                                            from_email, use_tls, updated_at)
                    VALUES (1, ?, ?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(id) DO UPDATE SET
                        host       = excluded.host,
                        port       = excluded.port,
                        username   = excluded.username,
                        password   = excluded.password,
                        from_email = excluded.from_email,
                        use_tls    = excluded.use_tls,
                        updated_at = excluded.updated_at
                """, (host.strip(), int(port), username.strip(),
                      password.replace(" ", ""),
                      from_email.strip(), int(use_tls)))
                
                con.commit()
            finally:
                con.close()
        log_journal(actor, "Alert Management",
                    "UPDATE", f"SMTP config updated (host={host}, from={from_email})")
        return True, "SMTP settings saved."
    except Exception as e:
        logger.exception("save_smtp_config failed")
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Alert Rules CRUD
# ─────────────────────────────────────────────────────────────────────────────

def get_alert_rules() -> list[AlertRule]:
    """Return all alert rules with their email lists, ordered by threshold.
    NOTE: callers that need a live reload (UI) should call this directly.
    Hot-path callers (polling) should call _get_cached_rules() instead.
    """
    try:
        with _db_lock:
            con = _connect()
            try:
                rules = con.execute(
                    "SELECT id, threshold, occurrence, created_at, created_by "
                    "FROM alert_rules ORDER BY threshold"
                ).fetchall()

                result = []

                for r in rules:
                    # Fetch associated emails for this rule
                    emails = [
                        row["email"] for row in con.execute(
                            "SELECT email FROM alert_emails WHERE rule_id = ? "
                            "ORDER BY added_at",
                            (r["id"],),
                        ).fetchall()
                    ]

                    result.append(AlertRule(
                        rule_id    = r["id"],
                        threshold  = r["threshold"],
                        occurrence = r["occurrence"],
                        created_at = r["created_at"],
                        created_by = r["created_by"],
                        emails     = emails,
                    ))
                return result
            finally:
                con.close()
    except Exception:
        logger.exception("get_alert_rules failed")
        return []


def add_alert(email: str, threshold: float, occurrence: str,
              actor: str = "system") -> tuple[bool, str]:
    """
    Add an alert entry.

    Rules:
      • If the exact (threshold, email) pair already exists → return error.
      • If threshold exists but email is new → add email to the existing rule.
      • If threshold is new → create a new rule + add email.

    Returns (ok, message).
    """
    email = email.strip().lower()
    if not _valid_email(email):
        return False, "Invalid email address."
    if threshold <= 0:
        return False, "Threshold must be a positive number."
    if occurrence not in _ALLOWED_OCCURRENCES:
        return False, "Occurrence must be one of: 10-min, hourly, bi-hourly."

    try:
        with _db_lock:
            con = _connect()
            try:
                # Look for an existing rule with same threshold
                existing = con.execute(
                    "SELECT id FROM alert_rules WHERE threshold = ?",
                    (threshold,),
                ).fetchone()

                if existing:
                    rule_id = existing["id"]
                    # Check if this email is already registered for this rule
                    dup = con.execute(
                        "SELECT id FROM alert_emails "
                        "WHERE rule_id = ? AND email = ?",
                        (rule_id, email),
                    ).fetchone()

                    if dup:
                        return False, (
                            f"Alert already configured: {email} is already in "
                            f"the list for threshold {threshold}."
                        )
                    
                    # Add email to existing rule
                    con.execute(
                        "INSERT INTO alert_emails (rule_id, email) VALUES (?, ?)",
                        (rule_id, email),
                    )

                    con.commit()
                    _invalidate_cache()

                    log_journal(actor, "Alert Management", "ADD",
                                f"Email '{email}' added to existing rule "
                                f"(threshold={threshold})")
                    return True, (
                        f"Email '{email}' added to existing alert rule "
                        f"for threshold {threshold}."
                    )
                else:
                    # Create a new rule + add the email
                    cur = con.execute(
                        "INSERT INTO alert_rules (threshold, occurrence, created_by) "
                        "VALUES (?, ?, ?)",
                        (threshold, occurrence, actor),
                    )

                    # Get the new rule_id for the email insertion
                    rule_id = cur.lastrowid
                    
                    con.execute(
                        "INSERT INTO alert_emails (rule_id, email) VALUES (?, ?)",
                        (rule_id, email),
                    )

                    con.commit()
                    _invalidate_cache()
                    
                    log_journal(actor, "Alert Management", "ADD",
                                f"New rule created: threshold={threshold}, "
                                f"occurrence={occurrence}, email='{email}'")
                    
                    return True, (
                        f"Alert rule created: threshold={threshold} "
                        f"({occurrence}), email='{email}'."
                    )
            finally:
                con.close()
    except Exception as e:
        logger.exception("add_alert failed")
        return False, str(e)


def update_rule_occurrence(rule_id: int, occurrence: str,
                            actor: str = "system") -> tuple[bool, str]:
    """Change the occurrence setting of an existing rule."""
    if occurrence not in _ALLOWED_OCCURRENCES:
        return False, "Occurrence must be one of: 10-min, hourly, bi-hourly."
    try:
        with _db_lock:
            con = _connect()
            try:
                row = con.execute(
                    "SELECT threshold FROM alert_rules WHERE id = ?",
                    (rule_id,),
                ).fetchone()

                if not row:
                    return False, "Rule not found."
                
                con.execute(
                    "UPDATE alert_rules SET occurrence = ? WHERE id = ?",
                    (occurrence, rule_id),
                )

                con.commit()
                _invalidate_cache()
            finally:
                con.close()

        log_journal(actor, "Alert Management", "UPDATE",
                    f"Rule id={rule_id} occurrence changed to '{occurrence}'")
        return True, "Occurrence updated."
    except Exception as e:
        logger.exception("update_rule_occurrence failed")
        return False, str(e)


def delete_alert_email(rule_id: int, email: str,
                        actor: str = "system") -> tuple[bool, str]:
    """
    Remove one email from a rule.
    If it was the last email, the rule itself is also deleted.
    """
    try:
        with _db_lock:
            con = _connect()
            try:
                con.execute(
                    "DELETE FROM alert_emails WHERE rule_id = ? AND email = ?",
                    (rule_id, email),
                )

                remaining = con.execute(
                    "SELECT COUNT(*) AS cnt FROM alert_emails WHERE rule_id = ?",
                    (rule_id,),
                ).fetchone()["cnt"]

                if remaining == 0:
                    threshold_row = con.execute(
                        "SELECT threshold FROM alert_rules WHERE id = ?",
                        (rule_id,),
                    ).fetchone()

                    thr = threshold_row["threshold"] if threshold_row else "?"
                    
                    con.execute(
                        "DELETE FROM alert_rules WHERE id = ?", (rule_id,)
                    )

                    log_journal(actor, "Alert Management", "DELETE",
                                f"Rule for threshold={thr} deleted "
                                f"(last email removed)")
                else:
                    log_journal(actor, "Alert Management", "DELETE",
                                f"Email '{email}' removed from rule id={rule_id}")
                con.commit()
                _invalidate_cache()
            finally:
                con.close()
        return True, "Deleted."
    except Exception as e:
        logger.exception("delete_alert_email failed")
        return False, str(e)


def delete_alert_rule(rule_id: int, actor: str = "system") -> tuple[bool, str]:
    """Delete an entire rule (cascades to all its emails)."""
    try:
        with _db_lock:
            con = _connect()
            try:
                row = con.execute(
                    "SELECT threshold FROM alert_rules WHERE id = ?",
                    (rule_id,),
                ).fetchone()

                if not row:
                    return False, "Rule not found."
                
                thr = row["threshold"]

                con.execute(
                    "DELETE FROM alert_rules WHERE id = ?", (rule_id,)
                )
                
                con.commit()
                _invalidate_cache()
            finally:
                con.close()
        log_journal(actor, "Alert Management", "DELETE",
                    f"Alert rule for threshold={thr} deleted entirely")
        return True, f"Rule for threshold {thr} deleted."
    except Exception as e:
        logger.exception("delete_alert_rule failed")
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Alert firing engine
# ─────────────────────────────────────────────────────────────────────────────

_OCCURRENCE_SECONDS = {
    "10-min":   600,
    "hourly":    3_600,
    "bi-hourly": 7_200,
}


def check_and_fire(device_addr: int, concentration: float) -> None:
    """
    Called on every poll for each online device.
    Fires email alerts whose threshold is exceeded and whose throttle has expired.
    Never raises — all errors are logged only.
    """
    try:
        rules = _get_cached_rules()
    except Exception:
        return

    for rule in rules:
        if concentration <= rule.threshold:
            continue
        if not rule.emails:
            continue

        cooldown = _OCCURRENCE_SECONDS.get(rule.occurrence, 3_600)
        key = (int(rule.rule_id), int(device_addr))
        last = _get_last_alert_fire_epoch(
            "threshold",
            device_addr,
            rule_id=rule.rule_id,
        )
        now = time.time()
        if now - last < cooldown:
            continue   # still in throttle window

        with _threshold_alert_inflight_lock:
            if key in _threshold_alert_inflight:
                continue
            _threshold_alert_inflight.add(key)

        # Fire in a background thread so UI is never blocked
        threading.Thread(
            target=_send_alert,
            args=(rule, device_addr, concentration, last),
            daemon=True,
        ).start()


def _send_alert(rule: AlertRule, device_addr: int, concentration: float, last: float) -> None:
    """Build and send the alert email, then log it."""
    try:
        cfg = get_smtp_config()
        host = cfg.get("host", "").strip()
        if not host:
            logger.warning("Alert triggered but SMTP host is not configured.")
            return

        subject = (
            f"[H2 Dashboard] Gas Concentration Alert — "
            f"Device {device_addr:03d} exceeded {rule.threshold}"
        )
        last_fired_msg = (
            f"Last alert for this rule was fired at "
            f"{datetime.fromtimestamp(last).strftime('%Y-%m-%d %H:%M:%S')}.\n"
            if last > 0
            else ""
        )
        body = (
            f"Alert triggered at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"  Device   : {device_addr:03d}\n"
            f"  Reading  : {concentration:.2f}\n"
            f"  Threshold: {rule.threshold}\n"
            f"  Schedule : {rule.occurrence}\n\n"
            f"This alert was fired because the concentration reading exceeded your "
            f"configured threshold.\n"
            f"{last_fired_msg}"
            f"Next alert (if the condition persists) will fire after the "
            f"'{rule.occurrence}' interval.\n\n"
            f"— H2 Gas Detector Dashboard"
        )

        sent_to: list[str] = []
        errors:  list[str] = []

        for recipient in rule.emails:
            try:
                msg = MIMEMultipart()
                msg["From"] = cfg.get("from_email") or cfg.get("username", "")
                msg["To"] = recipient
                msg["Subject"] = Header(subject, "utf-8")
                msg.attach(MIMEText(body, "plain", "utf-8"))

                port = int(cfg.get("port", 587))
                sec  = int(cfg.get("use_tls", 1))  # 1=STARTTLS, 2=SSL, 0=None

                import ssl as _ssl
                ctx = _ssl.create_default_context()
                if sec == 2:          # Direct SSL (port 465)
                    server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=15)
                    server.ehlo()
                elif sec == 1:        # STARTTLS (port 587)
                    server = smtplib.SMTP(host, port, timeout=15)
                    server.ehlo()
                    server.starttls(context=ctx)
                    server.ehlo()
                else:                 # No encryption
                    server = smtplib.SMTP(host, port, timeout=15)
                    server.ehlo()

                uname = cfg.get("username", "").strip()
                # Join on split() removes ALL whitespace (App Passwords copied with spaces)
                pwd   = "".join(cfg.get("password", "").split())
                if uname and pwd:
                    server.login(uname, pwd)

                server.sendmail(msg["From"], recipient, msg.as_string())
                server.quit()
                sent_to.append(recipient)
            except Exception as e:
                logger.warning("Failed to send alert to %s: %s", recipient, e)
                errors.append(f"{recipient}: {e}")

        # Log the fire event regardless of partial success
        if sent_to:
            try:
                _insert_alert_fire_log(
                    alert_type="threshold",
                    rule_id=rule.rule_id,
                    device_addr=device_addr,
                    concentration=concentration,
                    recipients=sent_to,
                )
            except Exception:
                logger.exception("alert_fire_log insert failed")

        if errors:
            logger.warning("Alert email errors: %s", "; ".join(errors))
    finally:
        key = (int(rule.rule_id), int(device_addr))
        with _threshold_alert_inflight_lock:
            _threshold_alert_inflight.discard(key)


# ─────────────────────────────────────────────────────────────────────────────
# Report generation and sending
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_report_rows(rows: list[dict]) -> list[dict]:
    """Normalize report rows to a stable shape for export/email rendering."""
    out: list[dict] = []
    for row in rows:
        ts_raw = str(row.get("recorded_at", row.get("polled_at", "")) or "").replace("T", " ")
        if not ts_raw:
            continue
        try:
            ts_dt = datetime.fromisoformat(ts_raw)
        except Exception:
            try:
                ts_dt = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue

        name = str(
            row.get("_dev_name")
            or row.get("device_name")
            or f"Device {int(row.get('device_addr', 0) or 0):03d}"
        )
        unit = str(row.get("gas_unit") or row.get("unit") or "ppm")
        try:
            val = float(row.get("concentration_value", row.get("concentration", 0.0)) or 0.0)
        except Exception:
            val = 0.0

        out.append({
            "recorded_at": ts_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "recorded_at_dt": ts_dt,
            "device_name": name,
            "gas_unit": unit,
            "concentration_value": val,
        })

    out.sort(key=lambda r: (r["recorded_at"], r["device_name"]))
    return out


def _aggregate_report_rows(rows: list[dict], frequency: str, start_dt: Optional[datetime] = None) -> list[dict]:
    """Aggregate normalized rows by frequency and device.

    If ``start_dt`` is provided aggregation buckets are aligned relative to
    that start (same behaviour as the UI CSV export). Otherwise buckets are
    aligned to epoch boundaries (legacy behaviour).
    """
    freq_map = {
        "raw": 0,
        "none": 0,
        "1 min": 60,
        "5 min": 300,
        "15 min": 900,
        "30 min": 1800,
        "60 min": 3600,
        "1 hour": 3600,
    }
    freq_secs = freq_map.get(str(frequency).strip().lower(), 0)
    if freq_secs <= 0:
        return list(rows)

    start_ts: Optional[int]
    if start_dt is not None:
        try:
            start_ts = int(start_dt.timestamp())
        except Exception:
            start_ts = None
    else:
        start_ts = None

    buckets: dict[tuple[str, int], dict] = {}
    for row in rows:
        epoch = int(row["recorded_at_dt"].timestamp())
        if start_ts is None:
            bucket_start = (epoch // freq_secs) * freq_secs
        else:
            elapsed = max(0, epoch - start_ts)
            bucket_idx = int(elapsed // freq_secs)
            bucket_start = start_ts + bucket_idx * freq_secs

        key = (row["device_name"], bucket_start)
        entry = buckets.setdefault(key, {
            "device_name": row["device_name"],
            "gas_unit": row.get("gas_unit", "ppm"),
            "sum": 0.0,
            "count": 0,
            "bucket_start": bucket_start,
        })
        entry["sum"] += float(row.get("concentration_value") or 0.0)
        entry["count"] += 1

    out: list[dict] = []
    for entry in buckets.values():
        count = max(1, int(entry["count"]))
        ts_dt = datetime.fromtimestamp(int(entry["bucket_start"]) + freq_secs)
        out.append({
            "recorded_at": ts_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "recorded_at_dt": ts_dt,
            "device_name": entry["device_name"],
            "gas_unit": entry.get("gas_unit", "ppm"),
            "concentration_value": entry["sum"] / count,
        })

    out.sort(key=lambda r: (r["recorded_at"], r["device_name"]))
    return out


def build_device_logs_report_pdf_bytes(
    rows: list[dict],
    *,
    frequency: str = "Raw",
    schedule_frequency: Optional[str] = None,
    report_range: Optional[tuple[str, str]] = None,
    forced_timestamps: Optional[list[str]] = None,
    forced_device_names: Optional[list[str]] = None,
) -> bytes:
    """Build Device Logs PDF report bytes using the same layout as export PDF."""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable

    norm_rows = _normalize_report_rows(rows)
    # If a report range start is provided, align aggregation buckets to that
    # start (this matches the CSV export behaviour in the UI).
    start_dt_obj: Optional[datetime] = None
    if report_range and len(report_range) >= 1 and report_range[0]:
        try:
            start_dt_obj = datetime.strptime(report_range[0], "%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                start_dt_obj = datetime.fromisoformat(str(report_range[0]))
            except Exception:
                start_dt_obj = None

    agg_rows = _aggregate_report_rows(norm_rows, frequency, start_dt=start_dt_obj)

    # Device columns in stable order.
    device_units: dict[str, str] = {}
    for row in norm_rows:
        device_units.setdefault(row["device_name"], row.get("gas_unit", "ppm"))
    if forced_device_names:
        for name in forced_device_names:
            device_units.setdefault(str(name), "ppm")
        device_names = [str(name) for name in forced_device_names]
    else:
        device_names = sorted(device_units.keys())

    if forced_timestamps is not None:
        timestamps = [str(ts) for ts in forced_timestamps]
    else:
        timestamps = sorted({str(r.get("recorded_at", "")) for r in agg_rows if r.get("recorded_at")})
    pivot: dict[str, dict[str, float]] = {ts: {} for ts in timestamps}
    for row in agg_rows:
        ts = str(row.get("recorded_at", ""))
        if ts not in pivot:
            continue
        try:
            pivot[ts][row["device_name"]] = float(row.get("concentration_value") or 0.0)
        except Exception:
            continue

    # Summary from raw rows
    values_by_name: dict[str, list[float]] = {name: [] for name in device_names}
    for row in norm_rows:
        if row["device_name"] in values_by_name:
            values_by_name[row["device_name"]].append(float(row["concentration_value"]))

    summary_rows: list[list[str]] = []
    for name in device_names:
        vals = values_by_name.get(name, [])
        unit = device_units.get(name, "ppm")
        if vals:
            mn = min(vals)
            mx = max(vals)
            avg = sum(vals) / len(vals)
            summary_rows.append([name, unit, f"{mn:.2f}", f"{mx:.2f}", f"{avg:.2f}"])
        else:
            summary_rows.append([name, unit, "—", "—", "—"])

    try:
        from db_repository import get_all_plants
        plants = get_all_plants()
        plant = plants[0] if plants else {}
    except Exception:
        plant = {}

    company_name = str(plant.get("company_name", "—") or "—")
    location = str(plant.get("location", "—") or "—")
    extracted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if report_range and len(report_range) == 2:
        range_str = f"{report_range[0]}  to  {report_range[1]}"
    elif norm_rows:
        range_str = f"{norm_rows[0]['recorded_at']}  to  {norm_rows[-1]['recorded_at']}"
    else:
        range_str = "—"
    freq_str = "Raw" if str(frequency).strip().lower() in {"raw", "none"} else str(frequency)

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=24,
        rightMargin=24,
        topMargin=24,
        bottomMargin=24,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#0F172A"),
        spaceAfter=6,
    )
    meta_style = ParagraphStyle(
        "Meta",
        parent=styles["Normal"],
        alignment=TA_LEFT,
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#0F172A"),
    )
    section_style = ParagraphStyle(
        "Section",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=colors.white,
        alignment=TA_LEFT,
    )

    BLUE = colors.HexColor("#0B9C42")
    BLUE_DARK = colors.HexColor("#0B9C42")
    BORDER = colors.HexColor("#BFDBFE")
    story = [
        Paragraph("H2 Gas Detector Report", title_style),
        HRFlowable(width="55%", thickness=1.2, color=BLUE_DARK, spaceBefore=2, spaceAfter=10, hAlign="CENTER"),
    ]

    meta_lines = [
        f"<b>Company Name:</b> {company_name}",
        f"<b>Location:</b> {location}",
        f"<b>Report Extracted Time:</b> {extracted_at}",
        f"<b>Report Range:</b> {range_str}",
    ]
    if schedule_frequency:
        meta_lines.append(f"<b>Report Schedule:</b> {schedule_frequency}")
    meta_lines.append(f"<b>Frequency:</b> {freq_str}")
    for line in meta_lines:
        story.append(Paragraph(line, meta_style))
    story.append(Spacer(1, 10))

    summary_title = Table([[Paragraph("Summary Data", section_style)]], colWidths=[doc.width])
    summary_title.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BLUE),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([summary_title, Spacer(1, 6)])

    summary_table_data = [["Device", "Units", "Min", "Max", "Average"]] + summary_rows
    summary_col_widths = [doc.width * 0.22, doc.width * 0.14,
                          doc.width * 0.21, doc.width * 0.21, doc.width * 0.22]

    summary_tbl = Table(summary_table_data, repeatRows=1, colWidths=summary_col_widths)
    summary_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLUE_DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EFF6FF")]),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([summary_tbl, Spacer(1, 12)])

    detail_title = Table([[Paragraph(f"Detailed Records (Avg By: {freq_str})", section_style)]], colWidths=[doc.width])
    detail_title.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BLUE),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([detail_title, Spacer(1, 6)])

    detail_header = ["Timestamp"] + device_names
    detail_rows = [detail_header]
    missing_text = "NA" if forced_timestamps is not None else "—"
    for ts in timestamps:
        row = [ts]
        row_map = pivot.get(ts, {})
        for dev_name in device_names:
            val = row_map.get(dev_name)
            row.append(f"{val:.2f}" if val is not None else missing_text)
        detail_rows.append(row)

    if len(detail_header) == 1:
        detail_rows.append(["No device columns selected"])

    ts_w = 160
    remaining = max(120, doc.width - ts_w)
    per_col = remaining / max(1, len(detail_header) - 1)
    detail_col_widths = [ts_w] + [per_col] * (len(detail_header) - 1)

    detail_tbl = Table(detail_rows, repeatRows=1, colWidths=detail_col_widths)
    detail_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLUE_DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(detail_tbl)

    doc.build(story)
    return buf.getvalue()


def generate_report_html(
    rows: list[dict],
    scheduler: dict,
) -> str:
    """Generate an HTML report from device log rows."""
    freq = scheduler.get("frequency", "Unknown")
    avg_at = scheduler.get("avg_at", "Raw")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; color: #333; }}
            h1 {{ color: #0C4A6E; }}
            .info {{ background: #F0F9FF; padding: 10px; border-radius: 5px; margin-bottom: 15px; }}
            table {{ border-collapse: collapse; width: 100%; margin-top: 10px; }}
            th {{ background: #0C4A6E; color: white; padding: 10px; text-align: left; }}
            td {{ padding: 8px; border-bottom: 1px solid #E2E8F0; }}
            tr:nth-child(even) {{ background: #F8FAFC; }}
            .footer {{ margin-top: 20px; text-align: center; color: #64748B; font-size: 12px; }}
        </style>
    </head>
    <body>
        <h1>H2 Gas Detector Report</h1>
        <div class="info">
            <p><strong>Frequency:</strong> {freq}</p>
            <p><strong>Averaging:</strong> {avg_at}</p>
            <p><strong>Generated:</strong> {timestamp}</p>
            <p><strong>Rows:</strong> {len(rows)}</p>
        </div>
        <table>
            <tr>
                <th>#</th>
                <th>Device</th>
                <th>Concentration</th>
                <th>Low Alarm</th>
                <th>High Alarm</th>
                <th>Alarm Status</th>
                <th>Recorded At</th>
            </tr>
    """

    for idx, row in enumerate(rows, 1):
        # Safe float conversion for concentration
        conc_val = row.get('concentration', row.get('concentration_value', '-'))
        try:
            conc_str = f"{float(conc_val):.2f}" if conc_val != '-' else '-'
        except (ValueError, TypeError):
            conc_str = str(conc_val)

        recorded_at = row.get('recorded_at', row.get('polled_at', '-'))

        html += f"""
            <tr>
                <td>{idx}</td>
                <td>{row.get('device_name', 'Unknown')}</td>
                <td>{conc_str}</td>
                <td>{row.get('low_alarm', '-')}</td>
                <td>{row.get('high_alarm', '-')}</td>
                <td>{row.get('alarm_status', '-')}</td>
                <td>{recorded_at}</td>
            </tr>
        """

    html += """
        </table>
        <div class="footer">
            <p>This is an automated H2 Gas Detector report.</p>
        </div>
    </body>
    </html>
    """
    return html


def send_report(
    scheduler_id: int,
    rows: list[dict],
    actor: str = "system",
    report_range: Optional[tuple[str, str]] = None,
) -> tuple[bool, str]:
    """Send a report email to all configured recipients."""
    try:
        with _db_lock:
            con = _connect()
            try:
                _ensure_report_scheduler_avg_at(con)
                sch = con.execute(
                    "SELECT id, frequency, avg_at FROM report_scheduler WHERE id = ?",
                    (scheduler_id,),
                ).fetchone()

                if not sch:
                    return False, "Scheduler event not found."

                scheduler = {
                    "id": sch["id"],
                    "frequency": sch["frequency"],
                    "avg_at": sch["avg_at"],
                }

                # Fetch email recipients
                emails = [
                    row["email"]
                    for row in con.execute(
                        "SELECT email FROM report_scheduler_emails WHERE scheduler_id = ?",
                        (scheduler_id,),
                    ).fetchall()
                ]
            finally:
                con.close()
    except Exception as e:
        logger.exception("Failed to fetch scheduler info")
        return False, str(e)

    if not emails:
        return False, "No recipients configured for this event."

    cfg = get_smtp_config()
    host = cfg.get("host", "").strip()
    if not host:
        logger.warning("Report email triggered but SMTP host not configured.")
        return False, "SMTP not configured."

    try:
        pdf_bytes = build_device_logs_report_pdf_bytes(
            rows,
            frequency=str(scheduler.get("avg_at", "Raw") or "Raw"),
            schedule_frequency=str(scheduler.get("frequency", "") or ""),
            report_range=report_range,
        )
    except Exception as e:
        logger.exception("Failed to build report PDF")
        return False, f"Failed to build report PDF: {e}"

    subject = (
        f"[H2 Dashboard] Device Logs Report — {scheduler['frequency']} "
        f"(Avg: {scheduler['avg_at']})"
    )

    sent_to: list[str] = []
    errors: list[str] = []

    for recipient in emails:
        try:
            msg = MIMEMultipart()
            msg["From"] = cfg.get("from_email") or cfg.get("username", "")
            msg["To"] = recipient
            msg["Subject"] = Header(subject, "utf-8")
            msg.attach(MIMEText(
                "Please find attached the scheduled Device Logs report in PDF format.",
                "plain",
                "utf-8",
            ))
            safe_freq = str(scheduler.get("frequency", "Report")).replace(" ", "_")
            file_name = f"device_logs_report_{safe_freq}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            pdf_part = MIMEApplication(pdf_bytes, _subtype="pdf")
            pdf_part.add_header("Content-Disposition", "attachment", filename=file_name)
            msg.attach(pdf_part)

            port = int(cfg.get("port", 587))
            sec = int(cfg.get("use_tls", 1))

            import ssl as _ssl

            ctx = _ssl.create_default_context()
            if sec == 2:  # Direct SSL
                server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=15)
                server.ehlo()
            elif sec == 1:  # STARTTLS
                server = smtplib.SMTP(host, port, timeout=15)
                server.ehlo()
                server.starttls(context=ctx)
                server.ehlo()
            else:  # No encryption
                server = smtplib.SMTP(host, port, timeout=15)
                server.ehlo()

            uname = cfg.get("username", "").strip()
            pwd = "".join(cfg.get("password", "").split())
            if uname and pwd:
                server.login(uname, pwd)

            server.sendmail(msg["From"], recipient, msg.as_string())
            server.quit()
            sent_to.append(recipient)
        except Exception as e:
            logger.warning("Failed to send report to %s: %s", recipient, e)
            errors.append(f"{recipient}: {e}")

    if sent_to:
        try:
            log_journal(
                actor,
                "Report Management",
                "SEND",
                f"Report sent for scheduler id={scheduler_id}, "
                f"freq={scheduler['frequency']}, recipients={len(sent_to)}",
            )
        except Exception:
            logger.exception("Failed to log report send")

    if errors:
        logger.warning("Report email errors: %s", "; ".join(errors))
        if not sent_to:
            return False, f"Failed to send report: {errors[0]}"

    return True, f"Report sent to {len(sent_to)} recipient(s)."

# ─────────────────────────────────────────────────────────────────────────────
# Device Offline Alert (per-device on each scan)
# ─────────────────────────────────────────────────────────────────────────────

def check_and_fire_offline_alert(device_addr: int, device_name: str) -> None:
    """
    Called when a device is detected as offline during scan.
    Sends offline alert email if enabled and cooldown has expired.
    Uses global cooldown setting from device_offline_alerts configuration.
    Never raises — all errors are logged only.
    """
    from db_repository import get_offline_alert_config, update_offline_alert_timestamp
    
    try:
        cfg_dict = get_offline_alert_config()
        enabled = cfg_dict.get("enabled", 1)
        if not enabled:
            return  # offline alerts disabled
        
        cooldown_minutes = cfg_dict.get("cooldown_minutes", 30)
        cooldown_secs = cooldown_minutes * 60
        
        now = time.time()
        last = _get_last_alert_fire_epoch("offline", device_addr)
        
        # Check if still in cooldown window
        if now - last < cooldown_secs:
            return  # Still in cooldown, don't alert
        
        # Get alert rules to find recipient emails
        rules = _get_cached_rules()
        if not rules:
            logger.debug("No alert rules configured — cannot send offline alert for device %d", device_addr)
            return
        
        # Use emails from the first rule (or collect from all rules)
        all_recipients = set()
        for rule in rules:
            all_recipients.update(rule.emails)
        
        if not all_recipients:
            logger.debug("No alert recipients configured — cannot send offline alert")
            return

        with _offline_alert_inflight_lock:
            if device_addr in _offline_alert_inflight:
                return
            _offline_alert_inflight.add(device_addr)
        
        # Fire in a background thread
        logger.warning(
            "Device %d (%s) is offline — firing offline alert",
            device_addr, device_name,
        )
        threading.Thread(
            target=_send_offline_alert,
            args=(device_addr, device_name, list(all_recipients), last),
            daemon=True,
        ).start()

        # Update DB timestamp for tracking
        try:
            update_offline_alert_timestamp(device_addr)
        except Exception:
            logger.exception("Failed to update offline alert timestamp in DB")
            
    except Exception:
        logger.exception("check_and_fire_offline_alert failed for device %d", device_addr)


def _send_offline_alert(device_addr: int, device_name: str, recipients: list[str], last: float) -> None:
    """Build and send the offline device alert email."""
    try:
        cfg = get_smtp_config()
        host = cfg.get("host", "").strip()
        if not host:
            logger.warning("Device offline alert triggered but SMTP host not configured")
            return
        
        subject = f"[H2 Dashboard] Device Offline Alert — {device_name} (Address {device_addr:03d})"
        last_fired_msg = (
            f"Last offline alert for this device was fired at "
            f"{datetime.fromtimestamp(last).strftime('%Y-%m-%d %H:%M:%S')}.\n"
            if last > 0
            else ""
        )
        body = (
            f"Alert triggered at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"  Device Name   : {device_name}\n"
            f"  Device Address: {device_addr:03d}\n"
            f"  Status        : OFFLINE\n\n"
            f"The device did not respond during the last device scan.\n"
            f"{last_fired_msg}"
            f"Please check the device connection and power status.\n\n"
            f"— H2 Gas Detector Dashboard"
        )
        
        sent_to: list[str] = []
        errors:  list[str] = []
        
        for recipient in recipients:
            try:
                msg = MIMEMultipart()
                msg["From"] = cfg.get("from_email") or cfg.get("username", "")
                msg["To"] = recipient
                msg["Subject"] = Header(subject, "utf-8")
                msg.attach(MIMEText(body, "plain", "utf-8"))
                
                port = int(cfg.get("port", 587))
                sec  = int(cfg.get("use_tls", 1))  # 1=STARTTLS, 2=SSL, 0=None
                
                import ssl as _ssl
                ctx = _ssl.create_default_context()
                if sec == 2:          # Direct SSL (port 465)
                    server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=15)
                    server.ehlo()
                elif sec == 1:        # STARTTLS (port 587)
                    server = smtplib.SMTP(host, port, timeout=15)
                    server.ehlo()
                    server.starttls(context=ctx)
                    server.ehlo()
                else:                 # No encryption
                    server = smtplib.SMTP(host, port, timeout=15)
                    server.ehlo()
                
                uname = cfg.get("username", "").strip()
                pwd   = "".join(cfg.get("password", "").split())
                if uname and pwd:
                    server.login(uname, pwd)
                
                server.sendmail(msg["From"], recipient, msg.as_string())
                server.quit()
                sent_to.append(recipient)
            except Exception as e:
                logger.warning("Failed to send offline alert to %s: %s", recipient, e)
                errors.append(f"{recipient}: {e}")
        
        if sent_to:
            try:
                _insert_alert_fire_log(
                    alert_type="offline",
                    rule_id=None,
                    device_addr=device_addr,
                    concentration=0.0,
                    recipients=sent_to,
                )
            except Exception:
                logger.exception("offline alert_fire_log insert failed")
            logger.info("Offline device alert sent to %d recipient(s) for device %d", len(sent_to), device_addr)
        if errors:
            logger.warning("Offline device alert errors: %s", "; ".join(errors))
    finally:
        with _offline_alert_inflight_lock:
            _offline_alert_inflight.discard(device_addr)