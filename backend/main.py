import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api.routes import router as api_router

# Make our own log.info() calls visible alongside Uvicorn's access logs.
# Without this, anything we log via the stdlib root logger is filtered out at
# WARNING level and progress messages from the ingest pipelines never appear
# in `docker compose logs`.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
# Quiet noisy chatter from the Neo4j driver and httpx.
logging.getLogger("neo4j").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

app = FastAPI(title="UC5 RAG Apple", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api")

# Serve the built frontend (Docker stage 2 copies it here). In local dev with
# the Vite dev server on :5173, this directory simply doesn't exist and we skip
# mounting — the dev proxy handles /api routing.
_static_dir = Path(__file__).parent / "static"
if _static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
