"""Isolated integration audit. Never writes to the classroom database or mirror.

Run with Python 3.11+: python tools/review_smoke.py
Preview: python tools/review_smoke.py --serve --port 8080
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import Config, load_config
from app.db import Database
from app.pipeline import Pipeline
from app.poller import Poller
from app.service import Service
from app.server import AppState, serve
from app.sources.local_mirror import LocalMirrorSource


class AuditConfig(Config):
    @property
    def data_dir(self):
        return self.audit_root / 'data'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--host', default='127.0.0.1', help='Preview bind address')
    args = parser.parse_args()
    output = ROOT / '.review'
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gnss-audit-', dir=output) as folder:
        cfg = load_config()
        cfg.__class__ = AuditConfig
        cfg.audit_root = Path(folder)
        cfg.server.host = args.host if args.serve else '127.0.0.1'
        cfg.server.port = args.port if args.serve else 0
        cfg.server.admin_token = secrets.token_urlsafe(24)
        cfg.local_mirror.root = str(cfg.audit_root / 'mirror')
        shutil.copytree(ROOT / 'demo_drive', cfg.local_mirror.root)
        cfg.polling.settle_s = 0
        cfg.polling.interval_s = 1
        db = Database(cfg.db_path)
        pipeline = Pipeline(cfg, db)
        service = Service(cfg, db, pipeline)
        source = LocalMirrorSource(cfg)
        source.validate()
        def changed(updates):
            service.bump_version()
            state.bus.publish({'type': 'update', 'groups': [x['group'] for x in updates]})
        poller = Poller(cfg, db, pipeline, source, on_change=changed)
        state = AppState(cfg, db, pipeline, source, poller, service)
        httpd = serve(state)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connect_host = '127.0.0.1' if cfg.server.host == '0.0.0.0' else cfg.server.host
        base = 'http://%s:%d' % (connect_host, httpd.server_address[1])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        checks = []
        def record(name, passed, detail=None):
            checks.append(dict(name=name, passed=bool(passed), detail=detail))
            print(('PASS ' if passed else 'FAIL ') + name + ': ' + str(detail), flush=True)
        def request(path, body=None, admin=False):
            headers = {'X-Admin-Token': cfg.server.admin_token} if admin else {}
            req = urllib.request.Request(base + path, data=body, headers=headers)
            try:
                with opener.open(req, timeout=5) as response:
                    raw = response.read()
                    return response.status, json.loads(raw) if 'application/json' in response.headers.get('Content-Type','') else raw
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode()
        def row(group):
            return next(r for r in request('/api/leaderboard')[1]['rows'] if r['group'] == group)
        def upload(group, content):
            return request('/api/admin/upload?group='+group, content, True)
        try:
            poller.poll_once()
            board = request('/api/leaderboard')[1]
            record('demo ingestion', board['n_submitted'] == 22, {'submitted':board['n_submitted'], 'ranked':board['n_ranked'], 'gt_epochs':pipeline.n_gt})
            ranked = sorted((r for r in board['rows'] if r['rank']), key=lambda r:r['rank'])
            record('ranking order', ranked == sorted(ranked, key=lambda r:(r['rmse2d'],r['rmse3d'],r['group'])), [(r['group'],r['rmse2d']) for r in ranked[:3]])
            record('availability gate', row('G04')['rank'] is None, row('G04')['availability'])
            for path in ['/', '/tv', '/rules', '/admin', '/group/G07', '/static/assets/app.js', '/static/assets/echarts.min.js', '/api/leaderboard.csv', '/api/group/G07/history.csv']:
                status, body = request(path)
                record('GET '+path, status == 200, status)
            record('admin requires token', request('/api/admin/state')[0] == 401)
            record('admin authenticated', request('/api/admin/state', admin=True)[0] == 200)
            record('private file traversal blocked', request('/static/../data/ground_truth/whampoa.private.csv')[0] == 403)
            record('group plots available in fresh database', bool(request('/api/group/G07')[1]['series']))
            if args.serve:
                poller.start()
                print('PREVIEW '+base+' (isolated demo; Ctrl+C to stop)', flush=True)
                while True:
                    time.sleep(1)
            stream = urllib.request.urlopen(base+'/api/events', timeout=5)
            while not stream.readline().startswith(b'data: '):
                pass
            body07 = (ROOT/'demo_drive/G07/G07_benchmark.csv').read_bytes()
            body15 = (ROOT/'demo_drive/G15/G15_benchmark.csv').read_bytes()
            destination = Path(cfg.local_mirror.root)/'G01/G01_benchmark.csv'
            destination.write_bytes(body07)
            poller.start()
            deadline = time.monotonic()+5
            while row('G01')['rmse2d'] != row('G07')['rmse2d'] and time.monotonic() < deadline:
                time.sleep(.1)
            record('background submission update', row('G01')['rmse2d'] == row('G07')['rmse2d'])
            event = b''
            while not event.startswith(b'data: '):
                event = stream.readline()
            record('SSE broadcasts update', b'update' in event, event.decode().strip())
            stream.close()
            poller.stop()
            upload('G01', b'bad header\n')
            record('latest invalid file removes rank', row('G01')['status']=='invalid' and row('G01')['rank'] is None)
            record('last good retained', row('G01')['last_good'] is not None)
            request('/api/admin/freeze', b'', True)
            record('freeze blocks polling', poller.poll_once().get('frozen') is True)
            request('/api/admin/freeze?frozen=0', b'', True)
            # Local mirror file changes within the same second, preserving length.
            destination.write_bytes(body07)
            stamp = int(time.time())-20
            os.utime(destination, (stamp+.1, stamp+.1))
            poller.poll_once()
            changed_body = body07.replace(b'2158',b'2159')
            destination.write_bytes(changed_body)
            os.utime(destination, (stamp+.8, stamp+.8))
            result = poller.poll_once()
            record('same-second same-size replacement detected', bool(result['updated']), {'updated':len(result['updated'])})
            # New method label with unchanged CSV.
            destination.write_bytes(body07)
            os.utime(destination, (stamp+2, stamp+2))
            poller.poll_once()
            (destination.parent/'G01_version.txt').write_text('AUDIT NEW METHOD',encoding='utf-8')
            poller.poll_once()
            record('method label only update detected', row('G01')['method_label']=='AUDIT NEW METHOD', row('G01')['method_label'])
            # A differently named backup must not replace the exact required filename.
            backup = destination.parent/'G01_benchmark_old.csv'
            backup.write_bytes(body15)
            os.utime(backup, (stamp+4, stamp+4))
            poller.poll_once()
            record('ignore noncanonical backup CSV', row('G01')['rmse2d']==row('G07')['rmse2d'], row('G01')['rmse2d'])
            # Exercise repeated withdrawal via real authenticated HTTP requests.
            upload('G02', body07)
            upload('G02', body15)
            request('/api/admin/withdraw?group=G02', b'', True)
            first = row('G02')['submission_id']
            request('/api/admin/withdraw?group=G02', b'', True)
            second = row('G02')['submission_id']
            record('repeated withdrawal steps back', second != first, {'first':first,'second':second})
            # Reload a different epoch set, then rescore: old scores must not survive.
            # Use a newly loaded reference rather than depending on internal lookup layout.
            gt_path = cfg.audit_root/'different_gt.csv'
            gt_lines = cfg.gt_path.read_text(encoding='utf-8').splitlines()
            gt_path.write_text('\n'.join([gt_lines[0]] + [str(int(line.split(',')[0])+1)+','+line.split(',',1)[1] for line in gt_lines[1:] if line.strip()])+'\n', encoding='utf-8')
            cfg.challenge.gt_file = str(gt_path)
            pipeline.load_ground_truth()
            count = pipeline.rescore_current()
            record('rescore invalidates unmatched old results', row('G07')['rank'] is None, {'rescored':count,'G07_rank':row('G07')['rank']})
            # Read the existing classroom DB without opening it through the writable Database API.
            connection = sqlite3.connect((ROOT/'data/competition.db').as_uri()+'?mode=ro', uri=True)
            paths = [r[0] for r in connection.execute('select source_path from submissions where is_current=1')]
            connection.close()
            record('existing archive references portable', all(p and Path(p).is_file() for p in paths), {'current':len(paths),'missing':sum(not p or not Path(p).is_file() for p in paths)})
            (output/'results.json').write_text(json.dumps(checks,indent=2,ensure_ascii=False),encoding='utf-8')
            print('RESULTS '+str(output/'results.json'),flush=True)
            return 0 if all(check['passed'] for check in checks) else 1
        except KeyboardInterrupt:
            pass
        finally:
            poller.stop()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            db.close()


if __name__ == '__main__':
    raise SystemExit(main())
