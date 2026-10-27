"""Read-side service: everything the web pages and the JSON API need.

All ranking rules live here so the HTTP layer stays dumb:

* rank only submissions with status ``ok`` and availability >= threshold;
* sort by 2D RMSE, tie-break with 3D RMSE, then group code;
* a group's row always shows its **current** file (latest wins, slide 34);
* if the current file is invalid, the last good result is shown next to it.
"""

from __future__ import annotations

import csv
import io
import math
import threading
from datetime import datetime

from .db import Database, format_time, now_iso
from .evaluator import evaluate
from .pipeline import Pipeline
from .submission import parse_submission_bytes

LEADERBOARD_COLUMNS = [
    ("rank", "Rank"),
    ("group", "Group"),
    ("method_label", "Version"),
    ("rmse2d", "2D RMSE (m)"),
    ("rmse3d", "3D RMSE (m)"),
    ("p95_2d", "2D P95 (m)"),
    ("availability", "Availability (%)"),
]


class Service:
    def __init__(self, cfg, db: Database, pipeline: Pipeline):
        self.cfg = cfg
        self.db = db
        self.pipeline = pipeline
        self._series_cache: dict[tuple, dict] = {}
        self._cache_lock = threading.Lock()
        self._version = 0
        # rank snapshot used to show movement since the previous board update
        self._board_lock = threading.Lock()
        self._board_version = -1
        self._cur_ranks: dict[str, int] = {}
        self._prev_ranks: dict[str, int] = {}

    # ------------------------------------------------------------------
    @property
    def challenge(self) -> str:
        return self.cfg.challenge.key

    def bump_version(self) -> int:
        self._version += 1
        return self._version

    def version(self) -> int:
        return self._version

    # ------------------------------------------------------------------
    def challenge_info(self) -> dict:
        gt = self.pipeline.gt
        meta = getattr(gt, "meta", {}) or {}
        return {
            "key": self.challenge,
            "name": self.cfg.challenge.name,
            "n_gt_epochs": gt.n if gt else 0,
            "gps_week": meta.get("gps_week") or ([gt.week[0]] if gt else []),
            "tow_start": meta.get("tow_start", gt.tow[0] if gt else None),
            "tow_end": meta.get("tow_end", gt.tow[-1] if gt else None),
            "duration_s": meta.get("duration_s"),
            "path_length_m": meta.get("path_length_m"),
            "availability_min": self.cfg.challenge.availability_min,
            "availability_min_pct": f"{self.cfg.challenge.availability_min:.0f} %",
            "enu_origin": [round(gt.origin_lat, 9), round(gt.origin_lon, 9),
                           round(gt.origin_h, 3)] if gt else None,
            "source": self.cfg.source_kind,
        }

    # ------------------------------------------------------------------
    def _row_from_submission(self, sub: dict | None, group: str) -> dict:
        base = {
            "group": group,
            "rank": None,
            "is_ranked": False,
            "method_label": "",
            "rmse2d": None,
            "rmse3d": None,
            "p95_2d": None,
            "availability": None,
            "n_matched": None,
            "n_gt": self.pipeline.n_gt,
            "status": "none",
            "unranked_reason": "No submission yet",
            "error_message": "",
            "suspicious": False,
            "suspicious_reason": "",
            "received_at": "",
            "received_at_iso": "",
            "submission_id": None,
            "prev_rank": None,
            "rank_delta": 0,
            "last_good": None,
        }
        if not sub:
            return base

        base.update({
            "method_label": sub.get("method_label") or "",
            "status": sub.get("status") or "invalid",
            "received_at": format_time(sub.get("received_at"), self.cfg.server.timezone),
            "received_at_iso": sub.get("received_at") or "",
            "submission_id": sub.get("id"),
            "suspicious": bool(sub.get("suspicious")),
            "suspicious_reason": sub.get("suspicious_reason") or "",
            "unranked_reason": sub.get("unranked_reason") or "",
            "error_message": sub.get("error_message") or "",
        })
        for field in ("rmse2d", "rmse3d", "p95_2d", "availability", "n_matched"):
            value = sub.get(field)
            base[field] = round(value, 4) if isinstance(value, float) else value
        return base

    # ------------------------------------------------------------------
    def leaderboard(self) -> dict:
        current = {r["group_code"]: r for r in
                   self.db.current_submissions(self.challenge, "benchmark")}
        declared = self.cfg.group_codes()
        extra = sorted(set(current) - set(declared))
        groups = declared + extra

        rows = [self._row_from_submission(current.get(g), g) for g in groups]

        # rank the current board
        ranked = [r for r in rows
                  if r["status"] == "ok" and r["rmse2d"] is not None
                  and (r["availability"] or 0.0) + 1e-9 >= self.cfg.challenge.availability_min]
        ranked.sort(key=lambda r: (r["rmse2d"], r["rmse3d"], r["group"]))
        for position, row in enumerate(ranked, start=1):
            row["rank"] = position
            row["is_ranked"] = True
            row["unranked_reason"] = ""
        ranked_groups = {r["group"] for r in ranked}
        for row in rows:
            if row["group"] in ranked_groups:
                continue
            if row["status"] == "ok" and row["availability"] is not None:
                row["unranked_reason"] = (
                    f"Availability {row['availability']:.1f} % is below the "
                    f"{self.cfg.challenge.availability_min:.0f} % threshold")
            elif row["status"] == "invalid":
                row["unranked_reason"] = row["error_message"] or "Invalid file"

        # movement since the previous board update
        current_ranks = {r["group"]: r["rank"] for r in rows if r["rank"]}
        with self._board_lock:
            if self._version != self._board_version:
                self._prev_ranks = dict(self._cur_ranks)
                self._board_version = self._version
            previous = dict(self._prev_ranks)
            self._cur_ranks = current_ranks

        for row in rows:
            before = previous.get(row["group"])
            row["prev_rank"] = before
            if before is not None and row["rank"]:
                row["rank_delta"] = before - row["rank"]
            elif before is not None and not row["rank"]:
                row["rank_delta"] = -1          # fell off the ranked board
            elif before is None and row["rank"]:
                row["rank_delta"] = 0           # new entry
            else:
                row["rank_delta"] = 0

        for row in rows:
            if row["status"] == "invalid":
                row["last_good"] = self._last_good(row["group"], exclude=row["submission_id"])

        return {
            "challenge": self.challenge_info(),
            "frozen": self.db.is_frozen(),
            "updated_at": format_time(now_iso(), self.cfg.server.timezone),
            "updated_at_iso": now_iso(),
            "version": self._version,
            "rows": rows,
            "n_ranked": len(ranked),
            "n_groups": len(groups),
            "n_submitted": sum(1 for r in rows if r["status"] in ("ok", "invalid")),
        }

    def _last_good(self, group: str, exclude: int | None = None) -> dict | None:
        for row in self.db.history(group, self.challenge, "benchmark", limit=30):
            if row["status"] != "ok" or row.get("id") == exclude:
                continue
            return {
                "rmse2d": row.get("rmse2d"),
                "rmse3d": row.get("rmse3d"),
                "availability": row.get("availability"),
                "received_at": format_time(row.get("received_at"),
                                           self.cfg.server.timezone),
            }
        return None

    # ------------------------------------------------------------------
    def group_detail(self, group: str) -> dict:
        current = self.db.latest_submission(group, self.challenge, "benchmark")
        board = self.leaderboard()
        row = next((r for r in board["rows"] if r["group"] == group), None)
        history = self.db.history(group, self.challenge, "benchmark", limit=200)
        owndata = self.db.latest_submission(group, self.challenge, "owndata")

        series = self._series(current) if current else None

        return {
            "group": group,
            "row": row,
            "challenge": board["challenge"],
            "frozen": board["frozen"],
            "series": series,
            "history": [self._history_row(h) for h in history],
            "owndata": self._history_row(owndata) if owndata else None,
            "n_gt": self.pipeline.n_gt,
        }

    def _history_row(self, sub: dict) -> dict:
        return {
            "id": sub.get("id"),
            "sha256": (sub.get("sha256") or "")[:12],
            "received_at": format_time(sub.get("received_at"),
                                       self.cfg.server.timezone),
            "filename": sub.get("filename") or "",
            "method_label": sub.get("method_label") or "",
            "status": sub.get("status"),
            "error_message": sub.get("error_message") or "",
            "rmse2d": _round(sub.get("rmse2d")),
            "rmse3d": _round(sub.get("rmse3d")),
            "p95_2d": _round(sub.get("p95_2d")),
            "availability": _round(sub.get("availability")),
            "n_matched": sub.get("n_matched"),
            "is_ranked": bool(sub.get("is_ranked")),
            "suspicious": bool(sub.get("suspicious")),
        }

    # ------------------------------------------------------------------
    def _series(self, sub: dict) -> dict | None:
        """Per-epoch errors for the plots - recomputed from the archive."""
        if sub.get('status') != 'ok':
            return None
        key = (sub.get("sha256"), self.pipeline.gt_revision,
               self.cfg.challenge.lenient_tolerance_s)
        with self._cache_lock:
            cached = self._series_cache.get(key)
            if cached is not None:
                return cached

        path = sub.get("source_path")
        if not path:
            return None
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            return None

        parsed = parse_submission_bytes(data, sub.get("filename") or "x.csv")
        if not parsed.ok:
            return None
        result = evaluate(self.pipeline.gt, parsed.epochs,
                          tolerance_s=self.cfg.challenge.lenient_tolerance_s,
                          availability_min=self.cfg.challenge.availability_min)
        if result.error_code:
            return None

        step = max(1, len(result.tow) // 1500)
        series = {
            "tow": [round(v, 1) for v in result.tow[::step]],
            "de": [round(v, 3) for v in result.de[::step]],
            "dn": [round(v, 3) for v in result.dn[::step]],
            "du": [round(v, 3) for v in result.du[::step]],
            "e2d": [round(v, 3) for v in result.e2d[::step]],
            "e3d": [round(v, 3) for v in result.e3d[::step]],
            "missing_tow": [round(v, 1) for v in result.missing_tow[:2000]],
            "cdf_e2d": _cdf(result.e2d),
            "de_mean": result.de_mean,
            "dn_mean": result.dn_mean,
            "du_mean": result.du_mean,
            "std2d": result.std2d,
            "n_matched": result.n_matched,
        }
        with self._cache_lock:
            if len(self._series_cache) > 60:
                self._series_cache.clear()
            self._series_cache[key] = series
        return series

    # ------------------------------------------------------------------
    def recent_events(self, limit: int = 25) -> list[dict]:
        events = []
        for event in self.db.recent_events(limit * 2):
            if event["kind"] not in ("upload", "invalid", "fairplay", "freeze",
                                     "unfreeze", "manual"):
                continue
            event["at_display"] = format_time(event["at"], self.cfg.server.timezone,
                                              with_date=False)
            events.append(event)
            if len(events) >= limit:
                break
        return events

    # ------------------------------------------------------------------
    def leaderboard_csv(self) -> str:
        board = self.leaderboard()
        rows = sorted(board["rows"],
                      key=lambda r: (r["rank"] is None, r["rank"] or 0))
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([label for _, label in LEADERBOARD_COLUMNS] + ["Status"])
        for row in rows:
            writer.writerow([
                row["rank"] if row["rank"] else "-",
                row["group"],
                row["method_label"] or "-",
                _fmt(row["rmse2d"]),
                _fmt(row["rmse3d"]),
                _fmt(row["p95_2d"]),
                _fmt(row["availability"]),
                "ranked" if row["is_ranked"]
                else (row["unranked_reason"] or row["status"]),
            ])
        return buffer.getvalue()

    def history_csv(self, group: str) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["uploaded_at", "filename", "method_label", "status",
                         "2D_RMSE_m", "3D_RMSE_m", "2D_P95_m", "availability_pct",
                         "n_matched", "n_gt_epochs", "ranked", "sha256_12"])
        for sub in reversed(self.db.history(group, self.challenge, "benchmark",
                                            limit=1000)):
            writer.writerow([
                sub.get("received_at"), sub.get("filename"),
                sub.get("method_label") or "", sub.get("status"),
                _fmt(sub.get("rmse2d")), _fmt(sub.get("rmse3d")),
                _fmt(sub.get("p95_2d")), _fmt(sub.get("availability")),
                sub.get("n_matched"), sub.get("n_gt"),
                "yes" if sub.get("is_ranked") else "no",
                (sub.get("sha256") or "")[:12],
            ])
        return buffer.getvalue()

    # ------------------------------------------------------------------
    def status(self) -> dict:
        return {
            "challenge": self.challenge_info(),
            "frozen": self.db.is_frozen(),
            "n_submissions": self.db.count_submissions(self.challenge),
            "server_time": format_time(now_iso(), self.cfg.server.timezone),
            "source_kind": self.cfg.source_kind,
        }

    def all_groups(self) -> list[str]:
        current = {r["group_code"] for r in
                   self.db.current_submissions(self.challenge, "benchmark")}
        return self.cfg.group_codes() + sorted(current - set(self.cfg.group_codes()))


def _round(value):
    if isinstance(value, float):
        return round(value, 4)
    return value


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        if math.isnan(value):
            return "-"
        return f"{value:.3f}"
    return str(value)


def _cdf(values: list[float], points: int = 200) -> list[list[float]]:
    if not values:
        return []
    data = sorted(values)
    step = max(1, len(data) // points)
    out = []
    for i in range(0, len(data), step):
        out.append([round(data[i], 3), round(100.0 * (i + 1) / len(data), 1)])
    if out and out[-1][1] < 100.0:
        out.append([round(data[-1], 3), 100.0])
    return out
