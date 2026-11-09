"""Locate the Tesseract binary and a tessdata directory that has Hebrew."""

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path

def _resource_root() -> Path:
    """Where bundled data lives: the PyInstaller temp dir, else the repo."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _resource_root()
BUNDLED_TESSDATA = PROJECT_ROOT / "tessdata"
# A frozen build ships its own copy of the engine so nothing has to be installed.
BUNDLED_BINARY = PROJECT_ROOT / "tesseract" / (
    "tesseract.exe" if sys.platform.startswith("win") else "tesseract"
)

_BINARY_CANDIDATES = [
    # Windows
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    # macOS (Homebrew on Apple silicon, then Intel), and Linux
    "/opt/homebrew/bin/tesseract",
    "/usr/local/bin/tesseract",
    "/opt/local/bin/tesseract",
    "/usr/bin/tesseract",
]

if sys.platform.startswith("win"):
    _INSTALL_HINT = "    winget install --id UB-Mannheim.TesseractOCR -e"
elif sys.platform == "darwin":
    _INSTALL_HINT = "    brew install tesseract"
else:
    _INSTALL_HINT = "    sudo apt install tesseract-ocr"


class TesseractMissing(RuntimeError):
    pass


def find_binary() -> str:
    override = os.environ.get("TESSERACT_CMD")
    if override and Path(override).exists():
        return override

    if BUNDLED_BINARY.exists():
        return str(BUNDLED_BINARY)

    found = shutil.which("tesseract")
    if found:
        return found

    for candidate in _BINARY_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate

    raise TesseractMissing(
        "מנוע הקריאה Tesseract לא נמצא.\n\n"
        "להתקנה:\n"
        f"{_INSTALL_HINT}\n\n"
        "או הגדירו את משתנה הסביבה TESSERACT_CMD לנתיב של Tesseract."
    )


def find_tessdata() -> str:
    """Prefer the Hebrew-capable tessdata shipped next to this project."""
    if (BUNDLED_TESSDATA / "heb.traineddata").exists():
        return str(BUNDLED_TESSDATA)

    env = os.environ.get("TESSDATA_PREFIX")
    if env and (Path(env) / "heb.traineddata").exists():
        return env

    binary = Path(find_binary())
    sibling = binary.parent / "tessdata"
    if (sibling / "heb.traineddata").exists():
        return str(sibling)

    raise TesseractMissing(
        "קובץ מודל העברית heb.traineddata לא נמצא.\n\n"
        f"יש להוריד אותו אל: {BUNDLED_TESSDATA}\n"
        "מתוך https://github.com/tesseract-ocr/tessdata_best"
    )


@lru_cache(maxsize=1)
def configure() -> tuple[str, str]:
    """Point pytesseract at the binary and Hebrew models. Returns both paths.

    Cached so callers that read a single photo can call it without paying for
    the filesystem search every time, which lets read_photo stand on its own
    instead of silently failing when nobody configured the engine first.
    """
    import pytesseract

    binary = find_binary()
    tessdata = find_tessdata()
    pytesseract.pytesseract.tesseract_cmd = binary
    os.environ["TESSDATA_PREFIX"] = tessdata
    return binary, tessdata
