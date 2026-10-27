"""Run the configured Drive mirror with a separate, persistent test database."""
import argparse
import json
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import Config, load_config
from app.db import Database
from app.pipeline import Pipeline
from app.poller import Poller
from app.service import Service
from app.server import AppState, serve
from app.sources import make_source


class TestConfig(Config):
    @property
    def data_dir(self):
        return ROOT / '.review' / 'drive-live' / 'data'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    cfg = load_config()
    cfg.__class__ = TestConfig
    cfg.server.host, cfg.server.port = args.host, args.port
    cfg.server.admin_token = secrets.token_urlsafe(32)
    source = make_source(cfg)
    source.validate()
    db = Database(cfg.db_path)
    pipeline = Pipeline(cfg, db)
    pipeline.rescore_current()
    service = Service(cfg, db, pipeline)
    def changed(updates):
        service.bump_version()
        state.bus.publish({'type':'update', 'version':service.version(),
                           'groups':[item['group'] for item in updates]})
        print(json.dumps(updates, ensure_ascii=True), flush=True)
    poller = Poller(cfg, db, pipeline, source, on_change=changed)
    state = AppState(cfg, db, pipeline, source, poller, service)
    httpd = serve(state)
    print('Drive mirror test server; isolated database: '+str(cfg.db_path), flush=True)
    print('Listening on '+args.host+':'+str(args.port), flush=True)
    print('Polling every %s s; settle %s s' % (cfg.polling.interval_s,cfg.polling.settle_s), flush=True)
    poller.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        httpd.server_close()
        db.close()


if __name__ == '__main__':
    main()
