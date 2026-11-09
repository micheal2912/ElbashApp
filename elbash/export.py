"""Write the comparison to a multi-sheet Excel workbook."""

from pathlib import Path

import pandas as pd

from .compare import ComparisonResult


CONFIDENCE_HE = {
    "anchor": "ודאי",
    "report-match": "הושלם מהדוח",
    "unreadable": "לא נקרא",
}


def _frames(result: ComparisonResult) -> dict[str, pd.DataFrame]:
    summary = pd.DataFrame(
        [
            ("דוח", result.report.title or Path(result.report.path).name),
            ("טווח תאריכים", result.report.date_range),
            ("שורות בדוח (מס' תעודה)", result.total_rows),
            ("תמונות שנסרקו", result.total_photos),
            ("נמצאו", len(result.matched)),
            ("חסרות תמונה", len(result.missing)),
            ("תמונות בעייתיות", len(result.problems)),
            ("מספרים שצולמו פעמיים", len(result.duplicates)),
            ("אחוז כיסוי", round(result.coverage, 2)),
            ("הושלם", "כן" if result.is_complete else "לא"),
        ],
        columns=["פריט", "ערך"],
    )

    overview = pd.DataFrame(
        [
            {
                "סטטוס": row.status,
                "מס' תעודה": row.number,
                "תאריך": row.date,
                "סוג": row.kind_label,
                "מסלול": row.route,
                "עמוד בדוח": row.page,
                "תמונות": ", ".join(row.photos),
                "הערה": row.detail,
            }
            for row in result.overview()
        ],
        columns=["סטטוס", "מס' תעודה", "תאריך", "סוג", "מסלול", "עמוד בדוח",
                 "תמונות", "הערה"],
    )

    missing = pd.DataFrame(
        [
            {
                "מס' תעודה": row.number,
                "תאריך": row.date,
                "סוג": row.kind_label,
                "מסלול": row.route,
                "עמוד בדוח": row.page,
            }
            for row in result.missing
        ],
        columns=["מס' תעודה", "תאריך", "סוג", "מסלול", "עמוד בדוח"],
    )

    matched = pd.DataFrame(
        [
            {
                "מס' תעודה": item.number,
                "תאריך": item.date,
                "סוג": item.kind_label,
                "מסלול": item.route,
                "תמונה": ", ".join(item.photos),
                "ודאות": CONFIDENCE_HE.get(item.confidence, item.confidence),
            }
            for item in result.matched
        ],
        columns=["מס' תעודה", "תאריך", "סוג", "מסלול", "תמונה", "ודאות"],
    )

    problems = pd.DataFrame(
        [
            {
                "תמונה": item.file,
                "מספר משלוח": item.number or "",
                "סיבה": item.reason,
                "פירוט": item.detail,
            }
            for item in result.problems
        ],
        columns=["תמונה", "מספר משלוח", "סיבה", "פירוט"],
    )

    by_date = pd.DataFrame(
        [
            {"תאריך": date, "יש": total - miss, "חסר": miss, "סה\"כ": total}
            for date, miss, total in result.missing_by_date()
        ],
        columns=["תאריך", "יש", "חסר", "סה\"כ"],
    )

    return {
        "סיכום": summary,
        "מבט כללי": overview,
        "חסרות": missing,
        "נמצאו": matched,
        "בעיות": problems,
        "לפי תאריך": by_date,
    }


def to_excel(result: ComparisonResult, path: str | Path) -> Path:
    path = Path(path)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in _frames(result).items():
            frame.to_excel(writer, sheet_name=name, index=False)
            sheet = writer.sheets[name]
            sheet.sheet_view.rightToLeft = True
            for column_cells in sheet.columns:
                width = max((len(str(c.value)) for c in column_cells if c.value), default=10)
                sheet.column_dimensions[column_cells[0].column_letter].width = min(60, width + 2)
    return path
