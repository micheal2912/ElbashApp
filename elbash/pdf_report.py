"""Read the courier payout PDF and pull out the מס' תעודה column."""

import json
import os
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pdfplumber

from .rtl import fix_rtl

CACHE_NAME = ".elbash_pdf_cache.json"

# Text extraction is CPU-bound inside pdfminer and holds the GIL, so pages are
# split across processes rather than threads. Each worker opens its own handle.
# Threads were measured at 0.91x of sequential here - all contention, no gain -
# against 5.3x for processes. Past 16 the spawn cost outweighs the extra pages.
PDF_WORKERS = min(16, max(2, os.cpu_count() or 4))
MIN_PAGES_FOR_POOL = 8

# A data row ends (visually) with: <date> <7-digit certificate no.> <kind>
# where kind is מ (מסירה / delivery), א (איסוף / pickup) or מ/א for both.
ROW_RE = re.compile(
    r"(?P<date>\d{2}/\d{2}/\d{2})\s+(?P<number>\d{7})\s+(?P<kind>[\u0590-\u05FF](?:/[\u0590-\u05FF])?)\s*$"
)
TOTAL_RE = re.compile(r"בונים[:\s]*([\d,]+)")
RANGE_RE = re.compile(r"(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})")

KIND_LABELS = {
    "מ": "מסירה",
    "א": "איסוף",
    "מ/א": "מסירה + איסוף",
    "א/מ": "מסירה + איסוף",
}


@dataclass
class ReportRow:
    number: str
    date: str
    kind: str
    kind_label: str
    route: str
    page: int

    @property
    def sort_key(self):
        day, month, year = self.date.split("/")
        return (year, month, day, self.number)


@dataclass
class Report:
    path: str
    rows: list[ReportRow] = field(default_factory=list)
    title: str = ""
    date_range: str = ""
    declared_total: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def numbers(self) -> set[str]:
        return {row.number for row in self.rows}

    def by_number(self) -> dict[str, ReportRow]:
        return {row.number: row for row in self.rows}


def find_report_pdf(folder: str | Path) -> Path:
    pdfs = sorted(Path(folder).glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"No PDF report found in {folder}")
    if len(pdfs) > 1:
        # Prefer the MM.YYYY_NNNN.pdf naming used by the courier reports.
        named = [p for p in pdfs if re.match(r"\d{2}\.\d{4}_", p.name)]
        if named:
            return named[0]
    return pdfs[0]


def _split_row(fixed_line: str) -> str:
    """Drop kind/number/date from the front and the two money columns off the end."""
    tokens = fixed_line.split()[3:]
    while tokens and re.fullmatch(r"[\d,]+\.\d{2}", tokens[-1]):
        tokens.pop()
    return " ".join(tokens).strip()


def _cache_key(path: Path) -> str:
    stat = path.stat()
    return f"{path.name}|{stat.st_size}|{int(stat.st_mtime)}"


def _load_cached(path: Path) -> Report | None:
    cache_file = path.parent / CACHE_NAME
    if not cache_file.exists():
        return None
    try:
        blob = json.loads(cache_file.read_text(encoding="utf-8"))
        if blob.get("key") != _cache_key(path):
            return None
        report = Report(**{k: v for k, v in blob["report"].items() if k != "rows"})
        report.rows = [ReportRow(**row) for row in blob["report"]["rows"]]
        return report
    except Exception:
        return None


def _store_cached(path: Path, report: Report) -> None:
    try:
        payload = {"key": _cache_key(path), "report": asdict(report)}
        (path.parent / CACHE_NAME).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _extract_span(task: tuple[str, int, int]) -> tuple[int, list[str]]:
    """Worker process: return the text of pages [start, end) in order."""
    path, start, end = task
    with pdfplumber.open(path) as pdf:
        return start, [pdf.pages[i].extract_text() or "" for i in range(start, end)]


def _extract_sequential(path: Path, progress=None) -> list[str]:
    texts: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        total = len(pdf.pages)
        for index, page in enumerate(pdf.pages, start=1):
            texts.append(page.extract_text() or "")
            if progress:
                progress(index, total)
    return texts


def _extract_all(path: Path, progress=None, workers: int | None = None) -> list[str]:
    """Page text in page order, extracted across processes when worthwhile."""
    with pdfplumber.open(str(path)) as pdf:
        total = len(pdf.pages)

    workers = workers or PDF_WORKERS
    if total < MIN_PAGES_FOR_POOL or workers < 2:
        return _extract_sequential(path, progress=progress)

    span = max(1, -(-total // workers))
    tasks = [(str(path), start, min(start + span, total))
             for start in range(0, total, span)]

    texts: list[str] = [""] * total
    done = 0
    try:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_extract_span, task) for task in tasks]
            for future in as_completed(futures):
                start, chunk = future.result()
                texts[start:start + len(chunk)] = chunk
                done += len(chunk)
                if progress:
                    progress(done, total)
    except Exception:
        # Sandboxes and frozen builds can forbid subprocesses; still work there.
        return _extract_sequential(path, progress=progress)

    return texts


def read_report(
    path: str | Path,
    progress=None,
    use_cache: bool = True,
    workers: int | None = None,
) -> Report:
    """Parse every data row out of the courier report."""
    path = Path(path)

    if use_cache:
        cached = _load_cached(path)
        if cached is not None:
            if progress:
                progress(1, 1)
            return cached

    texts = _extract_all(path, progress=progress, workers=workers)

    report = Report(path=str(path))
    seen: set[str] = set()
    duplicates = 0

    for index, text in enumerate(texts, start=1):
        for raw_line in text.split("\n"):
            line = raw_line.strip()
            if not line:
                continue

            match = ROW_RE.search(line)
            if match:
                number = match.group("number")
                kind = match.group("kind")
                if number in seen:
                    duplicates += 1
                    continue
                seen.add(number)
                report.rows.append(
                    ReportRow(
                        number=number,
                        date=match.group("date"),
                        kind=kind,
                        kind_label=KIND_LABELS.get(kind, kind),
                        route=_split_row(fix_rtl(line)),
                        page=index,
                    )
                )
                continue

            if index == 1 and not report.title and "לשליח" in fix_rtl(line):
                report.title = fix_rtl(line)
            if not report.date_range:
                found = RANGE_RE.search(line)
                if found:
                    report.date_range = f"{found.group(2)} - {found.group(1)}"
            if report.declared_total is None:
                total = TOTAL_RE.search(fix_rtl(line))
                if total:
                    report.declared_total = int(total.group(1).replace(",", ""))

    if duplicates:
        report.warnings.append(f"{duplicates} מספרי תעודה כפולים בדוח לא נכללו.")
    if report.declared_total and report.declared_total != len(report.rows):
        report.warnings.append(
            f"בתחתית הדוח מצוינות {report.declared_total} שורות, "
            f"אך נקראו {len(report.rows)}."
        )

    report.rows.sort(key=lambda r: r.sort_key)
    if use_cache:
        _store_cached(path, report)
    return report
