"""HTTP server: static pages, JSON API and a Server-Sent-Events live feed.

Standard library only - ``http.server`` on a thread pool.  One process, one
port, no build step.  Students just open ``http://<host>:8080/``.
"""

from __future__ import annotations

import json
import mimetypes
import queue
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .db import now_iso
from .service import Service

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
}


# ---------------------------------------------------------------------------
class EventBus:
    """Fan-out queue for SSE clients."""

    def __init__(self):
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        chan: queue.Queue = queue.Queue(maxsize=64)
        with self._lock:
            self._subscribers.add(chan)
        return chan

    def unsubscribe(self, chan: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(chan)

    def publish(self, event: dict) -> None:
        with self._lock:
            targets = list(self._subscribers)
        for chan in targets:
            try:
                chan.put_nowait(event)
            except queue.Full:
                pass

    @property
    def n_clients(self) -> int:
        with self._lock:
            return len(self._subscribers)


# ---------------------------------------------------------------------------
class AppState:
    def __init__(self, cfg, db, pipeline, source, poller, service: Service):
        self.cfg = cfg
        self.db = db
        self.pipeline = pipeline
        self.source = source
        self.poller = poller
        self.service = service
        self.bus = EventBus()
        self.started_at = time.time()


def make_handler(state: AppState):
    cfg = state.cfg
    web_dir: Path = cfg.web_dir

    class Handler(BaseHTTPRequestHandler):
        server_version = "GNSSBench/1.0"
        protocol_version = "HTTP/1.1"

        # -- plumbing ---------------------------------------------------
        def log_message(self, fmt, *args):        # noqa: A003
            if getattr(self, "_quiet", True):
                return
            super().log_message(fmt, *args)

        def _send_json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False,
                              default=_json_default).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text, status=200, content_type="text/plain; charset=utf-8",
                       extra_headers=None):
            body = text.encode("utf-8") if isinstance(text, str) else text
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _error(self, status, message):
            self._send_json({"ok": False, "error": message}, status=status)

        def _read_body(self, limit=40 * 1024 * 1024) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return b""
            if length > limit:
                raise ValueError(f"body too large ({length} bytes)")
            return self.rfile.read(length)

        # -- auth -------------------------------------------------------
        def _check_admin(self) -> bool:
            token = self.headers.get("X-Admin-Token") or ""
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token = token or (query.get("token") or [""])[0]
            return bool(token) and token == cfg.server.admin_token

        # -- routing ----------------------------------------------------
        def do_GET(self):                          # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            try:
                self._route_get(path, parsed)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:               # noqa: BLE001
                self._quiet = False
                self._error(500, f"{type(exc).__name__}: {exc}")

        def do_HEAD(self):                         # noqa: N802
            self.do_GET()

        def do_POST(self):                         # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            try:
                self._route_post(path, parsed)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:               # noqa: BLE001
                self._quiet = False
                self._error(500, f"{type(exc).__name__}: {exc}")

        # -- GET --------------------------------------------------------
        def _route_get(self, path, parsed):
            if path in ("/", "/index.html"):
                return self._serve_file(web_dir / "index.html")
            if path in ("/tv", "/tv.html"):
                return self._serve_file(web_dir / "tv.html")
            if path in ("/admin", "/admin.html"):
                return self._serve_file(web_dir / "admin.html")
            if path in ("/rules", "/rules.html"):
                return self._serve_file(web_dir / "rules.html")
            if path.startswith("/group/") and path.count("/") == 2:
                return self._serve_file(web_dir / "group.html")
            if path == "/api/leaderboard":
                return self._send_json(state.service.leaderboard())
            if path == "/api/leaderboard.csv":
                return self._send_text(
                    state.service.leaderboard_csv(),
                    content_type="text/csv; charset=utf-8",
                    extra_headers={"Content-Disposition":
                                   'attachment; filename="leaderboard.csv"'})
            if path == "/api/status":
                status = state.service.status()
                status["poller"] = state.poller.status()
                status["n_sse_clients"] = state.bus.n_clients
                status["uptime_s"] = round(time.time() - state.started_at, 1)
                return self._send_json(status)
            if path == "/api/recent":
                return self._send_json({"events": state.service.recent_events(20)})
            if path == "/api/events":
                return self._sse()
            if path.startswith("/api/group/"):
                rest = path[len("/api/group/"):]
                parts = [p for p in rest.split("/") if p]
                if not parts:
                    return self._error(404, "missing group code")
                group = parts[0].upper()
                if len(parts) == 1:
                    return self._send_json(state.service.group_detail(group))
                if parts[1] == "history.csv":
                    return self._send_text(
                        state.service.history_csv(group),
                        content_type="text/csv; charset=utf-8",
                        extra_headers={"Content-Disposition":
                                       f'attachment; filename="{group}_version_log.csv"'})
                return self._error(404, "unknown endpoint")
            if path == "/api/admin/state":
                if not self._check_admin():
                    return self._error(401, "invalid admin token")
                return self._send_json(self._admin_state())
            if path.startswith("/static/"):
                return self._serve_file(web_dir / path[len("/static/"):])
            if path == "/favicon.ico":
                return self._send_text("", status=204)
            return self._error(404, f"not found: {path}")

        # -- POST -------------------------------------------------------
        def _route_post(self, path, parsed):
            if not path.startswith("/api/admin/"):
                return self._error(404, "unknown endpoint")
            if not self._check_admin():
                return self._error(401, "invalid admin token")
            query = urllib.parse.parse_qs(parsed.query)

            if path == "/api/admin/rescan":
                state.poller._seen.clear()
                result = state.poller.poll_once()
                state.service.bump_version()
                state.bus.publish({"type": "update", "reason": "admin rescan"})
                return self._send_json({"ok": True, "result": result})

            if path == "/api/admin/freeze":
                frozen = (query.get("frozen") or ["1"])[0] not in ("0", "false", "no")
                state.db.set_setting("frozen", frozen)
                state.db.add_event("freeze" if frozen else "unfreeze",
                                   "Leaderboard frozen by the teacher" if frozen
                                   else "Leaderboard unfrozen")
                state.service.bump_version()
                state.bus.publish({"type": "freeze", "frozen": frozen})
                return self._send_json({"ok": True, "frozen": frozen})

            if path == "/api/admin/rescore":
                count = state.pipeline.rescore_current()
                state.service.bump_version()
                state.bus.publish({"type": "update", "reason": "rescore"})
                return self._send_json({"ok": True, "rescored": count})

            if path == "/api/admin/upload":
                group = (query.get("group") or [""])[0].upper()
                kind = (query.get("kind") or ["benchmark"])[0]
                filename = (query.get("filename") or [f"{group}_{kind}.csv"])[0]
                if not cfg.parses_group(group):
                    return self._error(400, f"bad group code: {group!r}")
                try:
                    body = self._read_body()
                except ValueError as exc:
                    return self._error(413, str(exc))
                if not body:
                    return self._error(400, "empty upload")
                outcome = state.pipeline.ingest(
                    group=group, kind=kind, filename=filename, data=body,
                    source="manual",
                    method_label=(query.get("label") or [""])[0] or None)
                state.db.add_event("manual", f"{filename} uploaded manually",
                                   group_code=group)
                state.service.bump_version()
                state.bus.publish({"type": "update", "reason": "manual upload"})
                return self._send_json({"ok": True, "outcome": outcome})

            if path == "/api/admin/withdraw":
                group = (query.get("group") or [""])[0].upper()
                if not state.db.withdraw_current(group, cfg.challenge.key):
                    return self._error(404, f"{group} has no submission")
                state.db.add_event("manual", f"{group}: current submission withdrawn",
                                   group_code=group)
                state.service.bump_version()
                state.bus.publish({"type": "update", "reason": "withdraw"})
                return self._send_json({"ok": True})

            if path == "/api/admin/reload-gt":
                state.pipeline.load_ground_truth()
                state.pipeline.rescore_current()
                state.service.bump_version()
                state.bus.publish({"type": "update", "reason": "ground truth reloaded"})
                return self._send_json({"ok": True,
                                        "n_gt": state.pipeline.n_gt})

            return self._error(404, "unknown admin endpoint")

        # -- helpers ----------------------------------------------------
        def _admin_state(self):
            service = state.service
            return {
                "ok": True,
                "challenge": service.challenge_info(),
                "frozen": state.db.is_frozen(),
                "source_kind": cfg.source_kind,
                "source_description": state.poller.source_description,
                "poller": state.poller.status(),
                "n_submissions": state.db.count_submissions(cfg.challenge.key),
                "n_sse_clients": state.bus.n_clients,
                "groups": service.all_groups(),
                "events": service.recent_events(60),
                "rows": service.leaderboard()["rows"],
                "config": {
                    "gt_file": str(cfg.gt_path),
                    "availability_min": cfg.challenge.availability_min,
                    "max_quality": cfg.challenge.max_quality,
                    "match_grid_s": cfg.challenge.match_grid_s,
                    "lenient_tolerance_s": cfg.challenge.lenient_tolerance_s,
                    "poll_interval_s": cfg.polling.interval_s,
                    "n_groups": cfg.groups.count,
                },
                "server_time": now_iso(),
            }

        def _serve_file(self, path: Path):
            try:
                resolved = path.resolve()
                resolved.relative_to(web_dir.resolve())
            except (ValueError, OSError):
                return self._error(403, "forbidden")
            if not resolved.is_file():
                return self._error(404, f"missing file: {path.name}")
            content_type = STATIC_TYPES.get(resolved.suffix.lower())
            if content_type is None:
                content_type = mimetypes.guess_type(resolved.name)[0] \
                    or "application/octet-stream"
            data = resolved.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _sse(self):
            chan = state.bus.subscribe()
            self.close_connection = True
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(b"retry: 3000\n\n")
                self.wfile.write(f"data: {json.dumps({'type': 'hello', 'version': state.service.version()})}\n\n".encode())
                self.wfile.flush()
                last = time.time()
                while True:
                    try:
                        event = chan.get(timeout=15)
                        payload = json.dumps(event, default=_json_default)
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    if time.time() - last > 3600:
                        break
                    last = time.time()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                state.bus.unsubscribe(chan)

    return Handler


def _json_default(value):
    if isinstance(value, float):
        if value != value:
            return None
        return value
    return str(value)


def serve(state: AppState) -> ThreadingHTTPServer:
    handler = make_handler(state)
    httpd = ThreadingHTTPServer((state.cfg.server.host, state.cfg.server.port), handler)
    httpd.daemon_threads = True
    return httpd
