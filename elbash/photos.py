"""OCR delivery screenshots and pull out the מספר משלוח value."""

import json
import os
import re
import time
from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pytesseract
from PIL import Image, ImageOps

from . import tesseract_setup
from .cpu import available_cores

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
CACHE_NAME = ".elbash_ocr_cache.json"

# Each OCR pass runs the tesseract binary in a subprocess, which releases the
# GIL, so threads already get full parallelism here - swapping in processes or
# handing each worker a batch of images both measured slower (0.99x and 0.90x
# on 44 photos). Scaling with the core count is what helps; gains flatten out
# past 16 because the tesseract processes start contending for cores.
OCR_WORKERS = min(16, max(2, available_cores()))

# Tesseract also splits a single image across OpenMP threads, which is wasted
# effort when every core is already busy with another photo: its threads just
# multiply against ours. Capping it to one cut the photo stage by 1.4x on a
# six-core container and 1.8x on a two-core one. Where the pool leaves cores
# spare - a 22-core machine runs only 16 workers - letting tesseract use them
# is the better trade, and measured 10% faster, so the cap is conditional.
if OCR_WORKERS >= available_cores():
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")

# Tesseract reads "משלוח" as "ישלוח"/"משלות" on dim photos, so anchor on the
# stable "שלוח" core instead of the full word.
ANCHOR = "שלוח"
CERTIFICATE_WORD = "תעודה"

# Below roughly 4 megapixels a photo is already cheap to read, and halving it
# starts to cost accuracy rather than buy time.
HALF_SCALE_ABOVE = 4_000_000

# Tilt beyond about five degrees starts costing tesseract whole lines, and a
# photo of a hand-held screen is rarely off by more than fifteen.
SKEW_SEARCH = 15
SKEW_STEP = 1.0
SKEW_SAMPLE = 700
# Below this the tilt is within what tesseract handles unaided, and rotating
# would only resample the text for nothing.
MIN_SKEW = 1.5
SEVEN_DIGITS = re.compile(r"(?<!\d)(\d{7})(?!\d)")

DELIVERY = "מסירה"
PICKUP = "איסוף"

CONFIDENCE_ANCHOR = "anchor"
CONFIDENCE_REPORT = "report-match"
CONFIDENCE_NONE = "unreadable"


def _log(line: str) -> None:
    """One line per photo on stdout, so a slow run is visible while it runs.

    The progress callback only tells the browser a count; when a batch crawls,
    the question is always which photo and how long, and that has to be
    readable in `docker compose logs -f` as it happens, not after it finishes.
    """
    print(line, flush=True)


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
    seconds: float = 0.0
    # Which rendering finally read the number, so the log says whether a photo
    # needed straightening rather than only that it was slow.
    fixed_by: str = ""


def list_photos(folder: str | Path) -> list[Path]:
    return sorted(
        p for p in Path(folder).iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )


def _load(path: Path) -> Image.Image:
    """Open honouring EXIF orientation - these photos are stored sideways."""
    return ImageOps.exif_transpose(Image.open(path))


def _shrink(image: Image.Image) -> Image.Image:
    """Half scale when the photo is big enough that the extra detail is idle.

    Tesseract's cost tracks pixel count, and a 7-megapixel photo of a phone
    screen carries far more detail than seven digits need. Half scale read the
    same numbers in half the time across the sample folder.
    """
    width, height = image.size
    if width * height > HALF_SCALE_ABOVE:
        return image.resize((width // 2, height // 2), Image.LANCZOS)
    return image


def _passes(image: Image.Image):
    """Cheapest-first renderings; we stop as soon as the anchor is found."""
    width, height = image.size

    # Only an attempt: whatever half scale cannot read falls through to the
    # full-resolution passes below, so a photo taken from further away still
    # gets its chance.
    small = _shrink(image)
    if small is not image:
        yield small, "--psm 3", "half"

    yield image, "--psm 3", "full"

    grey = ImageOps.autocontrast(image.convert("L"))
    yield grey, "--psm 6", "grey psm6"
    yield grey, "--psm 4", "grey psm4"
    yield grey.resize((width * 2, height * 2), Image.LANCZOS), "--psm 6", "2x psm6"


def _skew_angle(image: Image.Image) -> float:
    """Degrees of counter-clockwise rotation needed to level the text lines.

    Rotating until the horizontal projection of the ink is sharpest is the
    classic estimate, and it needs nothing beyond the array maths pandas
    already brings in. The search runs on a thumbnail because the tilt of a
    page does not change with resolution, and probing a full-size photo would
    cost more than simply reading it.
    """
    grey = ImageOps.autocontrast(image.convert("L"))
    grey.thumbnail((SKEW_SAMPLE, SKEW_SAMPLE), Image.BILINEAR)

    # Text rows only stand out once the background is gone, and a photo of a
    # screen can be dark text on light or the reverse, so pick the polarity
    # from the image rather than assuming one.
    levels = np.asarray(grey, dtype=np.uint8)
    ink = levels < 128 if levels.mean() > 127 else levels > 127
    mask = Image.fromarray(ink.astype(np.uint8) * 255)

    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-SKEW_SEARCH, SKEW_SEARCH + SKEW_STEP, SKEW_STEP):
        rotated = mask.rotate(float(angle), resample=Image.BILINEAR, fillcolor=0)
        profile = np.asarray(rotated, dtype=np.float32).sum(axis=1)
        # Level lines make the row totals swing hard between text and gap; the
        # derivative peaks exactly where that contrast is greatest.
        score = float(np.diff(profile).var())
        if score > best_score:
            best_angle, best_score = float(angle), score
    return best_angle


def _straighten(image: Image.Image, angle: float) -> Image.Image:
    # Fill from a corner so the padding reads as screen border rather than as
    # a hard edge the layout analysis might mistake for a column.
    corner = image.getpixel((0, 0))
    return image.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=corner)


def _deskew_passes(image: Image.Image):
    """Straightened renderings, for a photo taken at an angle to the screen.

    Past about five degrees of tilt tesseract starts reading digits wrong
    rather than not at all, which is the expensive kind of wrong: the number
    it invents is absent from the report, so the photo is filed as a delivery
    nobody ordered while the delivery it actually shows reads as missing.
    """
    angle = _skew_angle(image)
    if abs(angle) >= MIN_SKEW:
        straight = _straighten(image, angle)
        yield _shrink(straight), "--psm 3", f"deskew {angle:+.0f}"
        yield ImageOps.autocontrast(straight.convert("L")), "--psm 6", f"deskew {angle:+.0f} grey"


def _rotation_passes(image: Image.Image):
    """Quarter turns, for a photo taken with the phone held sideways.

    Kept apart from the deskew passes because the two symptoms differ: a tilted
    photo reads the wrong number, while a sideways one reads no number at all.
    Trying four orientations costs four more OCR runs, so it is worth spending
    only on the photos that came back with nothing.
    """
    for name, turn in (("rot270", Image.ROTATE_270),
                       ("rot90", Image.ROTATE_90),
                       ("rot180", Image.ROTATE_180)):
        turned = image.transpose(turn)
        yield _shrink(turned), "--psm 3", name

        # A sideways photo is usually a little tilted as well, so straighten
        # each turn before giving up on it.
        angle = _skew_angle(turned)
        if abs(angle) >= MIN_SKEW:
            yield _shrink(_straighten(turned, angle)), "--psm 3", f"{name} deskew {angle:+.0f}"


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


def read_photo(path: str | Path, known: Collection[str] | None = None) -> PhotoResult:
    """OCR one screenshot, escalating preprocessing until the number is found.

    `known` is the set of certificate numbers the report contains. Given it,
    a reading the report does not recognise is treated as unfinished rather
    than as an answer, because that is what a misread looks like: a tilted
    photo yields a confident seven digits with one digit wrong, which would
    otherwise be filed as a photo of a delivery nobody ordered.
    """
    started = time.monotonic()
    result = _read_photo(path, known)
    result.seconds = time.monotonic() - started
    return result


def _read_photo(path: str | Path, known: Collection[str] | None = None) -> PhotoResult:
    tesseract_setup.configure()
    path = Path(path)
    result = PhotoResult(file=path.name)

    try:
        image = _load(path)
    except Exception as exc:
        result.error = f"Could not open image: {exc}"
        return result

    seen: list[str] = []
    unvouched: tuple[str, str, str, str] | None = None

    def ladder():
        yield from _passes(image)
        yield from _deskew_passes(image)
        # No anchored number at all is the signature of a photo held sideways:
        # the lines run vertically, so tesseract never finds "משלוח" next to
        # digits. Measured against rotated copies, keying this off stray
        # candidates instead cost almost all of the recovery - a sideways photo
        # still yields loose seven-digit runs, which looked like progress and
        # suppressed the quarter turns.
        if unvouched is None:
            yield from _rotation_passes(image)

    for index, (variant, config, label) in enumerate(ladder(), start=1):
        try:
            text = pytesseract.image_to_string(variant, lang="heb+eng", config=config)
        except Exception as exc:
            result.error = f"OCR failed: {exc}"
            return result

        number, kind, anchor_line, candidates = _parse_text(text)
        for candidate in candidates:
            if candidate not in seen:
                seen.append(candidate)

        if not number:
            continue

        if known is None or number in known:
            result.number = number
            result.kind = kind
            result.anchor_line = anchor_line
            result.confidence = CONFIDENCE_ANCHOR
            result.passes_used = index
            result.candidates = seen
            result.fixed_by = label
            return result

        # Hold the first unrecognised reading and carry on down the ladder. A
        # straightened pass usually reads it correctly; if none does, this is
        # still the best answer we have, and a photo belonging to a different
        # report should say so rather than look unreadable.
        if unvouched is None:
            unvouched = (number, kind, anchor_line, label)

    if unvouched is not None:
        result.number, result.kind, result.anchor_line, result.fixed_by = unvouched
        result.confidence = CONFIDENCE_ANCHOR

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
    known: Collection[str] | None = None,
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
        clock = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(read_photo, p, known): p for p in pending}
            try:
                for future in as_completed(futures):
                    path = futures[future]
                    result = future.result()
                    results[path.name] = result
                    cache[_cache_key(path)] = asdict(result)
                    done += 1
                    _log(
                        f"photo {done}/{total} {result.seconds:5.1f}s "
                        f"{result.passes_used} pass{'' if result.passes_used == 1 else 'es'} "
                        f"[{result.fixed_by or 'unread'}] "
                        f"{result.number or result.error or 'no number'} {path.name}"
                    )
                    if progress:
                        progress(done, total, path.name)
                    if should_stop and should_stop():
                        break
            finally:
                # Drop anything not started yet so Stop takes effect promptly.
                for future in futures:
                    future.cancel()

        _log_summary([results[p.name] for p in pending if p.name in results],
                     time.monotonic() - clock, workers)

        try:
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    return [results[p.name] for p in files if p.name in results]


def _log_summary(read: list[PhotoResult], elapsed: float, workers: int) -> None:
    """Close the run with the numbers that decide whether to tune anything.

    Which photos were expensive is the whole question: a photo read on the
    first pass is near the floor, while one that falls through to the last
    costs several times that. The split between them is what says whether the
    escalation ladder is worth reworking, and it cannot be guessed from a
    sample - it depends on how the month's photos were taken.
    """
    if not read:
        return

    tally: dict[int, int] = {}
    for result in read:
        tally[result.passes_used] = tally.get(result.passes_used, 0) + 1
    spread = "  ".join(f"{passes} pass: {count}" for passes, count in sorted(tally.items()))

    slowest = sorted(read, key=lambda r: r.seconds, reverse=True)[:3]
    worst = ", ".join(f"{r.file} {r.seconds:.1f}s" for r in slowest)

    _log(
        f"photos done: {len(read)} in {elapsed:.1f}s "
        f"({elapsed / len(read):.2f}s each, {workers} workers)\n"
        f"  passes: {spread}\n"
        f"  slowest: {worst}"
    )
