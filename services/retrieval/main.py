"""
VibeForge — Retrieval Service HTTP API (Phase 1)
=================================================
FastAPI wrapper around RetrievalService.
Called by the worker's retrieval_fn which the Domain Wizard uses.

Endpoints:
    POST /retrieve       — main retrieval endpoint
    GET  /health         — liveness check
    GET  /metrics        — Prometheus metrics

Contract C8: tenant_id is mandatory on every /retrieve call.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
)

# ── Config ─────────────────────────────────────────────────────────────────────
QDRANT_URL      = os.getenv("QDRANT_URL", "http://qdrant:6333")
METRICS_PORT    = int(os.getenv("RETRIEVAL_METRICS_PORT", "9002"))

# ── Prometheus metrics ─────────────────────────────────────────────────────────
retrieval_requests = Counter(
    "vibeforge_retrieval_requests_total",
    "Total retrieval requests",
    ["tenant_id", "status"],
)
retrieval_latency = Histogram(
    "vibeforge_retrieval_latency_seconds",
    "Retrieval latency in seconds",
)
retrieval_chunks = Histogram(
    "vibeforge_retrieval_chunks_returned",
    "Number of chunks returned per retrieval",
    buckets=[0, 1, 2, 3, 4, 5],
)

# ── Request / Response models ──────────────────────────────────────────────────

class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The retrieval query")
    tenant_id: str = Field(..., min_length=1, description="Tenant ID — mandatory (Contract C8)")
    top_k: int = Field(default=5, ge=1, le=20, description="Number of chunks to return")
    include_shared: bool = Field(default=True, description="Include shared global pack docs")


class ChunkResponse(BaseModel):
    chunk_id: str
    text: str
    source_name: str
    source_version: str
    heading: str
    score: float
    is_shared: bool


class RetrieveResponse(BaseModel):
    query: str
    tenant_id: str
    chunks: list[ChunkResponse]
    total_candidates: int
    reranked: bool
    error: str = ""


# ── Global retrieval service instance ─────────────────────────────────────────
_retrieval_service = None


def _load_model_background():
    """Load bge-m3 in a background thread so Cloud Run health check passes immediately."""
    global _retrieval_service
    import os
    os.environ.setdefault("HF_HOME", "/tmp/hf_cache")
    os.environ.setdefault("TRANSFORMERS_CACHE", "/tmp/hf_cache")
    logger.info("[retrieval] Background model load starting...")
    try:
        from service import RetrievalService
        _retrieval_service = RetrievalService(qdrant_url=QDRANT_URL)
        logger.info("[retrieval] bge-m3 loaded OK — service fully ready")
    except Exception as e:
        logger.error("[retrieval] Model load failed: %s", e)
        logger.warning("[retrieval] /retrieve returns 503 until model is available")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start HTTP server immediately; load bge-m3 in background thread."""
    import threading
    thread = threading.Thread(target=_load_model_background, daemon=True)
    thread.start()
    logger.info("[retrieval] HTTP server ready on port — model loading in background")

    yield

    logger.info("[retrieval] Shutting down")


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="VibeForge Retrieval Service",
    version="1.0.0",
    description="RAG retrieval with tenant isolation (Contract C8)",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {
        "status": "healthy" if _retrieval_service is not None else "loading",
        "qdrant_url": QDRANT_URL,
    }


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(request: RetrieveRequest):
    """
    Retrieve relevant chunks for a query.
    Contract C8: tenant_id is mandatory.
    """
    if _retrieval_service is None:
        raise HTTPException(
            status_code=503,
            detail="Retrieval service is still loading models. Try again in a moment.",
        )

    start = time.time()
    try:
        result = await _retrieval_service.retrieve(
            query=request.query,
            tenant_id=request.tenant_id,
            top_k=request.top_k,
            include_shared=request.include_shared,
        )

        latency = time.time() - start
        retrieval_latency.observe(latency)
        retrieval_chunks.observe(len(result.chunks))

        status = "error" if result.error else "ok"
        retrieval_requests.labels(
            tenant_id=request.tenant_id[:8],
            status=status,
        ).inc()

        if result.error:
            raise HTTPException(status_code=500, detail=result.error)

        return RetrieveResponse(
            query=result.query,
            tenant_id=result.tenant_id,
            chunks=[
                ChunkResponse(
                    chunk_id=c.chunk_id,
                    text=c.text,
                    source_name=c.source_name,
                    source_version=c.source_version,
                    heading=c.heading,
                    score=c.score,
                    is_shared=c.is_shared,
                )
                for c in result.chunks
            ],
            total_candidates=result.total_candidates,
            reranked=result.reranked,
        )

    except HTTPException:
        raise
    except Exception as e:
        retrieval_requests.labels(tenant_id=request.tenant_id[:8], status="error").inc()
        logger.error("[retrieval] Unexpected error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8001,
        log_level="info",
    )
