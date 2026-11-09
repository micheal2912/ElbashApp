"""Headless version of the comparison - useful for testing and automation.

    python run_cli.py "C:\\path\\to\\month folder" [--rescan] [--excel out.xlsx]
"""

import argparse
import multiprocessing
import sys
import time
from pathlib import Path

from elbash import compare as compare_mod
from elbash import export, pdf_report, photos


def main() -> int:
    multiprocessing.freeze_support()
    # A frozen console defaults to the legacy code page and dies on Hebrew.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Compare delivery photos against the courier PDF.")
    parser.add_argument("folder", help="Folder holding the screenshots and the PDF report")
    parser.add_argument("--rescan", action="store_true", help="Ignore the OCR cache")
    parser.add_argument("--excel", help="Also write an Excel report to this path")
    parser.add_argument("--limit", type=int, default=25, help="Rows of 'missing' to print")
    parser.add_argument("--workers", type=int, help="Override the parallelism used")
    args = parser.parse_args()

    folder = Path(args.folder)
    pdf = pdf_report.find_report_pdf(folder)
    print(f"PDF    : {pdf.name}")
    clock = time.perf_counter()
    report = pdf_report.read_report(
        pdf, use_cache=not args.rescan, workers=args.workers
    )
    t_pdf = time.perf_counter() - clock
    print(f"Report : {report.title}")
    print(f"Range  : {report.date_range}")
    print(f"Rows   : {len(report.rows)} (PDF footer says {report.declared_total})")
    for warning in report.warnings:
        print(f"  ! {warning}")

    clock = time.perf_counter()
    # read_folder logs a line per photo itself, so there is nothing to add here.
    results = photos.read_folder(
        folder, use_cache=not args.rescan, workers=args.workers
    )
    t_ocr = time.perf_counter() - clock
    outcome = compare_mod.compare(report, results)

    print()
    print("================ SUMMARY ================")
    print(f"Rows in PDF    : {outcome.total_rows}")
    print(f"Photos scanned : {outcome.total_photos}")
    print(f"Matched        : {len(outcome.matched)}")
    print(f"Missing photos : {len(outcome.missing)}")
    print(f"Problem photos : {len(outcome.problems)}")
    print(f"Duplicates     : {len(outcome.duplicates)}")
    print(f"Coverage       : {outcome.coverage:.2f}%")
    print(f"Complete       : {'YES' if outcome.is_complete else 'NO'}")
    print(f"Timing         : pdf {t_pdf:.1f}s, photos {t_ocr:.1f}s"
          f"{'  (frozen build)' if getattr(sys, 'frozen', False) else ''}")

    if outcome.matched:
        print("\n---- matched ----")
        for item in outcome.matched:
            print(f"  {item.number}  {item.date}  {item.confidence:<13} {', '.join(item.photos)}")

    if outcome.problems:
        print("\n---- problems ----")
        for item in outcome.problems:
            print(f"  {item.file}  [{item.reason}] {item.detail}")

    if outcome.missing:
        print(f"\n---- missing (first {args.limit} of {len(outcome.missing)}) ----")
        for row in outcome.missing[: args.limit]:
            print(f"  {row.number}  {row.date}  {row.kind_label}")

    if args.excel:
        export.to_excel(outcome, args.excel)
        print(f"\nExcel written to {args.excel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
