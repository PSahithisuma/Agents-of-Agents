"""
VibeForge — Reviewer + Fixer (Phase 2)
=========================================
Two agents that work together in the fix loop.

REVIEWER (Qwen3-8B):
  - Reads the GateReport (machine-readable facts — not LLM opinion)
  - Maps each failing file back to its module
  - Checks acceptance criteria coverage
  - Writes a FixPlan: which files to fix and why
  - Never makes judgment calls — only reads facts

FIXER (Qwen2.5-32B):
  - Receives the FixPlan from the Reviewer
  - Regenerates ONLY the files listed in the FixPlan
  - The prompt ALWAYS contains the concrete compiler/test/scanner errors
  - Loop bounded at MAX_FIX_ITERATIONS = 3 (Contract C5)
  - If still failing after 3 iterations → Escalation Gate

Contract C5 (non-negotiable):
  The Fixer prompt ALWAYS contains the concrete GateReport errors for the
  files it regenerates. The loop converges on facts, not re-rolled prompts.

Contract C5 violation example (what we NEVER do):
  "Fix the failing files" — no errors in prompt, loop diverges

Contract C5 correct usage:
  "Fix OrderService.java — error: cannot find symbol 'OrderRepository'"
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

from agents.harness.structured_output import StructuredOutputHarness, HarnessError
from agents.generation.synthesizer import FileMapOutput, SynthesizedFile, STACK_CONVENTIONS
from agents.generation.assembler import Assembler, AssemblyResult
from agents.graphs.generation_graph import GateResult, FixPlan

logger = logging.getLogger(__name__)


# ── Reviewer output schema ─────────────────────────────────────────────────────

class FileFixInstruction(BaseModel):
    """One file that needs fixing, with its exact errors."""
    file_path: str = Field(..., description="Exact path of the file to fix")
    module_id: str = Field(..., description="Which module owns this file")
    errors: list[str] = Field(..., description="Exact compiler/test/scanner error messages")
    fix_guidance: str = Field(
        default="",
        description="Reviewer's analysis of what the Fixer needs to do. "
                    "Based on the GateReport facts only — no guessing."
    )
    error_source: str = Field(
        default="compile",
        description="compile | unit_test | migration | api_smoke | semgrep | trivy | gitleaks"
    )


class ReviewerOutput(BaseModel):
    """The Reviewer's structured analysis of a failed gate."""
    fix_instructions: list[FileFixInstruction] = Field(
        default_factory=list,
        description="One instruction per file that needs fixing"
    )
    acceptance_criteria_gaps: list[str] = Field(
        default_factory=list,
        description="Acceptance criteria from the spec that are not covered by generated tests"
    )
    escalation_recommended: bool = Field(
        default=False,
        description="True if the Reviewer believes this is capability-bound "
                    "(not mechanically fixable by regenerating files)"
    )
    failure_classification: str = Field(
        default="mechanical",
        description="mechanical | capability_bound | ambiguous"
    )
    reviewer_notes: str = Field(
        default="",
        description="Brief summary of what went wrong — for the job console display"
    )


# ── Reviewer system prompt ─────────────────────────────────────────────────────

_REVIEWER_SYSTEM = """\
You are the VibeForge Reviewer. You read a GateReport (compiler errors, test
failures, scan findings) and produce a structured FixPlan.

Rules:
1. Output ONLY valid JSON matching the ReviewerOutput schema. No preamble.
2. Base your analysis ONLY on the GateReport facts provided. No guessing.
3. For each failing file, copy the EXACT error message — do not paraphrase.
4. Map each error to the file it came from. If the error says
   "OrderService.java:45: error: cannot find symbol", the file_path is
   the path to OrderService.java and the error is the exact message.
5. failure_classification:
   - "mechanical" = a clear code error the Fixer can fix by reading the error
   - "capability_bound" = requires understanding the spec at a deeper level
   - "ambiguous" = unclear
6. escalation_recommended = true ONLY if all errors are capability_bound.
7. acceptance_criteria_gaps = list acceptance criteria IDs that have no
   corresponding test in the generated test files.
8. Keep reviewer_notes to one sentence — it appears in the job console.
"""

# ── Fixer system prompt ────────────────────────────────────────────────────────

_FIXER_SYSTEM = """\
You are the VibeForge Fixer. You regenerate specific source files to fix
the exact compiler/test/scanner errors shown in the prompt.

Rules:
1. Output ONLY valid JSON matching the FileMapOutput schema. No preamble.
2. Fix ONLY the files listed in the fix instructions. Do not touch other files.
3. The fix must address the EXACT errors shown. Do not invent other changes.
4. Every fixed file must be COMPLETE and COMPILABLE — no partial fixes.
5. Do not add new features. Do not refactor unrelated code. Fix the error.
6. If the error is a missing import, add the import.
7. If the error is a missing method, add the method with the correct signature.
8. If the error is a test assertion failure, fix the test or the implementation
   (whichever is wrong — the gate output tells you which).
9. Follow the stack conventions exactly.
"""


# ── Reviewer ───────────────────────────────────────────────────────────────────

class ReviewerAgent:
    """
    Qwen3-8B reviewer: reads GateReport → writes FixPlan.

    Called when the gate fails. Maps errors to files and modules.
    Does not attempt to fix anything — only analysis and routing.
    """

    MODEL = "agent-model"   # → Qwen3-8B

    def __init__(self, llm_client, assembler: Optional[Assembler] = None):
        self._harness = StructuredOutputHarness(llm_client)
        self._assembler = assembler or Assembler()

    async def review(
        self,
        gate_result: GateResult,
        assembly_result: AssemblyResult,
        spec_dict: dict[str, Any],
        fix_iteration: int,
        max_iterations: int,
        job_id: str = "",
    ) -> ReviewerOutput:
        """
        Review a failed gate and produce fix instructions.

        Args:
            gate_result:       The GateResult from the QA gate
            assembly_result:   The assembled project (to map files to modules)
            spec_dict:         The frozen spec (for acceptance criteria check)
            fix_iteration:     Which fix attempt this is (1-indexed)
            max_iterations:    Maximum fix iterations (3)
            job_id:            For correlation logging

        Returns:
            ReviewerOutput with per-file fix instructions
        """
        prompt = self._build_prompt(
            gate_result, assembly_result, spec_dict, fix_iteration, max_iterations
        )

        try:
            output, meta = await self._harness.call(
                output_schema=ReviewerOutput,
                user_message=prompt,
                system_prompt=_REVIEWER_SYSTEM,
                model=self.MODEL,
                context_tag=f"reviewer:{job_id}:iter{fix_iteration}",
            )
            logger.info(
                "[Reviewer] job=%s iter=%d files_to_fix=%d escalate=%s",
                job_id, fix_iteration,
                len(output.fix_instructions),
                output.escalation_recommended,
            )
            return output

        except HarnessError as e:
            logger.error("[Reviewer] Harness failed: %s — using deterministic fallback", e)
            return self._deterministic_fallback(gate_result, assembly_result)

    def _build_prompt(
        self,
        gate: GateResult,
        assembly: AssemblyResult,
        spec: dict[str, Any],
        iteration: int,
        max_iter: int,
    ) -> str:
        parts = [f"## GateReport (iteration {iteration}/{max_iter})"]

        # Gate step results
        for step in gate.steps:
            status = "PASSED" if step.passed else "FAILED"
            parts.append(f"\nStep: {step.step} — {status}")
            if not step.passed and step.output:
                parts.append(f"Output:\n{step.output[:800]}")
            if step.coverage_pct is not None:
                parts.append(f"Coverage: {step.coverage_pct:.1f}%")

        # Failing files with their module owners
        if gate.failing_files:
            parts.append("\n## Failing files")
            for fpath in gate.failing_files:
                owner = assembly.get_module_for_file(assembly, fpath) if hasattr(assembly, 'get_module_for_file') else "unknown"
                parts.append(f"  {fpath} (module: {owner})")

        # Conflicts detected in assembly
        if assembly.has_conflicts:
            parts.append("\n## Assembly conflicts (treat as errors)")
            for c in assembly.conflicts:
                parts.append(f"  {c}")

        # Acceptance criteria from spec
        criteria = spec.get("acceptance_criteria", [])
        if criteria:
            parts.append(f"\n## Acceptance criteria to verify ({len(criteria)} total)")
            for ac in criteria[:5]:  # first 5
                parts.append(f"  [{ac.get('criterion_id', '?')}] {ac.get('scenario', '')[:100]}")

        parts.append(
            f"\n## Task\n"
            f"Produce fix instructions for the {len(gate.failing_files)} failing file(s). "
            f"Copy exact error messages. Map each error to its file and module."
        )
        return "\n".join(parts)

    @staticmethod
    def _deterministic_fallback(
        gate: GateResult,
        assembly: AssemblyResult,
    ) -> ReviewerOutput:
        """Fallback: create one fix instruction per failing file with raw gate output."""
        instructions = []
        for fpath in gate.failing_files:
            failed_steps = [s for s in gate.steps if not s.passed]
            errors = []
            for step in failed_steps:
                if step.output:
                    errors.append(f"[{step.step}] {step.output[:200]}")
            # Use file_ownership dict directly — no method call needed
            module_id = assembly.file_ownership.get(fpath, "unknown")
            instructions.append(FileFixInstruction(
                file_path=fpath,
                module_id=module_id,
                errors=errors or ["Gate failure — see gate report"],
                fix_guidance="Fix the reported errors",
                error_source=failed_steps[0].step if failed_steps else "compile",
            ))
        return ReviewerOutput(
            fix_instructions=instructions,
            failure_classification="mechanical",
            escalation_recommended=False,
            reviewer_notes="Deterministic fallback — reviewer LLM unavailable",
        )


# ── Fixer ──────────────────────────────────────────────────────────────────────

class FixerAgent:
    """
    Qwen2.5-32B fixer: regenerates ONLY the files in the FixPlan.

    Contract C5: the prompt ALWAYS contains the concrete GateReport errors.
    The fix loop converges on facts, not re-rolled prompts.
    """

    MODEL = "coder-model"   # → Qwen2.5-Coder-32B

    def __init__(self, llm_client, stack_profile: str = "java_spring"):
        self._harness = StructuredOutputHarness(llm_client)
        self._stack = stack_profile
        self._assembler = Assembler()

    async def fix(
        self,
        reviewer_output: ReviewerOutput,
        assembly_result: AssemblyResult,
        fix_iteration: int,
        job_id: str = "",
    ) -> list[FileMapOutput]:
        """
        Fix the files identified by the Reviewer.

        Args:
            reviewer_output:   The Reviewer's structured analysis
            assembly_result:   Current assembled project (for file content)
            fix_iteration:     Which fix attempt (1-3)
            job_id:            For correlation logging

        Returns:
            List of FileMapOutput — one per module being fixed.
            The Assembler's apply_fixes() merges these into the tree.
        """
        fixed_maps: list[FileMapOutput] = []

        for instruction in reviewer_output.fix_instructions:
            fpath = instruction.file_path
            current_content = assembly_result.assembled_files.get(
                Assembler._normalize_path(fpath), ""
            )

            prompt = self._build_fix_prompt(
                instruction=instruction,
                current_content=current_content,
                fix_iteration=fix_iteration,
            )

            try:
                output, meta = await self._harness.call(
                    output_schema=FileMapOutput,
                    user_message=prompt,
                    system_prompt=_FIXER_SYSTEM,
                    model=self.MODEL,
                    context_tag=f"fixer:{instruction.module_id}:iter{fix_iteration}",
                )
                output.module_id = instruction.module_id
                fixed_maps.append(output)

                logger.info(
                    "[Fixer] job=%s iter=%d module=%s files=%d",
                    job_id, fix_iteration, instruction.module_id, len(output.files),
                )

            except HarnessError as e:
                logger.error(
                    "[Fixer] Module %s iter %d failed all harness attempts: %s",
                    instruction.module_id, fix_iteration, e,
                )
                # Return unchanged file — gate will catch it again
                fixed_maps.append(FileMapOutput(
                    module_id=instruction.module_id,
                    module_name=instruction.module_id,
                    files=[SynthesizedFile(
                        filename=fpath,
                        content=current_content or f"// FIX FAILED iteration {fix_iteration}",
                        language="java",
                    )],
                    synthesis_notes=f"Fix failed iteration {fix_iteration}",
                ))

        return fixed_maps

    def _build_fix_prompt(
        self,
        instruction: FileFixInstruction,
        current_content: str,
        fix_iteration: int,
    ) -> str:
        """
        Contract C5: errors are ALWAYS in this prompt.
        Never call the Fixer without the concrete gate errors.
        """
        conventions = STACK_CONVENTIONS.get(self._stack, STACK_CONVENTIONS["java_spring"])

        parts = [
            f"## Fix attempt {fix_iteration} — {instruction.file_path}",
            f"\n## EXACT ERRORS FROM GATE (fix these — do not invent other changes)",
        ]

        for i, err in enumerate(instruction.errors, 1):
            parts.append(f"{i}. {err}")

        if instruction.fix_guidance:
            parts.append(f"\n## Reviewer analysis\n{instruction.fix_guidance}")

        parts.append(f"\n## Stack conventions\n{conventions}")

        parts.append(f"\n## Current file content\n```\n{current_content[:2000]}\n```")

        parts.append(
            f"\n## Task\n"
            f"Regenerate '{instruction.file_path}' (module: {instruction.module_id}) "
            f"to fix the exact errors above.\n"
            f"module_id must be '{instruction.module_id}'.\n"
            f"Return the complete fixed file — not a diff, not a partial fix."
        )

        return "\n".join(parts)


# ── Metacognition Gate Tier 1 ──────────────────────────────────────────────────

class MetacognitionGateTier1:
    """
    Qwen3-8B small-model arbiter for ambiguous gate decisions.

    Phase 1: Tier 0 deterministic rules only.
    Phase 2: Tier 1 fires for cases Tier 0 can't resolve.

    Output: NEEDED | NOT_NEEDED | NARROW_QUERY:<refined_query>
    Every decision logged to gate_decisions table for HDPO training.
    """

    MODEL = "gate-model"   # → Qwen3-8B (same as agent-model, separate alias for tracking)

    def __init__(self, llm_client):
        self._harness = StructuredOutputHarness(llm_client)

    async def arbitrate(
        self,
        decision_point: str,
        context_summary: str,
        job_id: str = "",
    ) -> dict[str, Any]:
        """
        Arbitrate an ambiguous gate decision.

        Args:
            decision_point:    "retrieval" | "escalation" | "tool"
            context_summary:   Plain English description of the situation
            job_id:            For correlation logging

        Returns:
            dict with keys: verdict, narrow_query, rationale, tier
        """

        class Tier1Output(BaseModel):
            verdict: str = Field(
                ...,
                description="NEEDED | NOT_NEEDED | NARROW_QUERY"
            )
            narrow_query: str = Field(
                default="",
                description="If verdict is NARROW_QUERY, the refined search query"
            )
            rationale: str = Field(
                ...,
                description="One sentence explaining the decision — logged for HDPO training"
            )
            confidence: float = Field(
                default=0.7, ge=0.0, le=1.0,
                description="Confidence in this verdict 0.0-1.0"
            )

        system = """\
You are the VibeForge Metacognition Gate Tier 1 arbiter.
Your job: decide whether a retrieval, tool call, or escalation is NEEDED.

Output ONLY valid JSON. Verdicts:
- NEEDED:       The operation is necessary for correctness
- NOT_NEEDED:   The operation would be redundant or wasteful
- NARROW_QUERY: The operation is needed but with a more specific query

The rationale is logged as HDPO training data — be precise and factual.
Avoid: "it might help", "could be useful". Prefer: "entity X is not in context",
"all named entities are already present", "regulation Y requires documentation".
"""

        prompt = (
            f"Decision point: {decision_point}\n\n"
            f"Context:\n{context_summary}\n\n"
            f"Should this {decision_point} proceed?"
        )

        try:
            output, _ = await self._harness.call(
                output_schema=Tier1Output,
                user_message=prompt,
                system_prompt=system,
                model=self.MODEL,
                context_tag=f"gate_tier1:{decision_point}:{job_id}",
            )
            logger.info(
                "[Gate Tier1] point=%s verdict=%s confidence=%.2f rationale=%s",
                decision_point, output.verdict, output.confidence,
                output.rationale[:80],
            )
            return {
                "verdict": output.verdict,
                "narrow_query": output.narrow_query,
                "rationale": output.rationale,
                "confidence": output.confidence,
                "tier": 1,
            }
        except HarnessError:
            # Tier 1 failure → default NOT_NEEDED (conservative)
            return {
                "verdict": "NOT_NEEDED",
                "narrow_query": "",
                "rationale": "Tier 1 arbiter failed — defaulting to NOT_NEEDED (conservative)",
                "confidence": 0.3,
                "tier": 1,
            }
