"""
VibeForge — Metacognition Gate (Tier 0 + Tier 1 interface)
===========================================================
Decides WHETHER a retrieval, tool call, or escalation is needed
before it is invoked. Never reflexive — always gated.

Architecture spec Section 6 / Decision D3 / Principle P3.

Tier 0  — Deterministic rules. Zero LLM. Zero cost. Day one.
Tier 1  — Small-model arbiter (Qwen3-8B). Ambiguous cases only.
          Stub here; wired to real model when Ollama is running.
Tier 2  — HDPO fine-tune on own telemetry. Post-MVP.

Public API:
    gate = MetacognitionGate()

    # Should this spec-edit turn retrieve RAG context?
    decision = gate.evaluate_retrieval(context)

    # Should this escalate to a commercial model?
    decision = gate.evaluate_escalation(context)

    # Log outcome so gate_decisions table grows (HDPO training data)
    gate.record_outcome(decision, downstream_outcome)

GateDecision fields:
    verdict       NEEDED | NOT_NEEDED | NARROW_QUERY:<q>
    tier          0 | 1 | 2
    reason        plain English, logged to gate_decisions
    narrow_query  refined query string when verdict is NARROW_QUERY
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# ── Verdict enum ──────────────────────────────────────────────────────────────

class GateVerdict(str, Enum):
    NEEDED      = "NEEDED"        # invoke the retrieval / escalation
    NOT_NEEDED  = "NOT_NEEDED"    # skip it — context is sufficient
    NARROW      = "NARROW_QUERY"  # invoke but with a refined narrower query


# ── Decision context ──────────────────────────────────────────────────────────

@dataclass
class GateContext:
    """
    Everything the gate needs to make a decision.
    Passed in by the calling agent — never fetched by the gate itself.
    """
    # What decision point is this?
    decision_point: str                     # "retrieval" | "escalation" | "tool"

    # Current spec state
    vertical: str = ""
    domain_pack_id: str = ""
    entities_in_context: list[str] = field(default_factory=list)
    integrations_named_in_spec: list[str] = field(default_factory=list)
    connector_docs_available: list[str] = field(default_factory=list)

    # Conversation state
    turn_entities_mentioned: list[str] = field(default_factory=list)
    user_message: str = ""
    conversation_history_length: int = 0

    # Fix loop state (for escalation decisions)
    fix_iteration: int = 0
    max_fix_iterations: int = 3
    budget_remaining: float = float("inf")
    escalation_budget: float = 0.0
    tenant_escalation_enabled: bool = False
    tenant_external_llm_consent: bool = False

    # Job context
    job_id: str = ""
    tenant_id: str = ""
    spec_version: int = 1

    # Extra facts the Tier 1 model may need
    extra: dict[str, Any] = field(default_factory=dict)


# ── Gate decision output ──────────────────────────────────────────────────────

@dataclass
class GateDecision:
    """
    The gate's output. Logged to gate_decisions table.
    All fields used for HDPO training data (Tier 2).
    """
    decision_id: str                = field(default_factory=lambda: f"gd_{uuid4().hex[:8]}")
    decision_point: str             = ""
    verdict: GateVerdict            = GateVerdict.NOT_NEEDED
    tier: int                       = 0
    reason: str                     = ""
    narrow_query: str               = ""        # only when verdict == NARROW
    confidence: float               = 1.0       # 0.0-1.0; Tier 0 always 1.0
    decided_at: str                 = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Filled in later by record_outcome()
    downstream_outcome: str         = ""        # "spec_delta_accepted" | "gate_pass" | etc.
    outcome_recorded_at: str        = ""

    @property
    def needed(self) -> bool:
        return self.verdict in (GateVerdict.NEEDED, GateVerdict.NARROW)


# ── Tier 0 rule definitions ───────────────────────────────────────────────────

class _RetrievalRules:
    """
    Tier 0 retrieval rules. All deterministic — no model calls.

    Rule priority: MANDATORY > PROHIBITED > NARROW > ambiguous (→ Tier 1)
    """

    @staticmethod
    def evaluate(ctx: GateContext) -> Optional[GateDecision]:
        """
        Returns a GateDecision if a Tier 0 rule fires, else None (→ Tier 1).
        """

        # ── MANDATORY rules ───────────────────────────────────────────────────

        # R1: Named external system in spec but no connector doc in RAG
        missing_connectors = [
            s for s in ctx.integrations_named_in_spec
            if s not in ctx.connector_docs_available
        ]
        if missing_connectors:
            query = f"{' '.join(missing_connectors)} API integration connector documentation"
            return GateDecision(
                decision_point=ctx.decision_point,
                verdict=GateVerdict.NARROW,
                tier=0,
                reason=f"Named integrations lack connector docs: {missing_connectors}. "
                       f"Retrieval MANDATORY per spec Section 6 Tier 0 rule R1.",
                narrow_query=query,
                confidence=1.0,
            )

        # R2: User message explicitly references a regulation/standard
        #     not yet in the spec's compliance_model
        REGULATION_KEYWORDS = {
            "pci", "pci-dss", "gdpr", "rbi", "iso27001", "hipaa",
            "sox", "sebi", "irdai", "npci", "upi", "gst",
        }
        msg_lower = ctx.user_message.lower()
        regulation_hits = [kw for kw in REGULATION_KEYWORDS if kw in msg_lower]
        if regulation_hits:
            query = f"{' '.join(regulation_hits)} compliance requirements rules"
            return GateDecision(
                decision_point=ctx.decision_point,
                verdict=GateVerdict.NARROW,
                tier=0,
                reason=f"Regulation keywords detected in user message: {regulation_hits}. "
                       f"Retrieval MANDATORY per Tier 0 rule R2.",
                narrow_query=query,
                confidence=1.0,
            )

        # ── PROHIBITED rules ──────────────────────────────────────────────────

        # R3: All entities mentioned in this turn are already in context
        #     → retrieval would return what we already have
        if ctx.turn_entities_mentioned:
            all_known = all(
                e in ctx.entities_in_context
                for e in ctx.turn_entities_mentioned
            )
            if all_known:
                return GateDecision(
                    decision_point=ctx.decision_point,
                    verdict=GateVerdict.NOT_NEEDED,
                    tier=0,
                    reason=f"All entities mentioned ({ctx.turn_entities_mentioned}) are already "
                           f"in context. Retrieval PROHIBITED per Tier 0 rule R3.",
                    confidence=1.0,
                )

        # R4: Very short user message with no domain nouns → nothing useful to retrieve
        words = ctx.user_message.strip().split()
        if len(words) <= 3 and not ctx.integrations_named_in_spec:
            return GateDecision(
                decision_point=ctx.decision_point,
                verdict=GateVerdict.NOT_NEEDED,
                tier=0,
                reason=f"User message too short ({len(words)} words) with no named integrations. "
                       f"Retrieval PROHIBITED per Tier 0 rule R4.",
                confidence=1.0,
            )

        # No Tier 0 rule fired → ambiguous → Tier 1
        return None


class _EscalationRules:
    """
    Tier 0 escalation rules. All deterministic.
    """

    @staticmethod
    def evaluate(ctx: GateContext) -> Optional[GateDecision]:
        """
        Returns a GateDecision if a Tier 0 rule fires, else None (→ Tier 1).
        """

        # ── PROHIBITED rules (checked first — cheaper) ────────────────────────

        # E1: Fix loop has not reached iteration 3 yet → escalation too early
        if ctx.fix_iteration < ctx.max_fix_iterations:
            return GateDecision(
                decision_point=ctx.decision_point,
                verdict=GateVerdict.NOT_NEEDED,
                tier=0,
                reason=f"Fix iteration {ctx.fix_iteration} < {ctx.max_fix_iterations}. "
                       f"Escalation PROHIBITED until local fix loop is exhausted (Tier 0 rule E1).",
                confidence=1.0,
            )

        # E2: Tenant has not enabled escalation
        if not ctx.tenant_escalation_enabled:
            return GateDecision(
                decision_point=ctx.decision_point,
                verdict=GateVerdict.NOT_NEEDED,
                tier=0,
                reason="Tenant escalation_enabled=False. "
                       "Escalation PROHIBITED per Tier 0 rule E2.",
                confidence=1.0,
            )

        # E3: Tenant has not given external LLM consent
        if not ctx.tenant_external_llm_consent:
            return GateDecision(
                decision_point=ctx.decision_point,
                verdict=GateVerdict.NOT_NEEDED,
                tier=0,
                reason="Tenant external_llm_consent=False. "
                       "Escalation PROHIBITED per Tier 0 rule E3.",
                confidence=1.0,
            )

        # E4: Budget exhausted
        if ctx.budget_remaining <= 0 or ctx.budget_remaining < ctx.escalation_budget:
            return GateDecision(
                decision_point=ctx.decision_point,
                verdict=GateVerdict.NOT_NEEDED,
                tier=0,
                reason=f"Budget remaining ({ctx.budget_remaining:.2f}) < "
                       f"estimated escalation cost ({ctx.escalation_budget:.2f}). "
                       f"Escalation PROHIBITED per Tier 0 rule E4.",
                confidence=1.0,
            )

        # ── MANDATORY (all preconditions met) ────────────────────────────────

        # E5: All 3 local fix iterations exhausted + all consents + budget ok
        #     → escalation is NEEDED
        if ctx.fix_iteration >= ctx.max_fix_iterations:
            return GateDecision(
                decision_point=ctx.decision_point,
                verdict=GateVerdict.NEEDED,
                tier=0,
                reason=f"All {ctx.max_fix_iterations} local fix iterations exhausted. "
                       f"Tenant consent and budget confirmed. "
                       f"Escalation NEEDED per Tier 0 rule E5.",
                confidence=1.0,
            )

        return None


# ── Main gate class ───────────────────────────────────────────────────────────

class MetacognitionGate:
    """
    The platform-wide necessity arbiter.

    Usage:
        gate = MetacognitionGate()
        decision = gate.evaluate_retrieval(ctx)
        if decision.needed:
            chunks = retrieval_service.query(decision.narrow_query or ctx.user_message)
        gate.record_outcome(decision, "spec_delta_accepted")
    """

    def __init__(self, tier1_model_fn=None):
        """
        tier1_model_fn: optional async callable for Tier 1 arbiter.
            Signature: async (ctx: GateContext) -> GateDecision
            If None, ambiguous cases fall through as NOT_NEEDED with a warning.
        """
        self._tier1 = tier1_model_fn
        self._decisions: list[GateDecision] = []   # in-memory log; prod writes to DB

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate_retrieval(self, ctx: GateContext) -> GateDecision:
        """
        Should this agent turn retrieve RAG context?
        Returns a GateDecision synchronously (Tier 0 always sync).
        """
        ctx.decision_point = "retrieval"
        decision = _RetrievalRules.evaluate(ctx)

        if decision is None:
            # Ambiguous — Tier 1 would normally run here
            # For Phase 1 MVP: default NOT_NEEDED with warning
            decision = GateDecision(
                decision_point="retrieval",
                verdict=GateVerdict.NOT_NEEDED,
                tier=1,
                reason="No Tier 0 rule fired. Tier 1 arbiter not yet wired — "
                       "defaulting to NOT_NEEDED. Connect Qwen3-8B to enable Tier 1.",
                confidence=0.5,
            )
            logger.warning("[Gate] Tier 1 arbiter not available — defaulting NOT_NEEDED "
                          "for retrieval decision. job_id=%s", ctx.job_id)

        self._log(decision, ctx)
        return decision

    def evaluate_escalation(self, ctx: GateContext) -> GateDecision:
        """
        Should this failure escalate to a commercial model?
        Returns a GateDecision synchronously.
        """
        ctx.decision_point = "escalation"
        decision = _EscalationRules.evaluate(ctx)

        if decision is None:
            decision = GateDecision(
                decision_point="escalation",
                verdict=GateVerdict.NOT_NEEDED,
                tier=1,
                reason="No Tier 0 escalation rule fired. Tier 1 not wired — "
                       "defaulting to NOT_NEEDED.",
                confidence=0.5,
            )
            logger.warning("[Gate] Tier 1 not available for escalation decision. "
                          "job_id=%s", ctx.job_id)

        self._log(decision, ctx)
        return decision

    def evaluate_tool_call(self, ctx: GateContext, tool_name: str) -> GateDecision:
        """
        Should this agent invoke the named tool (exemplar lookup, connector doc, etc.)?
        Tier 0: always NEEDED for the first turn on a new section, NOT_NEEDED if
        the tool was already called this turn.
        """
        ctx.decision_point = f"tool:{tool_name}"
        already_called = ctx.extra.get(f"tool_called_{tool_name}", False)

        if already_called:
            decision = GateDecision(
                decision_point=ctx.decision_point,
                verdict=GateVerdict.NOT_NEEDED,
                tier=0,
                reason=f"Tool '{tool_name}' already called this turn. "
                       "NOT_NEEDED per Tier 0 tool rule T1.",
                confidence=1.0,
            )
        else:
            decision = GateDecision(
                decision_point=ctx.decision_point,
                verdict=GateVerdict.NEEDED,
                tier=0,
                reason=f"Tool '{tool_name}' not yet called this turn. NEEDED.",
                confidence=1.0,
            )

        self._log(decision, ctx)
        return decision

    def record_outcome(self, decision: GateDecision, downstream_outcome: str) -> None:
        """
        Record what happened after the gate's decision.
        This is the HDPO training signal — accuracy and efficiency rewards
        are computed from (verdict, downstream_outcome) pairs.

        downstream_outcome examples:
            "spec_delta_accepted"   — retrieval was useful
            "spec_delta_rejected"   — retrieval wasn't needed after all
            "gate_pass"             — escalation helped, code passed QA
            "gate_fail"             — escalation didn't help
            "no_op"                 — NOT_NEEDED was correct, nothing retrieved
        """
        decision.downstream_outcome = downstream_outcome
        decision.outcome_recorded_at = datetime.now(timezone.utc).isoformat()

        logger.info(
            "[Gate:outcome] id=%s point=%s verdict=%s tier=%d outcome=%s",
            decision.decision_id,
            decision.decision_point,
            decision.verdict.value,
            decision.tier,
            downstream_outcome,
        )
        # In production: INSERT into gate_decisions table
        # For Phase 1: just update the in-memory record
        for i, d in enumerate(self._decisions):
            if d.decision_id == decision.decision_id:
                self._decisions[i] = decision
                break

    def get_decisions(self) -> list[GateDecision]:
        """Return all decisions logged this session (for testing / dashboards)."""
        return list(self._decisions)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _log(self, decision: GateDecision, ctx: GateContext) -> None:
        self._decisions.append(decision)
        logger.info(
            "[Gate] point=%-14s verdict=%-12s tier=%d reason=%s",
            decision.decision_point,
            decision.verdict.value,
            decision.tier,
            decision.reason[:80],
        )
