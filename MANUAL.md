# User Manual · GNSS Benchmark Challenge

Instructions for starting, stopping, and maintaining the server. See
[README.md](README.md) for setup and [the TA runbook](docs/TA_runbook.md)
for the classroom checklist.

## Quick reference

Run all commands from the project folder containing `run.py`.

| Task | Command or address |
|---|---|
| Start | Double-click `start.bat`, or run `python run.py` |
| Stop | Press `Ctrl+C` in the server terminal |
| Check configuration | `python run.py --check` |
| Leaderboard | http://127.0.0.1:8080/ |
| Projector view | http://127.0.0.1:8080/tv |
| Submission guide | http://127.0.0.1:8080/rules |
| Teacher console | http://127.0.0.1:8080/admin |

The loopback addresses above work only on the server computer, and only when
it listens on loopback or all interfaces. For students, use the server's campus
network address and the port printed at startup.

## 1. Starting the server

### Option A: Windows launcher

Double-click **`start.bat`** in the project folder. It locates Python, checks
that Python 3.11 or newer is available, and starts the server. If startup fails,
the window stays open so you can read the error.

### Option B: Terminal

Open a terminal in the project folder and run:

```powershell
python run.py --check
python run.py
```

### Option C: Background operation

On Windows, run from the project folder:

```powershell
Start-Process -FilePath 'python' -ArgumentList 'run.py' -WindowStyle Hidden
```

For an established classroom server, Windows Task Scheduler can start the
service at boot. On Linux, a simple background launch is:

```bash
mkdir -p data
nohup python run.py > data/server.log 2>&1 &
```

### Confirming startup

The startup banner lists the leaderboard, projector view, submission guide,
admin console, reference dataset, source folder, polling interval, and database.
Check these values before sharing the leaderboard address with students.

For campus access, bind to the computer's current campus IPv4 address using
`--host`, and allow the selected TCP port through the firewall for the intended
campus subnet. Students must use that address rather than `127.0.0.1`.
Campus Wi-Fi addresses can change; update the bind address and firewall rule
when this happens. Network isolation may still prevent access between devices.

### Startup options

| Option | Purpose |
|---|---|
| `--port 8090` | Use a different port; the usual default is 8080. Check the startup banner for the actual port. |
| `--host 127.0.0.1` | Allow access only from this computer. |
| `--host CAMPUS_IPV4` | Listen on the specified campus interface; replace `CAMPUS_IPV4` with its actual address. |
| `--check` | Validate configuration, reference data, and the submission source without starting the web server. |
| `--no-poller` | Serve stored results without scanning for new files. |
| `--config alternate.toml` | Load a different configuration file. |

## 2. Stopping the server

### Normal shutdown

Press **`Ctrl+C`** in the terminal running the server. Wait until the command
prompt returns. If launched with `start.bat`, closing that terminal also stops
the foreground process; use `Ctrl+C` where possible.

### If the terminal is missing or the process is unresponsive

Identify the listener first. Do not stop an unrelated process using the same port.

On Windows PowerShell:

```powershell
Get-NetTCPConnection -LocalPort 8080 -State Listen |
    Select-Object LocalAddress, LocalPort, OwningProcess
```

Inspect the reported process ID, then stop that confirmed server process:

```powershell
Get-Process -Id SERVER_PID
Stop-Process -Id SERVER_PID
```

Replace `SERVER_PID` with the actual numeric ID. Use `-Force` only if necessary.
Alternatively, `netstat -ano | findstr :8080` lists listeners and their IDs.

On Linux or macOS:

```bash
lsof -nP -iTCP:8080 -sTCP:LISTEN
kill SERVER_PID
```

### Verify shutdown

Request `/api/status` at the address on which the server was listening:

```powershell
Invoke-WebRequest http://127.0.0.1:8080/api/status -TimeoutSec 3
```

A connection failure is expected after shutdown. A JSON response means a server
is still listening. Substitute the campus IP and actual port when applicable.

### Data persistence

Stopping the server does not erase saved submissions. Archived CSV files are in
`data/submissions/`, and submission records and scores are in
`data/competition.db`. Keep both when moving or backing up the installation.

## 3. Restarting

Stop the existing process, then run the same startup command again:

```powershell
python run.py
```

Preserve any custom `--host`, `--port`, or `--config` options. Configuration
changes require a restart. Startup re-evaluates current benchmark submissions
from their archives, so keep the reference data and archive paths available.
Rank-change arrows describe recent refreshes and may reset after a restart.

## 4. Routine checks

### Configuration check

```powershell
python run.py --check
```

Check that configuration, ground truth, database, source, and web assets report
`OK`. A source error can indicate an incorrect mirror path, unavailable Drive
Desktop mount, or missing Drive API credentials or folder permissions.

### Runtime health

Open `/api/status` on the running server to inspect the polling status and
latest error. The leaderboard's live indicator shows the connection state.
If it displays `reconnecting`, check the server and network; the page also uses
periodic polling as a fallback.

### Unit tests

```powershell
python -m unittest discover -s tests -v
```

All tests should finish with `OK`. See [validation notes](docs/validation.md)
for the recorded test results and integration-test prerequisites.

## 5. Troubleshooting

| Symptom | What to check |
|---|---|
| Python is not recognized | Install Python 3.11 or newer and add it to PATH, or try `start.bat`. |
| `Python 3.11 or newer is required` | Select a supported Python interpreter. |
| Port is occupied | Use another port, for example `python run.py --port 8090`, and check the actual startup address. |
| `SOURCE NOT AVAILABLE` | Check the configured source folder and Drive synchronization, then restart after correcting the configuration. |
| Upload does not appear | Confirm Drive Desktop has synchronized the file locally, check the exact filename and matching group folder, and inspect the admin event log. Polling normally runs every 10 seconds after synchronization. |
| A file is ignored | Replace the group placeholder: group 79 must use `G79/G79_benchmark.csv`, not `G79/GXX_benchmark.csv`. Backup names and duplicate-copy suffixes are ignored. |
| Identical replacement has no new result | Unchanged CSV content does not create a new scored version. |
| Rank is shown as a dash | Availability is below 90%, the CSV is invalid, or no benchmark has been submitted. Read the row's explanation. |
| A warning marker appears | Open the group page for the diagnostic reason; the marker itself does not deduct marks. |
| Students cannot open the page | Check the bind address, current campus IP, firewall rule, port, and campus network isolation. Loopback binding permits only local access. |

Both `GXX_benchmark.csv` and `GXX_owndata.csv` are required deliverables in the
matching group folder. Only the benchmark determines leaderboard rank. Follow
[the submission guide](web/rules.html) for the exact CSV format; when the server
is running, open `/rules` for the rendered guide and current dataset information.

See [the TA runbook](docs/TA_runbook.md) for additional operational guidance.

## 6. Storage and backups

| Location | Contents | Back up? |
|---|---|---|
| `data/competition.db` | Submission history and scores | Yes |
| `data/submissions/` | Archived uploaded CSV files | Yes |
| `data/ground_truth/` | Reference data used for scoring | Yes; keep access controlled |
| Configured Drive mirror | Synchronized source files | Confirm cloud copies and synchronization before relying on them |
| `demo_drive/` | Generated demonstration fixtures | Usually reproducible |

Stop the server before copying the complete `data/` directory for a consistent
backup. Also retain your local configuration and any required credentials in a
secure location outside the public repository. Existing archive paths may need
to be repaired if the installation moves to a different drive or directory.

### Starting a new competition

Back up the existing database and archives first. Use a separate installation
or explicitly selected empty data directory for a new competition rather than
mixing demonstration records with student submissions. Do not clear a live
Drive-synchronized submission folder as part of a local reset.

## 7. Before collecting real submissions

1. Set a strong `admin_token` in `config.toml`; do not use `change-me-please`.
2. Select the intended source kind and configure its mirror path or Drive folder ID.
3. Confirm the correct reference dataset and group settings.
4. Preserve previous data and prepare a clean competition database if needed.
5. Run `python run.py --check` and resolve any errors.
6. Start the server and verify source status, the submission guide, and access
   from another student device on the intended network.
7. Upload a clearly identified test submission, verify scoring and live updates,
   and keep test records separate from official submissions.

## Separate Drive test server

The optional `tools/live_drive_test.py` utility uses a persistent test database
under `.review/drive-live/data/`, rather than `data/competition.db`. Its archived
submissions are also stored under that test directory. Back up the directory
used by the server you actually run. For example, local testing uses:

```powershell
python tools/live_drive_test.py --host 127.0.0.1 --port 8080
```

Press `Ctrl+C` to stop it. To resume the same test leaderboard, restart this
utility with the same configuration and data directory.
