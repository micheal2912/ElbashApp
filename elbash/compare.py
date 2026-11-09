"""Reconcile photo delivery numbers against the PDF certificate numbers."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .pdf_report import Report, ReportRow
from .photos import (
    CONFIDENCE_NONE,
    CONFIDENCE_REPORT,
    DELIVERY,
    PICKUP,
    PhotoResult,
)

KIND_WORD = {DELIVERY: "מסירה", PICKUP: "איסוף", "": "סוג לא ידוע"}

REASON_NOT_IN_PDF = "לא בדוח"
REASON_UNREADABLE = "המספר לא זוהה"
REASON_AMBIGUOUS = "לא חד-משמעי"
REASON_OCR_ERROR = "שגיאת קריאה"


@dataclass
class MatchedItem:
    number: str
    date: str
    kind_label: str
    route: str
    photos: list[str]
    confidence: str


@dataclass
class ProblemItem:
    file: str
    number: str | None
    reason: str
    detail: str


@dataclass
class ComparisonResult:
    report: Report
    photos: list[PhotoResult]
    matched: list[MatchedItem] = field(default_factory=list)
    missing: list[ReportRow] = field(default_factory=list)
    problems: list[ProblemItem] = field(default_factory=list)
    duplicates: dict[str, list[str]] = field(default_factory=dict)

    @property
    def total_rows(self) -> int:
        return len(self.report.rows)

    @property
    def total_photos(self) -> int:
        return len(self.photos)

    @property
    def coverage(self) -> float:
        if not self.report.rows:
            return 0.0
        return 100.0 * len(self.matched) / len(self.report.rows)

    @property
    def is_complete(self) -> bool:
        return bool(self.report.rows) and not self.missing and not self.problems

    def missing_by_date(self) -> list[tuple[str, int, int]]:
        """(date, missing, total) per report date, chronological."""
        totals: Counter = Counter(row.date for row in self.report.rows)
        gaps: Counter = Counter(row.date for row in self.missing)
        order = sorted(totals, key=lambda d: tuple(reversed(d.split("/"))))
        return [(date, gaps.get(date, 0), totals[date]) for date in order]


def compare(report: Report, photos: list[PhotoResult]) -> ComparisonResult:
    """Match photos to report rows, then report gaps in both directions."""
    result = ComparisonResult(report=report, photos=photos)
    rows_by_number = report.by_number()
    known = report.numbers

    photos_for_number: dict[str, list[str]] = defaultdict(list)
    confidence_for_number: dict[str, str] = {}

    for photo in photos:
        if photo.error:
            result.problems.append(
                ProblemItem(photo.file, None, REASON_OCR_ERROR, photo.error)
            )
            continue

        number = photo.number
        confidence = photo.confidence

        # No anchored hit: accept a candidate only if the report vouches for it.
        if not number:
            vouched = [c for c in photo.candidates if c in known]
            if len(vouched) == 1:
                number, confidence = vouched[0], CONFIDENCE_REPORT
            elif len(vouched) > 1:
                result.problems.append(
                    ProblemItem(
                        photo.file,
                        None,
                        REASON_AMBIGUOUS,
                        "יותר ממספר אחד תואם לדוח: " + ", ".join(vouched),
                    )
                )
                continue

        if not number:
            hint = ", ".join(photo.candidates[:6]) or "לא זוהו מספרים"
            result.problems.append(
                ProblemItem(
                    photo.file,
                    None,
                    REASON_UNREADABLE,
                    f"לא ניתן לקרוא את מספר המשלוח ({hint}). "
                    "כדאי לצלם שוב בצורה ברורה וישרה.",
                )
            )
            continue

        photo.number = number
        photo.confidence = confidence

        if number not in known:
            kind = KIND_WORD.get(photo.kind, photo.kind)
            result.problems.append(
                ProblemItem(
                    photo.file,
                    number,
                    REASON_NOT_IN_PDF,
                    f"זוהה כ{kind}, אך {number} אינו מופיע בדוח זה.",
                )
            )
            continue

        photos_for_number[number].append(photo.file)
        existing = confidence_for_number.get(number)
        if existing is None or existing == CONFIDENCE_REPORT:
            confidence_for_number[number] = confidence

    for number, files in sorted(photos_for_number.items()):
        row = rows_by_number[number]
        result.matched.append(
            MatchedItem(
                number=number,
                date=row.date,
                kind_label=row.kind_label,
                route=row.route,
                photos=sorted(files),
                confidence=confidence_for_number.get(number, CONFIDENCE_NONE),
            )
        )
        if len(files) > 1:
            result.duplicates[number] = sorted(files)

    result.matched.sort(key=lambda m: tuple(reversed(m.date.split("/"))) + (m.number,))
    result.missing = [row for row in report.rows if row.number not in photos_for_number]
    return result
