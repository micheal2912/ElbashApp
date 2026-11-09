"""OCR delivery screenshots and pull out the מספר משלוח value."""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pytesseract
from PIL import Image, ImageOps

from . import tesseract_setup

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
CACHE_NAME = ".elbash_ocr_cache.json"

# Each OCR pass runs the tesseract binary in a subprocess, which releases the
# GIL, so threads already get full parallelism here - swapping in processes or
# handing each worker a batch of images both measured slower (0.99x and 0.90x
# on 44 photos). Scaling with the core count is what helps; gains flatten out
# past 16 because the tesseract processes start contending for cores.
OCR_WORKERS = min(16, max(2, os.cpu_count() or 4))

# Tesseract reads "משלוח" as "ישלוח"/"משלות" on dim photos, so anchor on the
# stable "שלוח" core instead of the full word.
ANCHOR = "שלוח"
CERTIFICATE_WORD = "תעודה"
SEVEN_DIGITS = re.compile(r"(?<!\d)(\d{7})(?!\d)")

DELIVERY = "מסירה"
PICKUP = "איסוף"

CONFIDENCE_ANCHOR = "anchor"
CONFIDENCE_REPORT = "report-match"
CONFIDENCE_NONE = "unreadable"


@dataclass
class PhotoResult:
    file: str
    number: str | None = None
    kind: str = ""
    confidence: str = CONFIDENCE_NONE
    candidates: list[str] = field(default_factory=list)
    anchor_line: str = ""
    passes_used: int = 0
    error: str = ""


def list_photos(folder: str | Path) -> list[Path]:
    return sorted(
        p for p in Path(folder).iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )


def _load(path: Path) -> Image.Image:
    """Open honouring EXIF orientation - these photos are stored sideways."""
    return ImageOps.exif_transpose(Image.open(path))


def _passes(image: Image.Image):
    """Cheapest-first renderings; we stop as soon as the anchor is found."""
    width, height = image.size
    yield image, "--psm 3"

    grey = ImageOps.autocontrast(image.convert("L"))
    yield grey, "--psm 6"
    yield grey, "--psm 4"
    yield grey.resize((width * 2, height * 2), Image.LANCZOS), "--psm 6"


def _parse_text(text: str) -> tuple[str | None, str, str, list[str]]:
    """Return (anchored number, kind, anchor line, every 7-digit candidate)."""
    anchored: str | None = None
    kind = ""
    anchor_line = ""
    candidates: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        numbers = SEVEN_DIGITS.findall(line)
        if not numbers:
            continue

        # The 13-digit "מספר תעודה" field can shed a stray 7-digit run, and on
        # some screens it genuinely holds 7 digits - never treat it as the
        # delivery number.
        if CERTIFICATE_WORD in line:
            continue

        candidates.extend(numbers)

        if anchored is None and ANCHOR in line:
            anchored = numbers[0]
            anchor_line = line
            if PICKUP in line or "אסוף" in line:
                kind = PICKUP
            elif "מסיר" in line or "מסיי" in line:
                kind = DELIVERY

    return anchored, kind, anchor_line, candidates


def read_photo(path: str | Path) -> PhotoResult:
    """OCR one screenshot, escalating preprocessing until the number is found."""
    path = Path(path)
    result = PhotoResult(file=path.name)

    try:
        image = _load(path)
    except Exception as exc:
        result.error = f"Could not open image: {exc}"
        return result

    seen: list[str] = []
    for index, (variant, config) in enumerate(_passes(image), start=1):
        try:
            text = pytesseract.image_to_string(variant, lang="heb+eng", config=config)
        except Exception as exc:
            result.error = f"OCR failed: {exc}"
            return result

        number, kind, anchor_line, candidates = _parse_text(text)
        for candidate in candidates:
            if candidate not in seen:
                seen.append(candidate)

        if number:
            result.number = number
            result.kind = kind
            result.anchor_line = anchor_line
            result.confidence = CONFIDENCE_ANCHOR
            result.passes_used = index
            result.candidates = seen
            return result

    result.passes_used = index
    result.candidates = seen
    return result


def _cache_key(path: Path) -> str:
    stat = path.stat()
    return f"{path.name}|{stat.st_size}|{int(stat.st_mtime)}"


def read_folder(
    folder: str | Path,
    progress=None,
    use_cache: bool = True,
    workers: int | None = None,
    should_stop=None,
) -> list[PhotoResult]:
    """OCR every image in a folder, reusing cached results where possible."""
    tesseract_setup.configure()

    folder = Path(folder)
    files = list_photos(folder)
    cache_path = folder / CACHE_NAME

    cache: dict[str, dict] = {}
    if use_cache and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    results: dict[str, PhotoResult] = {}
    pending: list[Path] = []
    for path in files:
        entry = cache.get(_cache_key(path)) if use_cache else None
        if entry:
            results[path.name] = PhotoResult(**entry)
        else:
            pending.append(path)

    done = len(results)
    total = len(files)
    if progress:
        progress(done, total, "")

    if pending:
        workers = max(1, min(workers or OCR_WORKERS, len(pending)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(read_photo, p): p for p in pending}
            try:
                for future in as_completed(futures):
                    path = futures[future]
                    result = future.result()
                    results[path.name] = result
                    cache[_cache_key(path)] = asdict(result)
                    done += 1
                    if progress:
                        progress(done, total, path.name)
                    if should_stop and should_stop():
                        break
            finally:
                # Drop anything not started yet so Stop takes effect promptly.
                for future in futures:
                    future.cancel()

        try:
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    return [results[p.name] for p in files if p.name in results]
