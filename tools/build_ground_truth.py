"""Build the private ground-truth store from a reference trajectory file.

Supported inputs
----------------
* ``ie`` : Inertial-Explorer / NovAtel style text export, i.e. the
  ``UrbanNav_*_raw.txt`` files (UTCTime, Week, GPSTime, lat D M S, lon D M S,
  H-Ell, velocities, ...).  Two header lines, whitespace separated.
* ``csv``: a CSV with a header row containing week / tow (or utc) and either
  ``x_ecef_m,y_ecef_m,z_ecef_m`` or ``lat,lon,h``.

Output
------
``data/ground_truth/<name>.private.csv`` with the same five columns students
must submit, plus an extra ``q`` quality column used only for filtering::

    gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m,q

and a sibling ``<name>.private.meta.json`` describing the dataset.

Usage
-----
    python tools/build_ground_truth.py UrbanNav_whampoa_raw.txt \
        --format ie --name whampoa --max-quality 5
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.geodetic import dms_to_deg, ecef_to_llh, haversine_m, llh_to_ecef  # noqa: E402

COLUMNS = ["gps_week", "gps_tow_s", "x_ecef_m", "y_ecef_m", "z_ecef_m", "q"]


# ---------------------------------------------------------------------------
# parsers
# ---------------------------------------------------------------------------
def parse_ie(path: Path) -> list[dict]:
    """Parse an Inertial-Explorer style fixed-width text export."""
    rows: list[dict] = []
    problems: list[str] = []

    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for lineno, line in enumerate(handle, 1):
            tokens = line.split()
            if not tokens:
                continue
            # The two header lines start with letters or '(' - data lines start
            # with the UTC timestamp.
            if not (tokens[0].lstrip("+-").replace(".", "", 1).isdigit()):
                continue
            if len(tokens) < 20:
                problems.append(f"line {lineno}: expected 20 fields, got {len(tokens)}")
                continue
            try:
                week = int(float(tokens[1]))
                tow = float(tokens[2])
                lat = dms_to_deg(tokens[3], tokens[4], tokens[5])
                lon = dms_to_deg(tokens[6], tokens[7], tokens[8])
                h = float(tokens[9])
                q = int(float(tokens[-1]))
            except (ValueError, IndexError) as exc:
                problems.append(f"line {lineno}: {exc}")
                continue
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                problems.append(f"line {lineno}: lat/lon out of range ({lat}, {lon})")
                continue
            rows.append({"gps_week": week, "gps_tow_s": tow,
                         "lat": lat, "lon": lon, "h": h, "q": q})

    if problems:
        print(f"  ! {len(problems)} unreadable line(s); first 5:")
        for msg in problems[:5]:
            print("    " + msg)
    return rows


def parse_csv(path: Path) -> list[dict]:
    """Parse a generic CSV with week/tow and ECEF or llh columns."""
    import csv

    rows: list[dict] = []
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SystemExit(f"{path}: empty CSV")
        names = {(n or "").strip().lower(): n for n in reader.fieldnames}

        def pick(*candidates: str) -> str | None:
            for cand in candidates:
                if cand in names:
                    return names[cand]
            return None

        c_week = pick("gps_week", "week")
        c_tow = pick("gps_tow_s", "tow", "gpstime", "gps_tow")
        c_utc = pick("utctime", "utc", "utc_time", "unix_time")
        c_x, c_y, c_z = pick("x_ecef_m", "x"), pick("y_ecef_m", "y"), pick("z_ecef_m", "z")
        c_lat, c_lon = pick("lat", "latitude"), pick("lon", "longitude")
        c_h = pick("h", "height", "h_ell", "h-ell", "alt", "altitude")
        c_q = pick("q", "quality")

        if not c_tow and not c_utc:
            raise SystemExit(f"{path}: need a gps_tow_s or utc column")
        if not (c_x and c_y and c_z) and not (c_lat and c_lon and c_h):
            raise SystemExit(f"{path}: need x/y/z ECEF or lat/lon/h columns")

        for record in reader:
            try:
                tow = float(record[c_tow]) if c_tow else None
                week = int(float(record[c_week])) if c_week else None
                utc = float(record[c_utc]) if c_utc else None
            except (TypeError, ValueError):
                continue
            if tow is None and utc is not None:
                # GPS time = UTC + 18 leap seconds since 2017; week/tow derived
                # from the GPS epoch.  Only used for exotic inputs.
                gps_seconds = utc + 18.0 - 315964800.0
                week = int(gps_seconds // 604800)
                tow = gps_seconds - week * 604800.0
            if week is None or tow is None:
                continue
            try:
                if c_x:
                    x, y, z = float(record[c_x]), float(record[c_y]), float(record[c_z])
                else:
                    x, y, z = llh_to_ecef(float(record[c_lat]), float(record[c_lon]),
                                          float(record[c_h]))
                q = int(float(record[c_q])) if c_q and record.get(c_q) else 5
            except (TypeError, ValueError):
                continue
            rows.append({"gps_week": week, "gps_tow_s": tow,
                         "x": x, "y": y, "z": z, "q": q})
    return rows


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="reference trajectory file")
    ap.add_argument("--format", choices=["auto", "ie", "csv"], default="auto")
    ap.add_argument("--name", default=None, help="output stem (default: input stem)")
    ap.add_argument("--max-quality", type=int, default=5,
                    help="drop epochs whose Q flag is greater than this (default 5 = keep all)")
    ap.add_argument("--out-dir", default="data/ground_truth")
    ap.add_argument("--keep-duplicates", action="store_true",
                    help="keep duplicate (week,tow) entries instead of dropping them")
    ap.add_argument("--obs", default=None, metavar="RINEX",
                    help="restrict the ground truth to the epochs present in this "
                         "RINEX observation file, so 100 %% availability is "
                         "reachable")
    ap.add_argument("--grid", type=float, default=0.1,
                    help="epoch quantisation used when matching against --obs "
                         "(default 0.1 s)")
    args = ap.parse_args(argv)

    src = Path(args.input)
    if not src.is_file():
        print(f"error: {src} not found", file=sys.stderr)
        return 2

    fmt = args.format
    if fmt == "auto":
        head = src.read_text(encoding="utf-8", errors="replace")[:400].lower()
        fmt = "ie" if ("gps time" in head or "gps_time" in head.replace(" ", "_")) else "csv"
        if "utctime" in head.replace(" ", ""):
            fmt = "ie"
    print(f"reading {src}  (format={fmt})")

    rows = parse_ie(src) if fmt == "ie" else parse_csv(src)
    if not rows:
        print("error: no epochs parsed", file=sys.stderr)
        return 2
    print(f"  parsed {len(rows)} epochs")

    # to ECEF where needed
    for row in rows:
        if "x" not in row:
            row["x"], row["y"], row["z"] = llh_to_ecef(row["lat"], row["lon"], row["h"])

    # quality filter
    q_hist_before = Counter(r["q"] for r in rows)
    kept = [r for r in rows if r["q"] <= args.max_quality]
    if len(kept) != len(rows):
        print(f"  dropped {len(rows) - len(kept)} epochs with Q > {args.max_quality}")
    rows = kept
    if not rows:
        print("error: quality filter removed every epoch", file=sys.stderr)
        return 2

    # order + duplicates
    rows.sort(key=lambda r: (r["gps_week"], r["gps_tow_s"]))
    if not args.keep_duplicates:
        seen: set[tuple[int, float]] = set()
        unique = []
        dropped = 0
        for row in rows:
            key = (row["gps_week"], round(row["gps_tow_s"], 3))
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            unique.append(row)
        if dropped:
            print(f"  dropped {dropped} duplicate (week,tow) epochs")
        rows = unique

    if len(rows) < 2:
        print("error: fewer than 2 epochs remain", file=sys.stderr)
        return 2

    # -- restrict to the epochs the observation file actually contains ------
    obs_summary = None
    if args.obs:
        from app.rinex import describe, epoch_keys
        obs_path = Path(args.obs)
        if not obs_path.is_file():
            print(f"error: RINEX observation file not found: {obs_path}",
                  file=sys.stderr)
            return 2
        keys = epoch_keys(obs_path, grid_s=args.grid)
        obs_summary = describe(obs_path)
        if not keys:
            print("error: no epochs could be read from the RINEX file",
                  file=sys.stderr)
            return 2

        def key_of(row):
            return (row["gps_week"],
                    round(round(row["gps_tow_s"] / args.grid) * args.grid, 6))

        before = len(rows)
        kept, dropped_rows = [], []
        for row in rows:
            (kept if key_of(row) in keys else dropped_rows).append(row)
        rows = kept
        print(f"  restricted to the observation file: kept {len(rows)} of "
              f"{before} epochs (dropped {len(dropped_rows)} that no group "
              "could produce)")
        if dropped_rows:
            sample = ", ".join(f"{r['gps_tow_s']:.0f}" for r in dropped_rows[:10])
            print(f"    dropped epochs: {sample}")

        gt_keys = {key_of(r) for r in rows}
        unreachable = sorted(keys - gt_keys)
        if unreachable:
            print(f"  ! {len(unreachable)} observation epochs have no reference "
                  f"position (first: {unreachable[:5]})")

        if len(rows) < 2:
            print("error: the observation file removed every epoch", file=sys.stderr)
            return 2

    # sanity: monotonic time, plausible spacing
    tows = [r["gps_tow_s"] for r in rows]
    gaps = [(tows[i], tows[i + 1], tows[i + 1] - tows[i])
            for i in range(len(tows) - 1) if tows[i + 1] - tows[i] > 1.5]
    dt_hist = Counter(round(tows[i + 1] - tows[i], 3) for i in range(len(tows) - 1))

    out_dir = (REPO_ROOT / args.out_dir) if not Path(args.out_dir).is_absolute() \
        else Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.name or src.stem.replace("_raw", "")
    out_csv = out_dir / f"{stem}.private.csv"
    out_meta = out_dir / f"{stem}.private.meta.json"

    with open(out_csv, "w", encoding="utf-8", newline="") as handle:
        handle.write(",".join(COLUMNS) + "\n")
        for row in rows:
            handle.write("%d,%.3f,%.3f,%.3f,%.3f,%d\n" % (
                row["gps_week"], row["gps_tow_s"], row["x"], row["y"], row["z"], row["q"]))

    # geometry summary
    lat0, lon0, h0 = ecef_to_llh(rows[0]["x"], rows[0]["y"], rows[0]["z"])
    path_len = sum(haversine_m(ecef_to_llh(a["x"], a["y"], a["z"])[0],
                               ecef_to_llh(a["x"], a["y"], a["z"])[1],
                               ecef_to_llh(b["x"], b["y"], b["z"])[0],
                               ecef_to_llh(b["x"], b["y"], b["z"])[1])
                   for a, b in zip(rows, rows[1:]))

    meta = {
        "name": stem,
        "source_file": src.name,
        "source_format": fmt,
        "epochs": len(rows),
        "gps_week": sorted({r["gps_week"] for r in rows}),
        "tow_start": rows[0]["gps_tow_s"],
        "tow_end": rows[-1]["gps_tow_s"],
        "duration_s": rows[-1]["gps_tow_s"] - rows[0]["gps_tow_s"],
        "dt_histogram": {str(k): v for k, v in sorted(dt_hist.items())},
        "gap_count": len(gaps),
        "max_gap_s": max((g[2] for g in gaps), default=1.0),
        "max_quality_kept": args.max_quality,
        "observation_file": args.obs,
        "observation_summary": obs_summary,
        "quality_histogram_raw": {str(k): v for k, v in sorted(q_hist_before.items())},
        "quality_histogram_kept": {
            str(k): v for k, v in sorted(Counter(r["q"] for r in rows).items())},
        "enu_origin_llh": [round(lat0, 9), round(lon0, 9), round(h0, 3)],
        "enu_origin_ecef": [round(rows[0]["x"], 6), round(rows[0]["y"], 6),
                            round(rows[0]["z"], 6)],
        "path_length_m": round(path_len, 1),
        "lat_min": round(min(ecef_to_llh(r["x"], r["y"], r["z"])[0] for r in rows), 9),
        "lat_max": round(max(ecef_to_llh(r["x"], r["y"], r["z"])[0] for r in rows), 9),
        "lon_min": round(min(ecef_to_llh(r["x"], r["y"], r["z"])[1] for r in rows), 9),
        "lon_max": round(max(ecef_to_llh(r["x"], r["y"], r["z"])[1] for r in rows), 9),
        "built_at": __import__("datetime").datetime.now().astimezone().isoformat(
            timespec="seconds"),
    }
    out_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print()
    print("  epochs        :", meta["epochs"])
    print("  GPS week      :", meta["gps_week"])
    print("  time span     : %.1f s  (%.1f min)" % (meta["duration_s"], meta["duration_s"] / 60))
    print("  epoch spacing :", meta["dt_histogram"])
    print("  gaps > 1.5 s  :", meta["gap_count"])
    print("  Q histogram   :", meta["quality_histogram_kept"])
    print("  path length   : %.1f m" % path_len)
    print("  ENU origin    : lat %.9f  lon %.9f  h %.3f" % (lat0, lon0, h0))
    print()
    print("written:", out_csv.relative_to(REPO_ROOT) if out_csv.is_relative_to(REPO_ROOT) else out_csv)
    print("written:", out_meta.relative_to(REPO_ROOT) if out_meta.is_relative_to(REPO_ROOT) else out_meta)
    print()
    print("NOTE: this file is the answer key.  Keep it off any public share.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
