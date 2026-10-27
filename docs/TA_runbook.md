# TA runbook

One page. Everything you need on the day.

---

## One week before

- [ ] Build the ground truth: `python tools/build_ground_truth.py UrbanNav_whampoa_raw.txt --format ie --name whampoa`
- [ ] `python run.py --check` → every line must say `OK`
- [ ] Change `server.admin_token` in `config.toml` (or set `AAE4203_ADMIN_TOKEN`)
- [ ] Share the Drive folder with the service account, **or** install Drive for Desktop
      and confirm the mirror folder is filling up
- [ ] Create one sub-folder per group (`G01` … `G18`) and give the students the link
- [ ] Rehearse: `make_demo_dataset.py` then `rehearse.py` (see README)
- [ ] Confirm the projector can reach `http://<ip>:8080/tv`
- [ ] Tell students to run `check_submission.py` before uploading, and to keep a copy of
      their best file

## On the day

```bash
python run.py                  # or double-click start.bat
```

The banner prints the LAN URL. Put it on the board.

| time | action |
|---|---|
| start | board is empty — every group shows "not uploaded yet" |
| as they work | files appear one by one, rows flash, ranks move |
| ~2:50 | open `/tv` on the projector for the preliminary leaderboard |
| end of session | screenshot `/tv`, keep the URL live until the deadline |
| deadline | `/admin` → **Freeze leaderboard**, then **Export leaderboard** |

## If something goes wrong

| symptom | cause | fix |
|---|---|---|
| banner says `SOURCE NOT AVAILABLE` | mirror folder missing or Drive key not shared | the site still serves the old database; fix the path and restart |
| a file was uploaded but nothing happened | Drive has not synced yet | wait one poll interval; check `[sources.local_mirror] root` |
| a group is stuck on an old score | they replaced the file with an identical copy | nothing to do — byte-identical files are skipped on purpose |
| a group uploaded the wrong file | — | `/admin` → **withdraw** restores their previous version |
| scores look wrong after a config change | thresholds / ground truth changed | `/admin` → **Re-evaluate all current files** |
| a row shows a `!` | fair-play flag, e.g. implausible accuracy | open the group page, check the repository |
| the port is busy | another process | `python run.py --port 8090` (it also walks up automatically) |
| you need to restart mid-session | — | safe: every upload is archived, the board is rebuilt from SQLite |

## Never do this

- **Do not put the ground truth in the Drive folder.** It lives only in
  `data/ground_truth/` on the server.
- **Do not delete `data/competition.db`** during the session — it is the record of what
  was scored and when.
- Do not run two servers against the same `data/` folder.

## After the session

1. `/admin` → **Freeze leaderboard**.
2. **Export leaderboard** → CSV, and screenshot `/tv`.
3. Ask each group for `Download version log (CSV)` from their group page — that is the
   ablation table for their report.
4. Archive `data/` (database + submissions + ground truth) somewhere durable.
5. Spot-check two or three repositories against their archived CSV (`tools/score_cli.py`
   will rescore any file offline).

## Useful numbers

| | |
|---|---|
| benchmark epochs | 1534 |
| GPS week | 2158 |
| time of week | 455346 – 456879 s (25.6 min) |
| availability threshold | 90 % → 1381 epochs minimum |
| reference | UrbanNav Whampoa, from the teaching team's high-grade system |

The ground truth is restricted to the epochs present in
`UrbanNav-HK-Deep-Urban-1.ublox.f9p.obs`, so 100 % availability is reachable.
Rebuild it whenever the observation file changes:

```bash
python tools/build_ground_truth.py UrbanNav_whampoa_raw.txt --format ie \
    --name whampoa --obs UrbanNav-HK-Deep-Urban-1.ublox.f9p.obs
```
