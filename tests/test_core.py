"""Unit tests for the scoring core.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

from app.evaluator import evaluate, is_ranked, load_ground_truth   # noqa: E402
from app.geodetic import (A, E2, ecef_delta_to_enu, ecef_to_llh,   # noqa: E402
                          llh_to_ecef, percentile_linear)
from app.submission import parse_submission_bytes                  # noqa: E402

import check_submission                                            # noqa: E402

# a fixed point in Hong Kong: PolyU Block X area
LAT0, LON0, H0 = 22.3050, 114.1800, 5.0


def _write_gt(path: Path, epochs=20, spacing=1.0, tow0=455342.0, week=2158):
    x0, y0, z0 = llh_to_ecef(LAT0, LON0, H0)
    lines = ["gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m,q"]
    for i in range(epochs):
        # a straight 1 m/s line north
        dx, dy, dz = 0.0, i * 1.0, 0.0
        lines.append(f"{week},{tow0 + i * spacing:.3f},"
                     f"{x0 + dx:.3f},{y0 + dy:.3f},{z0 + dz:.3f},1")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return x0, y0, z0


def _csv(rows, header="gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m"):
    body = [header] + ["%s,%s,%s,%s,%s" % row for row in rows]
    return ("\n".join(body) + "\n").encode("utf-8")


class GeodeticTests(unittest.TestCase):
    def test_roundtrip(self):
        for lat, lon, h in [(22.305, 114.18, 5.0), (-33.86, 151.21, 58.0),
                            (0.0, 0.0, 0.0), (64.1, -21.9, 120.0)]:
            x, y, z = llh_to_ecef(lat, lon, h)
            lat2, lon2, h2 = ecef_to_llh(x, y, z)
            self.assertAlmostEqual(lat, lat2, places=9)
            self.assertAlmostEqual(lon, lon2, places=9)
            self.assertAlmostEqual(h, h2, places=4)

    def test_known_ecef(self):
        # a point on the equator at Greenwich sea level is (a, 0, 0)
        x, y, z = llh_to_ecef(0.0, 0.0, 0.0)
        self.assertAlmostEqual(x, 6378137.0, places=3)
        self.assertAlmostEqual(y, 0.0, places=6)
        self.assertAlmostEqual(z, 0.0, places=6)

    def test_enu_axes(self):
        # 10 m north of the origin must come back as +10 m North.  Use the
        # local meridional radius of curvature, not 111.32 km/deg.
        x0, y0, z0 = llh_to_ecef(LAT0, LON0, H0)
        meridional = A * (1 - E2) / (1 - E2 * math.sin(math.radians(LAT0)) ** 2) ** 1.5
        lat1 = LAT0 + math.degrees(10.0 / meridional)
        x1, y1, z1 = llh_to_ecef(lat1, LON0, H0)
        e, n, u = ecef_delta_to_enu(x1 - x0, y1 - y0, z1 - z0, LAT0, LON0)
        self.assertAlmostEqual(e, 0.0, places=3)
        self.assertAlmostEqual(n, 10.0, places=3)
        self.assertAlmostEqual(u, 0.0, places=2)

    def test_percentile_matches_numpy_definition(self):
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertAlmostEqual(percentile_linear(data, 50), 3.0)
        self.assertAlmostEqual(percentile_linear(data, 0), 1.0)
        self.assertAlmostEqual(percentile_linear(data, 100), 5.0)
        # linear interpolation: 95th of 1..5 is 4 + 0.8
        self.assertAlmostEqual(percentile_linear(data, 95), 4.8)
        # identical to numpy.percentile(..., 95) on an even-length sample
        even = [0.0, 10.0, 20.0, 30.0]
        # numpy: 28.5
        self.assertAlmostEqual(percentile_linear(even, 95), 28.5)


class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.gt_path = self.dir / "gt.csv"
        self.x0, self.y0, self.z0 = _write_gt(self.gt_path, epochs=20)
        self.gt = load_ground_truth(self.gt_path, grid_s=0.1, max_quality=5)

    def submit(self, offset_e=0.0, offset_n=0.0, offset_u=0.0):
        """A submission that is the truth displaced by a constant ENU offset."""
        lat = math.radians(self.gt.origin_lat)
        lon = math.radians(self.gt.origin_lon)
        sin_lat, cos_lat = math.sin(lat), math.cos(lat)
        sin_lon, cos_lon = math.sin(lon), math.cos(lon)
        de, dn, du = offset_e, offset_n, offset_u
        dx = -sin_lon * de - sin_lat * cos_lon * dn + cos_lat * cos_lon * du
        dy = cos_lon * de - sin_lat * sin_lon * dn + cos_lat * sin_lon * du
        dz = cos_lat * dn + sin_lat * du
        return [(self.gt.week[i], self.gt.tow[i],
                 self.gt.x[i] + dx, self.gt.y[i] + dy, self.gt.z[i] + dz)
                for i in range(self.gt.n)]

    def parse(self, rows):
        return parse_submission_bytes(_csv(
            [(w, f"{t:.1f}", f"{x:.3f}", f"{y:.3f}", f"{z:.3f}")
             for w, t, x, y, z in rows]))

    def test_perfect_submission(self):
        parsed = self.parse(self.submit())
        self.assertTrue(parsed.ok, parsed.errors)
        result = evaluate(self.gt, parsed.epochs)
        self.assertTrue(result.ok)
        self.assertEqual(result.n_matched, 20)
        self.assertAlmostEqual(result.availability, 100.0, places=6)
        self.assertAlmostEqual(result.rmse2d, 0.0, places=6)
        self.assertAlmostEqual(result.rmse3d, 0.0, places=6)

    def test_constant_offset_matches_rmse(self):
        for de, dn, du in [(3.0, 4.0, 0.0), (-2.0, 0.0, 5.0)]:
            parsed = self.parse(self.submit(de, dn, du))
            result = evaluate(self.gt, parsed.epochs)
            expected_2d = math.hypot(de, dn)
            expected_3d = math.sqrt(de * de + dn * dn + du * du)
            self.assertAlmostEqual(result.rmse2d, expected_2d, places=3)
            self.assertAlmostEqual(result.rmse3d, expected_3d, places=3)
            self.assertAlmostEqual(result.p95_2d, expected_2d, places=3)
            self.assertAlmostEqual(result.de_mean, de, places=3)
            self.assertAlmostEqual(result.dn_mean, dn, places=3)
            self.assertAlmostEqual(result.du_mean, du, places=3)

    def test_missing_epochs_reduce_availability(self):
        rows = self.submit()[:10]
        parsed = self.parse(rows)
        result = evaluate(self.gt, parsed.epochs)
        self.assertEqual(result.n_matched, 10)
        self.assertEqual(result.n_missing, 10)
        self.assertAlmostEqual(result.availability, 50.0, places=6)
        ranked, reason = is_ranked(result, 90.0)
        self.assertFalse(ranked)
        self.assertIn("50.0", reason)

    def test_ranking_gate(self):
        parsed = self.parse(self.submit()[:18])
        result = evaluate(self.gt, parsed.epochs)
        self.assertAlmostEqual(result.availability, 90.0, places=6)
        ranked, _ = is_ranked(result, 90.0)
        self.assertTrue(ranked, "exactly 90 % must be ranked")

        parsed = self.parse(self.submit()[:17])
        result = evaluate(self.gt, parsed.epochs)
        self.assertAlmostEqual(result.availability, 85.0, places=6)
        ranked, _ = is_ranked(result, 90.0)
        self.assertFalse(ranked)

    def test_epochs_outside_the_window_are_extra(self):
        rows = self.submit()
        rows.append((rows[0][0], 999999.0 % 604800.0, rows[0][2], rows[0][3], rows[0][4]))
        rows.sort(key=lambda r: (r[0],r[1]))
        parsed = self.parse(rows)
        # 999999 % 604800 = 395199 s, not in the GT window -> one extra epoch
        result = evaluate(self.gt, parsed.epochs)
        self.assertEqual(result.n_matched, 20)
        self.assertEqual(result.n_extra, 1)

    def test_duplicates_are_rejected_at_parse(self):
        rows = self.submit()
        rows.append(rows[5])
        parsed = parse_submission_bytes(_csv(
            [(w, f"{t:.1f}", f"{x:.3f}", f"{y:.3f}", f"{z:.3f}")
             for w, t, x, y, z in rows]))
        self.assertFalse(parsed.ok)
        self.assertEqual(parsed.errors[0].code, "E_DUPLICATES")

    def test_lenient_matching_absorbs_small_offsets(self):
        rows = [(w, t + 0.08, x, y, z) for w, t, x, y, z in self.submit()]
        parsed = self.parse(rows)
        result = evaluate(self.gt, parsed.epochs, tolerance_s=0.25)
        self.assertEqual(result.n_matched, 20)
        self.assertEqual(result.n_lenient, 20)

        result = evaluate(self.gt, parsed.epochs, tolerance_s=0.0)
        self.assertEqual(result.n_matched, 0)
        self.assertEqual(result.error_code, "E_NO_MATCH")

    def test_tow_with_one_decimal_matches(self):
        rows = [(w, t + 0.0, x, y, z) for w, t, x, y, z in self.submit()]
        parsed = parse_submission_bytes(_csv(
            [(w, f"{t:.1f}", f"{x:.3f}", f"{y:.3f}", f"{z:.3f}") for w, t, x, y, z in rows]))
        self.assertTrue(parsed.ok)

    def test_centimetres_are_rejected(self):
        parsed = self.parse([(w, t, x * 100, y * 100, z * 100)
                             for w, t, x, y, z in self.submit()])
        self.assertFalse(parsed.ok)
        self.assertEqual(parsed.errors[0].code, "E_LARGE_VALUES")

    def test_llh_is_rejected(self):
        rows = []
        for w, t, x, y, z in self.submit():
            la, lo, h = ecef_to_llh(x, y, z)
            rows.append((w, t, la, lo, h))
        parsed = self.parse(rows)
        self.assertFalse(parsed.ok)
        self.assertEqual(parsed.errors[0].code, "E_NOT_ECEF")


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.row = (2158, "455342.0", "-2418202.586", "5385847.524", "2405648.799")

    def test_good_file(self):
        parsed = parse_submission_bytes(_csv([self.row, (2158, "455343.0",
                                                          "-2418202.174", "5385847.238",
                                                          "2405648.952")]))
        self.assertTrue(parsed.ok, parsed.errors)
        self.assertEqual(len(parsed.epochs), 2)

    def test_empty(self):
        self.assertFalse(parse_submission_bytes(b"").ok)

    def test_header_only(self):
        parsed = parse_submission_bytes(b"gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n")
        self.assertFalse(parsed.ok)
        self.assertEqual(parsed.errors[0].code, "E_NO_ROWS")

    def test_wrong_header(self):
        parsed = parse_submission_bytes(_csv([self.row], header="week,tow,x,y,z"))
        self.assertFalse(parsed.ok)
        self.assertEqual(parsed.errors[0].code, "E_HEADER")

    def test_extra_column(self):
        data = ("gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m,extra\n"
                "2158,455342.0,-2418202.586,5385847.524,2405648.799,1\n")
        parsed = parse_submission_bytes(data.encode())
        self.assertFalse(parsed.ok)

    def test_binary_file(self):
        parsed = parse_submission_bytes(b"PK\x03\x04\x00\x00binaryxlsx")
        self.assertFalse(parsed.ok)

    def test_nan_rejected(self):
        data = ("gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n"
                "2158,455342.0,nan,5385847.524,2405648.799\n")
        parsed = parse_submission_bytes(data.encode())
        self.assertFalse(parsed.ok)

    def test_tow_out_of_range(self):
        data = ("gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n"
                "2158,1621578524.0,-2418202.586,5385847.524,2405648.799\n")
        parsed = parse_submission_bytes(data.encode())
        self.assertFalse(parsed.ok)
        self.assertIn("604800", parsed.errors[0].message)

    def test_non_integer_week(self):
        data = ("gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n"
                "2158.5,455342.0,-2418202.586,5385847.524,2405648.799\n")
        self.assertFalse(parse_submission_bytes(data.encode()).ok)

    def test_unsorted_is_rejected(self):
        data = ("gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n"
                "2158,455343.0,-2418202.174,5385847.238,2405648.952\n"
                "2158,455342.0,-2418202.586,5385847.524,2405648.799\n")
        parsed = parse_submission_bytes(data.encode())
        self.assertFalse(parsed.ok)
        self.assertEqual(parsed.errors[0].code, "E_UNSORTED")


class CheckerEquivalenceTests(unittest.TestCase):
    """The student-side checker and the server must agree on every file."""

    CASES = {
        "good": ("gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n"
                 "2158,455342.0,-2418202.586,5385847.524,2405648.799\n"
                 "2158,455343.0,-2418202.174,5385847.238,2405648.952\n"),
        "cm": ("gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n"
               "2158,455342.0,-241820258.6,538584752.4,240564879.9\n"),
        "header": ("week,tow,x,y,z\n2158,455342.0,-2418202.586,5385847.524,2405648.799\n"),
        "empty": "",
        "header_only": "gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n",
        "llh": ("gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n"
                "2158,455342.0,22.301559,114.190309,2.894\n"),
        "short_row": ("gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n"
                      "2158,455342.0,-2418202.586,5385847.524\n"),
        "nan": ("gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n"
                "2158,455342.0,nan,5385847.524,2405648.799\n"),
        "tow_range": ("gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n"
                      "2158,1621578524.0,-2418202.586,5385847.524,2405648.799\n"),
        "unsorted": ("gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n"
                     "2158,455343.0,-2418202.174,5385847.238,2405648.952\n"
                     "2158,455342.0,-2418202.586,5385847.524,2405648.799\n"),
        "bom": ("\ufeffgps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\n"
                "2158,455342.0,-2418202.586,5385847.524,2405648.799\n"),
        "crlf": ("gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m\r\n"
                 "2158,455342.0,-2418202.586,5385847.524,2405648.799\r\n"),
    }

    def test_verdicts_agree(self):
        import contextlib
        import io as _io

        with tempfile.TemporaryDirectory() as tmp:
            for name, text in self.CASES.items():
                with self.subTest(case=name):
                    path = Path(tmp) / f"{name}.csv"
                    path.write_text(text, encoding="utf-8", newline="")

                    server_ok = parse_submission_bytes(path.read_bytes(), path.name).ok

                    buffer = _io.StringIO()
                    with contextlib.redirect_stdout(buffer):
                        checker_status = check_submission.check(path, 2158, 1534)

                    self.assertEqual(
                        server_ok, checker_status == 0,
                        f"{name}: server ok={server_ok} but checker exit="
                        f"{checker_status}\n{buffer.getvalue()}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
