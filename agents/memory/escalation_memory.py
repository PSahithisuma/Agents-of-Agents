"""
VibeForge — Escalation Memory (Phase 3)
=========================================
Problem signature → bge-m3 embedding → Qdrant vector store.

When escalation succeeds, the problem signature and solution are stored.
Next time a similar failure occurs, memory is checked BEFORE calling
the commercial model — potentially saving the escalation cost entirely.

Contract C14:
  - Tenant-scoped by default (solutions from tenant A never visible to tenant B)
  - Gate-passed outcomes only (failed escalations are never stored)
  - Promotion to shared memory requires 3 independent reuses OR admin approval
  - Problem signatures must NOT contain PII (same pre-egress scan as C18)

Problem signature = error_class + stack_profile + rule_id
  - error_class:   category of error (CompileError, TestFailure, SemgrepViolation)
  - stack_profile: java_spring | python_fastapi | dotnet
  - rule_id:       the specific rule that failed (e.g. semgrep rule ID, test class)

In production: Qdrant running in the Docker Compose stack.
In tests:      In-memory mock that implements the same interface.
"""

from __future__ import annotations

import hashlib
import re
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# ── Problem signature ──────────────────────────────────────────────────────────

class ErrorClass(str, Enum):
    COMPILE_ERROR       = "CompileError"
    TEST_FAILURE        = "TestFailure"
    MIGRATION_ERROR     = "MigrationError"
    API_SMOKE_FAILURE   = "ApiSmokeFailure"
    SEMGREP_VIOLATION   = "SemgrepViolation"
    TRIVY_CVE           = "TrivyCVE"
    GITLEAKS_SECRET     = "GitleaksSecret"
    ASSEMBLY_CONFLICT   = "AssemblyConflict"
    UNKNOWN             = "Unknown"


@dataclass
class ProblemSignature:
    """
    The key used to look up and store escalation memory.
    Must be deterministic for the same class of failure.
    Must NOT contain file content or PII.
    """
    error_class: ErrorClass
    stack_profile: str                      # java_spring | python_fastapi | dotnet
    rule_id: str = ""                       # semgrep rule ID, test class, etc.
    error_pattern: str = ""                 # normalized error message pattern
    module_type: str = ""                   # entity | service | repository | etc.
    vertical: str = ""                      # ecommerce | banking | logistics

    def to_text(self) -> str:
        """Canonical text representation for embedding."""
        parts = [
            f"error_class:{self.error_class.value}",
            f"stack:{self.stack_profile}",
        ]
        if self.rule_id:
            parts.append(f"rule:{self.rule_id}")
        if self.error_pattern:
            parts.append(f"pattern:{self.error_pattern[:100]}")
        if self.module_type:
            parts.append(f"module_type:{self.module_type}")
        if self.vertical:
            parts.append(f"vertical:{self.vertical}")
        return " | ".join(parts)

    def fingerprint(self) -> str:
        """Deterministic hash of the signature — used as point ID in Qdrant."""
        return hashlib.sha256(self.to_text().encode()).hexdigest()[:16]

    @classmethod
    def from_gate_errors(
        cls,
        failing_step: str,
        error_lines: list[str],
        stack_profile: str,
        module_type: str = "",
        vertical: str = "",
    ) -> "ProblemSignature":
        """
        Build a ProblemSignature from gate step output.
        Normalizes the error to remove file-specific details.
        """
        error_class_map = {
            "compile":    ErrorClass.COMPILE_ERROR,
            "unit_tests": ErrorClass.TEST_FAILURE,
            "migrations": ErrorClass.MIGRATION_ERROR,
            "api_smoke":  ErrorClass.API_SMOKE_FAILURE,
            "semgrep":    ErrorClass.SEMGREP_VIOLATION,
            "trivy_osv":  ErrorClass.TRIVY_CVE,
            "gitleaks":   ErrorClass.GITLEAKS_SECRET,
        }
        error_class = error_class_map.get(failing_step, ErrorClass.UNKNOWN)

        # Extract rule ID from semgrep output
        rule_id = ""
        if error_class == ErrorClass.SEMGREP_VIOLATION and error_lines:
            match = re.search(r'([\w/.-]+rule[\w/.-]+)', error_lines[0])
            if match:
                rule_id = match.group(1)

        # Normalize error pattern — remove file paths, line numbers, variable names
        pattern = ""
        if error_lines:
            raw = error_lines[0]
            # Remove file paths
            raw = re.sub(r'[A-Za-z0-9_/\\.-]+\.(java|py|cs|ts|go):\d+:', '', raw)
            # Remove variable names (camelCase/PascalCase tokens)
            raw = re.sub(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b', 'X', raw)
            # Remove numbers
            raw = re.sub(r'\b\d+\b', 'N', raw)
            pattern = raw.strip()[:150]

        return cls(
            error_class=error_class,
            stack_profile=stack_profile,
            rule_id=rule_id,
            error_pattern=pattern,
            module_type=module_type,
            vertical=vertical,
        )


# ── Memory record ──────────────────────────────────────────────────────────────

@dataclass
class MemoryRecord:
    """
    One stored solution in escalation memory.
    Contract C14: gate-passed outcomes only.
    """
    record_id: str = field(default_factory=lambda: str(uuid4()))
    signature: Optional[ProblemSignature] = None
    signature_text: str = ""            # for display / logging
    signature_fingerprint: str = ""     # deterministic hash

    # The solution (fixed file contents that passed the gate)
    fixed_files: dict[str, str] = field(default_factory=dict)
    fix_summary: str = ""               # human-readable description

    # Provenance
    tenant_id: str = ""
    job_id: str = ""
    model_used: str = ""                # commercial model that produced this
    gate_passed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # Reuse tracking (Contract C14: 3 reuses to promote to shared)
    reuse_count: int = 0
    shared: bool = False                # False = tenant-only, True = shared
    promoted_at: Optional[str] = None

    # Embedding (stored externally in Qdrant, included here for in-memory mock)
    embedding: Optional[list[float]] = None


@dataclass
class MemoryHit:
    """A memory lookup result."""
    record: MemoryRecord
    similarity_score: float             # 0.0–1.0, cosine similarity
    is_shared: bool = False

    @property
    def is_useful(self) -> bool:
        """Consider a hit useful if similarity > 0.85."""
        return self.similarity_score >= 0.85


# ── In-memory store (for tests, replaces Qdrant in CI) ────────────────────────

class InMemoryEscalationStore:
    """
    Test/offline replacement for Qdrant.
    Uses cosine similarity on simple keyword overlap vectors.
    In production: replaced by Qdrant with bge-m3 embeddings.
    """

    def __init__(self):
        self._records: list[MemoryRecord] = []

    def store(self, record: MemoryRecord) -> None:
        # Update if fingerprint matches
        for i, r in enumerate(self._records):
            if r.signature_fingerprint == record.signature_fingerprint and \
               r.tenant_id == record.tenant_id:
                self._records[i] = record
                return
        self._records.append(record)

    def lookup(
        self,
        signature: ProblemSignature,
        tenant_id: str,
        top_k: int = 3,
        min_similarity: float = 0.85,
    ) -> list[MemoryHit]:
        """Simple keyword overlap similarity for offline testing."""
        query_tokens = set(signature.to_text().lower().split())
        hits: list[MemoryHit] = []

        for record in self._records:
            # Only return records visible to this tenant
            if not record.shared and record.tenant_id != tenant_id:
                continue

            record_tokens = set(record.signature_text.lower().split())
            if not query_tokens or not record_tokens:
                continue

            # Jaccard similarity
            intersection = len(query_tokens & record_tokens)
            union = len(query_tokens | record_tokens)
            similarity = intersection / union if union > 0 else 0.0

            if similarity >= min_similarity:
                hits.append(MemoryHit(
                    record=record,
                    similarity_score=similarity,
                    is_shared=record.shared,
                ))

        hits.sort(key=lambda h: h.similarity_score, reverse=True)
        return hits[:top_k]

    def increment_reuse(self, record_id: str) -> Optional[MemoryRecord]:
        for i, r in enumerate(self._records):
            if r.record_id == record_id:
                self._records[i].reuse_count += 1
                # Auto-promote at 3 reuses (Contract C14)
                if self._records[i].reuse_count >= 3 and not self._records[i].shared:
                    self._records[i].shared = True
                    self._records[i].promoted_at = datetime.now(timezone.utc).isoformat()
                    logger.info(
                        "[EscalationMemory] Record %s auto-promoted to shared "
                        "(3 reuses reached — Contract C14)", record_id,
                    )
                return self._records[i]
        return None

    def get_all(self, tenant_id: str) -> list[MemoryRecord]:
        return [r for r in self._records if r.tenant_id == tenant_id or r.shared]


# ── Qdrant adapter (production) ───────────────────────────────────────────────

class QdrantEscalationStore:
    """
    Production Qdrant-backed store with bge-m3 embeddings.
    Requires: qdrant-client, sentence-transformers

    Collection: "escalation_memory"
    Vector dim: 1024 (bge-m3)
    Distance:   COSINE
    Payload:    All MemoryRecord fields (except embedding)
    """

    COLLECTION = "escalation_memory"
    VECTOR_DIM = 1024

    def __init__(self, qdrant_url: str = "http://localhost:6333"):
        self._url = qdrant_url
        self._client = None
        self._encoder = None

    def _get_client(self):
        if self._client is None:
            try:
                from qdrant_client import QdrantClient
                self._client = QdrantClient(url=self._url)
            except ImportError:
                raise RuntimeError(
                    "qdrant-client not installed. "
                    "pip install qdrant-client sentence-transformers"
                )
        return self._client

    def _get_encoder(self):
        if self._encoder is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._encoder = SentenceTransformer("BAAI/bge-m3")
            except ImportError:
                raise RuntimeError(
                    "sentence-transformers not installed. "
                    "pip install sentence-transformers"
                )
        return self._encoder

    def _embed(self, text: str) -> list[float]:
        encoder = self._get_encoder()
        return encoder.encode(text, normalize_embeddings=True).tolist()

    def ensure_collection(self) -> None:
        """Create collection if it doesn't exist."""
        from qdrant_client.models import Distance, VectorParams
        client = self._get_client()
        existing = [c.name for c in client.get_collections().collections]
        if self.COLLECTION not in existing:
            client.create_collection(
                collection_name=self.COLLECTION,
                vectors_config=VectorParams(
                    size=self.VECTOR_DIM,
                    distance=Distance.COSINE,
                ),
            )
            logger.info("[EscalationMemory] Created Qdrant collection: %s", self.COLLECTION)

    def store(self, record: MemoryRecord) -> None:
        from qdrant_client.models import PointStruct
        client = self._get_client()
        embedding = self._embed(record.signature_text)
        payload = {
            "record_id":              record.record_id,
            "signature_text":         record.signature_text,
            "signature_fingerprint":  record.signature_fingerprint,
            "fix_summary":            record.fix_summary,
            "tenant_id":              record.tenant_id,
            "job_id":                 record.job_id,
            "model_used":             record.model_used,
            "gate_passed_at":         record.gate_passed_at,
            "reuse_count":            record.reuse_count,
            "shared":                 record.shared,
            "fixed_files_json":       json.dumps(record.fixed_files),
        }
        point = PointStruct(
            id=abs(hash(record.record_id)) % (2**31),
            vector=embedding,
            payload=payload,
        )
        client.upsert(collection_name=self.COLLECTION, points=[point])
        logger.info("[EscalationMemory] Stored record %s", record.record_id)

    def lookup(
        self,
        signature: ProblemSignature,
        tenant_id: str,
        top_k: int = 3,
        min_similarity: float = 0.85,
    ) -> list[MemoryHit]:
        client = self._get_client()
        query_vector = self._embed(signature.to_text())

        # Search with tenant filter (shared OR same tenant)
        from qdrant_client.models import Filter, FieldCondition, MatchValue, Should
        tenant_filter = Filter(
            should=[
                FieldCondition(key="shared", match=MatchValue(value=True)),
                FieldCondition(key="tenant_id", match=MatchValue(value=tenant_id)),
            ]
        )

        results = client.search(
            collection_name=self.COLLECTION,
            query_vector=query_vector,
            query_filter=tenant_filter,
            limit=top_k,
            score_threshold=min_similarity,
        )

        hits = []
        for r in results:
            fixed_files = json.loads(r.payload.get("fixed_files_json", "{}"))
            record = MemoryRecord(
                record_id=r.payload["record_id"],
                signature_text=r.payload.get("signature_text", ""),
                signature_fingerprint=r.payload.get("signature_fingerprint", ""),
                fixed_files=fixed_files,
                fix_summary=r.payload.get("fix_summary", ""),
                tenant_id=r.payload.get("tenant_id", ""),
                job_id=r.payload.get("job_id", ""),
                model_used=r.payload.get("model_used", ""),
                reuse_count=r.payload.get("reuse_count", 0),
                shared=r.payload.get("shared", False),
            )
            hits.append(MemoryHit(
                record=record,
                similarity_score=r.score,
                is_shared=record.shared,
            ))
        return hits

    def increment_reuse(self, record_id: str) -> None:
        """Increment reuse count and auto-promote at 3 (Contract C14)."""
        client = self._get_client()
        # Get current record
        results = client.scroll(
            collection_name=self.COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="record_id", match=MatchValue(value=record_id))
            ]),
            limit=1,
        )
        if not results[0]:
            return

        point = results[0][0]
        new_count = point.payload.get("reuse_count", 0) + 1
        now_shared = new_count >= 3
        update = {"reuse_count": new_count}
        if now_shared and not point.payload.get("shared"):
            update["shared"] = True
            update["promoted_at"] = datetime.now(timezone.utc).isoformat()
            logger.info(
                "[EscalationMemory] Record %s promoted to shared (3 reuses — C14)",
                record_id,
            )
        client.set_payload(
            collection_name=self.COLLECTION,
            payload=update,
            points=[point.id],
        )


# ── Escalation Memory service ──────────────────────────────────────────────────

class EscalationMemory:
    """
    High-level service for escalation memory.
    Used by the generation graph before and after escalation.

    Usage:
        memory = EscalationMemory()  # uses in-memory store by default

        # Before escalation: check memory
        hits = await memory.lookup(signature, tenant_id)
        if hits and hits[0].is_useful:
            return hits[0].record.fixed_files  # reuse cached fix

        # After successful escalation:
        await memory.store_success(
            signature=signature,
            fixed_files=fixed_files,
            tenant_id=tenant_id,
            job_id=job_id,
            model_used="claude-opus-4-6",
        )
    """

    def __init__(self, store=None, use_qdrant: bool = False, qdrant_url: str = "http://localhost:6333"):
        if store is not None:
            self._store = store
        elif use_qdrant:
            self._store = QdrantEscalationStore(qdrant_url=qdrant_url)
        else:
            self._store = InMemoryEscalationStore()

    async def lookup(
        self,
        signature: ProblemSignature,
        tenant_id: str,
        min_similarity: float = 0.85,
    ) -> list[MemoryHit]:
        """
        Look up similar problems in memory.
        Returns hits sorted by similarity descending.
        """
        try:
            hits = self._store.lookup(
                signature=signature,
                tenant_id=tenant_id,
                top_k=3,
                min_similarity=min_similarity,
            )
            if hits:
                logger.info(
                    "[EscalationMemory] Found %d hit(s) for signature: %s "
                    "(top score: %.3f)",
                    len(hits), signature.to_text()[:60], hits[0].similarity_score,
                )
            else:
                logger.info(
                    "[EscalationMemory] No hits found for: %s",
                    signature.to_text()[:60],
                )
            return hits
        except Exception as e:
            logger.error("[EscalationMemory] Lookup failed: %s", e)
            return []

    async def store_success(
        self,
        signature: ProblemSignature,
        fixed_files: dict[str, str],
        tenant_id: str,
        job_id: str,
        model_used: str,
    ) -> MemoryRecord:
        """
        Store a successful fix in memory.
        Contract C14: gate-passed outcomes only — caller must verify gate passed.

        Args:
            signature:   The problem that was fixed
            fixed_files: The file contents that passed the gate
            tenant_id:   Owner tenant (private by default)
            job_id:      Source job
            model_used:  Which commercial model produced the fix

        Returns:
            The stored MemoryRecord
        """
        sig_text = signature.to_text()

        # Build fix summary (no file content — just metadata)
        fix_summary = (
            f"Fixed {len(fixed_files)} file(s) for "
            f"{signature.error_class.value} on {signature.stack_profile}"
        )

        record = MemoryRecord(
            signature=signature,
            signature_text=sig_text,
            signature_fingerprint=signature.fingerprint(),
            fixed_files=fixed_files,
            fix_summary=fix_summary,
            tenant_id=tenant_id,
            job_id=job_id,
            model_used=model_used,
        )

        try:
            self._store.store(record)
            logger.info(
                "[EscalationMemory] Stored fix for %s (fingerprint=%s)",
                sig_text[:60], record.signature_fingerprint,
            )
        except Exception as e:
            logger.error("[EscalationMemory] Store failed: %s", e)

        return record

    async def record_reuse(self, record_id: str) -> None:
        """
        Record that a memory hit was reused successfully.
        Increments reuse_count. Auto-promotes at 3 reuses (Contract C14).
        """
        try:
            self._store.increment_reuse(record_id)
        except Exception as e:
            logger.error("[EscalationMemory] Reuse recording failed: %s", e)
