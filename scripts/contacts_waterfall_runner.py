"""
contacts_waterfall_runner.py — KJLE persistent waterfall service runner

Systemd-managed, single-instance via fcntl.flock.
Loops: run waterfall → probe remaining work → sleep → repeat.
When no work: idle-loop at 600s to catch new ingests.
"""
import fcntl
import logging
import os
import subprocess
import sys
import time

from dotenv import load_dotenv
import psycopg2

load_dotenv("/opt/kjle/.env")

DATABASE_URL = os.environ["DATABASE_URL"]
LOCK_FILE = "/tmp/contacts_waterfall.lock"
LOG_FILE = "/tmp/contacts_waterfall_runner.log"
WATERFALL_SCRIPT = "/opt/kjle/scripts/contacts_waterfall.py"

SLEEP_WORK = 60    # seconds between passes when work remains
SLEEP_IDLE = 600   # seconds between checks when fully classified

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def acquire_lock() -> object:
    """
    Exclusive non-blocking flock on LOCK_FILE.
    If already held by another process, exit immediately — never two copies.
    The OS auto-releases the lock when this process dies, so systemd restarts
    safely acquire it without stale-lock cleanup.
    """
    lf = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.error(
            f"Lock {LOCK_FILE} is already held — another runner is active. Exiting immediately."
        )
        sys.exit(1)
    lf.write(f"{os.getpid()}\n")
    lf.flush()
    log.info(f"Lock acquired by pid={os.getpid()}")
    return lf  # keep open to hold the lock for our lifetime


def probe_remaining_work() -> tuple:
    """
    Three cheap LIMIT-1 existence probes (uses indexed columns).
    Returns (has_work: bool, reason: str).
    """
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM contacts "
                "WHERE email_provider = 'unknown' AND primary_email IS NOT NULL LIMIT 1"
            )
            provider_todo = cur.fetchone() is not None

            cur.execute(
                "SELECT 1 FROM contacts "
                "WHERE email_trust IS NULL AND primary_email IS NOT NULL LIMIT 1"
            )
            trust_todo = cur.fetchone() is not None

            cur.execute(
                "SELECT 1 FROM contacts "
                "WHERE (email_status IS NULL OR email_status = 'unvalidated') "
                "AND primary_email IS NOT NULL LIMIT 1"
            )
            status_todo = cur.fetchone() is not None
        conn.close()

        reasons = []
        if provider_todo:
            reasons.append("email_provider=unknown")
        if trust_todo:
            reasons.append("email_trust IS NULL")
        if status_todo:
            reasons.append("email_status IS NULL")

        return bool(reasons), (", ".join(reasons) if reasons else "all classified")
    except Exception as exc:
        log.error(f"DB probe failed: {exc} — assuming work remains to be safe")
        return True, f"db_error:{exc}"


def run_waterfall_pass(pass_num: int) -> int:
    """
    Invoke contacts_waterfall.py as a subprocess (clean state, no shared globals).
    Returns the process returncode.
    """
    log.info(f"Pass {pass_num}: launching contacts_waterfall.py")
    t0 = time.time()
    result = subprocess.run(
        [sys.executable, WATERFALL_SCRIPT],
        cwd="/opt/kjle",
    )
    elapsed = int(time.time() - t0)
    log.info(f"Pass {pass_num}: waterfall exited rc={result.returncode} elapsed={elapsed}s")
    return result.returncode


def main():
    log.info("contacts_waterfall_runner starting — acquiring single-instance lock")
    lock_fh = acquire_lock()

    pass_num = 0
    try:
        while True:
            pass_num += 1
            has_work, reason = probe_remaining_work()

            if not has_work:
                log.info(
                    f"Pass {pass_num}: all contacts classified. "
                    f"Idle-sleeping {SLEEP_IDLE}s to watch for new ingests."
                )
                time.sleep(SLEEP_IDLE)
                continue

            log.info(f"Pass {pass_num}: work detected — {reason}")
            run_waterfall_pass(pass_num)

            has_work_after, reason_after = probe_remaining_work()
            if has_work_after:
                log.info(
                    f"Pass {pass_num}: work still remains ({reason_after}). "
                    f"Sleeping {SLEEP_WORK}s then running again."
                )
                time.sleep(SLEEP_WORK)
            else:
                log.info(
                    f"Pass {pass_num}: classification complete after this pass. "
                    f"Idle-sleeping {SLEEP_IDLE}s."
                )
                time.sleep(SLEEP_IDLE)

    except KeyboardInterrupt:
        log.info("contacts_waterfall_runner received interrupt — exiting cleanly")
    finally:
        lock_fh.close()
        try:
            os.unlink(LOCK_FILE)
        except FileNotFoundError:
            pass
        log.info("contacts_waterfall_runner stopped")


if __name__ == "__main__":
    main()
