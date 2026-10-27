#!/usr/bin/env python3
"""Check a benchmark CSV before uploading it.

    python check_submission.py G05_benchmark.csv

The rules are the same ones the server applies, so a file that passes here
will not be rejected for format reasons.  This script is deliberately a single
file with no dependencies: copy it anywhere, it runs on any Python 3.8+.

What it checks
--------------
* the file exists, is readable, is text and is not empty;
* the header line is exactly
  ``gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m``;
* every data row has 5 numeric fields, finite, with an integer GPS week;
* the time of week is in [0, 604800) seconds;
* the positions are ECEF metres (not centimetres, not lat/lon/height);
* duplicate epochs, ordering, and epoch spacing.

The accuracy itself cannot be checked here - the reference trajectory is not
released.  Availability and RMSE appear on the leaderboard after you upload.

Exit status: 0 = ready to upload, 1 = problems found.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import math
import re
import statistics
import sys
from pathlib import Path

EXPECTED_HEADER = ["gps_week", "gps_tow_s", "x_ecef_m", "y_ecef_m", "z_ecef_m"]
TOW_MAX = 604800.0
SIZE_LIMIT_MB = 20

BENCHMARK_WEEK = 2158          # change together with the dataset
BENCHMARK_EPOCHS = 1534


def colour(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


OK = lambda s: colour(s, "32")
BAD = lambda s: colour(s, "31")
WARN = lambda s: colour(s, "33")
DIM = lambda s: colour(s, "90")


def line(char="-", width=66):
    print(DIM(char * width))


def fail(problems: list, message: str) -> None:
    problems.append(message)
    print("  " + BAD("FAIL") + "  " + message)


def warn(message: str) -> None:
    print("  " + WARN("warn") + "  " + message)


def ok(message: str) -> None:
    print("  " + OK("ok  ") + "  " + message)


def check(path: Path, ref_week: int = BENCHMARK_WEEK,
          ref_epochs: int = BENCHMARK_EPOCHS) -> int:
    problems: list[str] = []

    print()
    print(f"Checking {path}")
    line("=")
    print(f"  size     {path.stat().st_size / 1024:.1f} KiB")
    raw = path.read_bytes()
    print(f"  sha256   {hashlib.sha256(raw).hexdigest()[:32]}...")
    print()

    # -- basic file checks -------------------------------------------------
    if not raw:
        fail(problems, "the file is empty")
        return finish(problems)
    if len(raw) > SIZE_LIMIT_MB * 1024 * 1024:
        fail(problems, f"the file is larger than {SIZE_LIMIT_MB} MB")
        return finish(problems)

    try:
        text = raw.decode("utf-8-sig")
        encoding_note = "UTF-8"
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp1252")
            encoding_note = "Windows-1252"
            warn("the file is not UTF-8 (read as Windows-1252); re-save it as UTF-8")
        except UnicodeDecodeError:
            fail(problems, "the file is not text - is it an .xlsx workbook? "
                           "export it as CSV first")
            return finish(problems)
    ok(f"decoded as {encoding_note}")

    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        fail(problems, "the file has no header row")
        return finish(problems)

    # -- header ------------------------------------------------------------
    cleaned = [h.strip() for h in header]
    if header != EXPECTED_HEADER:
        fail(problems, "the header must be exactly "
                       f"{','.join(EXPECTED_HEADER)}\n"
                       f"          found: {','.join(header)}")
    else:
        ok("header line is correct")
        if cleaned != EXPECTED_HEADER:
            warn("header names differ only in case or spacing (accepted)")

    # -- rows --------------------------------------------------------------
    epochs: list[tuple[int, float, float, float, float]] = []
    seen: set[tuple[int, float]] = set()
    duplicates = 0
    bad_rows: list[str] = []
    blank_rows = 0

    for lineno, row in enumerate(reader, start=2):
        if not row or all(cell.strip() == "" for cell in row):
            blank_rows += 1
            continue
        cells = [c.strip() for c in row]
        if len(cells) != 5:
            if len(bad_rows) < 5:
                bad_rows.append(f"line {lineno}: {len(cells)} fields, expected 5")
            continue
        if (not re.fullmatch(r"[0-9]+", cells[0]) or
                not re.fullmatch(r"[0-9]+\.[0-9]+", cells[1]) or
                any(not re.fullmatch(r"-?[0-9]+\.[0-9]{3}", v) for v in cells[2:])):
            if len(bad_rows) < 5:
                bad_rows.append(f"line {lineno}: use integer GPS week, seconds with at least one decimal, and ECEF metres with exactly 3 decimals")
            continue
        try:
            week_f, tow, x, y, z = (float(cells[0]), float(cells[1]),
                                    float(cells[2]), float(cells[3]), float(cells[4]))
        except ValueError:
            if len(bad_rows) < 5:
                bad_rows.append(f"line {lineno}: non-numeric field "
                                f"({','.join(cells)})")
            continue
        if not all(math.isfinite(v) for v in (week_f, tow, x, y, z)):
            if len(bad_rows) < 5:
                bad_rows.append(f"line {lineno}: NaN or inf")
            continue
        if week_f != int(week_f):
            if len(bad_rows) < 5:
                bad_rows.append(f"line {lineno}: gps_week is not an integer "
                                f"({cells[0]})")
            continue
        if not (0.0 <= tow < TOW_MAX):
            if len(bad_rows) < 5:
                bad_rows.append(f"line {lineno}: gps_tow_s={cells[1]} is outside "
                                "[0, 604800) - it must be seconds of the GPS week")
            continue
        key = (int(week_f), round(tow, 3))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        epochs.append((int(week_f), tow, x, y, z))

    if bad_rows:
        for message in bad_rows:
            fail(problems, message)
        if len(bad_rows) >= 5:
            print(DIM("          ... further problems not shown"))
    if not epochs:
        fail(problems, "no valid data rows")
        return finish(problems)
    ok(f"{len(epochs)} data rows read" + (f" ({blank_rows} blank lines skipped)"
                                          if blank_rows else ""))

    # -- positions ---------------------------------------------------------
    magnitudes = [abs(v) for e in epochs for v in e[2:]]
    largest = max(magnitudes)
    if largest < 1e5:
        fail(problems, "the coordinate values are far too small for ECEF metres "
                       f"(largest is {largest:.3f}).\n"
                       "          Hong Kong is about X=-2.4e6, Y=5.4e6, Z=2.4e6 m. "
                       "You have probably written latitude/longitude/height.")
    elif largest > 1e8:
        fail(problems, f"the coordinates are far too large ({largest:.3g}); "
                       "ECEF metres should be around 6.4e6. Centimetres?")
    else:
        ok("coordinates are in the plausible ECEF range for Hong Kong")

    # -- ordering ----------------------------------------------------------
    ordered = all((a[0], a[1]) < (b[0], b[1]) for a, b in zip(epochs, epochs[1:]))
    if ordered:
        ok("rows are in time order")
    else:
        fail(problems, "rows must be in ascending GPS week/time order")
    if duplicates:
        fail(problems, f"{duplicates} duplicate epoch(s): use one row per epoch")

    # -- time --------------------------------------------------------------
    weeks = sorted({e[0] for e in epochs})
    tows = [e[1] for e in epochs]
    print()
    print(f"  GPS week      {', '.join(str(w) for w in weeks)}")
    print(f"  time of week  {min(tows):.1f} .. {max(tows):.1f}"
          f"   ({max(tows) - min(tows):.0f} s = {(max(tows) - min(tows)) / 60:.1f} min)")
    print(f"  epochs        {len(epochs)}")

    spacing: dict[float, int] = {}
    for a, b in zip(tows, tows[1:]):
        dt = round(b - a, 3)
        spacing[dt] = spacing.get(dt, 0) + 1
    top = sorted(spacing.items(), key=lambda kv: -kv[1])[:4]
    print("  spacing       " + ", ".join(f"{dt:g} s x{n}" for dt, n in top))

    if len(weeks) > 1:
        warn("the file spans more than one GPS week")
    if ref_week and ref_week not in weeks:
        warn(f"the benchmark is GPS week {ref_week}; this file has "
             f"{', '.join(str(w) for w in weeks)} - epochs will not match")

    # -- availability estimate --------------------------------------------
    low_coverage = False
    if ref_epochs:
        estimate = 100.0 * len(epochs) / ref_epochs
        print()
        print(f"  candidate availability  ~{estimate:.1f} %  "
              f"({len(epochs)} of {ref_epochs} benchmark epochs)")
        if estimate < 90.0:
            low_coverage = True
            warn("this is below the 90 % ranking threshold - fill in the "
                 "missing epochs before the deadline")
        else:
            ok("above the 90 % ranking threshold")

    # -- position spread ---------------------------------------------------
    xs = [e[2] for e in epochs]
    ys = [e[3] for e in epochs]
    zs = [e[4] for e in epochs]
    jumps = [math.dist(e[2:], f[2:]) for e, f in zip(epochs, epochs[1:])]
    if jumps:
        biggest = max(jumps)
        if biggest > 5000:
            warn(f"largest jump between consecutive epochs is {biggest / 1000:.1f} km")
        print(f"  position span  X {max(xs) - min(xs):.1f} m, "
              f"Y {max(ys) - min(ys):.1f} m, Z {max(zs) - min(zs):.1f} m")
    return finish(problems, low_coverage)


def finish(problems: list[str], low_coverage: bool = False) -> int:
    print()
    line("=")
    if problems:
        print(BAD(f"  {len(problems)} problem(s) found - fix them before uploading."))
        print()
        return 1
    if low_coverage:
        print(WARN("  Format is fine, but coverage is too low to be ranked."))
        print(DIM("  The file will be accepted and shown on the board with a dash."))
        print()
        return 0
    print(OK("  Ready to upload."))
    print(DIM("  Name it GXX_benchmark.csv and put it in your group folder."))
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check a benchmark submission CSV before uploading.")
    parser.add_argument("files", nargs="+", help="one or more CSV files")
    parser.add_argument("--week", type=int, default=BENCHMARK_WEEK,
                        help=f"benchmark GPS week (default {BENCHMARK_WEEK})")
    parser.add_argument("--epochs", type=int, default=BENCHMARK_EPOCHS,
                        help=f"number of benchmark epochs (default {BENCHMARK_EPOCHS})")
    args = parser.parse_args(argv)

    status = 0
    for name in args.files:
        path = Path(name)
        if not path.is_file():
            print(BAD(f"not found: {path}"))
            status = 2
            continue
        status = max(status, check(path, args.week, args.epochs))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
