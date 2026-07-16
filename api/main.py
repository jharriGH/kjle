"""
KJLE API — King James Lead Empire
Main FastAPI application entry point
Deployed on Render: https://kjle-api.onrender.com
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database import init_db
from .notify import notify, NotifyRequest


# ── Empire-wide brain key auth (shared across all KJ services) ────────────────
EMPIRE_BRAIN_KEY = os.environ.get("EMPIRE_BRAIN_KEY", "jim-brain-kje-2026-kingjames")


def verify_brain_key(x_brain_key: str = Header(...)):
    if x_brain_key != EMPIRE_BRAIN_KEY:
        raise HTTPException(status_code=401, detail="Invalid x-brain-key")
    return x_brain_key

# ── Route Imports ─────────────────────────────────────────────────────────────
from .routes import health
from .routes import leads
from .routes import segments
from .routes import segments_engine
from .routes import segment_manager
from .routes import segment_builder
from .routes import pipeline
from .routes import costs
from .routes import pain
from .routes import enrichment
from .routes import enrichment_stage2
from .routes import enrichment_stage3
from .routes import enrichment_stage4
from .routes import enrichment_email_clean
from .routes import enrichment_lookup
from .routes import csv_import
from .routes import lead_management
from .routes import integration_hub
from .routes import budget_control
from .routes import access_auth
from .routes import export
from .routes import push_demoenginez
from .routes import push_voicedrop
from .routes import webhooks
from .routes import scheduler
from .routes import admin_settings
from .routes import commander
from .routes import reachinbox
from .routes import campaigns
from .routes import dnc
from .routes import dnc_channel
from .routes import local_scraper_ingest
from .routes import scrape_jobs
from .routes import dnc_webhooks
from .routes import webhooks_truelist
from .routes import backlog_scrub
from .routes import ava_router
from .routes import campaign_attachments
from .routes import website_audit
from .routes import mail_suppressions
from .routes import scan
from .routes import reverify
from .routes.scheduler import setup_scheduler

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 KJLE API starting up")

    await init_db()
    logger.info("   Database      : ✓ Supabase client initialized")

    logger.info(f"   Yelp          : {'✓ configured' if settings.YELP_API_KEY           else '✗ not set (Stage 2 Yelp disabled)'}")
    logger.info(f"   Outscraper    : {'✓ configured' if settings.OUTSCRAPER_API_KEY     else '✗ not set (Stage 3 disabled)'}")
    logger.info(f"   Firecrawl     : {'✓ configured' if settings.FIRECRAWL_API_KEY      else '✗ not set (Stage 4 disabled)'}")
    logger.info(f"   ReachInbox    : {'✓ configured' if settings.REACHINBOX_API_KEY     else '✗ not set (RI export disabled)'}")
    logger.info(f"   Truelist      : {'✓ configured' if settings.TRUELIST_API_KEY       else '○ not set (use admin_settings table)'}")
    de_mode = "dedicated" if (settings.DEMOENGINEZ_SUPABASE_URL and settings.DEMOENGINEZ_SUPABASE_KEY) else "shared Supabase"
    vd_mode = "dedicated" if (settings.VOICEDROP_SUPABASE_URL   and settings.VOICEDROP_SUPABASE_KEY)   else "shared Supabase"
    logger.info(f"   DemoEnginez   : push enabled → {de_mode} client")
    logger.info(f"   VoiceDrop OS  : push enabled → {vd_mode} client")
    logger.info(f"   Webhooks      : system active ({len(webhooks.SUPPORTED_EVENTS)} event types registered)")
    logger.info(f"   Admin Settings: ✓ loaded")
    logger.info(f"   CSV Import    : ✓ ready")
    logger.info(f"   Lead Mgmt     : ✓ ready")
    logger.info(f"   Segment Bldr  : ✓ ready")
    logger.info(f"   Integration H : ✓ ready")
    logger.info(f"   Budget Ctrl   : ✓ ready")
    logger.info(f"   Access & Auth : ✓ ready")
    logger.info(f"   Email Clean   : ✓ ready (Truelist nightly job active)")

    # Scheduler ownership is env-gated so the 13 jobs can run in a SEPARATE
    # process (kjle-scheduler Background Worker) instead of blocking the API
    # event loop. Default "true" preserves legacy in-process behavior when the
    # var is unset, so a missing var degrades LOUDLY (flapping returns) rather
    # than SILENTLY (all 13 jobs vanish). Cutover sets RUN_SCHEDULER=false on
    # kjle-api ONLY; the worker is the sole owner thereafter.
    run_scheduler = os.getenv("RUN_SCHEDULER", "true").lower() not in ("false", "0", "no")
    _scheduler = None
    if run_scheduler:
        _scheduler = setup_scheduler()
        _scheduler.start()
        logger.info(f"   Scheduler     : ⏰ APScheduler started ({len(_scheduler.get_jobs())} jobs active)")
    else:
        logger.info("   Scheduler     : ⏸️  in-process scheduler DISABLED (RUN_SCHEDULER=false) — owned by kjle-scheduler worker")

    yield

    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
    logger.info("🛑 KJLE API shutting down")


# ── App Init ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="KJLE — King James Lead Empire",
    description=(
        "Master lead intelligence platform. "
        "Ingestion → Enrichment → Segmentation → Export."
    ),
    version="1.0.0",
    docs_url="/kjle/docs",
    redoc_url="/kjle/redoc",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Router Registration ───────────────────────────────────────────────────────
# ⚠️ Registration order matters — specific routes must come before catch-all /{id} routes:
#    - lead_management before leads (prevents /leads/stats + /leads/bulk being swallowed)
#    - segment_builder before segments + segment_manager (prevents /segments/saved + /segments/preview being swallowed)
PREFIX = "/kjle/v1"

app.include_router(health.router,                 prefix=PREFIX,                  tags=["Health"])
app.include_router(lead_management.router,        prefix=PREFIX,                  tags=["Lead Management"])
app.include_router(segment_builder.router,        prefix=PREFIX,                  tags=["Segment Builder"])
app.include_router(integration_hub.router,        prefix=PREFIX,                  tags=["Integration Hub"])
app.include_router(budget_control.router,         prefix=PREFIX,                  tags=["Budget Control"])
app.include_router(access_auth.router,            prefix=PREFIX,                  tags=["Access & Auth"])
app.include_router(leads.router,                  prefix=PREFIX,                  tags=["Leads"])
app.include_router(segments_engine.router,        prefix=PREFIX,                  tags=["Segmentation"])
app.include_router(segments.router,               prefix=PREFIX,                  tags=["Segments"])
app.include_router(segment_manager.router,        prefix=PREFIX,                  tags=["Segment Manager"])
app.include_router(pipeline.router,               prefix=PREFIX,                  tags=["Pipeline"])
app.include_router(costs.router,                  prefix=PREFIX,                  tags=["Costs"])
app.include_router(pain.router,                   prefix=PREFIX,                  tags=["Pain Intelligence"])
app.include_router(enrichment.router,             prefix=f"{PREFIX}/enrichment",  tags=["Enrichment"])
app.include_router(enrichment_stage2.router,      prefix=f"{PREFIX}/enrichment",  tags=["Enrichment Stage 2"])
app.include_router(enrichment_stage3.router,      prefix=f"{PREFIX}/enrichment",  tags=["Enrichment Stage 3"])
app.include_router(enrichment_stage4.router,      prefix=f"{PREFIX}/enrichment",  tags=["Enrichment Stage 4"])
app.include_router(enrichment_email_clean.router, prefix=f"{PREFIX}/enrichment",  tags=["Enrichment — Email Clean"])
app.include_router(enrichment_lookup.router,      prefix=f"{PREFIX}/enrichment",  tags=["Enrichment — Lookup"])
app.include_router(csv_import.router,             prefix=PREFIX,                  tags=["CSV Import"])
app.include_router(export.router,                 prefix=PREFIX,                  tags=["Export"])
app.include_router(push_demoenginez.router,       prefix=PREFIX,                  tags=["Push — DemoEnginez"])
app.include_router(push_voicedrop.router,         prefix=PREFIX,                  tags=["Push — VoiceDrop"])
app.include_router(webhooks.router,               prefix=PREFIX,                  tags=["Webhooks"])
app.include_router(scheduler.router,              prefix=PREFIX,                  tags=["Scheduler"])
app.include_router(commander.router,              prefix=PREFIX,                  tags=["Commander"])
app.include_router(reachinbox.router,             prefix=PREFIX,                  tags=["ReachInbox"])
app.include_router(dnc.router,                    prefix=PREFIX,                  tags=["DNC"])
app.include_router(dnc_channel.router,            prefix=PREFIX,                  tags=["DNC — Channel-Aware"])
app.include_router(local_scraper_ingest.router,   prefix=PREFIX,                  tags=["Ingest — Local Scraper"])
app.include_router(scrape_jobs.router,            prefix=PREFIX,                  tags=["Scrape Jobs — Worker Queue"])
app.include_router(dnc_webhooks.router,           prefix=PREFIX,                  tags=["DNC Webhooks"])
app.include_router(webhooks_truelist.router,      prefix=PREFIX,                  tags=["Truelist Webhooks"])
app.include_router(backlog_scrub.router,          prefix=PREFIX,                  tags=["Backlog Scrub"])
app.include_router(admin_settings.router,         prefix=PREFIX,                  tags=["Admin Settings"])
app.include_router(campaigns.router,              prefix=PREFIX,                  tags=["Campaign Performance"])
app.include_router(ava_router.router,             prefix=PREFIX,                  tags=["AVA Router"])
app.include_router(campaign_attachments.router,   prefix=PREFIX,                  tags=["Campaign Attachments"])
app.include_router(website_audit.router,          prefix=PREFIX,                  tags=["Website Audit"])
app.include_router(mail_suppressions.router,      prefix=PREFIX,                  tags=["Mail Suppressions"])
app.include_router(scan.router,                   prefix=PREFIX,                  tags=["Scan"])
app.include_router(reverify.router,               prefix=PREFIX,                  tags=["Reverify"])


# ── Empire-wide notification endpoints (top-level, x-brain-key auth) ──────────
@app.post("/notify", dependencies=[Depends(verify_brain_key)], tags=["Notify"])
async def post_notify(req: NotifyRequest):
    return await notify(req)


@app.post("/notify/test", dependencies=[Depends(verify_brain_key)], tags=["Notify"])
async def notify_test():
    return await notify(NotifyRequest(
        severity="info",
        message="P2 notify smoke test — endpoint live on kjle",
        channel="both",
    ))
