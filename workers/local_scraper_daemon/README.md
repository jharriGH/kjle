# KJLE Local Scraper Worker Daemon (Phase 4.2)

## Purpose

Single-file Python daemon that runs on a worker box (Windows Z820 or Linux VPS),
polls KJLE for queued LocalScraper jobs, runs `LocalScraper.exe` with the
job's parameters, then ingests the resulting leads back into KJLE via the
existing HMAC-signed `/kjle/v1/ingest/localscraper` endpoint. Workers have NO
inbound public URL — every interaction is worker-initiated (reverse polling).

## Architecture

```
   KJLE API (Render)                Worker box (Z820 or VPS)
   ─────────────────                ─────────────────────────
   scrape_jobs table                ┌──────────────────────┐
        │                           │   daemon.py (loop)   │
        │   GET  /scrape/jobs/poll  │          │           │
        │ ◄─────────────────────────│  POST started        │
        │                           │          │           │
        │                           │   spawn  ▼           │
        │                           │  ┌───────────────┐   │
        │                           │  │ ls-webhook-   │   │
        │                           │  │ handler.py    │◄──┐
        │                           │  │ (127.0.0.1)   │   │
        │                           │  └───────────────┘   │
        │                           │          ▲           │
        │                           │  exec    │ POST      │
        │                           │  ┌───────┴───────┐   │
        │                           │  │ LocalScraper  │   │
        │                           │  │ .exe          │   │
        │                           │  └───────────────┘   │
        │                           │     writes JSON ─►   │
        │                           │     save_dir         │
        │   POST /ingest/local..    │                      │
        │ ◄─────────────────────────│  HMAC-sign + ingest  │
        │   POST /jobs/{id}/complete│                      │
        │ ◄─────────────────────────│                      │
        └──────────────────────────►└──────────────────────┘
```

## Prerequisites

- Python 3.10+
- `requests >= 2.31` (`pip install -r requirements.txt`)
- LocalScraper installed and license-activated on the worker box
- Env vars from `env.example` set in the daemon's environment

## Required Env Vars

| Var | Purpose |
|---|---|
| `KJLE_API_BASE` | KJLE API root (default `https://kjle-api.onrender.com`) |
| `KJLE_WORKER_API_KEY` | Auth for poll/started/complete endpoints |
| `LOCAL_SCRAPER_EXE_PATH` | Full path to `LocalScraper.exe` |
| `LOCAL_SCRAPER_SAVE_DIR` | Directory for per-job scrape output |
| `LOCAL_SCRAPER_WEBHOOK_SECRET` | HMAC secret for ingest endpoint |
| `WORKER_ID` | Exactly `z820` or `vps` |
| `POLL_INTERVAL_SEC` | Defaults to 15 |
| `WEBHOOK_HANDLER_PORT` | Defaults to 8765 |
| `LOG_LEVEL` | Defaults to `INFO` |

## Installation — Z820 (Windows)

1. Clone or pull the KJLE repo onto the box. (`workers/local_scraper_daemon/`
   is the only directory the daemon needs.)
2. Install Python 3.10+ from python.org. Tick "Add Python to PATH" during install.
3. Open a fresh `cmd` window and verify:
   ```
   python --version
   ```
4. From the daemon directory:
   ```
   cd C:\path\to\kjle\workers\local_scraper_daemon
   pip install -r requirements.txt
   ```
5. Copy `env.example` to `worker.env` (kept OUTSIDE the repo) and fill in real values.
6. Set `WORKER_ID=z820` for this box.
7. Test in foreground (see "Running daemon" below). Once green, schedule it via
   Windows Task Scheduler (see `daemon-windows-setup.md`).

## Installation — VPS (Linux)

> Honest note on LocalScraper on Linux: LocalScraper.exe is a Windows binary.
> Running it on the VPS requires Wine (`sudo apt install wine64`) plus a one-time
> license activation under Wine. Until that's done, the daemon will start cleanly
> but every job it picks up will fail with `ls_exe_missing` or
> `ls_spawn_failed` and post `success=False` back to KJLE. That is the
> intended "queue holder" mode — KJLE still tracks the queue, the job just
> never completes from the VPS side.

1. SSH to the VPS as a user with sudo.
2. Clone or pull the KJLE repo to `/opt/kjle` (or update existing checkout).
3. Install Python + pip:
   ```
   sudo apt update && sudo apt install -y python3 python3-pip
   ```
4. Install daemon deps:
   ```
   sudo pip3 install -r /opt/kjle/workers/local_scraper_daemon/requirements.txt
   ```
5. Create the env file at `/etc/kjle-worker.env` (root-owned, mode 0600).
   Use `env.example` as a template. Set `WORKER_ID=vps`.
6. Create `LOCAL_SCRAPER_SAVE_DIR` (e.g. `/var/lib/kjle/scrapes`) writeable by
   the user that systemd will run the daemon as.
7. Install the systemd unit:
   ```
   sudo cp /opt/kjle/workers/local_scraper_daemon/daemon.service \
           /etc/systemd/system/kjle-local-scraper-daemon.service
   # edit and replace User=REPLACE_ME with the real user
   sudo systemctl daemon-reload
   sudo systemctl enable --now kjle-local-scraper-daemon
   ```
8. Tail logs:
   ```
   sudo journalctl -u kjle-local-scraper-daemon -f
   ```

## Running the daemon

**Foreground (any OS, for testing):**
```
python daemon.py
```
You should see a JSON log line `daemon_starting`, then periodic poll activity.

**Systemd (Linux, prod):** see VPS install section above.

**Windows Task Scheduler (Z820, prod):** see `daemon-windows-setup.md`.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `config_error` on startup | One of the required env vars is empty or `WORKER_ID` isn't `z820`/`vps` |
| `poll_non_200` with status 401 | `KJLE_WORKER_API_KEY` doesn't match Render `WORKER_API_KEY` |
| `ls_exe_missing` | `LOCAL_SCRAPER_EXE_PATH` is wrong or LS isn't installed yet |
| `marker_timeout` after LS exits | LS didn't POST to the webhook URL — verify `--webhook-url` in the spawn args and that `WEBHOOK_HANDLER_PORT` isn't firewalled (it's 127.0.0.1, but personal firewalls sometimes block loopback bindings) |
| `ingest_post` with status 401 | `LOCAL_SCRAPER_WEBHOOK_SECRET` doesn't match Render value |
| Daemon doesn't pick up jobs | Confirm the queued job in `scrape_jobs` has `worker_id` matching this box's `WORKER_ID` |
| Disk filling up | Per-job folders are `rmtree`'d after successful ingest; failed jobs ALSO clean up. If you see leftovers, the daemon crashed mid-job — safe to delete manually |

## End-to-end test WITHOUT a working LocalScraper

Useful when LS isn't installed yet, or you want to test the daemon → KJLE path
in isolation.

1. Set `LOCAL_SCRAPER_EXE_PATH` to a tiny shim script that creates one fake
   lead JSON file in `--save-location`, then POSTs an empty body to the
   `--webhook-url` and exits 0. Example shim (`fake_ls.py`):
   ```python
   import json, os, sys, urllib.request
   save_dir = sys.argv[sys.argv.index("--save-location") + 1]
   webhook  = sys.argv[sys.argv.index("--webhook-url") + 1]
   with open(os.path.join(save_dir, "fake.json"), "w") as f:
       json.dump([{"business_name": "Mock Co", "phone": "+15555550100"}], f)
   urllib.request.urlopen(webhook, data=b'{"status":"done"}', timeout=10)
   ```
   Then point `LOCAL_SCRAPER_EXE_PATH` at `python /full/path/to/fake_ls.py`
   (you'll need to adapt the daemon's spawn to allow that, or wrap with a
   one-line `.bat`/`.sh`).
2. Insert a scrape job for this worker via the KJLE `/scrape/start` endpoint.
3. Watch the daemon logs: you should see
   `job_picked_up` → `ls_spawning` → `ls_exited` → `leads_loaded` →
   `ingest_post success=true` → `job_done`.
4. Confirm the lead appears in KJLE's `leads` table with `source=local_scraper`.
