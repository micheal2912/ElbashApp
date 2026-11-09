"""Upload storage and the background comparison queue."""

import os
import re
import secrets
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from elbash import compare as compare_mod
from elbash import export, pdf_report, photos

from . import settings

CONFIDENCE_HE = {
    photos.CONFIDENCE_ANCHOR: "ודאי",
    photos.CONFIDENCE_REPORT: "הושלם מהדוח",
    photos.CONFIDENCE_NONE: "לא נקרא",
}

# Keep Hebrew, Latin, digits and the few punctuation marks a camera uses; turn
# everything else into an underscore. Any directory part is dropped separately.
_UNSAFE = re.compile(r"[^\w.\-() \u0590-\u05FF]+", re.UNICODE)


def safe_name(raw: str) -> str:
    """A filename that cannot escape the job directory."""
    name = Path((raw or "").replace("\\", "/")).name
    name = _UNSAFE.sub("_", name).strip(". ")
    return name[:120] or "file"


@dataclass
class Job:
    id: str
    owner: str
    folder: Path
    created: float = field(default_factory=time.time)
    # Last write. Separate from `created` because a month of photos takes
    # minutes to arrive, and only this distinguishes an upload still in
    # progress from one the client walked away from.
    touched: float = field(default_factory=time.time)

    # new -> queued -> running -> done | error | stopped
    status: str = "new"
    phase: str = ""
    message: str = "ממתין לקבצים…"
    detail: str = ""
    done: int = 0
    total: int = 0

    bytes_received: int = 0
    file_count: int = 0

    result: compare_mod.ComparisonResult | None = None
    error: str = ""

    stop_flag: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def progress(self, **fields) -> None:
        with self.lock:
            for key, value in fields.items():
                setattr(self, key, value)
            # A job that just finished has its result on somebody's screen, so
            # the grace period should run from here rather than from the last
            # photo that was uploaded hours earlier.
            self.touched = time.time()

    def state(self) -> dict:
        with self.lock:
            payload = {
                "id": self.id,
                "status": self.status,
                "phase": self.phase,
                "message": self.message,
                "detail": self.detail,
                "done": self.done,
                "total": self.total,
                "files": self.file_count,
                "error": self.error,
            }
        if self.result is not None:
            payload["result"] = result_payload(self.result)
        return payload


class OutOfSpace(RuntimeError):
    """The volume cannot take another upload even after old jobs were cleared."""


FREE_CHECK_EVERY = 25
# How long after its last write a job is still presumed in use. Long enough to
# cover the gap between two batches of a slow upload, short enough that an
# abandoned one is reclaimed on the next attempt rather than at the TTL.
ACTIVE_GRACE_SECONDS = 180


_jobs: dict[str, Job] = {}
_registry_lock = threading.Lock()
# Width 1 by default: the comparison already uses every core, so a second
# concurrent job would just halve the speed of both.
_pool = ThreadPoolExecutor(
    max_workers=max(1, settings.MAX_CONCURRENT_JOBS), thread_name_prefix="elbash-job"
)


def count() -> int:
    with _registry_lock:
        return len(_jobs)


def free_mb() -> int:
    """Megabytes still available on the volume holding the job folders."""
    try:
        return shutil.disk_usage(settings.DATA_DIR).free // (1024 * 1024)
    except OSError:
        return 0


def _discard(job: Job) -> None:
    with _registry_lock:
        _jobs.pop(job.id, None)
    shutil.rmtree(job.folder, ignore_errors=True)


def _reclaimable(owner: str | None = None) -> list[Job]:
    """Jobs safe to delete, oldest first.

    A running job is the obvious exclusion. So is one written to moments ago:
    that is an upload still arriving rather than an abandoned one, and deleting
    it would strand a client midway through sending a month of photos.
    """
    now = time.time()
    with _registry_lock:
        idle = [
            job for job in _jobs.values()
            if job.status != "running"
            and now - job.touched > ACTIVE_GRACE_SECONDS
            and (owner is None or job.owner == owner)
        ]
    return sorted(idle, key=lambda job: job.created)


def make_room(owner: str) -> None:
    """Free disk before a new comparison, newest results kept longest.

    A month of photos is a few gigabytes and the client runs this several
    times a day, so holding every upload for a full day would fill the volume
    on its own. This user's earlier jobs go first: the app shows one
    comparison at a time with no way back to an older one, so by starting
    another they have abandoned them - including an upload they walked away
    from before pressing start. Other people's are only touched if that was
    not enough, and a running job is never touched at all.
    """
    purge_expired()

    mine = _reclaimable(owner)
    for job in mine[: max(0, len(mine) - settings.KEEP_PREVIOUS_JOBS)]:
        _discard(job)

    for job in _reclaimable():
        if free_mb() >= settings.MIN_FREE_MB:
            break
        _discard(job)
        print(f"storage: dropped job {job.id} to free space, {free_mb()} MB now free")


def create(owner: str) -> Job:
    make_room(owner)
    if free_mb() < settings.MIN_FREE_MB:
        raise OutOfSpace(
            f"אין מספיק מקום פנוי בשרת ({free_mb()} מ״ב). "
            "יש לפנות מקום ולנסות שוב."
        )
    job_id = secrets.token_urlsafe(12)
    folder = settings.JOBS_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)
    job = Job(id=job_id, owner=owner, folder=folder)
    with _registry_lock:
        _jobs[job_id] = job
    return job


def get(job_id: str, owner: str) -> Job | None:
    """Scoped to the owner, so a guessed id still returns nothing."""
    with _registry_lock:
        job = _jobs.get(job_id)
    if job is None or job.owner != owner:
        return None
    return job


def purge_expired() -> None:
    cutoff = time.time() - settings.JOB_TTL_HOURS * 3600
    with _registry_lock:
        stale = [j for j in _jobs.values() if j.created < cutoff and j.status != "running"]
        for job in stale:
            _jobs.pop(job.id, None)
    for job in stale:
        shutil.rmtree(job.folder, ignore_errors=True)

    # Directories left behind by a previous process have no Job object.
    if settings.JOBS_DIR.exists():
        known = set(_jobs)
        for path in settings.JOBS_DIR.iterdir():
            if path.is_dir() and path.name not in known:
                try:
                    if path.stat().st_mtime < cutoff:
                        shutil.rmtree(path, ignore_errors=True)
                except OSError:
                    pass


def store_upload(job: Job, filename: str, source: BinaryIO) -> str:
    """Stream one uploaded file into the job folder. Returns the stored name.

    Blocking on purpose: the caller hands this to a worker thread so the copy
    never runs on the event loop, where it would stall the other uploads in
    flight and the progress polling behind them.
    """
    name = safe_name(filename)
    if Path(name).suffix.lower() not in settings.ALLOWED_SUFFIXES:
        raise ValueError(f"סוג קובץ לא נתמך: {name}")

    source.seek(0, os.SEEK_END)
    size = source.tell()
    source.seek(0)
    if size > settings.MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"הקובץ {name} גדול מ-{settings.MAX_FILE_MB} מגה-בייט.")

    with job.lock:
        if job.status != "new":
            raise ValueError("העבודה כבר התחילה.")
        if job.file_count >= settings.MAX_FILES:
            raise ValueError(f"יותר מ-{settings.MAX_FILES} קבצים.")
        if job.bytes_received + size > settings.MAX_JOB_MB * 1024 * 1024:
            raise ValueError(f"סך ההעלאה חורג מ-{settings.MAX_JOB_MB} מגה-בייט.")
        job.bytes_received += size
        job.file_count += 1
        job.touched = time.time()
        reserve = job.file_count % FREE_CHECK_EVERY == 0

    # Checked periodically rather than per file: a thousand-photo upload would
    # otherwise stat the volume a thousand times, and a few photos either side
    # of the line make no difference next to the margin being defended.
    if reserve and free_mb() < settings.MIN_FREE_MB:
        raise OutOfSpace(
            f"נגמר המקום הפנוי בשרת אחרי {job.file_count} קבצים. "
            "ההעלאה נעצרה כדי לא לשמור תמונות חלקיות."
        )

    # Exclusive create rather than exists()-then-write: uploads now land in
    # parallel threads, so two photos sharing a name could otherwise collide.
    target = job.folder / name
    while True:
        try:
            handle = target.open("xb")
            break
        except FileExistsError:
            target = job.folder / f"{Path(name).stem}_{secrets.token_hex(3)}{Path(name).suffix}"

    with handle:
        shutil.copyfileobj(source, handle, 1024 * 1024)
    return target.name


def start(job: Job) -> None:
    pdfs = list(job.folder.glob("*.pdf"))
    images = photos.list_photos(job.folder)
    if not pdfs:
        raise ValueError("לא נמצא קובץ PDF של הדוח.")
    if not images:
        raise ValueError("לא נמצאו תמונות.")

    job.progress(status="queued", message="ממתין בתור…", phase="queued", done=0, total=0)
    _pool.submit(_run, job)


def stop(job: Job) -> None:
    job.stop_flag.set()
    job.progress(message="עוצר…")


def _run(job: Job) -> None:
    started = time.time()
    try:
        job.progress(status="running", phase="pdf", message="קורא את הדוח…")
        pdf = pdf_report.find_report_pdf(job.folder)

        def pdf_progress(page: int, total: int) -> None:
            job.progress(
                done=page, total=total, message=f"קורא עמוד {page} מתוך {total} בדוח"
            )

        # Every job gets a fresh directory, so the on-disk OCR cache can never
        # hit; skipping it avoids writing a file nothing will ever read.
        report = pdf_report.read_report(pdf, progress=pdf_progress, use_cache=False)
        pdf_done = time.time()
        if job.stop_flag.is_set():
            job.progress(status="stopped", message="הופסק.")
            return

        job.progress(
            phase="photos",
            message=f"{len(report.rows)} שורות בדוח. מתחיל בקריאת התמונות…",
            done=0,
            total=0,
        )

        def photo_progress(done: int, total: int, name: str) -> None:
            job.progress(
                done=done, total=total,
                message=f"קורא תמונה {done} מתוך {total}", detail=name,
            )

        results = photos.read_folder(
            job.folder,
            progress=photo_progress,
            use_cache=False,
            should_stop=job.stop_flag.is_set,
            # The report is already read, so the OCR can check a number against
            # it and keep trying when it does not fit.
            known=report.numbers,
        )
        if job.stop_flag.is_set():
            job.progress(status="stopped", message="הופסק.")
            return

        job.result = compare_mod.compare(report, results)
        job.progress(status="done", phase="done", message="הסתיים.", detail="")
        # Printed so a slow run can be split into its two halves from the logs
        # without reproducing it.
        photo_seconds = time.time() - pdf_done
        print(
            f"job {job.id}: pdf {pdf_done - started:.1f}s "
            f"({len(report.rows)} rows), photos {photo_seconds:.1f}s "
            f"({len(results)} images, {photo_seconds / max(1, len(results)):.1f}s each, "
            f"{photos.OCR_WORKERS} workers)"
        )

    except Exception as exc:
        job.error = str(exc)
        job.progress(
            status="error", phase="error", message="התרחשה שגיאה.", detail=str(exc)
        )
        traceback.print_exc()


def result_payload(result: compare_mod.ComparisonResult) -> dict:
    """The shape the browser renders. Hebrew stays in logical order - the
    browser's own bidirectional algorithm lays it out."""
    return {
        "summary": {
            "rows": result.total_rows,
            "photos": result.total_photos,
            "matched": len(result.matched),
            "missing": len(result.missing),
            "problems": len(result.problems),
            "duplicates": len(result.duplicates),
            "coverage": round(result.coverage, 1),
            "complete": result.is_complete,
        },
        "report": {
            "title": result.report.title,
            "name": Path(result.report.path).name,
            "range": result.report.date_range,
            "warnings": result.report.warnings,
        },
        "missing": [
            {
                "number": row.number, "date": row.date, "kind": row.kind_label,
                "route": row.route, "page": row.page,
            }
            for row in result.missing
        ],
        "matched": [
            {
                "number": item.number, "date": item.date, "kind": item.kind_label,
                "route": item.route, "photos": item.photos,
                "confidence": CONFIDENCE_HE.get(item.confidence, item.confidence),
            }
            for item in result.matched
        ],
        "problems": [
            {
                "file": item.file, "number": item.number or "",
                "reason": item.reason, "detail": item.detail,
            }
            for item in result.problems
        ],
        "by_date": [
            {"date": date, "have": total - miss, "missing": miss, "total": total}
            for date, miss, total in result.missing_by_date()
        ],
        "overview": [
            {
                "status": row.status, "number": row.number, "date": row.date,
                "kind": row.kind_label, "route": row.route, "page": row.page,
                "photos": row.photos, "detail": row.detail,
            }
            for row in result.overview()
        ],
    }


def export_file(job: Job) -> Path:
    """Build the Excel export inside the job folder and return its path."""
    if job.result is None:
        raise ValueError("אין עדיין תוצאה לייצוא.")
    path = job.folder / "השוואה.xlsx"
    export.to_excel(job.result, path)
    return path
