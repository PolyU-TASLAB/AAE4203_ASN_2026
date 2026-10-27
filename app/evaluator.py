"""Epoch matching and accuracy scoring.

Rules implemented here are taken from slides 32-33 of the lab deck:

* epochs are matched by GPS week + time of week;
* a missing benchmark epoch lowers availability, which is
  ``matched / total_benchmark_epochs``;
* a group is ranked only when availability >= ``availability_min``;
* errors are formed in the local East-North-Up frame at the reference
  position: 2D = sqrt(dE^2 + dN^2), 3D = sqrt(dE^2 + dN^2 + dU^2);
* ranking sorts by 2D RMSE, ties broken by 3D RMSE;
* the 95th percentile of the 2D error is reported alongside.
"""

from __future__ import annotations

import bisect
import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from .geodetic import ecef_delta_to_enu, ecef_to_llh, percentile_linear
from .submission import Epoch


# ---------------------------------------------------------------------------
# ground truth
# ---------------------------------------------------------------------------
@dataclass
class GroundTruth:
    week: list[int]
    tow: list[float]
    x: list[float]
    y: list[float]
    z: list[float]
    q: list[int]
    origin_lat: float
    origin_lon: float
    origin_h: float
    grid_s: float
    meta: dict = field(default_factory=dict)
    _index: dict[float, int] = field(default_factory=dict, repr=False)

    @property
    def n(self) -> int:
        return len(self.tow)

    def key(self, tow: float) -> float:
        """Quantise a time-of-week onto the matching grid."""
        return round(round(tow / self.grid_s) * self.grid_s, 6)

    def build_index(self) -> None:
        self._index = {self.key(t): i for i, t in enumerate(self.tow)}

    def lookup(self, week: int, tow: float, tolerance_s: float) -> int | None:
        """Return the GT index for this epoch, or None."""
        if week != self.week[0]:
            return None
        hit = self._index.get(self.key(tow))
        if hit is not None:
            return hit
        if tolerance_s <= 0:
            return None
        pos = bisect.bisect_left(self.tow, tow)
        best: tuple[float, int] | None = None
        for cand in (pos - 1, pos, pos + 1):
            if 0 <= cand < self.n:
                delta = abs(self.tow[cand] - tow)
                if delta <= tolerance_s and (best is None or delta < best[0]):
                    best = (delta, cand)
        return best[1] if best else None


def load_ground_truth(path: str | Path, grid_s: float = 0.1,
                      max_quality: int = 5,
                      enu_origin: str = "auto") -> GroundTruth:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Ground truth not found: {path}")
    if grid_s <= 0:
        raise ValueError("match grid must be positive")

    weeks: list[int] = []
    tows: list[float] = []
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    qs: list[int] = []

    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        import csv as _csv
        reader = _csv.DictReader(handle)
        required = {"gps_week", "gps_tow_s", "x_ecef_m", "y_ecef_m", "z_ecef_m"}
        if not reader.fieldnames or not required.issubset(
                {f.strip().lower() for f in reader.fieldnames}):
            raise ValueError(
                f"{path.name}: expected columns "
                "gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m[,q]")
        field_names = {f.strip().lower(): f for f in reader.fieldnames}
        for row in reader:
            try:
                week = int(float(row[field_names["gps_week"]]))
                tow = float(row[field_names["gps_tow_s"]])
                x = float(row[field_names["x_ecef_m"]])
                y = float(row[field_names["y_ecef_m"]])
                z = float(row[field_names["z_ecef_m"]])
            except (TypeError, ValueError):
                continue
            q = 5
            if "q" in field_names:
                try:
                    q = int(float(row[field_names["q"]]))
                except (TypeError, ValueError):
                    q = 5
            if q > max_quality:
                continue
            if not all(math.isfinite(v) for v in (tow, x, y, z)):
                continue
            weeks.append(week)
            tows.append(tow)
            xs.append(x)
            ys.append(y)
            zs.append(z)
            qs.append(q)

    if len(tows) < 2:
        raise ValueError(f"{path.name}: fewer than 2 usable epochs")

    order = sorted(range(len(tows)), key=lambda i: (weeks[i], tows[i]))
    weeks = [weeks[i] for i in order]
    tows = [tows[i] for i in order]
    xs = [xs[i] for i in order]
    ys = [ys[i] for i in order]
    zs = [zs[i] for i in order]
    qs = [qs[i] for i in order]

    if enu_origin and enu_origin.strip().lower() not in ("auto", ""):
        parts = [p.strip() for p in enu_origin.split(",")]
        if len(parts) != 3:
            raise ValueError("challenge.enu_origin must be 'auto' or 'lat,lon,h'")
        lat0, lon0, h0 = (float(parts[0]), float(parts[1]), float(parts[2]))
    else:
        lat0, lon0, h0 = ecef_to_llh(xs[0], ys[0], zs[0])

    gt = GroundTruth(weeks, tows, xs, ys, zs, qs, lat0, lon0, h0, grid_s)
    gt.build_index()

    meta_path = path.with_suffix(".meta.json")
    if meta_path.is_file():
        try:
            gt.meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            gt.meta = {}
    return gt


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------
@dataclass
class EvalResult:
    ok: bool = False
    error_code: str = ""
    error_message: str = ""

    n_gt: int = 0
    n_matched: int = 0
    n_missing: int = 0
    n_extra: int = 0
    n_lenient: int = 0
    availability: float = 0.0

    rmse2d: float = float("nan")
    rmse3d: float = float("nan")
    p95_2d: float = float("nan")
    p95_3d: float = float("nan")
    mean2d: float = float("nan")
    mean3d: float = float("nan")
    std2d: float = float("nan")
    max2d: float = float("nan")
    max3d: float = float("nan")
    de_mean: float = float("nan")
    dn_mean: float = float("nan")
    du_mean: float = float("nan")

    # per-epoch series for the plots (matched epochs only, time-ordered)
    tow: list[float] = field(default_factory=list)
    de: list[float] = field(default_factory=list)
    dn: list[float] = field(default_factory=list)
    du: list[float] = field(default_factory=list)
    e2d: list[float] = field(default_factory=list)
    e3d: list[float] = field(default_factory=list)
    missing_tow: list[float] = field(default_factory=list)

    @property
    def score_key(self) -> tuple[float, float]:
        return (self.rmse2d, self.rmse3d)


def evaluate(gt: GroundTruth, epochs: list[Epoch], *,
             tolerance_s: float = 0.0,
             availability_min: float = 90.0) -> EvalResult:
    """Match ``epochs`` against ``gt`` and compute all metrics."""
    result = EvalResult(n_gt=gt.n)
    if not epochs:
        result.error_code = "E_NO_EPOCHS"
        result.error_message = "No epochs to evaluate."
        return result
    if gt.n == 0:
        result.error_code = "E_NO_GT"
        result.error_message = "The ground truth is empty."
        return result

    used = bytearray(gt.n)                 # 1 once a GT epoch has been matched
    matched: list[tuple[int, float, float, float]] = []   # (gt_idx, de, dn, du)

    for epoch in epochs:
        idx = gt.lookup(epoch.week, epoch.tow, tolerance_s)
        if idx is None:
            result.n_extra += 1
            continue
        if used[idx]:
            result.n_extra += 1
            continue
        used[idx] = 1
        if abs(gt.tow[idx] - epoch.tow) > gt.grid_s / 2.0:
            result.n_lenient += 1
        dx = epoch.x - gt.x[idx]
        dy = epoch.y - gt.y[idx]
        dz = epoch.z - gt.z[idx]
        lat, lon, _ = ecef_to_llh(gt.x[idx], gt.y[idx], gt.z[idx])
        de, dn, du = ecef_delta_to_enu(dx, dy, dz, lat, lon)
        matched.append((idx, de, dn, du))

    result.n_matched = len(matched)
    result.n_missing = gt.n - result.n_matched
    result.availability = 100.0 * result.n_matched / gt.n

    if result.n_matched == 0:
        result.error_code = "E_NO_MATCH"
        result.error_message = (
            "None of the submitted epochs match the benchmark epoch list. "
            "Check gps_week and gps_tow_s (seconds of the GPS week), and that "
            "you processed the benchmark dataset rather than your own data.")
        return result

    matched.sort(key=lambda item: gt.tow[item[0]])

    e2d_sq: list[float] = []
    for idx, de, dn, du in matched:
        e2d = math.hypot(de, dn)
        e3d = math.hypot(e2d, du)
        result.tow.append(gt.tow[idx])
        result.de.append(de)
        result.dn.append(dn)
        result.du.append(du)
        result.e2d.append(e2d)
        result.e3d.append(e3d)
        e2d_sq.append(e2d * e2d)

    result.missing_tow = [gt.tow[i] for i in range(gt.n) if not used[i]]

    n = result.n_matched
    result.rmse2d = math.sqrt(sum(v * v for v in result.e2d) / n)
    result.rmse3d = math.sqrt(sum(v * v for v in result.e3d) / n)
    result.mean2d = sum(result.e2d) / n
    result.mean3d = sum(result.e3d) / n
    result.std2d = statistics.pstdev(result.e2d)
    result.max2d = max(result.e2d)
    result.max3d = max(result.e3d)
    result.p95_2d = percentile_linear(result.e2d, 95.0)
    result.p95_3d = percentile_linear(result.e3d, 95.0)
    result.de_mean = sum(result.de) / n
    result.dn_mean = sum(result.dn) / n
    result.du_mean = sum(result.du) / n

    # A submission whose positions are off by hundreds of kilometres is
    # almost always a unit or datum mistake, not a bad algorithm.
    if result.mean3d > 5.0e5:
        result.error_code = "E_UNIT_SUSPECT"
        result.error_message = (
            f"The submitted positions are {result.mean3d / 1000:.0f} km from the "
            "reference on average, which is not a positioning error. Check the "
            "units (ECEF in metres, not centimetres) and that both files use "
            "WGS-84.")

    result.ok = bool(result.n_matched)
    result.availability = round(result.availability, 4)
    for name in ("rmse2d", "rmse3d", "p95_2d", "p95_3d", "mean2d", "mean3d",
                 "std2d", "max2d", "max3d", "de_mean", "dn_mean", "du_mean"):
        value = getattr(result, name)
        if value == value:                                   # not NaN
            setattr(result, name, round(value, 4))
    return result


def is_ranked(result: EvalResult, availability_min: float) -> tuple[bool, str]:
    """Return (ranked, reason).  Slide 32: ranked only if availability >= 90 %."""
    if not result.ok:
        return False, result.error_message or "Not evaluated"
    if result.availability + 1e-9 < availability_min:
        return False, (f"Availability {result.availability:.1f} % is below the "
                       f"{availability_min:.0f} % threshold")
    return True, ""


def assign_ranks(rows: list[dict], availability_min: float) -> None:
    """Sort evaluated rows in place and fill ``rank`` / ``unranked_reason``."""
    ranked = [r for r in rows if r.get("ok") and is_ranked(
        _as_result(r), availability_min)[0]]
    ranked.sort(key=lambda r: (r["rmse2d"], r["rmse3d"], r.get("group", "")))
    for position, row in enumerate(ranked, start=1):
        row["rank"] = position
        row["is_ranked"] = True
        row["unranked_reason"] = ""
    ranked_ids = {id(r) for r in ranked}
    for row in rows:
        if id(row) in ranked_ids:
            continue
        row["rank"] = None
        row["is_ranked"] = False
        if not row.get("ok"):
            row["unranked_reason"] = row.get("error_message") or "Not evaluated"
        else:
            row["unranked_reason"] = (
                f"Availability {row['availability']:.1f} % < {availability_min:.0f} %")


class _AsResult:
    """Tiny shim so ``is_ranked`` can work on dict rows as well as EvalResult."""

    def __init__(self, row: dict):
        self.ok = row.get("ok", False)
        self.availability = row.get("availability", 0.0)
        self.error_message = row.get("error_message", "")


def _as_result(row: dict) -> _AsResult:
    return _AsResult(row)
