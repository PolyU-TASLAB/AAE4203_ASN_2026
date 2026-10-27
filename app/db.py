"""SQLite persistence.

One file (``data/competition.db``) holds every submission ever seen, its
metrics, the version log students need for their report, and an event feed for
the admin console.  Nothing is deleted, so the final leaderboard can always be
reconstructed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS submissions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    group_code        TEXT    NOT NULL,
    challenge_key     TEXT    NOT NULL DEFAULT 'benchmark',
    kind              TEXT    NOT NULL DEFAULT 'benchmark',
    source            TEXT    NOT NULL DEFAULT 'local_mirror',
    source_path       TEXT,
    filename          TEXT,
    sha256            TEXT,
    size_bytes        INTEGER,
    received_at       TEXT    NOT NULL,
    method_label      TEXT,
    status            TEXT    NOT NULL,
    error_code        TEXT,
    error_message     TEXT,
    n_epochs          INTEGER,
    warnings          TEXT,
    n_gt              INTEGER,
    n_matched         INTEGER,
    n_missing         INTEGER,
    n_extra           INTEGER,
    n_lenient         INTEGER,
    availability      REAL,
    rmse2d            REAL,
    rmse3d            REAL,
    p95_2d            REAL,
    p95_3d            REAL,
    mean2d            REAL,
    mean3d            REAL,
    std2d             REAL,
    max2d             REAL,
    max3d             REAL,
    de_mean           REAL,
    dn_mean           REAL,
    du_mean           REAL,
    is_ranked         INTEGER DEFAULT 0,
    unranked_reason   TEXT,
    suspicious        INTEGER DEFAULT 0,
    suspicious_reason TEXT,
    is_current        INTEGER DEFAULT 1,
    evaluated_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_sub_group
    ON submissions (group_code, challenge_key, kind, is_current);
CREATE INDEX IF NOT EXISTS idx_sub_sha ON submissions (sha256);
CREATE INDEX IF NOT EXISTS idx_sub_time ON submissions (received_at);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    group_code TEXT,
    message    TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_at ON events (at DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

METRIC_FIELDS = [
    "n_gt", "n_matched", "n_missing", "n_extra", "n_lenient",
    "availability", "rmse2d", "rmse3d", "p95_2d", "p95_3d",
    "mean2d", "mean3d", "std2d", "max2d", "max3d",
    "de_mean", "dn_mean", "du_mean",
]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- low level ---------------------------------------------------------
    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    # -- submissions -------------------------------------------------------
    def insert_submission(self, record: dict[str, Any]) -> int:
        """Insert a submission and make it the current one for its group."""
        with self._lock:
            self._conn.execute(
                "UPDATE submissions SET is_current = 0 "
                "WHERE group_code = ? AND challenge_key = ? AND kind = ?",
                (record["group_code"], record.get("challenge_key", "benchmark"),
                 record.get("kind", "benchmark")))
            columns = [c for c in record if c != "id"]
            placeholders = ",".join("?" for _ in columns)
            cursor = self._conn.execute(
                f"INSERT INTO submissions ({','.join(columns)}) "
                f"VALUES ({placeholders})",
                tuple(_encode(record[c]) for c in columns))
            self._conn.commit()
            return int(cursor.lastrowid)

    def current_submissions(self, challenge_key: str = "benchmark",
                            kind: str = "benchmark") -> list[dict]:
        rows = self._query(
            "SELECT * FROM submissions WHERE challenge_key = ? AND kind = ? "
            "AND is_current = 1", (challenge_key, kind))
        return [dict(r) for r in rows]

    def latest_submission(self, group: str, challenge_key: str = "benchmark",
                          kind: str = "benchmark") -> dict | None:
        rows = self._query(
            "SELECT * FROM submissions WHERE group_code = ? AND challenge_key = ? "
            "AND kind = ? AND is_current = 1", (group, challenge_key, kind))
        return dict(rows[0]) if rows else None

    def withdraw_current(self, group: str, challenge_key: str) -> bool:
        """Atomically step back from the current submission, not the newest archive."""
        with self._lock, self._conn:
            current = self._conn.execute(
                "SELECT id FROM submissions WHERE group_code=? AND challenge_key=? "
                "AND kind='benchmark' AND is_current=1", (group, challenge_key)).fetchone()
            if current is None:
                return False
            previous = self._conn.execute(
                "SELECT id FROM submissions WHERE group_code=? AND challenge_key=? "
                "AND kind='benchmark' AND id<? ORDER BY id DESC LIMIT 1",
                (group, challenge_key, current['id'])).fetchone()
            self._conn.execute('UPDATE submissions SET is_current=0 WHERE id=?',
                               (current['id'],))
            if previous:
                self._conn.execute('UPDATE submissions SET is_current=1 WHERE id=?',
                                   (previous['id'],))
            return True

    def history(self, group: str, challenge_key: str = "benchmark",
                kind: str = "benchmark", limit: int = 500) -> list[dict]:
        rows = self._query(
            "SELECT * FROM submissions WHERE group_code = ? AND challenge_key = ? "
            "AND kind = ? ORDER BY id DESC LIMIT ?",
            (group, challenge_key, kind, limit))
        return [dict(r) for r in rows]

    def submission_by_id(self, submission_id: int) -> dict | None:
        rows = self._query("SELECT * FROM submissions WHERE id = ?", (submission_id,))
        return dict(rows[0]) if rows else None

    def count_submissions(self, challenge_key: str = "benchmark") -> int:
        rows = self._query(
            "SELECT COUNT(*) AS n FROM submissions WHERE challenge_key = ?",
            (challenge_key,))
        return int(rows[0]["n"]) if rows else 0

    def groups_sharing_sha(self, sha256: str, exclude_group: str) -> list[str]:
        rows = self._query(
            "SELECT DISTINCT group_code FROM submissions WHERE sha256 = ? "
            "AND group_code <> ?", (sha256, exclude_group))
        return [r["group_code"] for r in rows]

    def mark_current_flags(self, challenge_key: str, availability_min: float) -> None:
        """Recompute is_ranked on the current rows (config may have changed)."""
        rows = self.current_submissions(challenge_key)
        for row in rows:
            ok = row["status"] == "ok" and row["availability"] is not None
            ranked = bool(ok and row["availability"] + 1e-9 >= availability_min)
            if ranked != bool(row["is_ranked"]):
                self._execute("UPDATE submissions SET is_ranked = ? WHERE id = ?",
                              (1 if ranked else 0, row["id"]))

    # -- events ------------------------------------------------------------
    def add_event(self, kind: str, message: str, group_code: str | None = None,
                  at: str | None = None) -> None:
        self._execute(
            "INSERT INTO events (at, kind, group_code, message) VALUES (?,?,?,?)",
            (at or now_iso(), kind, group_code, message))

    def recent_events(self, limit: int = 40) -> list[dict]:
        rows = self._query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # -- settings ----------------------------------------------------------
    def set_setting(self, key: str, value: Any) -> None:
        self._execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)))

    def get_setting(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM settings WHERE key = ?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except (TypeError, ValueError):
            return default

    def is_frozen(self) -> bool:
        return bool(self.get_setting("frozen", False))


def _encode(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    if isinstance(value, bool):
        return int(value)
    return value


def local_zone(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:                      # pragma: no cover - missing tzdata
        return None


def format_time(iso_string: str | None, zone_name: str = "Asia/Hong_Kong",
                with_date: bool = True) -> str:
    if not iso_string:
        return "-"
    try:
        moment = datetime.fromisoformat(iso_string)
    except ValueError:
        return iso_string
    zone = local_zone(zone_name)
    if zone is not None:
        moment = moment.astimezone(zone)
    return moment.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")
