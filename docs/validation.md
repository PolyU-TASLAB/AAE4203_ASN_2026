# Validation

Validated on Windows with Python 3.12 on 27 October 2026.

- `python -m unittest discover -s tests -v`: 36 tests passed.
- `python tools/review_smoke.py`: 27 integration checks passed.

Coverage includes Section 9 CSV validation, per-epoch ENU scoring,
availability-based ranking, matching group folders, own-data handling,
Drive source change detection, live SSE updates, authenticated admin actions,
withdrawal, and rescoring. The submission guide was also checked in the browser.

Unit tests generate temporary fixtures. The integration audit requires a local
configuration, reference dataset, generated `demo_drive` fixtures, and an existing
classroom database; these local data files are not included in the repository.
The audit uses an isolated database and mirror and only reads the classroom
database to check archive paths. Its detailed output is written to
`.review/results.json`.

For this validation run, 22 legacy archive references pointing to the former
`E:` project location were repaired to their existing `O:` files after verifying
each file's SHA-256. The local database was backed up before changing paths.
No submission contents or scores were changed by this path repair.
