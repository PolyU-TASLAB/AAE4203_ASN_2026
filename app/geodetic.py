"""Geodetic conversions (WGS-84) - pure standard library, no numpy.

Everything the evaluator needs:
    llh_to_ecef, ecef_to_llh, ecef_delta_to_enu, enu_origin_vectors
    haversine_m, percentile_linear
"""

from __future__ import annotations

import math

# WGS-84 ellipsoid
A = 6378137.0                     # semi-major axis [m]
F = 1.0 / 298.257223563           # flattening
E2 = F * (2.0 - F)                # first eccentricity squared
B = A * (1.0 - F)                 # semi-minor axis [m]


def llh_to_ecef(lat_deg: float, lon_deg: float, h_m: float) -> tuple[float, float, float]:
    """Geodetic latitude/longitude (degrees) and ellipsoidal height -> ECEF metres."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    n = A / math.sqrt(1.0 - E2 * sin_lat * sin_lat)
    x = (n + h_m) * cos_lat * math.cos(lon)
    y = (n + h_m) * cos_lat * math.sin(lon)
    z = (n * (1.0 - E2) + h_m) * sin_lat
    return x, y, z


def ecef_to_llh(x: float, y: float, z: float) -> tuple[float, float, float]:
    """ECEF metres -> geodetic latitude/longitude (degrees) and ellipsoidal height [m]."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)

    if p < 1e-9:                                   # on the spin axis
        lat = math.copysign(math.pi / 2.0, z)
        h = abs(z) - B
        return math.degrees(lat), 0.0, h

    # Bowring's initial guess, then a short fixed-point iteration.
    ep2 = (A * A - B * B) / (B * B)
    theta = math.atan2(z * A, p * B)
    lat = math.atan2(z + ep2 * B * math.sin(theta) ** 3,
                     p - E2 * A * math.cos(theta) ** 3)

    h = 0.0
    for _ in range(8):
        sin_lat = math.sin(lat)
        n = A / math.sqrt(1.0 - E2 * sin_lat * sin_lat)
        h = p / math.cos(lat) - n
        lat_new = math.atan2(z, p * (1.0 - E2 * n / (n + h)))
        if abs(lat_new - lat) < 1e-14:
            lat = lat_new
            break
        lat = lat_new

    sin_lat = math.sin(lat)
    n = A / math.sqrt(1.0 - E2 * sin_lat * sin_lat)
    h = p / math.cos(lat) - n
    return math.degrees(lat), math.degrees(lon), h


def ecef_delta_to_enu(dx: float, dy: float, dz: float,
                      lat0_deg: float, lon0_deg: float) -> tuple[float, float, float]:
    """Rotate an ECEF difference into the local East-North-Up frame at (lat0, lon0)."""
    lat0 = math.radians(lat0_deg)
    lon0 = math.radians(lon0_deg)
    sin_lat, cos_lat = math.sin(lat0), math.cos(lat0)
    sin_lon, cos_lon = math.sin(lon0), math.cos(lon0)

    east = -sin_lon * dx + cos_lon * dy
    north = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
    up = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz
    return east, north, up


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2.0) ** 2
    return 2.0 * 6371000.0 * math.asin(min(1.0, math.sqrt(a)))


def percentile_linear(values: list[float], pct: float) -> float:
    """Percentile with linear interpolation - same definition as numpy's default.

    ``values`` need not be sorted; ``pct`` is in [0, 100].
    """
    if not values:
        return float("nan")
    if len(values) == 1:
        return float(values[0])
    data = sorted(values)
    if pct <= 0:
        return float(data[0])
    if pct >= 100:
        return float(data[-1])
    pos = (len(data) - 1) * (pct / 100.0)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(data) - 1)
    frac = pos - lo
    return float(data[lo] + (data[hi] - data[lo]) * frac)


def dms_to_deg(d: float, m: float, s: float) -> float:
    """Degrees/minutes/seconds with a sign carried by the degrees field."""
    sign = -1.0 if (float(d) < 0 or str(d).startswith("-")) else 1.0
    return sign * (abs(float(d)) + float(m) / 60.0 + float(s) / 3600.0)
