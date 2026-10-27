"""AAE4203 GNSS Benchmark Challenge - entry point.

    python run.py                 start the server with config.toml
    python run.py --check         validate config / ground truth / Drive, then exit
    python run.py --port 9000     override the port
    python run.py --no-poller     serve the existing database only
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from pathlib import Path

from app.config import load_config
from app.db import Database
from app.pipeline import Pipeline
from app.poller import Poller
from app.server import AppState, serve
from app.service import Service
from app.sources import SourceError, make_source


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def banner(cfg, state) -> None:
    host = "127.0.0.1" if cfg.server.host in ("0.0.0.0", "::") else cfg.server.host
    port = cfg.server.port
    ip = local_ip()
    line = "=" * 68
    info = state.service.challenge_info()
    weeks = ", ".join(str(w) for w in (info["gps_week"] or []))
    origin = info["enu_origin"]
    origin_text = (f"{origin[0]:.6f}, {origin[1]:.6f}" if origin
                   else "(no ground truth)")

    print(line)
    print(f"  {cfg.server.page_title}")
    print(line)
    print(f"  Leaderboard    http://{ip}:{port}/")
    print(f"  Projector      http://{ip}:{port}/tv")
    print(f"  Student rules  http://{ip}:{port}/rules")
    print(f"  Admin console  http://{ip}:{port}/admin")
    print(f"  Local          http://{host}:{port}/")
    print("-" * 68)
    print(f"  Challenge      {info['name']}")
    print(f"  Benchmark      {info['n_gt_epochs']} epochs, GPS week {weeks}")
    print(f"  ENU origin     {origin_text}")
    print(f"  Ranked when    availability >= {info['availability_min']:.0f} %")
    print(f"  Source         {state.poller.source_description}")
    print(f"  Polling        every {cfg.polling.interval_s:.0f} s")
    print(f"  Database       {cfg.db_path}")
    print(line)
    print("  Ctrl+C to stop")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GNSS benchmark leaderboard server")
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--no-poller", action="store_true",
                        help="do not watch for new uploads")
    parser.add_argument("--check", action="store_true",
                        help="validate everything and exit")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.port:
        cfg.server.port = args.port
    if args.host:
        cfg.server.host = args.host

    # -- ground truth -----------------------------------------------------
    if not cfg.gt_path.is_file():
        print("ground truth missing:", cfg.gt_path, file=sys.stderr)
        print("Build it first, e.g.", file=sys.stderr)
        print("    python tools/build_ground_truth.py UrbanNav_whampoa_raw.txt "
              "--format ie --name whampoa", file=sys.stderr)
        return 2

    db = Database(cfg.db_path)
    try:
        pipeline = Pipeline(cfg, db)
    except (ValueError, OSError) as exc:
        print(f"cannot load ground truth: {exc}", file=sys.stderr)
        return 2

    # -- source -----------------------------------------------------------
    source = None
    source_error = ""
    if not (args.no_poller or args.check):
        try:
            source = make_source(cfg)
            source.validate()
        except SourceError as exc:
            source_error = str(exc)
            source = None
    elif args.check:
        try:
            candidate = make_source(cfg)
            candidate.validate()
            source = candidate
        except SourceError as exc:
            source_error = str(exc)

    service = Service(cfg, db, pipeline)
    if not args.check:
        pipeline.rescore_current()

    if args.check:
        print("config        OK")
        print("ground truth  OK  %d epochs, week %s, origin %.6f/%.6f"
              % (pipeline.n_gt, pipeline.gt.meta.get("gps_week", "?"),
                 pipeline.gt.origin_lat, pipeline.gt.origin_lon))
        print("database      OK  %s" % cfg.db_path)
        print("source        %s" % ("OK  " + cfg.source_kind if source
                                    else "FAILED\n" + source_error))
        print("web assets    %s" % ("OK" if (cfg.web_dir / "index.html").is_file()
                                    else "MISSING (web/index.html)"))
        return 0 if source else 3

    class _NullPoller:
        source_description = source_error or "disabled"
        def status(self): return {"source": self.source_description,
                                  "last_poll_at": 0, "n_polls": 0,
                                  "n_ingested": 0, "last_error": source_error,
                                  "last_error_at": 0,
                                  "interval_s": cfg.polling.interval_s}
        def poll_once(self): return {"updated": []}
        _seen = {}
        def start(self): pass
        def stop(self): pass

    if source is None:
        poller = _NullPoller()
    else:
        def on_change(updated):
            service.bump_version()
            state.bus.publish({"type": "update", "version": service.version(),
                               "groups": [u.get("group") for u in updated]})

        poller = Poller(cfg, db, pipeline, source, on_change=on_change)

    state = AppState(cfg, db, pipeline, source, poller, service)

    if source_error:
        print("!" * 68, file=sys.stderr)
        print("SOURCE NOT AVAILABLE - the site will run, but no new uploads "
              "will be picked up:", file=sys.stderr)
        print(source_error, file=sys.stderr)
        print("!" * 68, file=sys.stderr)

    httpd = None
    for attempt in range(6):
        try:
            httpd = serve(state)
            break
        except OSError as exc:
            if attempt == 5:
                print(f"cannot bind {cfg.server.host}:{cfg.server.port}: {exc}",
                      file=sys.stderr)
                return 3
            cfg.server.port += 1
    if httpd is None:
        return 3

    print()
    banner(cfg, state)
    print()

    poller.start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nshutting down ...")
    finally:
        poller.stop()
        httpd.shutdown()
        httpd.server_close()
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
