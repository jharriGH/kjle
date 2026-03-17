"""
KJLE API — Main Application Entry Point
FastAPI app with all routers registered under /kjle/v1
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routes import (
    leads,
    enrichment,
    segments,
    pain_points,
    export,
    admin_settings,
    admin_costs,
    webhooks,
    automation,
    push_integrations,
    pipeline,
    enrichment_email_clean,  # Prompt 26B — Truelist.io
)

# ── App init ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="KJLE API",
    description="King James Lead Empire — FastAPI Backend",
    version="1.0.0",
)

PREFIX = "/kjle/v1"

# ── CORS ─────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(leads.router,              prefix=f"{PREFIX}/leads",             tags=["Leads"])
app.include_router(enrichment.router,         prefix=f"{PREFIX}/enrichment",        tags=["Enrichment"])
app.include_router(segments.router,           prefix=f"{PREFIX}/segments",          tags=["Segments"])
app.include_router(pain_points.router,        prefix=f"{PREFIX}/pain",              tags=["Pain Points"])
app.include_router(export.router,             prefix=f"{PREFIX}/export",            tags=["Export"])
app.include_router(admin_settings.router,     prefix=f"{PREFIX}/admin",             tags=["Admin — Settings"])
app.include_router(admin_costs.router,        prefix=f"{PREFIX}/admin",             tags=["Admin — Costs"])
app.include_router(webhooks.router,           prefix=f"{PREFIX}/webhooks",          tags=["Webhooks"])
app.include_router(automation.router,         prefix=f"{PREFIX}/automation",        tags=["Automation"])
app.include_router(push_integrations.router,  prefix=f"{PREFIX}/push",              tags=["Push Integrations"])
app.include_router(pipeline.router,           prefix=f"{PREFIX}/pipeline",          tags=["Pipeline"])

# Prompt 26B — Truelist.io Email Cleaning
app.include_router(
    enrichment_email_clean.router,
    prefix=f"{PREFIX}/enrichment",
    tags=["Enrichment — Email Clean"],
)

# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "service": "KJLE API", "version": "1.0.0"}


@app.get(f"{PREFIX}/health", tags=["Health"])
async def health():
    return {"status": "ok", "prefix": PREFIX}
