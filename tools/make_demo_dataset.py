"""Synthesise demo submissions from the ground truth.

Two uses:

1. **Rehearsal** - produce a folder that looks exactly like the TA's Google
   Drive folder, with 18 groups of different quality, so the leaderboard can be
   verified end to end before the lab.  The profiles reproduce the illustrative
   table on slide 35 of the deck.
2. **Deliberate failures** - a few files with the classic mistakes (centimetres
   instead of metres, wrong header, missing epochs) so the error reporting can
   be tested.

Usage
-----
    python tools/make_demo_dataset.py --out drive_mirror
    python tools/make_demo_dataset.py --out demo --groups 18 --faults
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.evaluator import load_ground_truth  # noqa: E402
from app.geodetic import ecef_delta_to_enu, ecef_to_llh, llh_to_ecef  # noqa: E402

# Profile table: group, method label, target 2D RMSE [m], vertical/2D ratio,
# availability [%], bias share (0 = pure noise, 1 = pure constant offset)
PROFILES = [
    ("G07", "GPS+BDS+GAL, C/N0 weights, outlier rejection", 6.8, 2.19, 99.4, 0.35),
    ("G15", "GPS+BDS, elevation weights",                   9.3, 1.96, 100.0, 0.40),
    ("G02", "GPS+BDS, elevation mask 15 deg",              12.6, 1.90,  97.1, 0.45),
    ("G11", "GPS only (baseline)",                         31.4, 1.46, 100.0, 0.60),
    ("G04", "GPS only, many epochs missing",                8.9, 1.83,  71.0, 0.40),
    ("G01", "GPS only, elevation mask 10 deg",             40.2, 1.52,  99.9, 0.55),
    ("G03", "GPS+BDS, equal weights",                      22.5, 1.71, 100.0, 0.50),
    ("G05", "GPS+GAL, C/N0 weights",                       15.8, 1.80,  98.6, 0.45),
    ("G06", "GPS+BDS+GAL, outlier rejection",              11.2, 2.05,  99.2, 0.40),
    ("G08", "GPS+BDS, carrier smoothing",                  17.4, 1.75, 100.0, 0.42),
    ("G09", "GPS only, no ionosphere correction",          46.7, 1.44,  99.5, 0.65),
    ("G10", "GPS+BDS, Klobuchar + Saastamoinen",           19.3, 1.68,  96.4, 0.45),
    ("G12", "GPS+BDS+GAL, elevation mask 12 deg",          13.9, 1.95,  94.2, 0.42),
    ("G13", "GPS only, first attempt",                     55.1, 1.38,  98.0, 0.70),
    ("G14", "GPS+BDS, per-system clock bias",              26.8, 1.62, 100.0, 0.50),
    ("G16", "GPS+GAL+BDS, Kalman filter",                   8.1, 2.10,  99.8, 0.38),
    ("G17", "GPS only, wrong tcvr correction",             62.3, 1.32,  97.5, 0.72),
    ("G18", "GPS+BDS, elevation weights, mask 15 deg",     20.1, 1.66,  88.3, 0.48),
]


def smooth_noise(n: int, rng: random.Random, *, amplitude: float,
                 wavelength: float) -> list[float]:
    """Correlated multipath-like series: sum of a few random sinusoids."""
    components = []
    for _ in range(4):
        freq = rng.uniform(0.6, 1.6) / max(1.0, wavelength)
        phase = rng.uniform(0.0, 2.0 * math.pi)
        amp = amplitude * rng.uniform(0.5, 1.0)
        components.append((freq, phase, amp))
    out = []
    for i in range(n):
        value = 0.0
        for freq, phase, amp in components:
            value += amp * math.sin(2.0 * math.pi * freq * i + phase)
        out.append(value)
    return out


def make_profile_errors(n: int, rng: random.Random, target_rmse2d: float,
                        vertical_ratio: float, bias_share: float):
    """Return (de[], dn[], du[]) whose 2D RMSE equals ``target_rmse2d``."""
    # horizontal: a constant offset plus slowly varying noise plus white noise
    bias_e = rng.gauss(0.0, 1.0)
    bias_n = rng.gauss(0.0, 1.0)
    slow_e = smooth_noise(n, rng, amplitude=1.0, wavelength=45.0)
    slow_n = smooth_noise(n, rng, amplitude=1.0, wavelength=45.0)
    white_e = [rng.gauss(0.0, 0.30) for _ in range(n)]
    white_n = [rng.gauss(0.0, 0.30) for _ in range(n)]

    raw_e, raw_n = [], []
    for i in range(n):
        raw_e.append(bias_share * bias_e + (1.0 - bias_share) * (slow_e[i] + white_e[i]))
        raw_n.append(bias_share * bias_n + (1.0 - bias_share) * (slow_n[i] + white_n[i]))

    rms = math.sqrt(sum(a * a + b * b for a, b in zip(raw_e, raw_n)) / n)
    scale = target_rmse2d / rms if rms > 0 else 1.0

    de = [v * scale for v in raw_e]
    dn = [v * scale for v in raw_n]
    # vertical error streams from the same physics, scaled by the ratio
    du = [v * vertical_ratio for v in dn]
    return de, dn, du


def write_csv(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n")
        writer = csv.writer(handle)
        for week, tow, x, y, z in rows:
            writer.writerow([week, f"{tow:.1f}", f"{x:.3f}", f"{y:.3f}", f"{z:.3f}"])


def build_group(gt, code: str, label: str, target_rmse2d: float,
                vertical_ratio: float, availability: float, bias_share: float,
                seed: int) -> list[tuple[int, float, float, float, float]]:
    rng = random.Random(seed)
    n = gt.n
    de, dn, du = make_profile_errors(n, rng, target_rmse2d, vertical_ratio, bias_share)

    # apply the errors in the local ENU frame, then go back to ECEF
    lat0 = math.radians(gt.origin_lat)
    lon0 = math.radians(gt.origin_lon)
    sin_lat, cos_lat = math.sin(lat0), math.cos(lat0)
    sin_lon, cos_lon = math.sin(lon0), math.cos(lon0)

    keep = n if availability >= 100.0 else max(2, int(round(n * availability / 100.0)))
    if keep >= n:
        indices = list(range(n))
    else:
        # drop epochs in contiguous bursts, like a receiver losing enough
        # satellites to produce no solution
        indices = sorted(rng.sample(range(n), keep))
        indices[0] = 0                       # always include the first epoch

    rows = []
    for i in indices:
        e, nn, u = de[i], dn[i], du[i]
        dx = -sin_lon * e - sin_lat * cos_lon * nn + cos_lat * cos_lon * u
        dy = cos_lon * e - sin_lat * sin_lon * nn + cos_lat * sin_lon * u
        dz = cos_lat * nn + sin_lat * u
        rows.append((gt.week[i], gt.tow[i],
                     gt.x[i] + dx, gt.y[i] + dy, gt.z[i] + dz))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="drive_mirror", help="output folder")
    ap.add_argument("--gt", default=None, help="ground truth csv (default from config)")
    ap.add_argument("--groups", type=int, default=len(PROFILES))
    ap.add_argument("--faults", action="store_true",
                    help="also write deliberately broken submissions")
    ap.add_argument("--seed", type=int, default=4203)
    args = ap.parse_args(argv)

    gt_path = args.gt
    if gt_path is None:
        sys.path.insert(0, str(REPO_ROOT))
        from app.config import load_config
        gt_path = str(load_config().gt_path)

    gt = load_ground_truth(gt_path)
    out = Path(args.out)
    if not out.is_absolute():
        out = REPO_ROOT / out
    out.mkdir(parents=True, exist_ok=True)

    print(f"ground truth : {gt_path}")
    print(f"epochs       : {gt.n}")
    print(f"output       : {out}")
    print()

    profiles = PROFILES[:args.groups]
    for index, (code, label, rmse, vratio, avail, bias) in enumerate(profiles):
        rows = build_group(gt, code, label, rmse, vratio, avail, bias,
                           seed=args.seed + index)
        folder = out / code
        write_csv(folder / f"{code}_benchmark.csv", rows)
        (folder / f"{code}_version.txt").write_text(label + "\n", encoding="utf-8")
        print(f"  {code}  epochs={len(rows):5d}  target 2D RMSE={rmse:5.1f} m  "
              f"avail={avail:5.1f}%  {label}")

    if args.faults:
        print()
        print("deliberately broken files:")
        rows = build_group(gt, "G90", "centimetres", 10.0, 1.5, 100.0, 0.4, args.seed + 90)
        write_csv(out / "G90" / "G90_benchmark.csv",
                  [(w, t, x * 100, y * 100, z * 100) for w, t, x, y, z in rows])
        print("  G90  coordinates in centimetres")

        folder = out / "G91"
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "G91_benchmark.csv", "w", encoding="utf-8", newline="") as h:
            h.write("week,tow,x,y,z\n2158,455342.0,-2418202.586,5385847.524,2405648.799\n")
        print("  G91  wrong header")

        rows = build_group(gt, "G92", "degrees", 10.0, 1.5, 100.0, 0.4, args.seed + 92)
        llh = []
        for week, tow, x, y, z in rows:
            la, lo, hgt = ecef_to_llh(x, y, z)
            llh.append((week, tow, la, lo, hgt))
        write_csv(out / "G92" / "G92_benchmark.csv", llh)
        print("  G92  latitude/longitude/height instead of ECEF")

        (out / "G93").mkdir(parents=True, exist_ok=True)
        (out / "G93" / "G93_benchmark.csv").write_text("", encoding="utf-8")
        print("  G93  empty file")

    print()
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
