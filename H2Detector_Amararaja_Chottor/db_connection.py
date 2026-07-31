"""
db_connection.py
----------------
Singleton SQLite connection manager for the H2 Gas Detector Dashboard.

All other database modules import `get_connection()` from here.
The database file is stored alongside this script so the application
is fully self-contained (no installation needed on the target machine).
"""

import sqlite3
import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (stable across UI EXE + Service EXE)
# ---------------------------------------------------------------------------
def _resolve_data_dir() -> Path:
    """
    Return a stable writable directory for DB/logs.

    Priority:
    1) H2_DATA_DIR environment variable (explicit override)
    2) Frozen EXE: %PROGRAMDATA%\H2GasDetector
    3) Source run: directory containing this file
    """
    env_dir = os.environ.get("H2_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    if getattr(sys, "frozen", False):
        program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        return (program_data / "H2GasDetector").resolve()

    return Path(__file__).parent.resolve()


DATA_DIR = _resolve_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
_LOG_DIR  = DATA_DIR
_LOG_FILE = _LOG_DIR / "h2_dashboard_errors.log"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)s  —  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),          # also echo to console / terminal
    ],
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database file path
# ---------------------------------------------------------------------------
DB_PATH = DATA_DIR / "h2_dashboard.db"

# ---------------------------------------------------------------------------
# Module-level connection (created once per process)
# ---------------------------------------------------------------------------
_connection: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    """
    Return the shared SQLite connection, creating it on first call.

    Thread-safety: SQLite in check_same_thread=False mode is used because
    the polling thread writes readings while the main thread may read.
    All writes go through explicit transactions so there is no data race.
    """
    global _connection
    if _connection is None:
        try:
            _connection = sqlite3.connect(
                str(DB_PATH),
                check_same_thread=False,   # polling engine runs on a bg thread
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            )
            # Return rows as dict-like objects
            _connection.row_factory = sqlite3.Row
            # Enforce foreign-key constraints
            _connection.execute("PRAGMA foreign_keys = ON")
            _connection.execute("PRAGMA journal_mode = WAL")   # better concurrency
            _connection.commit()
            logger.info("SQLite connection opened: %s", DB_PATH)
        except Exception:
            logger.exception("Failed to open SQLite database at %s", DB_PATH)
            raise
    return _connection


def close_connection() -> None:
    """Close the shared connection gracefully (call on app shutdown)."""
    global _connection
    if _connection is not None:
        try:
            _connection.close()
            logger.info("SQLite connection closed.")
        except Exception:
            logger.exception("Error while closing SQLite connection.")
        finally:
            _connection = None
