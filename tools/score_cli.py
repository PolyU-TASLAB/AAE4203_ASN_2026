"""Score one CSV, or a whole folder of ``GXX_benchmark.csv`` files, offline.

Useful for the TA to check a submission without running the server, and for
regenerating the final leaderboard from the archived uploads.

Examples
--------
    python tools/score_cli.py demo_drive
    python tools/score_cli.py demo_drive/G07/G07_benchmark.csv
    python tools/score_cli.py demo_drive --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.config import load_config                       # noqa: E402
from app.evaluator import evaluate, is_ranked, load_ground_truth  # noqa: E402
from app.submission import parse_submission_file         # noqa: E402


def score_one(gt, path: Path, tolerance: float, availability_min: float) -> dict:
    parsed = parse_submission_file(path)
    row = {
        "file": str(path),
        "ok": False,
        "epochs": len(parsed.epochs),
        "availability": 0.0,
        "rmse2d": None,
        "rmse3d": None,
        "p95_2d": None,
        "is_ranked": False,
        "reason": "",
    }
    if not parsed.ok:
        row["reason"] = "; ".join(i.message for i in parsed.errors)
        return row
    result = evaluate(gt, parsed.epochs, tolerance_s=tolerance,
                      availability_min=availability_min)
    if result.error_code:
        row["reason"] = result.error_message
        return row
    ranked, reason = is_ranked(result, availability_min)
    row.update({
        "ok": True,
        "n_matched": result.n_matched,
        "n_missing": result.n_missing,
        "n_extra": result.n_extra,
        "n_lenient": result.n_lenient,
        "availability": result.availability,
        "rmse2d": result.rmse2d,
        "rmse3d": result.rmse3d,
        "p95_2d": result.p95_2d,
        "mean2d": result.mean2d,
        "max2d": result.max2d,
        "is_ranked": ranked,
        "reason": reason,
    })
    return row


GROUP_RE = re.compile(r"^(G\d{1,3})[_-]", re.IGNORECASE)


def collect(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    files = sorted(target.rglob("*_benchmark.csv"))
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="a CSV file or a folder to scan")
    ap.add_argument("--gt", default=None)
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--all", action="store_true", help="include unranked rows")
    args = ap.parse_args(argv)

    cfg = load_config()
    gt = load_ground_truth(args.gt or cfg.gt_path,
                           grid_s=cfg.challenge.match_grid_s,
                           max_quality=cfg.challenge.max_quality,
                           enu_origin=cfg.challenge.enu_origin)
    files = collect(Path(args.target))
    if not files:
        print(f"no *_benchmark.csv found under {args.target}", file=sys.stderr)
        return 2

    rows = []
    for path in files:
        match = GROUP_RE.match(path.name)
        row = score_one(gt, path, cfg.challenge.lenient_tolerance_s,
                        cfg.challenge.availability_min)
        row["group"] = match.group(1).upper() if match else path.parent.name
        rows.append(row)

    ranked = sorted([r for r in rows if r.get("is_ranked")],
                    key=lambda r: (r["rmse2d"], r["rmse3d"]))
    for position, row in enumerate(ranked, start=1):
        row["rank"] = position

    if args.as_json:
        print(json.dumps({"ground_truth_epochs": gt.n, "rows": rows}, indent=2))
        return 0

    print(f"ground truth: {args.gt or cfg.gt_path}  ({gt.n} epochs)")
    print(f"availability threshold: {cfg.challenge.availability_min:.0f} %")
    print()
    header = (f"{'Rank':>4}  {'Group':<6} {'2D RMSE':>9} {'3D RMSE':>9} "
              f"{'2D P95':>8} {'Avail':>7}  {'Matched':>8}")
    print(header)
    print("-" * len(header))
    for row in ranked:
        print(f"{row['rank']:>4}  {row['group']:<6} {row['rmse2d']:>9.3f} "
              f"{row['rmse3d']:>9.3f} {row['p95_2d']:>8.3f} "
              f"{row['availability']:>6.1f}% {row['n_matched']:>8}")
    others = [r for r in rows if not r.get("is_ranked")]
    if others and args.all:
        print()
        print("not ranked:")
        for row in others:
            print(f"  {row['group']:<6} {row['reason']}")
    elif others:
        print()
        print(f"{len(others)} submission(s) not ranked - use --all to list them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
