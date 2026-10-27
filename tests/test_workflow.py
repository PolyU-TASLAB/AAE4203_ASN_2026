"""Regression tests for submission lifecycle and both source adapters."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Config
from app.db import Database
from app.pipeline import Pipeline
from app.poller import Poller
from app.service import Service
from app.sources.base import parse_filename
from app.sources.local_mirror import LocalMirrorSource
from app.sources.gdrive_api import GoogleDriveSource
from test_core import _write_gt
from app.submission import parse_submission_bytes
from app.evaluator import evaluate, load_ground_truth
from app.geodetic import llh_to_ecef
import contextlib
import io
import math
import check_submission


class TestConfig(Config):
    @property
    def data_dir(self):
        return self.test_root / 'data'


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = TestConfig()
        self.cfg.test_root = Path(self.tmp.name)
        self.gt_path = self.cfg.test_root / 'gt.csv'
        _write_gt(self.gt_path)
        self.cfg.challenge.gt_file = str(self.gt_path)
        self.cfg.local_mirror.root = str(self.cfg.test_root / 'mirror')
        Path(self.cfg.local_mirror.root).mkdir()
        self.cfg.polling.settle_s = 0
        self.db = Database(self.cfg.db_path)
        self.addCleanup(self.db.close)
        self.pipeline = Pipeline(self.cfg, self.db)
        self.service = Service(self.cfg, self.db, self.pipeline)
        lines = self.gt_path.read_text().splitlines()
        self.csv = ('\n'.join(','.join(line.split(',')[:5]) for line in lines)+'\n').encode()

    def ingest(self, data=None, label=None):
        return self.pipeline.ingest(group='G01', kind='benchmark', filename='G01_benchmark.csv',
                                    data=data or self.csv, source='test', method_label=label)

    def test_section9_format_checker_and_server_agree(self):
        header = 'gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m'
        good = '2158,455342.0,-2418202.586,5385847.524,2405648.799'
        cases = [header+'\n'+good.replace('455342.0', '455342'),
                 header+'\n'+good.replace('-2418202.586','-2418202.58'),
                 header+'\n'+good.replace('-2418202.586','-2418202.5860'),
                 header+',\n'+good+',', header.upper()+'\n'+good,
                 header+'\n'+good.replace('2158,','2158.0,'),
                 header+'\n'+good+'\n'+good]
        for content in cases:
            with self.subTest(content=content):
                self.assertFalse(parse_submission_bytes(content.encode()).ok)
                path = self.cfg.test_root/'G01_benchmark.csv'
                path.write_text(content,encoding='utf-8')
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(check_submission.check(path),1)

    def test_enu_uses_each_reference_position(self):
        gt_rows = ['gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m,q']
        sub_rows = ['gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m']
        for i, lat in enumerate([0.0, 60.0]):
            x,y,z = llh_to_ecef(lat, 0, 0)
            gt_rows.append(f'2158,{100+i}.0,{x:.3f},{y:.3f},{z:.3f},1')
            # Exactly 10 m north in the local frame at EACH epoch.
            x -= 10*math.sin(math.radians(lat))
            z += 10*math.cos(math.radians(lat))
            sub_rows.append(f'2158,{100+i}.0,{x:.3f},{y:.3f},{z:.3f}')
        self.gt_path.write_text('\n'.join(gt_rows),encoding='utf-8')
        result = evaluate(load_ground_truth(self.gt_path),
                          parse_submission_bytes('\n'.join(sub_rows).encode()).epochs)
        self.assertAlmostEqual(result.rmse2d,10,places=3)
        self.assertAlmostEqual(result.rmse3d,10,places=3)

    def test_own_data_is_required_deliverable_but_not_ranked(self):
        self.pipeline.ingest(group='G01', kind='owndata',filename='G01_owndata.csv',
                             data=self.csv, source='test')
        detail = self.service.group_detail('G01')
        self.assertEqual(detail['owndata']['status'],'ok')
        self.assertIsNone(detail['row']['rank'])
        self.ingest()
        self.assertEqual(self.service.group_detail('G01')['row']['rank'],1)

    def test_group_folder_must_match_filename(self):
        folder=Path(self.cfg.local_mirror.root)/'G02'
        folder.mkdir()
        (folder/'G01_benchmark.csv').write_bytes(self.csv)
        scan=LocalMirrorSource(self.cfg).scan()
        self.assertEqual(scan.refs,[])
        self.assertIn('Wrong folder',scan.errors[0])

    def test_canonical_names_only(self):
        self.assertEqual(parse_filename('G01_benchmark.csv'), ('G01','benchmark'))
        for name in ['G01_benchmark_old.csv', 'G01 benchmark.csv', 'G01_benchmark (1).csv']:
            self.assertIsNone(parse_filename(name))

    def test_metadata_update_preserves_history_and_can_clear(self):
        self.ingest(label='old')
        self.assertEqual(self.ingest(label='new')['status'], 'updated')
        self.assertEqual(self.db.count_submissions(), 1)
        self.ingest(label='')
        self.assertEqual(self.db.latest_submission('G01')['method_label'], '')

    def test_rescore_invalidates_then_recovers_and_refreshes_plots(self):
        self.ingest()
        before = self.service.group_detail('G01')['series']
        self.assertIsNotNone(before)
        _write_gt(self.gt_path, week=2159)
        self.pipeline.load_ground_truth()
        self.pipeline.rescore_current()
        current = self.db.latest_submission('G01')
        self.assertEqual(current['status'], 'invalid')
        self.assertIsNone(current['rmse2d'])
        self.assertIsNone(self.service.group_detail('G01')['series'])
        _write_gt(self.gt_path)
        self.pipeline.load_ground_truth()
        self.pipeline.rescore_current()
        current = self.db.latest_submission('G01')
        self.assertEqual(current['status'], 'ok')
        self.assertEqual(current['error_code'], '')
        self.assertTrue(current['is_ranked'])
        self.assertIsNotNone(self.service.group_detail('G01')['series'])

    def test_missing_archive_clears_score(self):
        self.ingest()
        Path(self.db.latest_submission('G01')['source_path']).unlink()
        self.pipeline.rescore_current()
        current = self.db.latest_submission('G01')
        self.assertEqual(current['error_code'], 'E_ARCHIVE')
        self.assertIsNone(current['rmse2d'])

    def test_withdraw_to_empty(self):
        self.ingest()
        self.ingest(self.csv+b'\n')
        self.assertTrue(self.db.withdraw_current('G01','benchmark'))
        self.assertEqual(self.db.latest_submission('G01')['id'], 1)
        self.assertTrue(self.db.withdraw_current('G01','benchmark'))
        self.assertIsNone(self.db.latest_submission('G01'))
        self.assertFalse(self.db.withdraw_current('G01','benchmark'))

    def test_duplicate_sources_do_not_overwrite_and_transient_failure_retries(self):
        root = Path(self.cfg.local_mirror.root)
        (root/'G01_benchmark.csv').write_bytes(self.csv)
        (root/'G01').mkdir()
        other = root/'G01/G01_benchmark.csv'
        other.write_bytes(self.csv+b'\n')
        poller = Poller(self.cfg,self.db,self.pipeline,LocalMirrorSource(self.cfg))
        scan = poller.source.scan()
        self.assertTrue(any('Wrong folder' in error for error in scan.errors))
        scan.refs = scan.refs * 2
        with patch.object(poller.source, 'scan', return_value=scan):
            self.assertEqual(poller.poll_once()['updated'], [])
        self.assertIn('Duplicate',poller.last_error)
        with patch.object(self.pipeline,'ingest',side_effect=OSError('temporary')):
            self.assertEqual(poller.poll_once()['updated'], [])
        self.assertEqual(len(poller.poll_once()['updated']), 1)

    def test_drive_sidecar_changes_signature_without_csv_change(self):
        source = GoogleDriveSource(self.cfg)
        source.folder_id = 'root'
        files = [{'id':'csv','name':'G01_benchmark.csv','md5Checksum':'same','size':'100'},
                 {'id':'label','name':'G01_version.txt','modifiedTime':'2026-01-01T00:00:00Z'}]
        with patch.object(source,'_list_folder',side_effect=lambda folder: [{'id':'group','name':'G01','mimeType':'application/vnd.google-apps.folder'}] if folder == 'root' else files), patch.object(source,'_download',return_value=b'old'):
            first = source.scan().refs[0]
        files[1]['modifiedTime'] = '2026-01-01T00:01:00Z'
        with patch.object(source,'_list_folder',side_effect=lambda folder: [{'id':'group','name':'G01','mimeType':'application/vnd.google-apps.folder'}] if folder == 'root' else files), patch.object(source,'_download',return_value=b'new'):
            second = source.scan().refs[0]
        self.assertNotEqual(first.signature,second.signature)
        self.assertEqual(second.method_label,'new')
