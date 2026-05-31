# ── Stage 1: build the React frontend ────────────────────────────────────
FROM node:20-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --silent
COPY frontend/ ./
# Vite writes to ../backend/static by config; for an isolated stage, write to ./dist.
RUN npx vite build --outDir dist

# ── Stage 2: Python backend serving API + built frontend ─────────────────
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY data /app/data
COPY --from=frontend /fe/dist /app/backend/static

ENV PYTHONPATH=/app

EXPOSE 8000
# Apply migrations (Postgres + Neo4j + GraphDB, all idempotent), then serve.
# GraphDB setup creates the repo if missing and uploads the apple ontology;
# if GraphDB is unreachable we DON'T crash — UE1/UE2/UE3 still work.
CMD ["sh", "-c", "python -m backend.data.migrate && python -m backend.data.neo4j_client && (python -m backend.data.graphdb_client || echo 'GraphDB setup skipped — UE4 will be unavailable') && uvicorn backend.main:app --host 0.0.0.0 --port 8000"]
