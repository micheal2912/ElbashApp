"""End-to-end check against the real March folder, over the real HTTP stack.

    python tests/test_web_e2e.py

Creates a throwaway data directory, logs in, uploads every photo plus the PDF
the way the browser does (in batches), waits for the comparison and verifies
the numbers plus the Excel/CSV downloads.
"""

import io
import os
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SOURCE = Path(r"C:\Users\mshamshoum\Downloads\Mahmood Elbash Project")
EXPECTED = {"rows": 1393, "matched": 9, "missing": 1384, "problems": 2}

DATA = Path(tempfile.mkdtemp(prefix="elbash-test-"))
os.environ["ELBASH_DATA_DIR"] = str(DATA)
os.environ["ELBASH_SECURE_COOKIES"] = "0"
os.environ["ELBASH_SECRET_KEY"] = "test-only-key"

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

from web import auth, jobs, main  # noqa: E402

PASSWORD = "test-password-123"
failures: list[str] = []


def check(label: str, actual, expected=None) -> None:
    ok = actual == expected if expected is not None else bool(actual)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {actual}"
          + (f" (expected {expected})" if not ok and expected is not None else ""))
    if not ok:
        failures.append(label)


def main_test() -> int:
    if not SOURCE.is_dir():
        print(f"Source folder not found: {SOURCE}")
        return 2

    auth.init_db()
    auth.create_user("tester", PASSWORD)

    with TestClient(main.app) as client:
        print("\n-- auth")
        anonymous = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
        check("anonymous / redirects to login", anonymous.status_code, 303)

        page = client.get("/login")
        csrf = page.text.split('name="csrf" value="')[1].split('"')[0]

        bad = client.post(
            "/login",
            data={"username": "tester", "password": "wrong", "csrf": csrf},
            follow_redirects=False,
        )
        check("wrong password rejected", bad.status_code, 400)

        good = client.post(
            "/login",
            data={"username": "tester", "password": PASSWORD, "csrf": csrf},
            follow_redirects=False,
        )
        check("login redirects to app", good.status_code, 303)

        home = client.get("/")
        check("app page loads", home.status_code, 200)
        check("page is RTL Hebrew", 'dir="rtl"' in home.text)
        # Without a version stamp the browser keeps an old app.js against a new
        # page, and a tab the page declares but the script lacks breaks all of
        # them - which is what happened in production.
        check("script is version-stamped", "/static/app.js?v=" in home.text)
        check("stylesheet is version-stamped", "/static/app.css?v=" in home.text)
        token = home.text.split("csrf: \"")[1].split('"')[0]
        head = {"X-CSRF-Token": token}

        print("\n-- csrf")
        no_token = client.post("/api/jobs")
        check("POST without CSRF token is refused", no_token.status_code, 403)

        # Hostile uploads go to their own job: anything stored here would
        # otherwise show up as an extra unreadable photo in the real run.
        print("\n-- upload rules")
        probe = client.post("/api/jobs", headers=head).json()["id"]
        traversal = client.post(
            f"/api/jobs/{probe}/files",
            headers=head,
            files=[("files", ("../../escape.jpg", b"x", "image/jpeg"))],
        ).json()
        check("path traversal name is flattened", traversal["stored"], ["escape.jpg"])
        check(
            "escaped file stayed inside the job folder",
            (main.settings.JOBS_DIR / probe / "escape.jpg").is_file(),
            True,
        )

        blocked = client.post(
            f"/api/jobs/{probe}/files",
            headers=head,
            files=[("files", ("evil.exe", b"x", "application/octet-stream"))],
        ).json()
        check("unsupported type rejected", len(blocked["rejected"]), 1)

        # Uploads are copied in parallel worker threads now, so two photos
        # sharing a name must not land on top of each other.
        clash = client.post(
            f"/api/jobs/{probe}/files",
            headers=head,
            files=[
                ("files", ("same.jpg", b"first-file-contents", "image/jpeg")),
                ("files", ("same.jpg", b"second-different-contents", "image/jpeg")),
            ],
        ).json()
        check("duplicate names kept apart", len(set(clash["stored"])), 2)
        sizes = {
            (main.settings.JOBS_DIR / probe / n).read_bytes()
            for n in clash["stored"]
        }
        check("neither duplicate was overwritten", len(sizes), 2)

        print("\n-- upload")
        job_id = client.post("/api/jobs", headers=head).json()["id"]
        check("job created", bool(job_id))

        payload_files = sorted(SOURCE.glob("*.jpg")) + sorted(SOURCE.glob("*.pdf"))
        start = time.time()
        for index in range(0, len(payload_files), 6):
            batch = [
                ("files", (p.name, p.read_bytes(), "application/octet-stream"))
                for p in payload_files[index:index + 6]
            ]
            response = client.post(f"/api/jobs/{job_id}/files", headers=head, files=batch)
            check(f"batch {index // 6} uploaded", response.status_code, 200)
        print(f"  uploaded {len(payload_files)} files in {time.time() - start:.1f}s")

        print("\n-- run")
        started = client.post(f"/api/jobs/{job_id}/start", headers=head)
        check("job started", started.status_code, 200)

        deadline = time.time() + 600
        state = {}
        while time.time() < deadline:
            state = client.get(f"/api/jobs/{job_id}").json()
            if state["status"] in {"done", "error", "stopped"}:
                break
            time.sleep(0.5)
        check("job finished", state.get("status"), "done")
        if state.get("status") != "done":
            print(f"  error: {state.get('error')}")
            return 1

        summary = state["result"]["summary"]
        print("\n-- results")
        for key, expected in EXPECTED.items():
            check(key, summary[key], expected)
        check("coverage", summary["coverage"], 0.6)
        check("missing table length", len(state["result"]["missing"]), EXPECTED["missing"])
        check("by_date has entries", len(state["result"]["by_date"]) > 0)

        # The overview is meant to hold everything: one row per report row,
        # plus the problem photos, which belong to no row.
        overview = state["result"]["overview"]
        check("overview covers rows and problems",
              len(overview), EXPECTED["rows"] + EXPECTED["problems"])
        statuses = Counter(row["status"] for row in overview)
        check("overview found count", statuses["נמצא"], EXPECTED["matched"])
        check("overview missing count", statuses["חסר"], EXPECTED["missing"])
        check("overview problem count", statuses["בעיה"], EXPECTED["problems"])

        print("\n-- downloads")
        xlsx = client.get(f"/api/jobs/{job_id}/export/excel")
        check("excel status", xlsx.status_code, 200)
        check("excel is a zip container", xlsx.content[:2], b"PK")
        sheets = load_workbook(io.BytesIO(xlsx.content), read_only=True).sheetnames
        check("overview sheet follows the summary", sheets[:2], ["סיכום", "מבט כללי"])
        check("csv sheet set unchanged otherwise", "חסרות" in sheets)
        gone = client.get(f"/api/jobs/{job_id}/export/csv")
        check("retired csv export is 404", gone.status_code, 404)

        photo_name = payload_files[0].name
        img = client.get(f"/api/jobs/{job_id}/photo/{photo_name}")
        check("photo served", img.status_code, 200)
        missing_img = client.get(f"/api/jobs/{job_id}/photo/nope.jpg")
        check("unknown photo is 404", missing_img.status_code, 404)

        print("\n-- isolation")
        auth.create_user("other", PASSWORD)
        with TestClient(main.app) as intruder:
            page = intruder.get("/login")
            c2 = page.text.split('name="csrf" value="')[1].split('"')[0]
            intruder.post("/login", data={"username": "other", "password": PASSWORD, "csrf": c2})
            stolen = intruder.get(f"/api/jobs/{job_id}")
            check("another user cannot read the job", stolen.status_code, 404)

        print("\n-- storage")
        # A month is a few gigabytes and the client runs this several times a
        # day, so a superseded upload has to give its disk back. Its own user,
        # to leave the job the isolation checks above rely on alone.
        auth.create_user("housekeeper", PASSWORD)
        with TestClient(main.app) as keeper:
            page = keeper.get("/login")
            c3 = page.text.split('name="csrf" value="')[1].split('"')[0]
            keeper.post("/login",
                        data={"username": "housekeeper", "password": PASSWORD, "csrf": c3})
            h3 = {"X-CSRF-Token": keeper.get("/").text.split('csrf: "')[1].split('"')[0]}

            old = keeper.post("/api/jobs", headers=h3).json()["id"]
            keeper.post(f"/api/jobs/{old}/files", headers=h3,
                        files=[("files", ("a.jpg", b"x" * 2048, "image/jpeg"))])
            old_folder = jobs.settings.JOBS_DIR / old

            fresh = keeper.post("/api/jobs", headers=h3).json()["id"]
            check("an upload still in flight is spared", old_folder.exists(), True)

            # Age it past the grace that protects an arriving upload, the way
            # hours between two runs would.
            jobs._jobs[old].touched -= jobs.ACTIVE_GRACE_SECONDS + 60
            keeper.post("/api/jobs", headers=h3)
            check("superseded job is deleted", old_folder.exists(), False)
            check("superseded job is gone from the api",
                  keeper.get(f"/api/jobs/{old}", headers=h3).status_code, 404)

            # And the full-disk path, forced rather than waited for.
            floor = jobs.settings.MIN_FREE_MB
            jobs.settings.MIN_FREE_MB = 10 ** 9
            try:
                full = keeper.post("/api/jobs", headers=h3)
                check("full disk refuses a new job", full.status_code, 507)
                check("full disk says so in Hebrew", "מקום" in full.json()["error"])
                check("healthz reports the shortage", keeper.get("/healthz").json()["ok"], False)
            finally:
                jobs.settings.MIN_FREE_MB = floor
            check("healthz recovers", keeper.get("/healthz").json()["ok"], True)
            check("fresh job survived the sweep",
                  keeper.get(f"/api/jobs/{fresh}", headers=h3).status_code, 200)

        print("\n-- logout")
        client.get("/logout")
        after = client.get("/api/jobs/" + job_id)
        check("session ended", after.status_code, 401)

    print("\n" + ("ALL PASSED" if not failures else f"FAILED: {', '.join(failures)}"))
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        code = main_test()
    finally:
        shutil.rmtree(DATA, ignore_errors=True)
    raise SystemExit(code)
