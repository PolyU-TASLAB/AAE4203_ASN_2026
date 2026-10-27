"""Minimal RINEX epoch reader.

Only the epoch records are parsed - enough to answer the question that matters
for the benchmark: *which epochs can a group actually produce?*  If the
reference trajectory contains epochs that are missing from the observation
file, nobody can ever reach 100 % availability, so those epochs must be
dropped when the ground truth is built.

Supports RINEX 3 (``> 2021 5 21 6 29 6.0040000  0 34``) and the fixed-column
RINEX 2 epoch record.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

GPS_EPOCH = datetime(1980, 1, 6)
SECONDS_PER_WEEK = 604800


def _to_gps(dt: datetime, seconds: float) -> tuple[int, float]:
    delta = dt - GPS_EPOCH
    total = delta.days * 86400 + delta.seconds + seconds
    week = int(total // SECONDS_PER_WEEK)
    return week, total - week * SECONDS_PER_WEEK


def parse_rinex_epochs(path: str | Path) -> list[tuple[int, float]]:
    """Return ``[(gps_week, gps_tow_s), ...]`` for every observation epoch."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"RINEX file not found: {path}")

    epochs: list[tuple[int, float]] = []
    in_header = True
    saw_rinex3 = False

    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if in_header:
                if "END OF HEADER" in line:
                    in_header = False
                continue

            if line.startswith(">"):
                saw_rinex3 = True
                tokens = line[1:].split()
                if len(tokens) < 6:
                    continue
                try:
                    year = int(tokens[0])
                    month = int(tokens[1])
                    day = int(tokens[2])
                    hour = int(tokens[3])
                    minute = int(tokens[4])
                    second = float(tokens[5])
                except ValueError:
                    continue
                try:
                    epochs.append(_to_gps(datetime(year, month, day, hour, minute),
                                          second))
                except ValueError:
                    continue
                continue

            if saw_rinex3:
                continue                       # event records, not epochs

            # RINEX 2: the epoch record is a fixed 80-column line whose first
            # character is blank and whose seconds field is F11.7.
            if len(line) < 26 or line[0] != " ":
                continue
            try:
                year2 = int(line[0:3])
                month = int(line[3:6])
                day = int(line[6:9])
                hour = int(line[9:12])
                minute = int(line[12:15])
                second = float(line[15:26])
            except ValueError:
                continue
            year = year2 + (2000 if year2 < 80 else 1900)
            try:
                epochs.append(_to_gps(datetime(year, month, day, hour, minute),
                                      second))
            except ValueError:
                continue

    return epochs


def epoch_keys(path: str | Path, grid_s: float = 0.1
               ) -> set[tuple[int, float]]:
    """The set of ``(week, quantised tow)`` keys present in a RINEX file."""
    keys: set[tuple[int, float]] = set()
    for week, tow in parse_rinex_epochs(path):
        keys.add((week, round(round(tow / grid_s) * grid_s, 6)))
    return keys


def describe(path: str | Path) -> dict:
    """Small summary for the admin console and the build report."""
    epochs = parse_rinex_epochs(path)
    if not epochs:
        return {"epochs": 0}
    tows = [tow for _, tow in epochs]
    offsets = sorted({round(tow - int(tow), 3) for tow in tows})
    spacings: dict[float, int] = {}
    for a, b in zip(tows, tows[1:]):
        gap = round(b - a, 3)
        spacings[gap] = spacings.get(gap, 0) + 1
    return {
        "file": Path(path).name,
        "epochs": len(epochs),
        "weeks": sorted({week for week, _ in epochs}),
        "tow_first": round(tows[0], 3),
        "tow_last": round(tows[-1], 3),
        "sub_second_offsets": [o for o in offsets if o][:6],
        "spacing": {str(k): v for k, v in sorted(spacings.items())[:5]},
    }
