"""
VibeForge — Planner Agent (Phase 2)
=====================================
Converts a frozen ApplicationSpec into an ordered module plan (DAG).

Phase 0: deterministic stub — entity × 3 modules + migration.
Phase 2: Qwen3-8B generates the real DAG with cross-cutting sequencing.

Decision locked in Phase 2: SEQUENTIAL synthesis.
Parallel synthesis is a Phase 3+ optimisation — not here.

Why sequential first:
  - Assembler conflict detection is trivial with sequential ordering
  - Module N can depend on code written by module N-1 (e.g. shared DTOs)
  - Parallel adds complexity with zero quality benefit at MVP scale

Output: ordered list[ModulePlan] where each module has:
  - module_id, name, module_type, dependencies
  - spec_slice: the exact subset of the spec this module reads
  - build_order: integer position in the sequence
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

from agents.harness.structured_output import StructuredOutputHarness, HarnessError

logger = logging.getLogger(__name__)


# ── Output schema ─────────────────────────────────────────────────────────────

class PlannedModule(BaseModel):
    """One module in the build plan."""
    module_id: str = Field(..., description="Unique ID e.g. 'product_entity'")
    name: str = Field(..., description="Class/file name e.g. 'ProductEntity'")
    module_type: str = Field(
        ...,
        description="entity | repository | service | controller | migration | test | config"
    )
    dependencies: list[str] = Field(
        default_factory=list,
        description="module_ids this module depends on — must be earlier in build order"
    )
    build_order: int = Field(
        ...,
        description="Sequential position — 1-based. No two modules share the same number."
    )
    rationale: str = Field(
        default="",
        description="Why this module is in this position in the DAG"
    )
    spec_paths: list[str] = Field(
        default_factory=list,
        description="Dot-notation paths into the spec that this module reads. "
                    "e.g. ['domain_model.entities.Product', 'api_model.endpoints']"
    )


class ModulePlanOutput(BaseModel):
    """Full planner output."""
    modules: list[PlannedModule] = Field(..., description="Ordered list — index = build order")
    synthesis_mode: str = Field(
        default="sequential",
        description="sequential (Phase 2) | parallel (Phase 3+). Always sequential here."
    )
    total_modules: int = Field(default=0)
    planner_notes: str = Field(default="", description="Any cross-cutting sequencing decisions")


# ── System prompt ──────────────────────────────────────────────────────────────

_PLANNER_SYSTEM = """\
You are the VibeForge Planner. Given a frozen application specification,
you produce an ordered module build plan (a DAG) for code generation.

Rules:
1. Output ONLY valid JSON matching the schema. No preamble, no markdown.
2. Synthesis mode is always "sequential" — never parallel.
3. Build order rules (strict):
   - Entities first (no dependencies)
   - Repositories after their entity
   - Services after their repositories
   - Controllers/APIs after their services
   - Migrations last among domain modules
   - Test modules after the module they test
   - Config/shared modules before anything that uses them
4. Every dependency in a module's 'dependencies' list must have a lower build_order.
5. module_id must be snake_case, unique, and descriptive.
6. spec_paths should list only the spec sections this module actually reads.
7. Keep planner_notes concise — note only non-obvious sequencing decisions.
"""


# ── Planner ────────────────────────────────────────────────────────────────────

class PlannerAgent:
    """
    Qwen3-8B planner: spec → ordered module DAG.

    In Phase 2, this replaces the deterministic stub in node_plan().
    Called by the generation graph before synthesis begins.
    """

    MODEL = "agent-model"   # → Qwen3-8B via LiteLLM

    def __init__(self, llm_client):
        self._harness = StructuredOutputHarness(llm_client)

    async def plan(
        self,
        spec_dict: dict[str, Any],
        stack_profile: str = "java_spring",
        job_id: str = "",
    ) -> ModulePlanOutput:
        """
        Generate the module build plan for a frozen spec.

        Args:
            spec_dict:     Frozen ApplicationSpec as dict
            stack_profile: Target stack (java_spring | python_fastapi | dotnet)
            job_id:        For correlation logging

        Returns:
            ModulePlanOutput with ordered list of PlannedModule objects
        """
        prompt = self._build_prompt(spec_dict, stack_profile)

        try:
            output, meta = await self._harness.call(
                output_schema=ModulePlanOutput,
                user_message=prompt,
                system_prompt=_PLANNER_SYSTEM,
                model=self.MODEL,
                context_tag=f"planner:{job_id or 'unknown'}",
            )
            # Enforce sequential mode always
            output.synthesis_mode = "sequential"
            output.total_modules = len(output.modules)

            # Validate: no circular dependencies
            self._validate_dag(output.modules)

            logger.info(
                "[Planner] job=%s modules=%d stack=%s",
                job_id, output.total_modules, stack_profile,
            )
            return output

        except HarnessError as e:
            logger.error("[Planner] Harness failed: %s — falling back to deterministic plan", e)
            return self._deterministic_fallback(spec_dict)

    # ── Prompt builder ────────────────────────────────────────────────────────

    def _build_prompt(self, spec: dict[str, Any], stack_profile: str) -> str:
        entities = [
            e["name"] for e in
            spec.get("domain_model", {}).get("entities", [])
        ]
        endpoints = spec.get("api_model", {}).get("endpoints", [])
        integrations = [
            i.get("name", "") for i in
            spec.get("integration_model", {}).get("integrations", [])
        ]
        workflows = spec.get("workflow_model", {}).get("state_machines", [])

        summary = {
            "vertical": spec.get("vertical", ""),
            "stack_profile": stack_profile,
            "entities": entities,
            "endpoint_count": len(endpoints),
            "endpoint_paths": [e.get("path", "") for e in endpoints[:10]],
            "integrations": integrations,
            "has_state_machines": len(workflows) > 0,
            "state_machine_names": [w.get("name", "") for w in workflows],
            "compliance_frameworks": spec.get("compliance_model", {}).get("frameworks", []),
            "security_roles": [
                r.get("name") for r in
                spec.get("security_model", {}).get("roles", [])
            ],
        }

        return (
            f"Generate a sequential module build plan for this application spec.\n\n"
            f"Spec summary:\n{json.dumps(summary, indent=2)}\n\n"
            f"Stack: {stack_profile}\n\n"
            f"For each entity ({', '.join(entities) or 'none yet'}), create:\n"
            f"  - <Entity>Entity (module_type: entity)\n"
            f"  - <Entity>Repository (module_type: repository, depends on entity)\n"
            f"  - <Entity>Service (module_type: service, depends on repository)\n\n"
            f"Then add:\n"
            f"  - One controller per API group (module_type: controller)\n"
            f"  - DatabaseMigrations (module_type: migration, last)\n"
            f"  - Integration connectors for: {integrations or 'none'}\n\n"
            f"Synthesis mode must be 'sequential'.\n"
            f"Total build_order must start at 1 and increment by 1 with no gaps."
        )

    # ── DAG validation ─────────────────────────────────────────────────────────

    @staticmethod
    def _validate_dag(modules: list[PlannedModule]) -> None:
        """
        Validates that:
        1. No two modules share the same build_order
        2. All dependency module_ids exist in the plan
        3. No dependency has a higher build_order than the module that needs it
        """
        order_map = {m.module_id: m.build_order for m in modules}
        seen_orders: set[int] = set()

        for m in modules:
            if m.build_order in seen_orders:
                raise ValueError(
                    f"Duplicate build_order {m.build_order} for module {m.module_id}"
                )
            seen_orders.add(m.build_order)

            for dep_id in m.dependencies:
                if dep_id not in order_map:
                    raise ValueError(
                        f"Module {m.module_id} depends on unknown module: {dep_id}"
                    )
                if order_map[dep_id] >= m.build_order:
                    raise ValueError(
                        f"Module {m.module_id} (order={m.build_order}) depends on "
                        f"{dep_id} (order={order_map[dep_id]}) which is not earlier"
                    )

    # ── Deterministic fallback ────────────────────────────────────────────────

    @staticmethod
    def _deterministic_fallback(spec: dict[str, Any]) -> ModulePlanOutput:
        """
        If Qwen3-8B fails all harness attempts, fall back to the deterministic
        Phase 0 plan. Guarantees the pipeline always has a module plan.
        """
        entities = [
            e["name"] for e in
            spec.get("domain_model", {}).get("entities", [])
        ]
        if not entities:
            entities = ["Core"]

        modules: list[PlannedModule] = []
        order = 1
        for name in entities:
            base = name.lower().replace(" ", "_")
            modules.append(PlannedModule(
                module_id=f"{base}_entity",
                name=f"{name}Entity",
                module_type="entity",
                dependencies=[],
                build_order=order,
                rationale="Entity first — no dependencies",
                spec_paths=[f"domain_model.entities.{name}"],
            ))
            order += 1
            modules.append(PlannedModule(
                module_id=f"{base}_repository",
                name=f"{name}Repository",
                module_type="repository",
                dependencies=[f"{base}_entity"],
                build_order=order,
                rationale="Repository after entity",
                spec_paths=[f"domain_model.entities.{name}"],
            ))
            order += 1
            modules.append(PlannedModule(
                module_id=f"{base}_service",
                name=f"{name}Service",
                module_type="service",
                dependencies=[f"{base}_repository"],
                build_order=order,
                rationale="Service after repository",
                spec_paths=[f"domain_model.entities.{name}", "api_model.endpoints"],
            ))
            order += 1

        modules.append(PlannedModule(
            module_id="database_migrations",
            name="DatabaseMigrations",
            module_type="migration",
            dependencies=[f"{e.lower().replace(' ', '_')}_entity" for e in entities],
            build_order=order,
            rationale="Migrations last — all entities must be defined first",
            spec_paths=["domain_model.entities"],
        ))

        return ModulePlanOutput(
            modules=modules,
            synthesis_mode="sequential",
            total_modules=len(modules),
            planner_notes="Deterministic fallback plan — LLM unavailable",
        )
