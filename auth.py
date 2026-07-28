"""
auth.py – User authentication and management for H2 Dashboard
Uses SQLite + PBKDF2-HMAC-SHA256 password hashing.

Default seed account (created on first run):
    username : admin
    password : Admin@123
    role     : admin
"""

import sqlite3
import hashlib
import os
import sys
import secrets
import datetime
import logging
from dataclasses import dataclass
from typing import Optional, List

logger = logging.getLogger(__name__)

def _resolve_auth_data_dir() -> str:
    """
    Return a stable writable directory for auth DB.

    Priority:
    1) H2_DATA_DIR environment variable
    2) Frozen EXE: %PROGRAMDATA%\H2GasDetector
    3) Source run: directory containing this file
    """
    env_dir = os.environ.get("H2_DATA_DIR", "").strip()
    if env_dir:
        return os.path.abspath(os.path.expanduser(env_dir))

    if getattr(sys, "frozen", False):
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return os.path.abspath(os.path.join(program_data, "H2GasDetector"))

    return os.path.dirname(os.path.abspath(__file__))


_DATA_DIR = _resolve_auth_data_dir()
os.makedirs(_DATA_DIR, exist_ok=True)
_DB_PATH = os.path.join(_DATA_DIR, "auth.db")

# All available roles (add more here as the project grows)
ROLES: List[str] = ["admin", "operator"]


# ──────────────────────────────────────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class User:
    user_id:    int
    username:   str
    full_name:  str
    role:       str
    active:     bool
    created_at: str

    @property
    def display_name(self) -> str:
        return self.full_name if self.full_name.strip() else self.username

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """Return (hex_hash, hex_salt) using PBKDF2-HMAC-SHA256."""
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        260_000,
    )
    return dk.hex(), salt


def _verify_password(password: str, stored_hash: str, salt: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    computed, _ = _hash_password(password, salt)
    return secrets.compare_digest(computed, stored_hash)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        user_id   = row["user_id"],
        username  = row["username"],
        full_name = row["full_name"],
        role      = row["role"],
        active    = bool(row["active"]),
        created_at= row["created_at"],
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def init_auth_db() -> None:
    """Create the users table and seed the default admin account."""
    logger.info("Auth DB path: %s", _DB_PATH)
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                username   TEXT    NOT NULL UNIQUE COLLATE NOCASE,
                full_name  TEXT    NOT NULL DEFAULT '',
                pw_hash    TEXT    NOT NULL,
                pw_salt    TEXT    NOT NULL,
                role       TEXT    NOT NULL DEFAULT 'operator',
                active     INTEGER NOT NULL DEFAULT 1,
                created_at TEXT    NOT NULL
            )
        """)
        # Seed default admin if table is empty
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            pw_hash, pw_salt = _hash_password("Admin@123")
            conn.execute(
                """INSERT INTO users
                       (username, full_name, pw_hash, pw_salt, role, active, created_at)
                   VALUES (?, ?, ?, ?, 'admin', 1, ?)""",
                ("admin", "Administrator", pw_hash, pw_salt,
                 datetime.datetime.now().isoformat(timespec="seconds")),
            )
            logger.info("Auth DB seeded with default admin account.")


def authenticate(username: str, password: str) -> Optional[User]:
    """
    Return a User on successful login, or None if credentials are invalid
    or the account is inactive.
    """
    if not username or not password:
        return None
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ? AND active = 1",
                (username.strip(),),
            ).fetchone()
        if row is None:
            return None
        if not _verify_password(password, row["pw_hash"], row["pw_salt"]):
            return None
        return _row_to_user(row)
    except Exception:
        logger.exception("authenticate() failed")
        return None


def list_users() -> List[User]:
    """Return all users ordered by user_id."""
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY user_id"
            ).fetchall()
        return [_row_to_user(r) for r in rows]
    except Exception:
        logger.exception("list_users() failed")
        return []


def create_user(username: str, password: str,
                full_name: str, role: str) -> tuple[bool, str]:
    """
    Create a new user.  Returns (True, '') on success,
    or (False, reason_string) on failure.
    """
    username = username.strip()
    full_name = full_name.strip()
    if not username:
        return False, "Username cannot be empty."
    if len(password) < 6:
        return False, "Password must be at least 6 characters."
    if role not in ROLES:
        return False, f"Invalid role '{role}'."
    try:
        pw_hash, pw_salt = _hash_password(password)
        with _get_conn() as conn:
            conn.execute(
                """INSERT INTO users
                       (username, full_name, pw_hash, pw_salt, role, active, created_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (username, full_name, pw_hash, pw_salt, role,
                 datetime.datetime.now().isoformat(timespec="seconds")),
            )
        return True, ""
    except sqlite3.IntegrityError:
        return False, f"Username '{username}' already exists."
    except Exception as exc:
        logger.exception("create_user() failed")
        return False, str(exc)


def update_user(user_id: int, full_name: str,
                role: str, active: bool) -> tuple[bool, str]:
    """Update a user's profile. Returns (True, '') or (False, reason)."""
    if role not in ROLES:
        return False, f"Invalid role '{role}'."
    try:
        with _get_conn() as conn:
            # Prevent demoting / deactivating the last admin
            if role != "admin" or not active:
                admin_count = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1"
                ).fetchone()[0]
                target = conn.execute(
                    "SELECT role, active FROM users WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                if (target and target["role"] == "admin"
                        and bool(target["active"]) and admin_count <= 1):
                    return False, "Cannot demote or deactivate the last admin account."
            conn.execute(
                "UPDATE users SET full_name=?, role=?, active=? WHERE user_id=?",
                (full_name.strip(), role, int(active), user_id),
            )
        return True, ""
    except Exception as exc:
        logger.exception("update_user() failed")
        return False, str(exc)


def delete_user(user_id: int) -> tuple[bool, str]:
    """
    Delete a user.  Cannot delete the last admin or the currently
    logged-in user (caller is responsible for checking the latter).
    """
    try:
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT role, active FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            if row is None:
                return False, "User not found."
            if row["role"] == "admin":
                admin_count = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role='admin' AND active=1"
                ).fetchone()[0]
                if admin_count <= 1:
                    return False, "Cannot delete the last admin account."
            conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
        return True, ""
    except Exception as exc:
        logger.exception("delete_user() failed")
        return False, str(exc)


def change_password(user_id: int, new_password: str) -> tuple[bool, str]:
    """Change a user's password. Returns (True, '') or (False, reason)."""
    if len(new_password) < 6:
        return False, "Password must be at least 6 characters."
    try:
        pw_hash, pw_salt = _hash_password(new_password)
        with _get_conn() as conn:
            conn.execute(
                "UPDATE users SET pw_hash=?, pw_salt=? WHERE user_id=?",
                (pw_hash, pw_salt, user_id),
            )
        return True, ""
    except Exception as exc:
        logger.exception("change_password() failed")
        return False, str(exc)


def reset_user_password_for_recovery(username: str) -> tuple[bool, str, str, str]:
    """
    Reset the specified active user's password to a temporary value.

    Returns (ok, message, temp_password, role).
    temp_password and role are non-empty only on success.
    """
    user_txt = str(username or "").strip()
    if not user_txt:
        return False, "Username is required.", "", ""

    try:
        with _get_conn() as conn:
            row = conn.execute(
                """
                SELECT user_id, role
                  FROM users
                 WHERE LOWER(username) = LOWER(?)
                   AND active = 1
                 LIMIT 1
                """,
                (user_txt,),
            ).fetchone()
            if row is None:
                return False, "User not found or inactive.", "", ""

            # Keep format easy to type while still random enough for one-time recovery.
            temp_password = f"Temp@{secrets.token_hex(4)}"
            pw_hash, pw_salt = _hash_password(temp_password)
            conn.execute(
                "UPDATE users SET pw_hash=?, pw_salt=? WHERE user_id=?",
                (pw_hash, pw_salt, int(row["user_id"])),
            )

            role = str(row["role"] or "").strip().lower()
        return True, "Password reset successfully.", temp_password, role
    except Exception as exc:
        logger.exception("reset_user_password_for_recovery() failed")
        return False, str(exc), "", ""
