"""FastAPI application: login, uploads, the comparison queue and downloads."""

import os
import platform
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from elbash import cpu, pdf_report, photos, tesseract_setup

from . import auth, jobs, settings

HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

app = FastAPI(title=settings.APP_TITLE, docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key(),
    max_age=settings.SESSION_HOURS * 3600,
    https_only=settings.SECURE_COOKIES,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def _memory_budget() -> str:
    """How much RAM this process may actually use, as the container sees it.

    Each PDF worker is a separate process holding its own pdfplumber state, so
    an under-provisioned container swaps instead of running, which looks like
    slowness rather than an error.
    """
    for path, unlimited in (
        ("/sys/fs/cgroup/memory.max", "max"),                  # cgroup v2
        ("/sys/fs/cgroup/memory/memory.limit_in_bytes", None),  # cgroup v1
    ):
        try:
            raw = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw == unlimited:
            break
        value = int(raw)
        if value < 1 << 62:  # cgroup v1 uses a huge sentinel for "no limit"
            return f"{value / 1024 / 1024:,.0f} MB"
        break

    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        return f"{total / 1024 / 1024:,.0f} MB (no container limit)"
    except (AttributeError, OSError, ValueError):
        return "unknown"


@app.on_event("startup")
def _startup() -> None:
    auth.init_db()
    settings.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    jobs.purge_expired()
    try:
        binary, tessdata = tesseract_setup.configure()
        print(f"Tesseract: {binary}\nTessdata : {tessdata}")
        # The worker counts come from the cores this process can actually see,
        # which inside Docker is whatever the VM was given - not the host's.
        # Printing it makes an under-provisioned container obvious.
        usable, machine = cpu.available_cores(), os.cpu_count()
        limited = "" if usable == machine else f" ({machine} on the machine, capped)"
        print(
            f"Machine  : {platform.machine()} on {platform.system()}, "
            f"{usable} cores usable{limited}"
        )
        print(f"Memory   : {_memory_budget()}")
        print(f"Workers  : {photos.OCR_WORKERS} for photos, "
              f"{pdf_report.PDF_WORKERS} for the report")
    except tesseract_setup.TesseractMissing as exc:
        # Surfaced per job as well; logging it at boot makes a broken image obvious.
        print(f"WARNING: {exc}")
    if auth.count_users() == 0:
        print("No users yet. Create one:  python -m web.manage add <username>")


# ----------------------------------------------------------------- security
def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        request.session["csrf"] = token
    return token


def current_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="נדרשת התחברות.")
    return user


def verify_csrf(request: Request) -> None:
    sent = request.headers.get("x-csrf-token") or ""
    expected = request.session.get("csrf") or ""
    if not expected or not secrets.compare_digest(sent, expected):
        raise HTTPException(status_code=403, detail="בקשה לא תקינה. רעננו את הדף.")


def api_user(request: Request) -> str:
    """Auth + CSRF for every state-changing API call."""
    user = current_user(request)
    verify_csrf(request)
    return user


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    """Bounce browsers to the login page, but keep JSON for fetch calls."""
    if exc.status_code == 401 and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


# -------------------------------------------------------------------- pages
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {"title": settings.APP_TITLE, "csrf": csrf_token(request)},
    )


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    csrf: str = Form(""),
):
    expected = request.session.get("csrf") or ""
    if not expected or not secrets.compare_digest(csrf, expected):
        error = "פג תוקף הטופס. נסו שוב."
    else:
        try:
            user = auth.authenticate(username, password)
        except auth.AuthError as exc:
            error = str(exc)
        else:
            # New session id on login, so a pre-set cookie cannot be reused.
            request.session.clear()
            request.session["user"] = user.username
            request.session["admin"] = user.is_admin
            csrf_token(request)
            return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse(
        request, "login.html",
        {
            "title": settings.APP_TITLE, "csrf": csrf_token(request),
            "error": error, "username": username,
        },
        status_code=400,
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, user: str = Depends(current_user)):
    return templates.TemplateResponse(
        request, "index.html",
        {
            "title": settings.APP_TITLE,
            "user": user,
            "csrf": csrf_token(request),
            "limits": {
                "max_files": settings.MAX_FILES,
                "max_file_mb": settings.MAX_FILE_MB,
                "max_job_mb": settings.MAX_JOB_MB,
            },
        },
    )


# ---------------------------------------------------------------------- api
@app.post("/api/jobs")
def create_job(user: str = Depends(api_user)):
    return {"id": jobs.create(user).id}


def _job_or_404(job_id: str, user: str) -> jobs.Job:
    job = jobs.get(job_id, user)
    if job is None:
        raise HTTPException(status_code=404, detail="העבודה לא נמצאה.")
    return job


@app.post("/api/jobs/{job_id}/files")
async def upload_files(
    job_id: str,
    files: list[UploadFile] = File(...),
    user: str = Depends(api_user),
):
    job = _job_or_404(job_id, user)
    stored, rejected = [], []
    for upload in files:
        try:
            # Copied in a worker thread: a few megabytes written straight from
            # the event loop would block every other request on this process.
            stored.append(
                await run_in_threadpool(
                    jobs.store_upload, job, upload.filename or "", upload.file
                )
            )
        except ValueError as exc:
            rejected.append({"name": upload.filename, "reason": str(exc)})
        finally:
            await upload.close()
    return {"stored": stored, "rejected": rejected, "total": job.file_count}


@app.post("/api/jobs/{job_id}/start")
def start_job(job_id: str, user: str = Depends(api_user)):
    job = _job_or_404(job_id, user)
    try:
        jobs.start(job)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return job.state()


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str, user: str = Depends(api_user)):
    job = _job_or_404(job_id, user)
    jobs.stop(job)
    return job.state()


@app.get("/api/jobs/{job_id}")
def job_state(job_id: str, user: str = Depends(current_user)):
    return _job_or_404(job_id, user).state()


@app.get("/api/jobs/{job_id}/export/{kind}")
def download_export(job_id: str, kind: str, user: str = Depends(current_user)):
    if kind not in {"excel", "csv"}:
        raise HTTPException(status_code=404, detail="סוג ייצוא לא מוכר.")
    job = _job_or_404(job_id, user)
    try:
        path = jobs.export_file(job, kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    # A Hebrew filename has to be percent-encoded for the Content-Disposition header.
    return FileResponse(
        path,
        filename=path.name,
        headers={
            "Content-Disposition":
                f"attachment; filename*=UTF-8''{quote(path.name)}"
        },
    )


@app.get("/api/jobs/{job_id}/photo/{name}")
def photo(job_id: str, name: str, user: str = Depends(current_user)):
    job = _job_or_404(job_id, user)
    path = (job.folder / jobs.safe_name(name)).resolve()
    if not path.is_file() or job.folder.resolve() not in path.parents:
        raise HTTPException(status_code=404, detail="התמונה לא נמצאה.")
    return FileResponse(path)


@app.get("/healthz")
def healthz():
    return {"ok": True}
