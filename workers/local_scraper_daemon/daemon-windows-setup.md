# Windows Task Scheduler setup — KJLE Local Scraper daemon (Z820)

These instructions assume `daemon.py` has already been tested in foreground
(`python daemon.py`) and is exiting cleanly when you Ctrl-C it.

## 1 — Pick the right Python interpreter

In an admin `cmd`:

```
where python
```

Note the full path (e.g. `C:\Users\jim\AppData\Local\Programs\Python\Python311\python.exe`).
You'll paste this into Task Scheduler. Do NOT rely on `python` being on the
PATH of `SYSTEM` — Task Scheduler runs jobs in a stripped environment.

## 2 — Stage the env file

Create `C:\KJLE\worker.env` (NOT inside the repo — keep credentials off git).
Use `env.example` as a template. Lock down ACLs to your user only:

```
icacls C:\KJLE\worker.env /inheritance:r /grant:r "%USERNAME%:F"
```

## 3 — Create the scheduled task

1. Open Task Scheduler → Create Task (not "Create Basic Task").
2. **General tab**
   - Name: `KJLE Local Scraper Daemon`
   - Run whether user is logged on or not
   - Run with highest privileges
   - Configure for Windows 10/11
3. **Triggers tab**
   - New → At startup → OK
4. **Actions tab**
   - New → Start a program
   - Program/script: full path to `python.exe` from step 1
   - Add arguments: `daemon.py`
   - Start in: `C:\path\to\kjle\workers\local_scraper_daemon`
5. **Conditions tab**
   - Untick "Start only if on AC power" if Z820 is on battery occasionally.
6. **Settings tab**
   - Tick "If the task fails, restart every: 1 minute" / "Attempt restart up to: 3 times"
   - Tick "If the running task does not end when requested, force it to stop"

Since Task Scheduler doesn't accept inline env vars cleanly, use a wrapper
batch file as the program instead:

`C:\KJLE\start-daemon.bat`:
```
@echo off
for /f "tokens=1,* delims==" %%A in (C:\KJLE\worker.env) do set %%A=%%B
cd /d C:\path\to\kjle\workers\local_scraper_daemon
"C:\full\path\to\python.exe" daemon.py >> C:\KJLE\daemon.log 2>&1
```

Then point Task Scheduler's Program/script at the batch file.

## 4 — Verify it actually starts

```
schtasks /Run /TN "KJLE Local Scraper Daemon"
type C:\KJLE\daemon.log
```

You should see the JSON `daemon_starting` log line within a couple seconds.

## 5 — Viewing logs

The simplest path is the redirected log file (`C:\KJLE\daemon.log` in the
batch wrapper above). Each line is a standalone JSON object, so `jq` /
Notepad++ JSON plugins parse it cleanly:

```
type C:\KJLE\daemon.log | findstr ingest_post
```

Optional upgrade: pipe through `nssm` instead of Task Scheduler. NSSM gives
proper service-style log rotation, restart-on-crash, and per-event-log
visibility. We're staying on Task Scheduler for now to avoid the extra
dependency.

## Common Windows gotchas

- **Python not found**: Task Scheduler runs with a minimal PATH. Always use the
  full `python.exe` path or wrap with a batch file that sets PATH.
- **Antivirus quarantines LocalScraper.exe**: Add an exclusion for
  `LocalScraper.exe` and `LOCAL_SCRAPER_SAVE_DIR` in Defender / 3rd-party AV.
  Otherwise the daemon will see `ls_spawn_failed` with permission errors.
- **Save dir permissions**: The user the task runs as must have write access
  to `LOCAL_SCRAPER_SAVE_DIR`. If you run as SYSTEM, that's fine; if as a
  specific user, `icacls C:\KJLE\scrapes /grant USER:M` to be safe.
- **Loopback port already in use**: If something else on the box is bound to
  port 8765 (the default `WEBHOOK_HANDLER_PORT`), change it in `worker.env`.
  Listing in use: `netstat -ano | findstr :8765`.
- **Long file paths**: `LocalScraper.exe` and per-job save folders can blow
  past Windows' 260-char path limit. Keep `LOCAL_SCRAPER_SAVE_DIR` shallow
  (e.g. `C:\KJLE\scrapes`).
- **UTF-8 in env vars**: `setx` and `.bat` files love mangling non-ASCII.
  Stick to plain ASCII for all env values to avoid HMAC mismatches caused by
  invisible byte differences in `LOCAL_SCRAPER_WEBHOOK_SECRET`.
