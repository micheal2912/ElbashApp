"""User accounts, stored in SQLite with scrypt-hashed passwords."""

import hashlib
import secrets
import sqlite3
import time
from dataclasses import dataclass

from . import settings

# scrypt parameters: ~16 MB and ~100 ms per attempt, which is cheap for a real
# login and expensive for anyone grinding through a password list.
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
_MAXMEM = 64 * 1024 * 1024

# Throttling is per username and in-process. It is a speed bump against
# credential stuffing, not a substitute for a strong password.
_MAX_ATTEMPTS = 8
_LOCKOUT_SECONDS = 300
_failures: dict[str, list[float]] = {}


class AuthError(Exception):
    """Raised for anything the user is allowed to see on the login page."""


@dataclass
class User:
    username: str
    is_admin: bool


def connect() -> sqlite3.Connection:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(settings.DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with connect() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS users (
                   username  TEXT PRIMARY KEY,
                   salt      BLOB NOT NULL,
                   digest    BLOB NOT NULL,
                   is_admin  INTEGER NOT NULL DEFAULT 0,
                   created   REAL NOT NULL
               )"""
        )


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, maxmem=_MAXMEM, **_SCRYPT)


def normalise(username: str) -> str:
    return (username or "").strip().lower()


def create_user(username: str, password: str, is_admin: bool = False) -> User:
    username = normalise(username)
    if not username:
        raise AuthError("שם המשתמש ריק.")
    if len(password) < 8:
        raise AuthError("הסיסמה חייבת להכיל לפחות 8 תווים.")

    salt = secrets.token_bytes(16)
    init_db()
    try:
        with connect() as con:
            con.execute(
                "INSERT INTO users (username, salt, digest, is_admin, created)"
                " VALUES (?, ?, ?, ?, ?)",
                (username, salt, _hash(password, salt), int(is_admin), time.time()),
            )
    except sqlite3.IntegrityError:
        raise AuthError(f"המשתמש {username} כבר קיים.") from None
    return User(username, is_admin)


def set_password(username: str, password: str) -> None:
    username = normalise(username)
    if len(password) < 8:
        raise AuthError("הסיסמה חייבת להכיל לפחות 8 תווים.")
    salt = secrets.token_bytes(16)
    with connect() as con:
        changed = con.execute(
            "UPDATE users SET salt = ?, digest = ? WHERE username = ?",
            (salt, _hash(password, salt), username),
        ).rowcount
    if not changed:
        raise AuthError(f"המשתמש {username} לא נמצא.")


def delete_user(username: str) -> None:
    with connect() as con:
        changed = con.execute(
            "DELETE FROM users WHERE username = ?", (normalise(username),)
        ).rowcount
    if not changed:
        raise AuthError(f"המשתמש {username} לא נמצא.")


def list_users() -> list[User]:
    init_db()
    with connect() as con:
        rows = con.execute(
            "SELECT username, is_admin FROM users ORDER BY username"
        ).fetchall()
    return [User(r["username"], bool(r["is_admin"])) for r in rows]


def count_users() -> int:
    init_db()
    with connect() as con:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def _locked_for(username: str) -> int:
    """Seconds remaining in the lockout window, 0 when the user may try."""
    recent = [t for t in _failures.get(username, []) if time.time() - t < _LOCKOUT_SECONDS]
    _failures[username] = recent
    if len(recent) < _MAX_ATTEMPTS:
        return 0
    return int(_LOCKOUT_SECONDS - (time.time() - recent[0])) + 1


def authenticate(username: str, password: str) -> User:
    username = normalise(username)

    locked = _locked_for(username)
    if locked:
        raise AuthError(f"יותר מדי ניסיונות. נסו שוב בעוד {locked // 60 + 1} דקות.")

    with connect() as con:
        row = con.execute(
            "SELECT username, salt, digest, is_admin FROM users WHERE username = ?",
            (username,),
        ).fetchone()

    # Hash even when the user does not exist, so response time does not reveal
    # which usernames are real.
    salt = row["salt"] if row else b"\x00" * 16
    expected = row["digest"] if row else b"\x00" * 32
    ok = secrets.compare_digest(_hash(password, salt), expected) and row is not None

    if not ok:
        _failures.setdefault(username, []).append(time.time())
        raise AuthError("שם משתמש או סיסמה שגויים.")

    _failures.pop(username, None)
    return User(row["username"], bool(row["is_admin"]))
