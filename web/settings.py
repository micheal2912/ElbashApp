"""Deployment settings. Every value can be overridden by an environment variable."""

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


APP_TITLE = "אלבאש בדיקת משלוחים"

# Everything mutable lives here, so a container only needs this one volume.
DATA_DIR = Path(os.environ.get("ELBASH_DATA_DIR") or ROOT / "data")
JOBS_DIR = DATA_DIR / "jobs"
DB_PATH = DATA_DIR / "elbash.db"

# Sessions must survive a restart, so a generated key is written to disk rather
# than regenerated per process - otherwise every deploy logs everybody out, and
# multiple workers would each sign cookies with a different key.
_KEY_FILE = DATA_DIR / "secret_key"


def secret_key() -> str:
    configured = os.environ.get("ELBASH_SECRET_KEY")
    if configured:
        return configured
    if _KEY_FILE.exists():
        return _KEY_FILE.read_text(encoding="utf-8").strip()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    _KEY_FILE.write_text(key, encoding="utf-8")
    _KEY_FILE.chmod(0o600)
    return key


SESSION_HOURS = _int("ELBASH_SESSION_HOURS", 12)
# Must be off for plain-HTTP testing, on in production or the browser will not
# send the cookie back at all.
SECURE_COOKIES = _flag("ELBASH_SECURE_COOKIES", True)

MAX_FILES = _int("ELBASH_MAX_FILES", 5000)
MAX_FILE_MB = _int("ELBASH_MAX_FILE_MB", 40)
MAX_JOB_MB = _int("ELBASH_MAX_JOB_MB", 4096)

# OCR saturates every core it is given, so running two jobs at once makes both
# slower rather than either faster. Extra jobs wait in line.
MAX_CONCURRENT_JOBS = _int("ELBASH_MAX_CONCURRENT_JOBS", 1)
JOB_TTL_HOURS = _int("ELBASH_JOB_TTL_HOURS", 24)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
ALLOWED_SUFFIXES = IMAGE_SUFFIXES | {".pdf"}
