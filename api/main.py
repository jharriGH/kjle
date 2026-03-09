"""
KJLE API — King James Lead Empire
Main FastAPI application entry point
Deployed on Render: https://kjle-api.onrender.com
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings

# ── Route Imports ─────────────────────────────────────────────────────────────
from .routes import health
from .routes import leads
from .routes import segments
from .routes import segments_engine
from .routes import segment_manager
from .routes import pipeline
from .routes import costs
from .routes import pain
from .routes import enrichment
from .routes import enrichment_stage2
from .routes import enrichment_stage3
from .routes import enrichment_stage4
from .routes import export

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
    logger.info(f"   Yelp          : {'✓ configured' if settings.YELP_API_KEY        else '✗ not set (Stage 2 Yelp disabled)'}")
    logger.info(f"   Outscraper    : {'✓ configured' if settings.OUTSCRAPER_API_KEY  else '✗ not set (Stage 3 disabled)'}")
    logger.info(f"   Firecrawl     : {'✓ configured' if settings.FIRECRAWL_API_KEY   else '✗ not set (Stage 4 disabled)'}")
    logger.info(f"   ReachInbox    : {'✓ configured' if settings.REACHINBOX_API_KEY  else '✗ not set (RI export disabled)'}")
    yield
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
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Router Registration ───────────────────────────────────────────────────────
PREFIX = "/kjle/v1"

app.include_router(health.router,             prefix=PREFIX,                  tags=["Health"])
app.include_router(leads.router,              prefix=PREFIX,                  tags=["Leads"])
app.include_router(segments.router,           prefix=PREFIX,                  tags=["Segments"])
app.include_router(segments_engine.router,    prefix=PREFIX,                  tags=["Segmentation"])
app.include_router(segment_manager.router,    prefix=PREFIX,                  tags=["Segment Manager"])
app.include_router(pipeline.router,           prefix=PREFIX,                  tags=["Pipeline"])
app.include_router(costs.router,              prefix=PREFIX,                  tags=["Costs"])
app.include_router(pain.router,               prefix=PREFIX,                  tags=["Pain Intelligence"])
app.include_router(enrichment.router,         prefix=f"{PREFIX}/enrichment",  tags=["Enrichment"])
app.include_router(enrichment_stage2.router,  prefix=f"{PREFIX}/enrichment",  tags=["Enrichment Stage 2"])
app.include_router(enrichment_stage3.router,  prefix=f"{PREFIX}/enrichment",  tags=["Enrichment Stage 3"])
app.include_router(enrichment_stage4.router,  prefix=f"{PREFIX}/enrichment",  tags=["Enrichment Stage 4"])
app.include_router(export.router,             prefix=PREFIX,                  tags=["Export"])
