"""Background poller: notice changed CSV files and score them.

The loop is deliberately boring - scan the source, compare each file's cheap
signature with the last one seen, read and ingest only what changed.  Any
exception is caught and reported, because the poller must never die in the
middle of a lab session.
"""

from __future__ import annotations

import threading
import time
import traceback

from .db import Database
from .pipeline import Pipeline
from .sources.base import Source, SourceError


class Poller:
    def __init__(self, cfg, db: Database, pipeline: Pipeline, source: Source,
                 on_change=None):
        self.cfg = cfg
        self.db = db
        self.pipeline = pipeline
        self.source = source
        self.on_change = on_change

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: dict[tuple[str, str], str] = {}

        self.last_poll_at: float = 0.0
        self.last_error: str = ""
        self.last_error_at: float = 0.0
        self.n_polls = 0
        self.n_ingested = 0
        self.source_description = source.describe()

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="poller",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        # a short delay so the HTTP server is up before the first scan
        self._stop.wait(0.4)
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:                            # noqa: BLE001
                self._record_error(traceback.format_exc(limit=3))
            interval = max(1.0, float(self.cfg.polling.interval_s))
            self._stop.wait(interval)

    # ------------------------------------------------------------------
    def poll_once(self) -> dict:
        started = time.time()
        self.n_polls += 1
        self.last_poll_at = time.time()

        if self.db.is_frozen():
            return {"frozen": True, "updated": []}

        try:
            scan = self.source.scan()
        except SourceError as exc:
            self._record_error(str(exc))
            return {"error": str(exc), "updated": []}

        if scan.errors:
            self._record_error("; ".join(scan.errors))

        updated: list[dict] = []
        now = time.time()
        settle = max(0.0, float(self.cfg.polling.settle_s))

        counts = {}
        for ref in scan.refs:
            counts[ref.key()] = counts.get(ref.key(), 0) + 1
        duplicates = {key for key, count in counts.items() if count > 1}
        if duplicates:
            self._record_error('Duplicate submission sources: ' + ', '.join(
                f'{group}/{kind}' for group, kind in sorted(duplicates)))

        for ref in scan.refs:
            key = ref.key()
            if key in duplicates:
                continue
            if self._seen.get(key) == ref.signature:
                continue
            # never read a file that is still being written / synced
            if ref.source == "local_mirror" and settle > 0 \
                    and now - ref.modified < settle:
                continue
            try:
                data = self.source.read(ref)
            except SourceError as exc:
                self._record_error(str(exc))
                continue

            try:
                outcome = self.pipeline.ingest(
                    group=ref.group,
                    kind=ref.kind,
                    filename=ref.filename,
                    data=data,
                    source=ref.source,
                    method_label=ref.method_label,
                )
            except Exception:                            # noqa: BLE001
                self._record_error(
                    f"failed to ingest {ref.group}/{ref.filename}:\n"
                    + traceback.format_exc(limit=3))
                continue

            self._seen[key] = ref.signature
            if outcome.get("status") in ("ok", "invalid", "updated"):
                self.n_ingested += 1
                updated.append(outcome)

        elapsed = time.time() - started
        if len(updated) > 0 and self.on_change:
            try:
                self.on_change(updated)
            except Exception:                            # noqa: BLE001
                pass

        return {"updated": updated,
                "scanned": len(scan.refs),
                "groups_seen": sorted(scan.groups_seen),
                "elapsed_ms": round(elapsed * 1000, 1)}

    # ------------------------------------------------------------------
    def _record_error(self, message: str) -> None:
        """Log a source/pipeline problem, but do not spam the event feed."""
        now = time.time()
        if message == self.last_error and now - self.last_error_at < 120:
            self.last_error_at = now
            return
        self.last_error = message
        self.last_error_at = now
        try:
            self.db.add_event("poll_error", message.strip()[:1000])
        except Exception:                                # noqa: BLE001
            pass

    def status(self) -> dict:
        return {
            "source": self.source_description,
            "last_poll_at": self.last_poll_at,
            "n_polls": self.n_polls,
            "n_ingested": self.n_ingested,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "interval_s": self.cfg.polling.interval_s,
        }
