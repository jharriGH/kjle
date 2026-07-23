"""
worker_scheduler.py — standalone scheduler host for the kjle-scheduler service.

Runs the SAME setup_scheduler() used by the API, but in its own process with
its own event loop, so blocking jobs can never stall the API's event loop.
kjle-api is set to RUN_SCHEDULER=false and stops running the scheduler; this
worker becomes the SOLE owner of all 13 jobs.

CRITICAL: exactly ONE process may run the scheduler. Never start this worker
while kjle-api still has RUN_SCHEDULER=true, or every job fires twice.

RUN:  python -m api.worker_scheduler        # from repo root
"""
import asyncio
import logging
import signal

from .database import init_db
from .routes.scheduler import setup_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("kjle.scheduler_worker")


async def main() -> None:
    # MUST run before setup_scheduler(): jobs call get_db(), which raises if the
    # module-global Supabase client was never initialized.
    await init_db()
    logger.info("Supabase client initialized for scheduler worker")

    scheduler = setup_scheduler()
    scheduler.start()
    logger.info("scheduler worker started (%d jobs active)", len(scheduler.get_jobs()))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    try:
        await stop.wait()
    finally:
        scheduler.shutdown(wait=True, timeout=30)
        logger.info("scheduler worker shutting down")


if __name__ == "__main__":
    asyncio.run(main())
