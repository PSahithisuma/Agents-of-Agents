"""
VibeForge — Post-QA Semantic Cache (Phase 3)
=============================================
Cache key = canonical Spec IR hash + stack_profile + scaffold_version + ruleset_version.
Written ONLY after full gate pass. Never silently substituted — near-hits are
suggested to the user, who decides whether to accept. Contract C7.

How it works:
  1. Before generation: compute cache key from frozen spec
  2. Lookup in cache (exact match first, then near-hits by vector similarity)
  3. If exact hit: return cached artifact bundle URL (skip generation entirely)
  4. If near-hit: suggest to user — "A similar spec was generated 3 days ago,
     would you like to start from that output?" — user decides
  5. After gate pass: write to cache (cache miss path only)

Cache key components:
  - canonical_hash:    SHA-256 of the normalized frozen spec (from Spec IR freeze())
  - stack_profile:     java_spring | python_fastapi | dotnet
  - scaffold_version:  version tag of the Copier scaffold template
  - ruleset_version:   version tag of the compliance ruleset in use

Why volatile fields are excluded from the key:
  Two specs with different job_ids/timestamps but identical content should
  hit the same cache entry. The canonical_hash already normalizes these out
  (see core/spec_ir.py freeze() implementation).

Contract C7:
  Cache write happens ONLY here, ONLY after full gate pass.
  No other code path writes to the cache.
  Near-hits are SUGGESTED, never silently substituted.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# ── Cache key ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CacheKey:
    """
    Immutable cache key. Same spec + same stack + same versions = same key.
    """
    canonical_hash: str       # from ApplicationSpec.canonical_hash (freeze())
    stack_profile: str        # java_spring | python_fastapi | dotnet
    scaffold_version: str     # semver tag of the scaffold template
    ruleset_version: str      # semver tag of the compliance ruleset

    def compute(self) -> str:
        """Deterministic string key for exact lookup."""
        raw = "|".join([
            self.canonical_hash,
            self.stack_profile,
            self.scaffold_version,
            self.ruleset_version,
        ])
        return hashlib.sha256(raw.encode()).hexdigest()

    def __str__(self) -> str:
        return (
            f"CacheKey(spec={self.canonical_hash[:12]}... "
            f"stack={self.stack_profile} "
            f"scaffold=v{self.scaffold_version} "
            f"ruleset=v{self.ruleset_version})"
        )


# ── Cache entry ────────────────────────────────────────────────────────────────

@dataclass
class CacheEntry:
    """
    One cached generation result.
    Written only after full gate pass (Contract C7).
    """
    entry_id: str = field(default_factory=lambda: str(uuid4()))
    cache_key_hash: str = ""            # CacheKey.compute()

    # The cached artifacts
    artifact_bundle_url: str = ""       # MinIO URL to the complete bundle
    gitea_repo_url: str = ""            # Gitea repo URL
    preview_url: str = ""               # Traefik preview URL

    # Metadata for near-hit suggestions
    spec_summary: str = ""              # human-readable spec description
    vertical: str = ""
    entity_count: int = 0
    endpoint_count: int = 0
    stack_profile: str = ""

    # Provenance
    job_id: str = ""
    tenant_id: str = ""
    gate_passed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # Usage stats
    hit_count: int = 0
    last_hit_at: Optional[str] = None

    # Embedding for near-hit search (stored externally in Qdrant)
    embedding_text: str = ""            # text used for embedding
    embedding: Optional[list[float]] = None


@dataclass
class CacheHit:
    """A cache lookup result."""
    entry: CacheEntry
    hit_type: str                       # "exact" | "near"
    similarity_score: float = 1.0       # 1.0 for exact, <1.0 for near

    @property
    def is_exact(self) -> bool:
        return self.hit_type == "exact"

    @property
    def is_near(self) -> bool:
        return self.hit_type == "near"


# ── In-memory cache store (for tests) ─────────────────────────────────────────

class InMemoryCacheStore:
    """
    Test/offline replacement for the production cache backend.
    Uses exact key matching only (no vector similarity without bge-m3).
    In production: Postgres for exact keys + Qdrant for near-hit similarity.
    """

    def __init__(self):
        self._entries: dict[str, CacheEntry] = {}  # cache_key_hash → entry

    def write(self, key: CacheKey, entry: CacheEntry) -> None:
        key_hash = key.compute()
        entry.cache_key_hash = key_hash
        self._entries[key_hash] = entry
        logger.info(
            "[SemanticCache] Written: key=%s entry=%s",
            str(key), entry.entry_id,
        )

    def lookup_exact(self, key: CacheKey) -> Optional[CacheEntry]:
        key_hash = key.compute()
        entry = self._entries.get(key_hash)
        if entry:
            entry.hit_count += 1
            entry.last_hit_at = datetime.now(timezone.utc).isoformat()
            logger.info(
                "[SemanticCache] EXACT HIT: key=%s hits=%d",
                str(key), entry.hit_count,
            )
        return entry

    def lookup_near(
        self,
        key: CacheKey,
        min_similarity: float = 0.92,
        top_k: int = 3,
    ) -> list[CacheHit]:
        """
        In the in-memory store, near-hit uses simple field overlap.
        In production Qdrant: bge-m3 embedding similarity.
        """
        hits: list[CacheHit] = []
        query_text = self._key_to_embedding_text(key)
        query_tokens = set(query_text.lower().split())

        for entry in self._entries.values():
            if not entry.embedding_text:
                continue
            entry_tokens = set(entry.embedding_text.lower().split())
            if not query_tokens or not entry_tokens:
                continue
            intersection = len(query_tokens & entry_tokens)
            union = len(query_tokens | entry_tokens)
            similarity = intersection / union if union > 0 else 0.0

            if similarity >= min_similarity:
                hits.append(CacheHit(
                    entry=entry,
                    hit_type="near",
                    similarity_score=similarity,
                ))

        hits.sort(key=lambda h: h.similarity_score, reverse=True)
        return hits[:top_k]

    @staticmethod
    def _key_to_embedding_text(key: CacheKey) -> str:
        return f"stack:{key.stack_profile} scaffold:{key.scaffold_version} ruleset:{key.ruleset_version}"

    def invalidate(self, cache_key_hash: str) -> bool:
        if cache_key_hash in self._entries:
            del self._entries[cache_key_hash]
            return True
        return False

    def stats(self) -> dict[str, Any]:
        return {
            "total_entries": len(self._entries),
            "total_hits": sum(e.hit_count for e in self._entries.values()),
        }


# ── Post-QA Semantic Cache ─────────────────────────────────────────────────────

class SemanticCache:
    """
    Post-QA semantic cache. Contract C7: written only after full gate pass.

    Usage in generation graph:

    BEFORE generation:
        cache = SemanticCache()
        key = CacheKey(spec.canonical_hash, stack_profile, scaffold_ver, ruleset_ver)
        hit = await cache.lookup(key)
        if hit:
            if hit.is_exact:
                return hit.entry.artifact_bundle_url   # skip generation
            else:
                suggest_to_user(hit)   # user decides, never auto-substitute

    AFTER gate passes:
        await cache.write(
            key=key,
            artifact_bundle_url=delivery.minio_bundle_url,
            gitea_repo_url=delivery.gitea_repo_url,
            spec_summary=spec_summary,
            job_id=job_id,
            tenant_id=tenant_id,
        )
    """

    # Near-hit threshold — below this, don't suggest the cached result
    NEAR_HIT_THRESHOLD = 0.92

    def __init__(self, store=None):
        self._store = store or InMemoryCacheStore()

    async def lookup(
        self,
        key: CacheKey,
        tenant_id: str = "",
    ) -> Optional[CacheHit]:
        """
        Look up the cache. Returns the best hit or None.

        Priority:
        1. Exact match → return immediately (skip generation)
        2. Near match (similarity ≥ 0.92) → suggest to user
        3. No match → return None (proceed with generation)
        """
        # Exact lookup
        exact = self._store.lookup_exact(key)
        if exact:
            return CacheHit(entry=exact, hit_type="exact", similarity_score=1.0)

        # Near-hit lookup
        near_hits = self._store.lookup_near(
            key=key,
            min_similarity=self.NEAR_HIT_THRESHOLD,
            top_k=1,
        )
        if near_hits:
            logger.info(
                "[SemanticCache] NEAR HIT: score=%.3f — will suggest to user",
                near_hits[0].similarity_score,
            )
            return near_hits[0]

        logger.info("[SemanticCache] MISS: %s", str(key))
        return None

    async def write(
        self,
        key: CacheKey,
        artifact_bundle_url: str,
        gitea_repo_url: str,
        spec_summary: str,
        job_id: str,
        tenant_id: str,
        vertical: str = "",
        entity_count: int = 0,
        endpoint_count: int = 0,
        preview_url: str = "",
    ) -> CacheEntry:
        """
        Write to cache after full gate pass.
        CONTRACT C7: this is the ONLY place cache writes happen.

        Args:
            key:                  CacheKey for this generation
            artifact_bundle_url:  MinIO URL to the complete zip bundle
            gitea_repo_url:       Gitea repo URL
            spec_summary:         Human-readable description for near-hit UI
            job_id:               Source job
            tenant_id:            Tenant that owns this artifact

        Returns:
            The written CacheEntry
        """
        embedding_text = self._build_embedding_text(
            key=key,
            spec_summary=spec_summary,
            vertical=vertical,
            entity_count=entity_count,
        )

        entry = CacheEntry(
            cache_key_hash=key.compute(),
            artifact_bundle_url=artifact_bundle_url,
            gitea_repo_url=gitea_repo_url,
            preview_url=preview_url,
            spec_summary=spec_summary,
            vertical=vertical,
            entity_count=entity_count,
            endpoint_count=endpoint_count,
            stack_profile=key.stack_profile,
            job_id=job_id,
            tenant_id=tenant_id,
            embedding_text=embedding_text,
        )

        self._store.write(key, entry)

        logger.info(
            "[SemanticCache] WRITE: key=%s job=%s bundle=%s",
            str(key), job_id, artifact_bundle_url[:60],
        )
        return entry

    async def invalidate(self, key: CacheKey) -> bool:
        """
        Invalidate a cache entry.
        Called when: scaffold version bumped, ruleset version bumped,
        or tenant requests cache bust.
        """
        key_hash = key.compute()
        result = self._store.invalidate(key_hash)
        if result:
            logger.info("[SemanticCache] Invalidated: %s", str(key))
        return result

    def stats(self) -> dict[str, Any]:
        return self._store.stats()

    @staticmethod
    def _build_embedding_text(
        key: CacheKey,
        spec_summary: str,
        vertical: str,
        entity_count: int,
    ) -> str:
        """
        Build the text used for near-hit embedding.
        Includes spec semantics, not just the key.
        """
        parts = [
            f"vertical:{vertical}",
            f"stack:{key.stack_profile}",
            f"entities:{entity_count}",
            f"scaffold:{key.scaffold_version}",
            f"ruleset:{key.ruleset_version}",
        ]
        if spec_summary:
            parts.append(f"summary:{spec_summary[:200]}")
        return " ".join(parts)

    @staticmethod
    def build_key(
        canonical_hash: str,
        stack_profile: str,
        scaffold_version: str = "1.0.0",
        ruleset_version: str = "1.0.0",
    ) -> CacheKey:
        """
        Convenience factory. Called by the generation graph before starting.
        """
        return CacheKey(
            canonical_hash=canonical_hash,
            stack_profile=stack_profile,
            scaffold_version=scaffold_version,
            ruleset_version=ruleset_version,
        )

    @staticmethod
    def format_near_hit_suggestion(hit: CacheHit) -> dict[str, Any]:
        """
        Format a near-hit for display in the job console UI.
        The user sees this and decides whether to use the cached output.
        Contract C7: never auto-substitute.
        """
        return {
            "type": "near_cache_hit",
            "similarity_pct": round(hit.similarity_score * 100, 1),
            "cached_spec_summary": hit.entry.spec_summary,
            "cached_at": hit.entry.gate_passed_at,
            "artifact_url": hit.entry.artifact_bundle_url,
            "gitea_url": hit.entry.gitea_repo_url,
            "message": (
                f"A similar application was generated "
                f"({round(hit.similarity_score * 100)}% match). "
                "Would you like to start from that output instead of generating fresh?"
            ),
            "user_choices": ["Use cached output", "Generate fresh"],
        }
