# Scoring

The exact definitions, so a result can always be reproduced.

## 1. Ground truth

`data/ground_truth/whampoa.private.csv`, built once from the UrbanNav Whampoa
reference export:

```
gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m,q
2158,455346.000,-2418233.123,5385855.730,2405790.128,1
```

* 1534 epochs at 1 Hz, no gaps, no duplicate epochs.
* The epoch list is **exactly** the epoch list of
  `UrbanNav-HK-Deep-Urban-1.ublox.f9p.obs` — every benchmark epoch is one a group
  can actually produce, so 100 % availability is reachable. Build the store with
  `--obs` to keep that property:

  ```bash
  python tools/build_ground_truth.py UrbanNav_whampoa_raw.txt --format ie \
      --name whampoa --obs UrbanNav-HK-Deep-Urban-1.ublox.f9p.obs
  ```

  Without `--obs` the reference export has 5 extra epochs (455342–455345 and
  456880) that no observation exists for, and nobody could ever exceed 99.7 %.
* The receiver's epochs sit at .004 / .005 s inside the second. The 0.1 s
  matching grid snaps them onto the reference epochs, so a group that writes the
  raw receiver time tags still matches exactly.
* `q` is the receiver's quality flag; epochs above `challenge.max_quality`
  (default 5 = keep everything) are dropped when the store is built.
* Latitude/longitude/height from the source are converted to ECEF with WGS-84.
* On the day of measurement the reference is the truth. It is never served.

## 2. Epoch matching

Slide 32: *"Epochs matched by GPS week + time of week"*.

1. Both sides are quantised onto `challenge.match_grid_s` (default 0.1 s):
   `key = round(round(tow / grid) * grid, 6)`.
2. The submitted `(week, key)` is looked up in the benchmark index.
3. Additional tolerance is disabled by default (`lenient_tolerance_s = 0`). If explicitly enabled and there is no hit, the nearest benchmark epoch
   within that tolerance is used instead. These matches are counted in
   `n_lenient` and reported on the group page.
4. An epoch that matches nothing is counted in `n_extra` and does not affect the
   score.
5. A benchmark epoch that no submitted row matches is **missing**.
6. Duplicate timestamps and unsorted rows are rejected at parse time.

## 3. Availability

```
A = 100 * n_matched / n_benchmark_epochs
```

`n_benchmark_epochs` is fixed by the dataset, so it cannot be inflated.

## 4. Errors

The local frame is built at the reference position of each matched epoch, as specified by Tutorial Section 9.3, Equation (17). `challenge.enu_origin` is used only for display metadata:

```
[dE, dN, dU] = R(lat_gt[k], lon_gt[k]) · ([x,y,z]_submitted - [x,y,z]_truth)

2D error = sqrt(dE² + dN²)
3D error = sqrt(dE² + dN² + dU²)
```

Only matched epochs contribute.

## 5. Metrics

| metric | definition |
|---|---|
| `rmse2d` | `sqrt(mean(2D²))` |
| `rmse3d` | `sqrt(mean(3D²))` |
| `p95_2d` | 95th percentile of the 2D errors, linear interpolation (numpy default) |
| `mean2d`, `std2d`, `max2d` | plain statistics over the 2D errors (`pstdev` for std) |
| `de_mean`, `dn_mean`, `du_mean` | mean signed error per axis — the systematic offset |

## 6. Ranking

```
ranked  ⇔  status == "ok"  and  availability >= availability_min   (default 90 %)
order   =  (rmse2d, rmse3d, group_code)   ascending
```

A group is ranked on its **current** file only. Re-uploading a worse file lowers
the score; the version log keeps the history for the report.

`rank_delta` in the API is the movement since the previous board update,
positive = moved up. It resets when the server restarts.

## 7. Validation, in order

The first failure wins and the message is what the student sees.

| code | condition |
|---|---|
| `E_EMPTY` | zero bytes, or no header, or header with no data rows |
| `E_TOO_LARGE` | more than 20 MB |
| `E_ENCODING` | not decodable as text (an `.xlsx` workbook, for example) |
| `E_HEADER` | header is not exactly `gps_week,gps_tow_s,x_ecef_m,y_ecef_m,z_ecef_m` |
| `E_TOO_MANY_ROWS` | more than 200 000 data rows |
| `E_BAD_ROWS` | a row that is not 5 finite numbers, a non-integer week, or `gps_tow_s` outside `[0, 604800)` |
| `E_NO_ROWS` | header fine, no usable rows |
| `E_NOT_ECEF` | all coordinates below 1e5 — latitude/longitude/height, not ECEF metres |
| `E_LARGE_VALUES` | any coordinate above 1e8 — centimetres instead of metres |
| `E_NO_MATCH` | nothing matched: wrong dataset, wrong week, or wrong time tag |
| `E_UNIT_SUSPECT` | mean 3D error above 500 km — a frame or unit mistake, not an algorithm |
| `E_IO` | the file could not be read |

Warnings never block a score: unsorted rows (they are sorted), duplicate epochs
(dropped), few rows, multiple weeks, header case, non-UTF-8 encoding, epoch jumps.

## 8. Fair play

Flags only, never a score change:

* `rmse2d < fairplay.implausible_rmse2d_m` (1 m) — impossible from pseudoranges;
* `max3d < fairplay.gt_leak_max_3d_m` (0.30 m) — looks like the truth itself;
* the file is byte-identical to one already submitted by another group.

Everything is archived with its SHA-256, so `tools/score_cli.py` can rescore any
file identically at any time.
