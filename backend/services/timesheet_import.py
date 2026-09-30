"""Reading a customer's timesheet file into day marks (25 Sep 2026).

Pure helpers behind `POST /api/timesheets/bulk-import`. Every source — an
.xlsx sheet, a PDF table, a PDF page's text lines — is first turned into a
GRID (rows of cell values); the detectors below never know which it was.

Two layouts are recognised:

* **Matrix** (Harman-style, 27 Aug 2026): one row of DATES as columns and,
  somewhere in the next few rows, one row of day CODES (P / WO / H / L / A /
  HD) or hours. Real sheets put a weekday row ("FRIDAY", "SATURDAY", …)
  between the two — the old importer read the row straight under the dates
  and failed every day with "unknown code 'FRIDAY'" (reported 25 Sep 2026).
  Now every candidate row under the dates is SCORED and the row that reads
  as codes wins; weekday names never count as codes.
* **Token stream**: the same matrix with its layout lost (pypdf writes a PDF
  table one cell per line) — a run of dates, then as many codes.
* **Day list**: one day per row — a date, optionally a weekday, a code and/or
  hours ("01-May-2026  Friday  P  9").

A date row may carry real dates or bare day numbers (1 2 3 …); day numbers
need the period, which comes from the upload's year/month or the file's own
"Year / Month" header. Nothing here touches the database.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from models import AttendanceStatus

#: How far under a date row the code row may sit (weekday, shift, blank rows…).
MAX_ROWS_BELOW_DATES = 6
#: A row needs this many dates to be a matrix's date row.
MIN_MATRIX_DAYS = 5
#: A day list needs this many dated rows before it is believed.
MIN_LIST_DAYS = 3
#: Pages read from a PDF — a month is one or two pages; this bounds a huge upload.
MAX_PDF_PAGES = 12

# code → (status, "full" | "half" | "none") — the hours are the caller's policy.
_CODES: dict[str, tuple[AttendanceStatus, str]] = {}
for _names, _status, _hours in (
    (("P", "PR", "PRESENT", "WFH", "WFO", "W F H"), AttendanceStatus.PRESENT, "full"),
    (("WO", "W", "OFF", "WEEKOFF", "WEEK OFF", "WEEKLY OFF", "W/O"), AttendanceStatus.WEEK_OFF, "none"),
    (("H", "PH", "HOLIDAY", "PUBLIC HOLIDAY", "GH", "FH"), AttendanceStatus.HOLIDAY, "none"),
    (("L", "LV", "LEAVE", "PL", "SL", "CL", "EL", "ML", "CO", "COMP OFF", "COMPOFF"),
     AttendanceStatus.LEAVE, "none"),
    (("A", "AB", "ABS", "ABSENT", "LOP", "LWP"), AttendanceStatus.ABSENT, "none"),
    (("HD", "H/D", "HALF", "HALF DAY", "HALFDAY", "HF"), AttendanceStatus.HALF_DAY, "half"),
):
    for _n in _names:
        _CODES[_n] = (_status, _hours)

_WEEKDAYS = {
    "MON", "TUE", "TUES", "WED", "THU", "THUR", "THURS", "FRI", "SAT", "SUN",
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY",
}
_BLANK = {"", "-", "--", "NA", "N/A", "NIL"}

_FULL_DATE_FORMATS = (
    "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y",
    "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%d-%b-%y", "%d-%B-%Y", "%d %b %y",
    "%b %d, %Y", "%B %d, %Y", "%d/%b/%Y", "%d/%b/%y",
)
_DAY_MONTH_FORMATS = ("%d-%b", "%d %b", "%d/%b", "%d-%B", "%d %B")


@dataclass(frozen=True)
class DayMark:
    """One day as the file states it: its code (normalised, may be "") and/or hours."""
    day: date
    code: str
    hours: Decimal | None = None


def norm_code(v) -> str:
    """'p ' → 'P', 'Week-Off' → 'WEEK OFF', None → ''."""
    if v is None:
        return ""
    s = str(v).strip().upper().replace("_", " ").replace("-", " ").replace(".", "")
    return re.sub(r"\s+", " ", s)


def is_code(code: str) -> bool:
    return code in _CODES


def status_for(code: str) -> tuple[AttendanceStatus, str] | None:
    """(status, hours kind) for a normalised code, None when unknown."""
    return _CODES.get(code)


def _hours(v) -> Decimal | None:
    if v is None or isinstance(v, (date, datetime)) or isinstance(v, bool):
        return None
    s = str(v).strip().replace(",", "")
    if not re.fullmatch(r"\d{1,2}(\.\d{1,2})?", s):
        return None
    try:
        h = Decimal(s)
    except InvalidOperation:
        return None
    return h if Decimal("0") <= h <= Decimal("24") else None


def parse_date(v, year: int | None = None) -> date | None:
    """A cell as a calendar date, or None. `year` completes "01-May"."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if v is None or isinstance(v, (int, float)):
        return None
    s = re.sub(r"\s+", " ", str(v).strip())
    if not s or len(s) > 20:
        return None
    for fmt in _FULL_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    if year:
        for fmt in _DAY_MONTH_FORMATS:
            try:
                return datetime.strptime(f"{s} {year}", f"{fmt} %Y").date()
            except ValueError:
                continue
    return None


def _day_number(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int) or (isinstance(v, float) and v.is_integer()):
        n = int(v)
    else:
        s = str(v).strip() if v is not None else ""
        if not re.fullmatch(r"\d{1,2}", s):
            return None
        n = int(s)
    return n if 1 <= n <= 31 else None


def _date_columns(row, year: int | None, month: int | None) -> list[tuple[int, date]]:
    """(column, date) for a row that is a matrix date row, else []."""
    full = [(ci, d) for ci, v in enumerate(row) if (d := parse_date(v, year)) is not None]
    if len(full) >= MIN_MATRIX_DAYS:
        return full
    if not (year and month):
        return []
    # Bare day numbers 1 2 3 … : believed only as a strictly consecutive run,
    # so an hours row (8 8 8) or a stray total can never pass for dates.
    nums = [(ci, n) for ci, v in enumerate(row) if (n := _day_number(v)) is not None]
    run: list[tuple[int, int]] = []
    best: list[tuple[int, int]] = []
    for ci, n in nums:
        run = run + [(ci, n)] if run and n == run[-1][1] + 1 else [(ci, n)]
        if len(run) > len(best):
            best = run
    if len(best) < MIN_MATRIX_DAYS:
        return []
    out = []
    for ci, n in best:
        try:
            out.append((ci, date(year, month, n)))
        except ValueError:
            break
    return out


def _codes_for_row(row, cols: list[tuple[int, date]]) -> tuple[int, list[DayMark]]:
    """Score a candidate row two ways and keep the better reading.

    Aligned by COLUMN (spreadsheets and PDF tables), or by SEQUENCE (a PDF
    text line, where a label like "Day of Month" shifts every token)."""
    aligned: list[DayMark] = []
    score_a = 0
    for ci, d in cols:
        raw = row[ci] if ci < len(row) else None
        code = norm_code(raw)
        h = _hours(raw)
        if is_code(code):
            score_a += 1
            aligned.append(DayMark(d, code))
        elif h is not None:
            score_a += 1
            aligned.append(DayMark(d, "", h))
        else:
            aligned.append(DayMark(d, "" if code in _BLANK or code in _WEEKDAYS else code))
    seq = [norm_code(v) for v in row]
    seq = [c for c in seq if is_code(c)]
    if len(seq) == len(cols) and len(seq) > score_a:
        return len(seq), [DayMark(d, c) for (_ci, d), c in zip(cols, seq)]
    return score_a, aligned


def find_matrix(grid: list[list], year: int | None = None,
                month: int | None = None) -> list[DayMark]:
    """Every matrix block in the grid: a date row + the best code row under it."""
    marks: list[DayMark] = []
    i = 0
    while i < len(grid):
        cols = _date_columns(grid[i], year, month)
        if not cols:
            i += 1
            continue
        best_score, best_marks, best_at = 0, [], i
        for j in range(i + 1, min(len(grid), i + 1 + MAX_ROWS_BELOW_DATES)):
            if _date_columns(grid[j], year, month):
                break                     # the next block starts here
            score, row_marks = _codes_for_row(grid[j], cols)
            if score > best_score:
                best_score, best_marks, best_at = score, row_marks, j
        if best_score:
            marks.extend(best_marks)
            i = best_at + 1
        else:
            i += 1
    return marks


def find_token_stream(grid: list[list], year: int | None = None,
                      month: int | None = None) -> list[DayMark]:
    """A matrix whose layout was lost: every cell of the grid as one flat token
    stream (pypdf writes a PDF table one cell per line). A run of ≥5 dates,
    then — skipping weekday names and labels — the same number of codes."""
    tokens = [c for row in grid for c in row if c is not None and str(c).strip()]
    marks: list[DayMark] = []
    i = 0
    while i < len(tokens):
        days: list[date] = []
        j = i
        while j < len(tokens):
            d = parse_date(tokens[j], year)
            if d is None and year and month and (n := _day_number(tokens[j])) is not None \
                    and (not days or n == days[-1].day + 1):
                try:
                    d = date(year, month, n)
                except ValueError:
                    d = None
            if d is None or (days and d <= days[-1]):
                break
            days.append(d)
            j += 1
        if len(days) < MIN_MATRIX_DAYS:
            i = j + 1 if j == i else j
            continue
        codes: list[str] = []
        k = j
        while k < len(tokens) and len(codes) < len(days):
            if parse_date(tokens[k], year) is not None:
                break                               # the next block's dates
            c = norm_code(tokens[k])
            if is_code(c):
                codes.append(c)
            k += 1
        if len(codes) == len(days):
            marks.extend(DayMark(d, c) for d, c in zip(days, codes))
        i = k if k > j else j
    return marks


def find_day_list(grid: list[list], year: int | None = None) -> list[DayMark]:
    """One day per row: the first date in the row, then its code and/or hours."""
    marks: list[DayMark] = []
    for row in grid:
        cells = list(row)
        di = next((k for k, v in enumerate(cells) if parse_date(v, year) is not None), None)
        if di is None:
            continue
        d = parse_date(cells[di], year)
        code, hours = "", None
        for v in cells[di + 1:]:
            c = norm_code(v)
            if not code and is_code(c):
                code = c
            elif hours is None and (h := _hours(v)) is not None:
                hours = h
        if code or hours is not None:
            marks.append(DayMark(d, code, hours))
    return marks if len(marks) >= MIN_LIST_DAYS else []


def header_period(grids: list[list[list]]) -> tuple[int, int] | None:
    """(year, month) from a "Year" / "Month" label with the value underneath."""
    months = ["january", "february", "march", "april", "may", "june", "july",
              "august", "september", "october", "november", "december"]
    for grid in grids:
        top = grid[:6]
        hy = hm = None
        for ri in range(len(top) - 1):
            below_row = top[ri + 1]
            for ci, v in enumerate(top[ri]):
                if not isinstance(v, str):
                    continue
                label = v.strip().lower()
                below = below_row[ci] if ci < len(below_row) else None
                if label == "year" and isinstance(below, (int, float)) and 2000 <= int(below) <= 2100:
                    hy = int(below)
                elif label == "month" and below is not None:
                    key = str(below).strip().lower()
                    if key.isdigit() and 1 <= int(key) <= 12:
                        hm = int(key)
                    elif len(key) >= 3:
                        hm = next((n for n, full in enumerate(months, 1) if full.startswith(key[:3])), None)
        if hy and hm:
            return hy, hm
    return None


def xlsx_grids(workbook, max_rows: int = 60) -> list[list[list]]:
    """Every worksheet's top rows as a grid."""
    return [[list(r) for r in ws.iter_rows(min_row=1, max_row=min(ws.max_row or 0, max_rows),
                                           values_only=True)]
            for ws in workbook.worksheets]


def pdf_grids(content: bytes) -> list[list[list]]:
    """A PDF's tables, then its text lines (split on whitespace), page by page.

    pdfplumber (used WHEN INSTALLED — not a requirement) reads ruled tables
    cell-for-cell; otherwise pypdf, already a dependency, supplies the text and
    `find_token_stream` rebuilds the matrix from it. A scanned (image-only)
    PDF yields no grid at all and the caller falls back to attaching the file."""
    grids: list[list[list]] = []
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None
    if pdfplumber is not None:
        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                for page in pdf.pages[:MAX_PDF_PAGES]:
                    for table in page.extract_tables() or []:
                        grids.append([[c for c in row] for row in table if row])
                    text = page.extract_text() or ""
                    grids.append([line.split() for line in text.splitlines() if line.strip()])
            return grids
        except Exception:
            grids = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        for page in reader.pages[:MAX_PDF_PAGES]:
            text = page.extract_text() or ""
            grids.append([line.split() for line in text.splitlines() if line.strip()])
    except Exception:
        return []
    return grids


def read_day_marks(grids: list[list[list]], year: int | None = None,
                   month: int | None = None, *, allow_day_list: bool = True) -> list[DayMark]:
    """Matrix blocks first (every grid), else a day list. First mark per date wins."""
    if not (year and month):
        hdr = header_period(grids)
        if hdr:
            year, month = hdr
    marks: list[DayMark] = []
    for g in grids:
        marks.extend(find_matrix(g, year, month))
    if not marks:
        for g in grids:
            marks.extend(find_token_stream(g, year, month))
    if not marks and allow_day_list:
        for g in grids:
            marks.extend(find_day_list(g, year))
    seen: set[date] = set()
    out: list[DayMark] = []
    for m in marks:
        if m.day not in seen:
            seen.add(m.day)
            out.append(m)
    return out
