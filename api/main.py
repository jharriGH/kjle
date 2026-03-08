"""
KJLE — King James Lead Empire
FastAPI Backend v1.0 — Prompt 3
================================
Main application entry point.
Namespaced routes: /kjle/v1/...
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import logging
from .config import settings
from .database import init_db
from .routes import leads, segments, pipeline, costs, health, pain

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kjle")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("KJLE API starting up...")
    await init_db()
    log.info("Database connection established")
    yield
    log.info("KJLE API shutting down")


app = FastAPI(
    title="KJLE — King James Lead Empire API",
    description="Lead intelligence platform API",
    version="1.0.0",
    docs_url="/kjle/docs",
    redoc_url="/kjle/redoc",
    openapi_url="/kjle/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# — Route registration ————————————————————————————
PREFIX = "/kjle/v1"

app.include_router(health.router,    prefix=PREFIX, tags=["Health"])
app.include_router(leads.router,     prefix=PREFIX, tags=["Leads"])
app.include_router(segments.router,  prefix=PREFIX, tags=["Segments"])
app.include_router(pipeline.router,  prefix=PREFIX, tags=["Pipeline"])
app.include_router(costs.router,     prefix=PREFIX, tags=["Costs"])
app.include_router(pain.router,      prefix=PREFIX, tags=["Pain Intelligence"])
