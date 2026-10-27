"""Parse and validate a student submission CSV.

Implements the contract from slide 33 of the lab deck::

    gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m
    2158,455342.0,-2418202.586,5385847.524,2405648.799

"ECEF in metres (WGS-84) - no extra columns".

The rules here are deliberate: a malformed upload must produce one clear,
actionable message instead of a silently wrong score.  The same rules are
duplicated in ``tools/check_submission.py`` (which students run themselves)
and an equivalence test keeps the two in sync.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

EXPECTED_HEADER = ["gps_week", "gps_tow_s", "x_ecef_m", "y_ecef_m", "z_ecef_m"]
SIZE_LIMIT = 20 * 1024 * 1024          # 20 MB is ~200x a full benchmark CSV
MAX_ROWS = 200_000
MIN_ROWS_WARN = 10
TOW_MAX = 604800.0                      # seconds in a GPS week


# ---------------------------------------------------------------------------
# data containers
# ---------------------------------------------------------------------------
@dataclass
class Issue:
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass
class Epoch:
    week: int
    tow: float
    x: float
    y: float
    z: float


@dataclass
class ParsedSubmission:
    ok: bool = False
    epochs: list[Epoch] = field(default_factory=list)
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    n_rows: int = 0                # data lines seen
    n_skipped: int = 0             # blank lines
    n_duplicates: int = 0
    sha256: str = ""
    size_bytes: int = 0
    week: int | None = None
    tow_start: float | None = None
    tow_end: float | None = None
    spacing_histogram: dict[str, int] = field(default_factory=dict)

    def error(self, code: str, message: str) -> None:
        self.errors.append(Issue(code, message))

    def warn(self, code: str, message: str) -> None:
        self.warnings.append(Issue(code, message))


# ---------------------------------------------------------------------------
def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_submission_bytes(data: bytes, name: str = "submission.csv") -> ParsedSubmission:
    """Parse and validate raw CSV bytes.  Never raises for bad input."""
    result = ParsedSubmission(sha256=sha256_of(data), size_bytes=len(data))

    if not data:
        result.error("E_EMPTY", "The file is empty (0 bytes).")
        return result
    if len(data) > SIZE_LIMIT:
        result.error("E_TOO_LARGE",
                     f"The file is {len(data) / 1e6:.1f} MB; the limit is "
                     f"{SIZE_LIMIT / 1e6:.0f} MB.")
        return result

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
            result.warn("W_ENCODING",
                        "The file is not UTF-8; it was read as Windows-1252. "
                        "Save it as UTF-8 to be safe.")
        except UnicodeDecodeError:
            result.error("E_ENCODING",
                         "The file is not valid UTF-8 text. It looks like a binary "
                         "file or an Excel workbook (.xlsx) - export as CSV first.")
            return result

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        result.error("E_EMPTY", "The file has no header row.")
        return result

    cleaned = [h.strip() for h in header]
    if header != EXPECTED_HEADER:
        result.error("E_HEADER", "Use exactly this five-column header: " + ','.join(EXPECTED_HEADER))
        return result

    bad_lines: list[str] = []
    epochs: list[Epoch] = []
    seen: dict[tuple[int, float], int] = {}
    total_data_rows = 0

    for lineno, row in enumerate(reader, start=2):
        if not row or all(cell.strip() == "" for cell in row):
            result.n_skipped += 1
            continue
        total_data_rows += 1
        if total_data_rows > MAX_ROWS:
            result.error("E_TOO_MANY_ROWS",
                         f"More than {MAX_ROWS} data rows.")
            return result

        cells = [c.strip() for c in row]
        if len(cells) != 5:
            if len(bad_lines) < 5:
                bad_lines.append(f"line {lineno}: expected 5 fields, found {len(cells)}")
            continue

        if (not re.fullmatch(r"[0-9]+", cells[0]) or
                not re.fullmatch(r"[0-9]+\.[0-9]+", cells[1]) or
                any(not re.fullmatch(r"-?[0-9]+\.[0-9]{3}", v) for v in cells[2:])):
            if len(bad_lines) < 5:
                bad_lines.append(f"line {lineno}: use integer GPS week, seconds with at least one decimal, and ECEF metres with exactly 3 decimals")
            continue

        try:
            week_f = float(cells[0])
            tow = float(cells[1])
            x = float(cells[2])
            y = float(cells[3])
            z = float(cells[4])
        except ValueError:
            if len(bad_lines) < 5:
                bad_lines.append(f"line {lineno}: non-numeric field "
                                 f"({','.join(cells)})")
            continue

        if not all(math.isfinite(v) for v in (week_f, tow, x, y, z)):
            if len(bad_lines) < 5:
                bad_lines.append(f"line {lineno}: contains NaN or inf")
            continue
        if week_f != int(week_f):
            if len(bad_lines) < 5:
                bad_lines.append(f"line {lineno}: gps_week must be an integer "
                                 f"(found {cells[0]})")
            continue
        week = int(week_f)
        if not (0.0 <= tow < TOW_MAX):
            if len(bad_lines) < 5:
                bad_lines.append(
                    f"line {lineno}: gps_tow_s={cells[1]} is outside "
                    f"[0, 604800) - use seconds of the GPS week, not a "
                    "unix time or seconds of day")
            continue

        key = (week, round(tow, 3))
        if key in seen:
            result.n_duplicates += 1
            continue
        seen[key] = 1
        epochs.append(Epoch(week, tow, x, y, z))

    if bad_lines:
        result.error("E_BAD_ROWS",
                     f"{len(bad_lines)} or more rows could not be read. "
                     "First problem(s): " + "; ".join(bad_lines))
    if not epochs:
        if not result.errors:
            result.error("E_NO_ROWS",
                         "The file has a valid header but no data rows.")
        return result
    if result.errors:
        return result

    result.n_rows = total_data_rows
    result.epochs = epochs

    # -- ordering ---------------------------------------------------------
    sorted_ok = all((a.week, a.tow) < (b.week, b.tow) for a, b in zip(epochs, epochs[1:]))
    if not sorted_ok:
        result.error("E_UNSORTED", "Rows must be in ascending GPS week/time order.")
        return result
    if result.n_duplicates:
        result.error("E_DUPLICATES", "Use one row per epoch; remove duplicate timestamps.")
        return result

    # -- magnitude sanity: is this really ECEF in metres? ------------------
    magnitudes = [abs(v) for e in epochs for v in (e.x, e.y, e.z)]
    if max(magnitudes) < 1.0e5:
        result.error(
            "E_NOT_ECEF",
            "The coordinate values are far too small for ECEF metres "
            f"(largest is {max(magnitudes):.3f}). "
            "Hong Kong's ECEF coordinates are about X=-2.4e6, Y=5.4e6, Z=2.4e6. "
            "You have probably written latitude/longitude/height.")
        result.ok = False
        return result
    if max(magnitudes) > 1.0e8:
        result.error(
            "E_LARGE_VALUES",
            f"The coordinate values are far too large for ECEF metres "
            f"(largest is {max(magnitudes):.3g}; ECEF magnitudes are about "
            "6.4e6 m in Hong Kong). You have probably written centimetres.")
        result.ok = False
        return result

    # -- shape of the dataset ---------------------------------------------
    tows = [e.tow for e in epochs]
    result.week = epochs[0].week
    result.tow_start = tows[0]
    result.tow_end = tows[-1]

    if len({e.week for e in epochs}) > 1:
        result.warn("W_MULTI_WEEK",
                    "The file spans more than one GPS week.")

    spacing: dict[float, int] = {}
    for a, b in zip(tows, tows[1:]):
        dt = round(b - a, 3)
        spacing[dt] = spacing.get(dt, 0) + 1
    result.spacing_histogram = {
        str(k): v for k, v in sorted(spacing.items(), key=lambda kv: -kv[1])[:6]}

    if len(epochs) < MIN_ROWS_WARN:
        result.warn("W_FEW_ROWS",
                    f"Only {len(epochs)} epochs - the benchmark has many more. "
                    "Missing epochs reduce your availability score.")
    # -- outlier sanity on the positions themselves ------------------------
    xs = [e.x for e in epochs]
    ys = [e.y for e in epochs]
    zs = [e.z for e in epochs]
    if len(epochs) >= 5:
        med = [statistics.median(xs), statistics.median(ys), statistics.median(zs)]
        jumps = [math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))
                 for a, b in zip(epochs, epochs[1:])]
        if jumps and max(jumps) > 5000.0:
            result.warn("W_JUMPS",
                        f"The largest jump between consecutive epochs is "
                        f"{max(jumps) / 1000:.1f} km; check for a bad epoch.")

    result.ok = True
    return result


def parse_submission_file(path: str | Path) -> ParsedSubmission:
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        result = ParsedSubmission()
        result.error("E_IO", f"Could not read {path.name}: {exc}")
        return result
    return parse_submission_bytes(data, path.name)


def summary_lines(result: ParsedSubmission) -> list[str]:
    """Human-readable summary used by the CLI checker and the admin console."""
    lines = []
    if result.errors:
        lines.append("INVALID")
        for issue in result.errors:
            lines.append(f"  [{issue.code}] {issue.message}")
    else:
        lines.append("FORMAT OK")
    for issue in result.warnings:
        lines.append(f"  warning [{issue.code}] {issue.message}")
    if result.epochs:
        lines.append(f"  epochs         : {len(result.epochs)}")
        lines.append(f"  GPS week       : {result.week}")
        lines.append(f"  tow            : {result.tow_start:.1f} .. {result.tow_end:.1f}"
                     f"  ({result.tow_end - result.tow_start:.0f} s)")
        lines.append(f"  epoch spacing  : {result.spacing_histogram}")
        lines.append(f"  sha256         : {result.sha256[:16]}...")
    return lines
