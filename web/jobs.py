"""Upload storage and the background comparison queue."""

import re
import secrets
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

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


_jobs: dict[str, Job] = {}
_registry_lock = threading.Lock()
# Width 1 by default: the comparison already uses every core, so a second
# concurrent job would just halve the speed of both.
_pool = ThreadPoolExecutor(
    max_workers=max(1, settings.MAX_CONCURRENT_JOBS), thread_name_prefix="elbash-job"
)


def create(owner: str) -> Job:
    purge_expired()
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


def store_upload(job: Job, filename: str, data: bytes) -> str:
    """Write one uploaded file into the job folder. Returns the stored name."""
    name = safe_name(filename)
    suffix = Path(name).suffix.lower()
    if suffix not in settings.ALLOWED_SUFFIXES:
        raise ValueError(f"סוג קובץ לא נתמך: {name}")
    if len(data) > settings.MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"הקובץ {name} גדול מ-{settings.MAX_FILE_MB} מגה-בייט.")

    with job.lock:
        if job.status != "new":
            raise ValueError("העבודה כבר התחילה.")
        if job.file_count >= settings.MAX_FILES:
            raise ValueError(f"יותר מ-{settings.MAX_FILES} קבצים.")
        if job.bytes_received + len(data) > settings.MAX_JOB_MB * 1024 * 1024:
            raise ValueError(f"סך ההעלאה חורג מ-{settings.MAX_JOB_MB} מגה-בייט.")
        job.bytes_received += len(data)
        job.file_count += 1

    target = job.folder / name
    if target.exists():
        stem, ext = Path(name).stem, Path(name).suffix
        target = job.folder / f"{stem}_{secrets.token_hex(3)}{ext}"
    target.write_bytes(data)
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
        )
        if job.stop_flag.is_set():
            job.progress(status="stopped", message="הופסק.")
            return

        job.result = compare_mod.compare(report, results)
        job.progress(status="done", phase="done", message="הסתיים.", detail="")

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
    }


def export_file(job: Job, kind: str) -> Path:
    """Build the Excel/CSV export inside the job folder and return its path."""
    if job.result is None:
        raise ValueError("אין עדיין תוצאה לייצוא.")
    stem = "השוואה" if kind == "excel" else "חסרות"
    path = job.folder / f"{stem}.{'xlsx' if kind == 'excel' else 'csv'}"
    if kind == "excel":
        export.to_excel(job.result, path)
    else:
        export.to_csv(job.result, path)
    return path
