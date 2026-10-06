from __future__ import annotations

"""
VibeForge Ingestion Service
============================
RAG pipeline: MinIO (vf-docs) → Docling → chunks → bge-m3 → Qdrant

Object key convention:  {tenant_id}/{filename}
  e.g.  00000000-0000-0000-0000-000000000001/api-design.pdf

On every poll cycle (default 30s):
  1. List all objects in MINIO_BUCKET_DOCS
  2. Skip objects already in ingested_docs table
  3. Download → Docling parse → chunk (512 words / 64 overlap)
  4. bge-m3 dense encode → Qdrant upsert (collection: vibeforge_{tenant_id_nodash})
  5. INSERT INTO ingested_docs

Prometheus metrics on METRICS_PORT (default 9001).
"""

import asyncio
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path

import asyncpg
from google.cloud import storage as gcs_storage
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from qdrant_client import QdrantClient
from qdrant_client import models as qm

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("ingestion")


# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


MINIO_ENDPOINT   = _env("MINIO_ENDPOINT", "minio:9000")
MINIO_ACCESS_KEY = _env("MINIO_ROOT_USER", "vibeforge")
MINIO_SECRET_KEY = _env("MINIO_ROOT_PASSWORD", "vibeforge_minio_secret")
MINIO_BUCKET     = _env("MINIO_BUCKET_DOCS", "vf-docs")

QDRANT_URL       = _env("QDRANT_URL", "http://qdrant:6333")
DATABASE_URL     = _env(
    "DATABASE_URL_SYNC",
    "postgresql://vibeforge:vibeforge_dev_secret@postgres:5432/vibeforge",
)

POLL_INTERVAL    = int(_env("INGESTION_POLL_INTERVAL", "30"))
METRICS_PORT     = int(_env("INGESTION_METRICS_PORT", "9001"))
CHUNK_SIZE       = int(_env("CHUNK_SIZE", "512"))   # words
CHUNK_OVERLAP    = int(_env("CHUNK_OVERLAP", "64"))  # words
EMBED_BATCH_SIZE = int(_env("EMBED_BATCH_SIZE", "8"))
EMBED_DIM        = 1024  # bge-m3 dense vector dimension


# ──────────────────────────────────────────────────────────────────────────────
# Prometheus metrics
# ──────────────────────────────────────────────────────────────────────────────
DOCS_INGESTED = Counter(
    "vibeforge_ingestion_docs_ingested_total",
    "Total documents successfully ingested",
    ["tenant_id"],
)
CHUNKS_CREATED = Counter(
    "vibeforge_ingestion_chunks_total",
    "Total chunks embedded and stored in Qdrant",
    ["tenant_id"],
)
INGESTION_ERRORS = Counter(
    "vibeforge_ingestion_errors_total",
    "Total ingestion errors",
    ["reason"],
)
INGESTION_DURATION = Histogram(
    "vibeforge_ingestion_doc_duration_seconds",
    "Time to ingest one document end-to-end",
    buckets=[5, 10, 30, 60, 120, 300],
)
QUEUE_DEPTH = Gauge(
    "vibeforge_ingestion_queue_depth",
    "Objects in MinIO bucket not yet ingested",
)


# ──────────────────────────────────────────────────────────────────────────────
# Text utilities
# ──────────────────────────────────────────────────────────────────────────────
def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Split text into word-level chunks with overlap.
    Returns empty list if text has fewer than 10 words.
    """
    words = text.split()
    if len(words) < 10:
        return []
    chunks: list[str] = []
    step = max(1, size - overlap)
    i = 0
    while i < len(words):
        chunk = " ".join(words[i : i + size])
        if chunk.strip():
            chunks.append(chunk)
        i += step
    return chunks


def collection_name(tenant_id: str) -> str:
    """
    Qdrant collection name for a tenant.
    Dashes removed because Qdrant collection names must be valid identifiers.
    e.g. '00000000-0000-0000-0000-000000000001' → 'vibeforge_00000000000000000000000000000001'
    """
    return f"vibeforge_{tenant_id.replace('-', '')}"


def point_id_from_key(object_key: str, chunk_idx: int) -> str:
    """
    Deterministic UUID for a Qdrant point — derived from object_key + chunk index.
    Uses UUIDv5 (namespace=DNS) so it's reproducible across restarts.
    """
    seed = f"{object_key}::{chunk_idx}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))


# ──────────────────────────────────────────────────────────────────────────────
# Qdrant helpers
# ──────────────────────────────────────────────────────────────────────────────
def ensure_collection(qdrant: QdrantClient, coll: str) -> None:
    """Create the Qdrant collection for a tenant if it doesn't exist."""
    existing = {c.name for c in qdrant.get_collections().collections}
    if coll not in existing:
        qdrant.create_collection(
            collection_name=coll,
            vectors_config=qm.VectorParams(
                size=EMBED_DIM,
                distance=qm.Distance.COSINE,
            ),
        )
        # Payload index for fast tenant_id filtering (Contract C8)
        qdrant.create_payload_index(
            collection_name=coll,
            field_name="tenant_id",
            field_schema=qm.PayloadSchemaType.KEYWORD,
        )
        logger.info("Created Qdrant collection: %s", coll)


# ──────────────────────────────────────────────────────────────────────────────
# Per-document ingestion
# ──────────────────────────────────────────────────────────────────────────────
async def ingest_object(
    *,
    minio_client: Minio,
    qdrant: QdrantClient,
    model,             # BGEM3FlagModel — passed in to avoid re-loading
    db_pool: asyncpg.Pool,
    bucket: str,
    object_key: str,
) -> None:
    """
    Full ingestion pipeline for one MinIO object.

    object_key must follow the convention: {tenant_id}/{filename}
    """
    t0 = time.monotonic()

    # ── Parse tenant_id from object key ──────────────────────────────────────
    parts = object_key.split("/", 1)
    if len(parts) < 2:
        logger.warning("Skipping object with no tenant prefix: %s", object_key)
        INGESTION_ERRORS.labels(reason="no_tenant_prefix").inc()
        return

    tenant_id, filename = parts[0], parts[1]

    # ── Skip if already ingested ──────────────────────────────────────────────
    async with db_pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM ingested_docs WHERE bucket = $1 AND object_key = $2",
            bucket,
            object_key,
        )
    if existing:
        return   # already done

    logger.info("Ingesting  tenant=%s  file=%s", tenant_id, filename)

    # ── Download from MinIO → temp file ──────────────────────────────────────
    suffix = Path(filename).suffix.lower() or ".bin"
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        minio_client.fget_object(bucket, object_key, tmp_path)

        # ── Parse with Docling ────────────────────────────────────────────────
        # Import inside the function: Docling has a long import chain and we only
        # want it loaded after bge-m3 is already in memory.
        from docling.document_converter import DocumentConverter  # noqa: PLC0415

        converter = DocumentConverter()
        doc_result = converter.convert(tmp_path)
        text = doc_result.document.export_to_markdown()

        if not text.strip():
            logger.warning("Empty document after parsing: %s", object_key)
            INGESTION_ERRORS.labels(reason="empty_document").inc()
            return

        # ── Chunk ─────────────────────────────────────────────────────────────
        chunks = chunk_text(text)
        if not chunks:
            logger.warning("No chunks produced for: %s", object_key)
            INGESTION_ERRORS.labels(reason="no_chunks").inc()
            return

        logger.info("  %d chunks from %s", len(chunks), filename)

        # ── Embed with bge-m3 ─────────────────────────────────────────────────
        # encode() returns dict with "dense_vecs" key
        embeddings = model.encode(
            chunks,
            batch_size=EMBED_BATCH_SIZE,
            max_length=512,
        )["dense_vecs"]

        # ── Upsert to Qdrant ──────────────────────────────────────────────────
        coll = collection_name(tenant_id)
        ensure_collection(qdrant, coll)

        points = [
            qm.PointStruct(
                id=point_id_from_key(object_key, i),
                vector=embeddings[i].tolist(),
                payload={
                    "tenant_id":   tenant_id,
                    "object_key":  object_key,
                    "doc_id":      object_key.replace("/", "__"),
                    "chunk_idx":   i,
                    "chunk_total": len(chunks),
                    "text":        chunk,
                    "filename":    filename,
                },
            )
            for i, chunk in enumerate(chunks)
        ]
        qdrant.upsert(collection_name=coll, points=points)

        # ── Mark ingested in Postgres ─────────────────────────────────────────
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ingested_docs
                    (tenant_id, bucket, object_key, doc_name, chunk_count)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (bucket, object_key) DO NOTHING
                """,
                tenant_id,
                bucket,
                object_key,
                filename,
                len(chunks),
            )

        elapsed = time.monotonic() - t0
        DOCS_INGESTED.labels(tenant_id=tenant_id).inc()
        CHUNKS_CREATED.labels(tenant_id=tenant_id).inc(len(chunks))
        INGESTION_DURATION.observe(elapsed)
        logger.info(
            "  ✓ ingested %s  chunks=%d  elapsed=%.1fs", filename, len(chunks), elapsed
        )

    except Exception as exc:
        logger.exception("Failed to ingest %s: %s", object_key, exc)
        INGESTION_ERRORS.labels(reason="exception").inc()

    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Main poll loop
# ──────────────────────────────────────────────────────────────────────────────
async def poll_loop(
    *,
    minio_client: Minio,
    qdrant: QdrantClient,
    model,
    db_pool: asyncpg.Pool,
) -> None:
    """
    Continuously polls MinIO for new objects and ingests them.
    Runs forever; any exception in a single iteration is logged and retried.
    """
    while True:
        try:
            objects = list(minio_client.list_objects(MINIO_BUCKET, recursive=True))
            logger.info("Poll: %d objects in bucket", len(objects))

            # Count un-ingested objects for the queue-depth gauge
            async with db_pool.acquire() as conn:
                already = await conn.fetchval(
                    "SELECT COUNT(*) FROM ingested_docs WHERE bucket = $1", MINIO_BUCKET
                )
            QUEUE_DEPTH.set(max(0, len(objects) - (already or 0)))

            for obj in objects:
                if obj.object_name:
                    await ingest_object(
                        minio_client=minio_client,
                        qdrant=qdrant,
                        model=model,
                        db_pool=db_pool,
                        bucket=MINIO_BUCKET,
                        object_key=obj.object_name,
                    )

        except S3Error as exc:
            logger.error("MinIO error during poll: %s", exc)
            INGESTION_ERRORS.labels(reason="minio_error").inc()
        except Exception as exc:
            logger.exception("Unexpected poll error: %s", exc)
            INGESTION_ERRORS.labels(reason="poll_error").inc()

        logger.info("Poll complete. Next poll in %ds", POLL_INTERVAL)
        await asyncio.sleep(POLL_INTERVAL)


# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────────
async def main() -> None:
    # Start Prometheus metrics endpoint
    start_http_server(METRICS_PORT)
    logger.info("Prometheus metrics on port %d", METRICS_PORT)

    # Load bge-m3 model (downloads ~570MB on first run, cached in /root/.cache)
    logger.info("Loading BAAI/bge-m3 model (may take a few minutes on first run)…")
    from FlagEmbedding import BGEM3FlagModel  # noqa: PLC0415

    model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=True)
    logger.info("bge-m3 model loaded ✓")

    # Connect to MinIO
    minio_client = Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=False,
    )

    # Connect to Qdrant
    qdrant = QdrantClient(url=QDRANT_URL)
    logger.info("Qdrant connected at %s ✓", QDRANT_URL)

    # Connect to Postgres (asyncpg, not SQLAlchemy — no RLS session needed here)
    db_pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=2,
        max_size=5,
        command_timeout=30,
    )
    logger.info("Postgres pool ready ✓")

    logger.info(
        "Ingestion service ready. Polling bucket '%s' every %ds",
        MINIO_BUCKET,
        POLL_INTERVAL,
    )
    await poll_loop(
        minio_client=minio_client,
        qdrant=qdrant,
        model=model,
        db_pool=db_pool,
    )


if __name__ == "__main__":
    asyncio.run(main())
