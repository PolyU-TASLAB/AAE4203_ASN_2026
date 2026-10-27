"""Turn raw uploaded bytes into a stored, scored submission.

This is the only place that writes to the database from the polling side, so
the sequence is: parse -> evaluate -> fair-play -> archive -> store -> event.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path

from . import fairplay
from .db import Database, METRIC_FIELDS, now_iso
from .evaluator import evaluate, is_ranked, load_ground_truth
from .submission import parse_submission_bytes

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class Pipeline:
    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db
        self.gt = None
        self.load_ground_truth()

    # ------------------------------------------------------------------
    def load_ground_truth(self) -> None:
        self.gt = load_ground_truth(
            self.cfg.gt_path,
            grid_s=self.cfg.challenge.match_grid_s,
            max_quality=self.cfg.challenge.max_quality,
            enu_origin=self.cfg.challenge.enu_origin,
        )
        self.gt_revision = getattr(self, 'gt_revision', 0) + 1

    @property
    def n_gt(self) -> int:
        return self.gt.n if self.gt else 0

    # ------------------------------------------------------------------
    def ingest(self, *, group: str, kind: str, filename: str, data: bytes,
               source: str, method_label: str | None = None,
               size: int | None = None, extra_event: str | None = None) -> dict:
        """Store one uploaded file.  Returns a small result dict."""
        challenge = self.cfg.challenge.key
        parsed = parse_submission_bytes(data, filename)

        previous = self.db.latest_submission(group, challenge, kind)
        if previous and previous.get("sha256") == parsed.sha256:
            if method_label is not None and method_label != previous.get('method_label'):
                self.db._execute('UPDATE submissions SET method_label=? WHERE id=?',
                                 (method_label, previous['id']))
                self.db.add_event('upload', 'Method label updated', group_code=group)
                return {'status': 'updated', 'group': group, 'kind': kind,
                        'submission_id': previous['id']}
            return {"status": "skipped", "group": group, "kind": kind,
                    "sha256": parsed.sha256,
                    "reason": "identical to the current submission"}

        archive_path = self._archive(group, filename, parsed.sha256, data)
        received = now_iso()

        record: dict = {
            "group_code": group,
            "challenge_key": challenge,
            "kind": kind,
            "source": source,
            "source_path": str(archive_path),
            "filename": filename,
            "sha256": parsed.sha256,
            "size_bytes": len(data),
            "received_at": received,
            "method_label": (method_label if method_label is not None else
                             (previous or {}).get("method_label") or ""),
            "n_epochs": len(parsed.epochs),
            "warnings": [w.as_dict() for w in parsed.warnings],
            "evaluated_at": now_iso(),
        }

        label = record["method_label"] or "no method label"

        # -- invalid file --------------------------------------------------
        if not parsed.ok:
            message = "; ".join(i.message for i in parsed.errors)
            record.update({
                "status": "invalid",
                "error_code": parsed.errors[0].code if parsed.errors else "E_UNKNOWN",
                "error_message": message,
                "is_ranked": 0,
                "unranked_reason": message,
            })
            submission_id = self.db.insert_submission(record)
            self.db.add_event("invalid", message, group_code=group)
            return {"status": "invalid", "group": group, "kind": kind,
                    "submission_id": submission_id, "message": message,
                    "filename": filename}

        # -- owndata: format check only, never ranked -----------------------
        if kind != "benchmark":
            record.update({"status": "ok", "is_ranked": 0,
                           "unranked_reason": "Own-data submission (format checked only)"})
            submission_id = self.db.insert_submission(record)
            self.db.add_event(
                "upload", f"{filename} received ({len(parsed.epochs)} epochs, "
                "own data - format check only)", group_code=group)
            return {"status": "ok", "group": group, "kind": kind,
                    "submission_id": submission_id,
                    "message": "own-data file accepted (not ranked)"}

        # -- benchmark: evaluate -------------------------------------------
        result = evaluate(self.gt, parsed.epochs,
                          tolerance_s=self.cfg.challenge.lenient_tolerance_s,
                          availability_min=self.cfg.challenge.availability_min)

        if result.error_code:
            record.update({
                "status": "invalid",
                "error_code": result.error_code,
                "error_message": result.error_message,
                "is_ranked": 0,
                "unranked_reason": result.error_message,
            })
            submission_id = self.db.insert_submission(record)
            self.db.add_event("invalid", result.error_message, group_code=group)
            return {"status": "invalid", "group": group, "kind": kind,
                    "submission_id": submission_id,
                    "message": result.error_message}

        ranked, reason = is_ranked(result, self.cfg.challenge.availability_min)
        suspicious, suspicious_reason = fairplay.inspect(
            self.cfg, self.db, group, result, parsed.sha256)

        record.update({
            "status": "ok",
            "error_code": "",
            "error_message": "",
            "n_gt": result.n_gt,
            "n_matched": result.n_matched,
            "n_missing": result.n_missing,
            "n_extra": result.n_extra,
            "n_lenient": result.n_lenient,
            "availability": result.availability,
            "rmse2d": result.rmse2d,
            "rmse3d": result.rmse3d,
            "p95_2d": result.p95_2d,
            "p95_3d": result.p95_3d,
            "mean2d": result.mean2d,
            "mean3d": result.mean3d,
            "std2d": result.std2d,
            "max2d": result.max2d,
            "max3d": result.max3d,
            "de_mean": result.de_mean,
            "dn_mean": result.dn_mean,
            "du_mean": result.du_mean,
            "is_ranked": 1 if ranked else 0,
            "unranked_reason": reason,
            "suspicious": 1 if suspicious else 0,
            "suspicious_reason": suspicious_reason,
        })
        submission_id = self.db.insert_submission(record)

        text = (f"{filename} evaluated: 2D RMSE {result.rmse2d:.2f} m, "
                f"3D RMSE {result.rmse3d:.2f} m, availability "
                f"{result.availability:.1f} %")
        if not ranked:
            text += f" - not ranked ({reason})"
        self.db.add_event("upload", text, group_code=group)
        if suspicious:
            self.db.add_event("fairplay", suspicious_reason, group_code=group)

        return {
            "status": "ok",
            "group": group,
            "kind": kind,
            "submission_id": submission_id,
            "ranked": ranked,
            "rmse2d": result.rmse2d,
            "rmse3d": result.rmse3d,
            "availability": result.availability,
            "message": text,
        }

    # ------------------------------------------------------------------
    def _archive(self, group: str, filename: str, sha256: str,
                 data: bytes) -> Path:
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        safe = SAFE_NAME.sub("_", Path(filename).name) or "submission.csv"
        folder = self.cfg.archive_dir / group
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{stamp}_{sha256[:8]}_{safe}"
        path.write_bytes(data)
        return path

    # ------------------------------------------------------------------
    def rescore_current(self) -> int:
        """Re-evaluate every current benchmark submission from its archive."""
        count = 0
        for row in self.db.current_submissions(self.cfg.challenge.key, "benchmark"):
            updates = {field: None for field in METRIC_FIELDS}
            updates.update(status="invalid", is_ranked=0, suspicious=0,
                           suspicious_reason="", evaluated_at=now_iso())
            try:
                data = Path(row.get("source_path") or "").read_bytes()
                parsed = parse_submission_bytes(data, row.get("filename") or "x.csv")
                if not parsed.ok:
                    code = parsed.errors[0].code
                    message = "; ".join(issue.message for issue in parsed.errors)
                else:
                    result = evaluate(self.gt, parsed.epochs,
                                      tolerance_s=self.cfg.challenge.lenient_tolerance_s,
                                      availability_min=self.cfg.challenge.availability_min)
                    code, message = result.error_code, result.error_message
                    if not code:
                        ranked, reason = is_ranked(result, self.cfg.challenge.availability_min)
                        suspicious, suspicion = fairplay.inspect(
                            self.cfg, self.db, row["group_code"], result, parsed.sha256)
                        updates.update({field: getattr(result, field) for field in METRIC_FIELDS})
                        updates.update(status="ok", is_ranked=int(ranked),
                                       unranked_reason=reason, suspicious=int(suspicious),
                                       suspicious_reason=suspicion)
            except OSError as exc:
                code, message = "E_ARCHIVE", f"Cannot read archived submission: {exc}"
            updates.update(error_code=code or "", error_message=message or "")
            if code:
                updates["unranked_reason"] = message
            fields = ", ".join(f"{field}=?" for field in updates)
            self.db._execute(f"UPDATE submissions SET {fields} WHERE id=?",
                             (*updates.values(), row["id"]))
            count += 1
        return count
