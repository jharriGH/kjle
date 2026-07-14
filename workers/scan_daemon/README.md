# KJLE WebSignalz Scan Daemon (Phase 3)

## Purpose

VPS-resident daemon that polls `scan_jobs` for queued URLs, runs headless
Chromium + vendored axe-core 4.10.2, and writes append-only rows to
`scan_results`. Designed for on-demand ComplianceEnginez scans.

## Architecture

```
   scan_jobs table (Supabase)
        |
        |  poll (status='queued', priority DESC)
        v
   daemon.py (ThreadPoolExecutor, max 4 workers)
        |
        |  per job: claim → navigate → axe.run() → INSERT scan_results → mark done/error
        v
   scan_results table (Supabase)
```

## Required Env Vars

| Var | Purpose |
|---|---|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | service_role key (never anon key) |
| `SCAN_CONCURRENCY` | parallel browsers (default: 4, hard cap: 4) |
| `POLL_INTERVAL_SEC` | sleep when queue is empty (default: 15) |
| `SCAN_BATCH_SIZE` | jobs per poll cycle (default: SCAN_CONCURRENCY) |
| `WORKER_ID` | label in metadata (default: scan-daemon) |
| `LOG_LEVEL` | INFO or DEBUG (default: INFO) |

## Installation — VPS (Linux)

1. SSH to VPS, pull the repo:
   ```
   cd /opt/kjle && git pull origin main
   ```
2. Install Python deps:
   ```
   pip3 install -r /opt/kjle/workers/scan_daemon/requirements.txt
   ```
3. Install Chromium + system deps:
   ```
   python3 -m playwright install chromium
   python3 -m playwright install-deps chromium   # may require sudo
   ```
4. Create `/etc/kjle-scan-daemon.env` (root-owned, mode 0600) using the
   vars above. Pull keys from the Brain vault — never hard-code.
5. Install the systemd unit:
   ```
   sudo cp /opt/kjle/workers/scan_daemon/daemon.service \
           /etc/systemd/system/kjle-scan-daemon.service
   # Edit User=REPLACE_ME to the unprivileged user that runs the daemon.
   sudo systemctl daemon-reload
   sudo systemctl enable --now kjle-scan-daemon
   ```
6. Tail logs:
   ```
   sudo journalctl -u kjle-scan-daemon -f
   ```

**DO NOT attempt to start the service during dispatch** — the dispatch user
is not in sudoers. Deployment is a manual step for Jim.

## Accessibility Score Formula

Score (0-100) is computed by impact-weighted deductions:

```
score = max(0, 100 - critical×25 - serious×10 - moderate×3 - minor×1)
```

Violation counts are per violation-type (not per affected node). The full
node-level detail is preserved in the `violations` JSONB column.

## Troubleshooting

| Symptom | Check |
|---|---|
| `config_error` on startup | `SUPABASE_URL` or `SUPABASE_SERVICE_KEY` unset |
| `axe_loaded` not logged | `axe.min.js` missing from daemon dir |
| `poll_error` on every cycle | Supabase connectivity or service_role key wrong |
| Jobs stay `running` forever | Daemon crashed mid-job — manually reset to `queued` |
| `scan_timeout` for all URLs | Chromium system deps not installed (run `playwright install-deps`) |
