"""
VibeForge — Completeness Validator
====================================
Checks whether the current ApplicationSpec is complete enough
for generation to begin.

Two layers:
  Layer 1 — Deterministic checklist (zero LLM, per domain pack)
             Checks required fields, minimum entity counts, etc.
             Fast, cheap, runs on every spec delta.

  Layer 2 — LLM gap analysis (Qwen3-8B)
             For genuinely ambiguous completeness questions.
             Produces 3-8 gap questions as choice chips.
             Only runs when Layer 1 passes (no point asking gap
             questions if required fields are missing).

Output:
  - is_complete: bool
  - missing_required: list[str]    — blocking, must fix
  - gap_questions: list[GapQuestion] — non-blocking, improve quality

GapQuestion.choices are shown as choice chips in the UI.
Free-text input only appears as a last resort (choice_chips=None).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, Field

from agents.harness.structured_output import StructuredOutputHarness, HarnessError

logger = logging.getLogger(__name__)


# ── Gap question schema ───────────────────────────────────────────────────────

class GapQuestion(BaseModel):
    """One gap question shown to the user in the Spec Sheet UI."""
    question_id: str
    question: str
    section: str                    # which spec section this fills
    json_path: str                  # where in the spec the answer goes
    choice_chips: Optional[list[str]] = None   # None = free text input
    default_choice: Optional[str] = None
    priority: str = "should"        # "must" | "should" | "could"


class GapAnalysisOutput(BaseModel):
    """What Qwen3-8B returns from the gap analysis prompt."""
    gap_questions: list[GapQuestion] = Field(default_factory=list)
    analysis_summary: str = ""


# ── Deterministic checklist item ─────────────────────────────────────────────

@dataclass
class ChecklistItem:
    check_id: str
    description: str
    section: str
    is_satisfied: bool
    current_value: Any = None
    required_value: Any = None


# ── Validation result ─────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    is_complete: bool
    checklist: list[ChecklistItem] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    gap_questions: list[GapQuestion] = field(default_factory=list)
    completeness_percent: float = 0.0

    @property
    def blocking_issues(self) -> list[str]:
        return self.missing_required

    @property
    def can_proceed_to_review(self) -> bool:
        """True when all required fields are present (gap questions are optional)."""
        return len(self.missing_required) == 0


# ── System prompt for gap analysis ───────────────────────────────────────────

_GAP_SYSTEM = """\
You are a VibeForge specification analyst. You review an application specification
and identify gaps — missing information that would improve the quality of the
generated application.

Rules:
1. Output ONLY valid JSON matching the schema. No preamble.
2. Generate 3-8 gap questions maximum. Less is better.
3. Every question must have choice_chips where possible.
   Free text (choice_chips: null) is a last resort.
4. Focus on questions whose answers would meaningfully change what is generated.
   Do not ask about things that have reasonable defaults.
5. Prioritize: "must" = generation will fail without it,
               "should" = generation will be better with it,
               "could" = nice to have.
6. Set json_path to the exact dot-notation path in the spec where the answer goes.
   Example: "security_model.session_timeout_minutes"
"""


# ── Completeness Validator ────────────────────────────────────────────────────

class CompletenessValidator:
    """
    Validates ApplicationSpec completeness.

    Layer 1 (deterministic) runs synchronously and cheaply.
    Layer 2 (LLM gap analysis) is async and only runs when Layer 1 passes.

    Usage:
        validator = CompletenessValidator(llm_client)
        result = await validator.validate(spec_dict, pack_rules)
        if not result.can_proceed_to_review:
            show_errors(result.missing_required)
        else:
            show_gap_questions(result.gap_questions)
    """

    MODEL = "agent-model"   # → Qwen3-8B

    def __init__(self, llm_client=None):
        self._harness = StructuredOutputHarness(llm_client) if llm_client else None

    async def validate(
        self,
        spec_dict: dict[str, Any],
        pack_rules: Optional[dict[str, Any]] = None,
        run_gap_analysis: bool = True,
    ) -> ValidationResult:
        """
        Full validation. Layer 1 always runs. Layer 2 only if Layer 1 passes
        and run_gap_analysis=True and an LLM client is configured.
        """
        # ── Layer 1: deterministic ────────────────────────────────────────────
        checklist = self._run_checklist(spec_dict, pack_rules or {})
        missing = [
            item.description
            for item in checklist
            if not item.is_satisfied
        ]
        pct = (
            sum(1 for i in checklist if i.is_satisfied) / len(checklist) * 100
            if checklist else 100.0
        )

        result = ValidationResult(
            is_complete=len(missing) == 0,
            checklist=checklist,
            missing_required=missing,
            completeness_percent=round(pct, 1),
        )

        # ── Layer 2: LLM gap analysis ────────────────────────────────────────
        if (
            result.can_proceed_to_review
            and run_gap_analysis
            and self._harness is not None
        ):
            try:
                gap_output = await self._run_gap_analysis(spec_dict)
                result.gap_questions = gap_output.gap_questions
            except Exception as e:
                logger.warning("[Validator] Gap analysis failed: %s", e)
                # Non-blocking — proceed without gap questions

        return result

    def validate_sync(
        self,
        spec_dict: dict[str, Any],
        pack_rules: Optional[dict[str, Any]] = None,
    ) -> ValidationResult:
        """
        Synchronous Layer 1 only. Use for fast per-delta checks.
        """
        checklist = self._run_checklist(spec_dict, pack_rules or {})
        missing = [i.description for i in checklist if not i.is_satisfied]
        pct = (
            sum(1 for i in checklist if i.is_satisfied) / len(checklist) * 100
            if checklist else 100.0
        )
        return ValidationResult(
            is_complete=len(missing) == 0,
            checklist=checklist,
            missing_required=missing,
            completeness_percent=round(pct, 1),
        )

    # ── Layer 1: deterministic checklist ─────────────────────────────────────

    def _run_checklist(
        self,
        spec: dict[str, Any],
        pack_rules: dict[str, Any],
    ) -> list[ChecklistItem]:
        """
        Built-in checks that apply to every vertical.
        Pack-specific rules from pack_rules override/extend these.
        """
        checks: list[ChecklistItem] = []

        # C1: Must have at least one entity in domain_model
        entities = spec.get("domain_model", {}).get("entities", [])
        checks.append(ChecklistItem(
            check_id="c1_has_entities",
            description="At least one data entity must be defined in the domain model",
            section="domain_model",
            is_satisfied=len(entities) >= 1,
            current_value=len(entities),
            required_value="≥ 1",
        ))

        # C2: Must have at least one API endpoint
        endpoints = spec.get("api_model", {}).get("endpoints", [])
        checks.append(ChecklistItem(
            check_id="c2_has_endpoints",
            description="At least one API endpoint must be defined",
            section="api_model",
            is_satisfied=len(endpoints) >= 1,
            current_value=len(endpoints),
            required_value="≥ 1",
        ))

        # C3: Stack must be specified
        backend = spec.get("stack", {}).get("backend", "")
        checks.append(ChecklistItem(
            check_id="c3_has_stack",
            description="Backend stack must be selected",
            section="stack",
            is_satisfied=bool(backend),
            current_value=backend or "not set",
        ))

        # C4: Vertical must be set
        vertical = spec.get("vertical", "")
        checks.append(ChecklistItem(
            check_id="c4_has_vertical",
            description="Vertical must be selected",
            section="vertical",
            is_satisfied=bool(vertical),
            current_value=vertical or "not set",
        ))

        # C5: Security model must have at least one role
        roles = spec.get("security_model", {}).get("roles", [])
        checks.append(ChecklistItem(
            check_id="c5_has_roles",
            description="At least one security role must be defined",
            section="security_model",
            is_satisfied=len(roles) >= 1,
            current_value=len(roles),
            required_value="≥ 1",
        ))

        # C6: Must have at least one acceptance criterion
        criteria = spec.get("acceptance_criteria", [])
        checks.append(ChecklistItem(
            check_id="c6_has_acceptance_criteria",
            description="At least one acceptance criterion must be defined",
            section="acceptance_criteria",
            is_satisfied=len(criteria) >= 1,
            current_value=len(criteria),
            required_value="≥ 1",
        ))

        # C7: Each entity must have at least one field
        entity_without_fields = [
            e.get("name", "unknown")
            for e in entities
            if not e.get("fields")
        ]
        checks.append(ChecklistItem(
            check_id="c7_entities_have_fields",
            description=f"All entities must have at least one field"
                       + (f" (missing: {entity_without_fields})" if entity_without_fields else ""),
            section="domain_model",
            is_satisfied=len(entity_without_fields) == 0,
            current_value=entity_without_fields or "all ok",
        ))

        # Pack-specific rules
        for rule in pack_rules.get("required_checks", []):
            path = rule.get("path", "")
            parts = path.split(".")
            value = spec
            for p in parts:
                value = value.get(p, None) if isinstance(value, dict) else None
            satisfied = bool(value) if rule.get("type") == "non_empty" else value == rule.get("expected")
            checks.append(ChecklistItem(
                check_id=f"pack_{rule.get('id', 'custom')}",
                description=rule.get("description", f"{path} must be set"),
                section=parts[0] if parts else "unknown",
                is_satisfied=satisfied,
                current_value=value,
                required_value=rule.get("expected", "non-empty"),
            ))

        return checks

    # ── Layer 2: LLM gap analysis ─────────────────────────────────────────────

    async def _run_gap_analysis(
        self,
        spec: dict[str, Any],
    ) -> GapAnalysisOutput:
        """
        Ask Qwen3-8B to identify meaningful gaps in the spec.
        Returns structured gap questions as choice chips.
        """
        import json

        # Build a compact spec summary (not the full spec — too long)
        summary = {
            "vertical": spec.get("vertical", ""),
            "stack": spec.get("stack", {}),
            "entity_count": len(spec.get("domain_model", {}).get("entities", [])),
            "entity_names": [
                e.get("name") for e in
                spec.get("domain_model", {}).get("entities", [])
            ],
            "endpoint_count": len(spec.get("api_model", {}).get("endpoints", [])),
            "integration_count": len(
                spec.get("integration_model", {}).get("integrations", [])
            ),
            "has_compliance": bool(
                spec.get("compliance_model", {}).get("frameworks", [])
            ),
            "has_acceptance_criteria": bool(spec.get("acceptance_criteria", [])),
            "security_roles": [
                r.get("name") for r in
                spec.get("security_model", {}).get("roles", [])
            ],
        }

        prompt = (
            f"Review this application specification summary and identify the most "
            f"important gaps that would improve generation quality:\n\n"
            f"{json.dumps(summary, indent=2)}\n\n"
            f"Generate 3-8 gap questions with choice chips where possible."
        )

        output, _ = await self._harness.call(
            output_schema=GapAnalysisOutput,
            user_message=prompt,
            system_prompt=_GAP_SYSTEM,
            model=self.MODEL,
            context_tag="completeness_validator",
        )
        return output
