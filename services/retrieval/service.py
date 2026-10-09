"""
VibeForge — RetrievalService (Phase 1)
Contract C8: tenant_id mandatory, single Qdrant choke-point.
Pipeline: bge-m3 dense embed → Qdrant top-20 → bge-reranker → top-5
"""
from __future__ import annotations
import logging, os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
QDRANT_URL       = os.getenv("QDRANT_URL", "http://qdrant:6333")
QDRANT_API_KEY   = os.getenv("QDRANT_API_KEY", "")
GLOBAL_COLLECTION = os.getenv("GLOBAL_COLLECTION", "vibeforge_global")
TOP_CANDIDATES   = int(os.getenv("RETRIEVAL_TOP_CANDIDATES", "20"))

@dataclass
class Chunk:
    chunk_id: str; text: str; source_name: str; source_version: str
    heading: str; score: float; is_shared: bool

@dataclass
class RetrievalResult:
    query: str; tenant_id: str
    chunks: list[Chunk] = field(default_factory=list)
    total_candidates: int = 0; reranked: bool = False; error: str = ""

def _col(tenant_id: str) -> str:
    return f"vibeforge_{tenant_id.replace('-','')}"

class RetrievalService:
    def __init__(self, qdrant_url: str = QDRANT_URL):
        os.environ.setdefault("HF_HOME", "/tmp/hf_cache")
        os.environ.setdefault("TRANSFORMERS_CACHE", "/tmp/hf_cache")
        from qdrant_client import QdrantClient
        from FlagEmbedding import BGEM3FlagModel, FlagReranker
        kw = {"url": qdrant_url}
        if QDRANT_API_KEY: kw["api_key"] = QDRANT_API_KEY
        self.qdrant  = QdrantClient(**kw)
        logger.info("[Retrieval] Connecting Qdrant at %s", qdrant_url)
        self.encoder = BGEM3FlagModel(os.environ.get("BGE_M3_PATH", "BAAI/bge-m3"), use_fp16=True)
        logger.info("[Retrieval] bge-m3 loaded")
        self.reranker = FlagReranker(os.environ.get("RERANKER_PATH", "BAAI/bge-reranker-v2-m3"), use_fp16=True)
        logger.info("[Retrieval] bge-reranker loaded")

    def _embed(self, q: str) -> list[float]:
        return self.encoder.encode([q], batch_size=1, max_length=512)["dense_vecs"][0].tolist()

    def _search(self, col: str, vec: list[float], tenant_id, limit: int, shared: bool) -> list[dict]:
        from qdrant_client import models as qm
        try:
            cols = {c.name for c in self.qdrant.get_collections().collections}
            if col not in cols: return []
            filt = None
            if tenant_id and not shared:
                filt = qm.Filter(must=[qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=tenant_id))])
            hits = self.qdrant.query_points(collection_name=col, query=vec, query_filter=filt, limit=limit, with_payload=True).points
            return [{"chunk_id": str(h.id), "text": h.payload.get("text",""), "source_name": h.payload.get("filename","?"),
                     "source_version": h.payload.get("doc_id",""), "heading": h.payload.get("heading",""),
                     "score": h.score, "is_shared": shared} for h in hits]
        except Exception as e:
            logger.warning("[Retrieval] search error on %s: %s", col, e); return []

    def _rerank(self, q: str, cands: list[dict], top_k: int) -> list[dict]:
        if not cands: return []
        try:
            scores = self.reranker.compute_score([[q, c["text"]] for c in cands], normalize=True)
            if isinstance(scores, float): scores = [scores]
            for c, s in zip(cands, scores): c["score"] = float(s)
            return sorted(cands, key=lambda x: x["score"], reverse=True)[:top_k]
        except Exception as e:
            logger.warning("[Retrieval] reranker error: %s", e)
            return sorted(cands, key=lambda x: x["score"], reverse=True)[:top_k]

    async def retrieve(self, query: str, tenant_id: str, top_k: int = 5, include_shared: bool = True) -> RetrievalResult:
        res = RetrievalResult(query=query, tenant_id=tenant_id)
        try:
            vec = self._embed(query)
            cands = self._search(_col(tenant_id), vec, tenant_id, TOP_CANDIDATES, False)
            if include_shared:
                cands += self._search(GLOBAL_COLLECTION, vec, None, 10, True)
            res.total_candidates = len(cands)
            final = self._rerank(query, cands, top_k) if len(cands) > top_k else sorted(cands, key=lambda x: x["score"], reverse=True)[:top_k]
            res.reranked = len(cands) > top_k
            res.chunks = [Chunk(chunk_id=c["chunk_id"], text=c["text"], source_name=c["source_name"],
                                source_version=c["source_version"], heading=c["heading"], score=c["score"], is_shared=c["is_shared"]) for c in final]
            logger.info("[Retrieval] query=%r tenant=%s candidates=%d returned=%d", query[:40], tenant_id[:8], res.total_candidates, len(res.chunks))
        except Exception as e:
            logger.exception("[Retrieval] failed: %s", e); res.error = str(e)
        return res