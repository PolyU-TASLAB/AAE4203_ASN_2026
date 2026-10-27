"""Rehearse a lab session: feed uploads in one at a time and watch the board.

Copies ``GXX_benchmark.csv`` files into the watched folder on a timer and prints
the leaderboard after each one, exactly as the projector page would show it.
Use it to check the ranking rules, the availability gate and the live updates
before the real session.

    python tools/rehearse.py --source demo_drive --target drive_mirror --interval 8

The server must be running and pointed at ``--target``.  Press Ctrl+C to stop;
files already copied stay there.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def render(board: dict) -> str:
    rows = board["rows"]
    ranked = sorted([r for r in rows if r["is_ranked"]], key=lambda r: r["rank"])
    others = [r for r in rows if not r["is_ranked"]]

    out = []
    out.append(f"  ranked {board['n_ranked']}/{board['n_groups']}"
               f"    updated {board['updated_at']}")
    out.append("  " + "-" * 76)
    out.append(f"  {'#':>3}  {'grp':<5}{'2D RMSE':>9}{'3D RMSE':>9}"
               f"{'2D P95':>8}{'avail':>8}   movement")
    for row in ranked:
        delta = row.get("rank_delta") or 0
        arrow = "same" if delta == 0 else (f"up {delta}" if delta > 0 else f"down {abs(delta)}")
        out.append(f"  {row['rank']:>3}  {row['group']:<5}{row['rmse2d']:>9.3f}"
                   f"{row['rmse3d']:>9.3f}{row['p95_2d']:>8.3f}"
                   f"{row['availability']:>7.1f}%   {arrow}")
    if others:
        out.append("")
        for row in others:
            label = row["group"]
            if row["status"] == "none":
                continue
            reason = row["unranked_reason"] or row["status"]
            out.append(f"  ---  {label:<5}{reason[:58]}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default="demo_drive")
    parser.add_argument("--target", default="drive_mirror")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--interval", type=float, default=8.0,
                        help="seconds between uploads (default 8)")
    parser.add_argument("--order", default=None,
                        help="comma separated group order, e.g. G11,G03,G07")
    parser.add_argument("--once", action="store_true", help="copy everything, no delay")
    parser.add_argument("--keep-existing", action="store_true",
                        help="do not clear the target folder first")
    args = parser.parse_args(argv)

    source = (REPO_ROOT / args.source) if not Path(args.source).is_absolute() \
        else Path(args.source)
    target = (REPO_ROOT / args.target) if not Path(args.target).is_absolute() \
        else Path(args.target)
    if not source.is_dir():
        print(f"source folder not found: {source}", file=sys.stderr)
        return 2
    target.mkdir(parents=True, exist_ok=True)

    files = sorted(source.glob("G*/*_benchmark.csv"))
    if not files:
        print(f"no G*/*_benchmark.csv under {source}", file=sys.stderr)
        return 2

    by_group: dict[str, Path] = {}
    for path in files:
        by_group[path.name.split("_")[0].upper()] = path

    order = list(by_group)
    if args.order:
        order = [g.strip().upper() for g in args.order.split(",") if g.strip()]
    if not args.keep_existing:
        for child in target.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)

    print(f"source   {source}")
    print(f"target   {target}")
    print(f"server   {args.url}")
    print(f"uploads  {len(order)} files, {args.interval:g} s apart")
    print()

    interval = 0 if args.once else args.interval
    for index, group in enumerate(order, start=1):
        path = by_group[group]
        folder = target / group
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, folder / path.name)

        version_file = path.parent / f"{group}_version.txt"
        if version_file.is_file():
            shutil.copy2(version_file, folder / version_file.name)

        print("=" * 82)
        print(f"[{index}/{len(order)}] uploaded {group}  "
              f"({path.stat().st_size // 1024} KiB)")
        print("=" * 82)

        time.sleep(min(interval, max(2.0, interval)))
        try:
            board = fetch(f"{args.url}/api/leaderboard")
            print(render(board))
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"  cannot reach the server: {exc}")
        print()
        if index < len(order) and interval:
            time.sleep(interval)

    print("rehearsal finished. Open the projector page to see the final board.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
