"""
VibeForge — Generation Graph (Phase 0 — all stubs, fully wired)
================================================================
State machine: plan_node → scaffold_node → synthesize_node → assemble_node
  → gate_node → reviewer_node → fixer_node (≤3) → escalation_gate_node
  → gate_node (C6: re-enter after escalation) → deliver_node

Contracts enforced here:
  C2  — No process-memory state. Checkpointed in Postgres per node.
  C3  — Long work is a job. Every node writes SSE events.
  C5  — Fix loop hard-capped at MAX_FIX_ITERATIONS = 3.
  C6  — Escalated output re-enters the sandbox gate (no trust shortcut).
  C10 — Scaffold is templates only, zero LLM.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, date as _date
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── Module-level LLM client + stack profile ───────────────────────────────────
# Set by build_generation_graph() before compilation.
# Avoids storing non-serialisable objects in LangGraph state.
_LLM_CLIENT = None          # set by build_generation_graph
_STACK_PROFILE = "python_fastapi"  # set by build_generation_graph

# ── Module-level budget-tracking clients (Contract C11) ───────────────────────
# Injected by build_generation_graph() — optional: silently skipped if None.
_REDIS_CLIENT = None    # redis.Redis compatible client
_PG_CONN = None         # asyncpg Connection or compatible


def _record_llm_usage(
    tenant_id: str,
    job_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float = 0.0,
) -> None:
    """
    Contract C11: per-tenant budget accounting.
    1. Redis  — INCRBY budget:{tenant_id}:{date} total_tokens  (atomic counter)
    2. Postgres — INSERT INTO llm_calls (...) on best-effort (never raises)
    Called synchronously from node_synthesize after each LLM module synthesis.
    """
    today = _date.today().isoformat()          # e.g. "2026-08-24"
    redis_key = f"budget:{tenant_id}:{today}"
    total_tokens = prompt_tokens + completion_tokens

    # ── Redis INCRBY ──────────────────────────────────────────────────────────
    if _REDIS_CLIENT is not None:
        try:
            _REDIS_CLIENT.incrby(redis_key, total_tokens)
            # Expire the daily key after 90 days so Redis doesn't grow unbounded
            _REDIS_CLIENT.expire(redis_key, 90 * 86400)
            logger.debug(
                "[budget] Redis INCRBY %s +%d tokens (job=%s)",
                redis_key, total_tokens, job_id[:8],
            )
        except Exception as _e:
            logger.warning("[budget] Redis write failed: %s", _e)

    # ── Postgres INSERT (sync path via psycopg2 or asyncpg run_sync) ──────────
    if _PG_CONN is not None:
        _SQL = (
            "INSERT INTO llm_calls "
            "(tenant_id, job_id, model, prompt_tokens, completion_tokens, cost_usd, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)"
        )
        try:
            cur = _PG_CONN.cursor()
            cur.execute(_SQL, (
                tenant_id, job_id, model,
                prompt_tokens, completion_tokens, cost_usd,
                datetime.now(timezone.utc),
            ))
            _PG_CONN.commit()
            cur.close()
            logger.debug(
                "[budget] PG INSERT llm_calls job=%s model=%s tokens=%d+%d cost=$%.6f",
                job_id[:8], model, prompt_tokens, completion_tokens, cost_usd,
            )
        except Exception as _e:
            logger.warning("[budget] Postgres write failed: %s", _e)




def _run_coro(coro):
    """
    Execute an async coroutine from a synchronous context.
    LangGraph runs nodes in thread pool workers which have NO asyncio event loop.
    `asyncio.get_event_loop()` raises RuntimeError in those threads, so we create
    a fresh event loop, run the coroutine, then tear it down cleanly.
    """
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Already inside an async context (e.g. tests) — use a new thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=300)
        if not loop.is_closed():
            return loop.run_until_complete(coro)
    except RuntimeError:
        pass
    # Thread pool: no event loop exists — create one
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


MAX_FIX_ITERATIONS = 3   # Contract C5


# ── Phase enum ────────────────────────────────────────────────────────────────

class GenerationPhase(str, Enum):
    """Phase values emitted in SSE events. Tests assert on .value strings."""
    PLANNING    = "planning"
    SCAFFOLDING = "scaffolding"
    SYNTHESIZING = "synthesizing"
    ASSEMBLING  = "assembling"
    GATING      = "gating"
    REVIEWING   = "reviewing"
    FIXING      = "fixing"
    ESCALATING  = "escalation"
    DELIVERING  = "delivering"
    DONE        = "done"
    FAILED      = "failed"

# Test-facing alias
JobPhase = GenerationPhase


class JobStatus(str, Enum):
    QUEUED       = "queued"
    RUNNING      = "running"
    GATED        = "gated"
    FIXING       = "fixing"
    ESCALATED    = "escalated"
    DELIVERED    = "delivered"
    FAILED       = "failed"
    PAUSED_HUMAN = "paused_human"


# ── Sub-models ────────────────────────────────────────────────────────────────

class GateStepResult(BaseModel):
    step: str
    passed: bool
    duration_ms: int = 0
    output: str = ""
    coverage_pct: Optional[float] = None


class GateResult(BaseModel):
    passed: bool
    steps: list[GateStepResult] = Field(default_factory=list)
    failing_files: list[str] = Field(default_factory=list)
    report_id: str = Field(default_factory=lambda: str(uuid4()))

    @property
    def failed_steps(self) -> list[GateStepResult]:
        return [s for s in self.steps if not s.passed]


class ModulePlan(BaseModel):
    module_id: str
    name: str
    module_type: str                               # entity|repository|service|migration|test
    dependencies: list[str] = Field(default_factory=list)
    spec_slice: dict[str, Any] = Field(default_factory=dict)
    spec_paths: list[str] = Field(default_factory=list)  # sections of spec this module reads


class FileMap(BaseModel):
    module_id: str
    files: dict[str, str] = Field(default_factory=dict)


class FixPlan(BaseModel):
    fix_plan_id: str = Field(default_factory=lambda: str(uuid4()))
    files_to_fix: list[str] = Field(default_factory=list)
    errors_by_file: dict[str, list[str]] = Field(default_factory=dict)
    iteration: int = 1
    escalation_recommended: bool = False
    failure_classification: str = ""


# ── Phase 2 agent imports ─────────────────────────────────────────────────────
# IMPORTANT: must be AFTER GateResult and FixPlan are defined.
# reviewer_fixer.py imports GateResult + FixPlan from this module.
# If we import reviewer_fixer before they're defined → circular ImportError.
try:
    from agents.generation.planner import PlannerAgent, ModulePlanOutput
    from agents.generation.synthesizer import SynthesizerAgent, FileMapOutput as SynthFileMap
    from agents.generation.assembler import Assembler, AssemblyResult
    from agents.generation.reviewer_fixer import ReviewerAgent, FixerAgent, MetacognitionGateTier1
    _PHASE2_AVAILABLE = True
except ImportError as _phase2_err:
    _PHASE2_AVAILABLE = False
    logger.warning("[Graph] Phase 2 agents not importable — running in Phase 0 stub mode. Error: %s", _phase2_err)


# ── Graph state ───────────────────────────────────────────────────────────────

class GenerationState(BaseModel):
    """
    Complete job state. Serialised by LangGraph into the Postgres checkpointer.
    Any worker can resume any job after a crash.
    Contract C2: nothing lives in process memory.
    """
    job_id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str = ""
    project_id: str = ""
    spec_id: str = ""
    spec_version: int = 1
    canonical_hash: str = ""

    current_phase: GenerationPhase = GenerationPhase.PLANNING
    job_status: JobStatus = JobStatus.QUEUED
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    # Fix-loop — CONTRACT C5: hard ceiling
    fix_iteration: int = 0                   # internal counter (1-indexed when active)
    fix_count: int = 0                       # alias exposed to tests
    max_fix_iterations: int = MAX_FIX_ITERATIONS
    escalation_used: bool = False
    escalation_approved: bool = False

    # Node outputs
    module_plan: list[ModulePlan] = Field(default_factory=list)
    scaffold_files: dict[str, str] = Field(default_factory=dict)
    file_maps: list[FileMap] = Field(default_factory=list)
    assembled_files: dict[str, str] = Field(default_factory=dict)
    assembly_conflicts: list[str] = Field(default_factory=list)
    gate_result: Optional[GateResult] = None
    fix_plan: Optional[FixPlan] = None

    # Delivery
    gitea_repo_url: Optional[str] = None
    minio_bundle_key: Optional[str] = None
    preview_url: Optional[str] = None
    sbom_ref: Optional[str] = None

    # Errors
    error_message: Optional[str] = None

    # SSE event log — plain dicts so LangGraph serialiser never chokes
    events: list[dict[str, Any]] = Field(default_factory=list)

    # Input helpers for stub nodes
    spec_entity_names: list[str] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}

    # ── synthesized_modules alias (tests use this name) ───────────────────────
    @property
    def synthesized_modules(self) -> list[FileMap]:
        return self.file_maps

    # ── Safe JSON serialisation ───────────────────────────────────────────────
    def model_dump_json(self, **kwargs) -> str:
        """
        Safely serialise state to JSON for the Postgres checkpointer.
        Converts any JobEvent wrapper objects back to plain dicts first,
        so Pydantic's serialiser never encounters an unknown type.
        """
        import json as _j

        def _to_dict(e) -> dict:
            if isinstance(e, dict):
                return e
            # JobEvent wrapper — convert back to dict
            return {
                "event_id": getattr(e, "event_id", ""),
                "job_id":   getattr(e, "job_id", ""),
                "node":     getattr(e, "node", ""),
                "phase":    e.phase.value if hasattr(e, "phase") else "",
                "payload":  getattr(e, "payload", {}),
                "ts":       getattr(e, "ts", ""),
            }

        # Temporarily swap events to plain dicts for serialisation
        original_events = self.events
        self.events = [_to_dict(e) for e in original_events]
        try:
            d = self.model_dump(mode="json")
        finally:
            self.events = original_events  # restore wrappers

        d.pop("model_config", None)
        return _j.dumps(d, default=str)


# ── SSE emission ──────────────────────────────────────────────────────────────

def _emit(state: GenerationState, node: str, phase: GenerationPhase,
          payload: dict[str, Any]) -> GenerationState:
    """
    Append a plain-dict event to state.events (feeds the SSE stream).
    Stored as dicts so LangGraph's checkpoint serialiser never fails.
    Contract C3: every node transition writes at least one event.
    """
    event: dict[str, Any] = {
        "event_id": str(uuid4()),
        "job_id": state.job_id,
        "node": node,
        "phase": phase.value,      # plain string — no enum wrapper in the dict
        "payload": payload,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    state.events.append(event)
    logger.info("[SSE] job=%s  node=%-20s  phase=%-14s  %s",
                state.job_id[:8], node, phase.value, json.dumps(payload))
    return state


# ── JobEvent wrapper (post-graph, for test assertions) ────────────────────────

class _PV:
    """Wraps a phase string so .value works the same as a Pydantic Enum."""
    __slots__ = ("value",)
    def __init__(self, v: str): self.value = v
    def __eq__(self, other):
        return self.value == (other.value if hasattr(other, "value") else other)
    def __hash__(self): return hash(self.value)
    def __repr__(self): return f"Phase({self.value!r})"


class JobEvent:
    """
    Thin wrapper around an event dict. Exposes .node and .phase.value
    so tests can do {e.phase.value for e in state.events}.
    """
    __slots__ = ("event_id", "job_id", "node", "phase", "payload", "ts")

    def __init__(self, d: dict):
        self.event_id = d.get("event_id", "")
        self.job_id   = d.get("job_id", "")
        self.node     = d.get("node", "")
        self.phase    = _PV(d.get("phase", ""))
        self.payload  = d.get("payload", {})
        self.ts       = d.get("ts", "")

    def __repr__(self):
        return f"JobEvent(node={self.node!r}, phase={self.phase.value!r})"


def _wrap_events(state: GenerationState) -> GenerationState:
    """Convert raw event dicts to JobEvent objects after ainvoke completes."""
    state.events = [
        JobEvent(e) if isinstance(e, dict) else e
        for e in state.events
    ]
    return state


# ── Node: plan_node ────────────────────────────────────────────────────────────

def node_plan(state: GenerationState) -> dict:
    """
    Planner: frozen Spec IR → ordered module DAG.
    Phase 2: PlannerAgent (Qwen3-8B) generates real DAG.
    Falls back to deterministic stub if agent unavailable.
    """
    state = _emit(state, "plan_node", GenerationPhase.PLANNING, {"status": "started"})
    state.current_phase = GenerationPhase.PLANNING
    state.job_status    = JobStatus.RUNNING
    state.started_at    = datetime.now(timezone.utc).isoformat()

    modules: list[ModulePlan] = []

    if _PHASE2_AVAILABLE and _LLM_CLIENT and state.spec_entity_names:
        # Phase 2: use real PlannerAgent
        import asyncio
        try:
            planner = PlannerAgent(_LLM_CLIENT)
            spec_dict = getattr(state, "spec_snapshot", {}) or {}
            if not spec_dict:
                # Build minimal spec_dict from entity names
                spec_dict = {
                    "domain_model": {
                        "entities": [{"name": n, "fields": []} for n in state.spec_entity_names]
                    },
                    "api_model": {"endpoints": []},
                    "vertical": "",
                    "stack": {"backend": _STACK_PROFILE},
                }
            plan_output = _run_coro(
                planner.plan(spec_dict, stack_profile=_STACK_PROFILE, job_id=state.job_id)
            )
            for pm in plan_output.modules:
                modules.append(ModulePlan(
                    module_id=pm.module_id,
                    name=pm.name,
                    module_type=pm.module_type,
                    dependencies=pm.dependencies,
                    spec_paths=getattr(pm, "spec_paths", []),
                    spec_slice={"spec_paths": getattr(pm, "spec_paths", [])},
                ))
            logger.info("[node_plan] Phase 2 planner: %d modules", len(modules))
        except Exception as e:
            logger.warning("[node_plan] Phase 2 planner failed (%s) — falling back to stub", e)
            modules = []

    if not modules:
        # Deterministic fallback (Phase 0 stub)
        if state.spec_entity_names:
            for i, name in enumerate(state.spec_entity_names):
                b = f"m{i*3}"
                modules += [
                    ModulePlan(module_id=f"{b}_entity",  name=f"{name}Entity",
                               module_type="entity",     dependencies=[]),
                    ModulePlan(module_id=f"{b}_repo",    name=f"{name}Repository",
                               module_type="repository", dependencies=[f"{b}_entity"]),
                    ModulePlan(module_id=f"{b}_service", name=f"{name}Service",
                               module_type="service",    dependencies=[f"{b}_repo"]),
                ]
            modules.append(ModulePlan(
                module_id="migration", name="DatabaseMigrations",
                module_type="migration", dependencies=[]))
        else:
            modules = [
                ModulePlan(module_id="m1", name="CustomerService",
                           module_type="service", dependencies=[]),
            ]

    state.module_plan = modules
    state = _emit(state, "plan_node", GenerationPhase.PLANNING,
                  {"status": "complete", "module_count": len(modules),
                   "modules": [m.name for m in modules]})
    return state.model_dump()


# ── Node: scaffold_node ────────────────────────────────────────────────────────

def node_scaffold(state: GenerationState) -> dict:
    """
    Scaffold: Copier template rendering. Zero LLM. Contract C10.
    Phase 0 stub — representative file set.
    """
    state = _emit(state, "scaffold_node", GenerationPhase.SCAFFOLDING, {"status": "started"})
    state.current_phase = GenerationPhase.SCAFFOLDING
    state.scaffold_files = {
        "pom.xml":                              "<!-- STUB: Maven build -->",
        "Dockerfile":                           "# STUB: FROM eclipse-temurin:21-jre",
        "docker-compose.yml":                   "# STUB: compose",
        "src/main/resources/application.yml":   "# STUB: Spring config",
        ".github/workflows/ci.yml":             "# STUB: CI workflow",
    }
    state = _emit(state, "scaffold_node", GenerationPhase.SCAFFOLDING,
                  {"status": "complete", "file_count": len(state.scaffold_files)})
    return state.model_dump()


# ── Node: synthesize_node ──────────────────────────────────────────────────────

def node_synthesize(state: GenerationState) -> dict:
    """
    Per-module code generation. Sequential (Phase 2 decision — locked).
    Phase 2: SynthesizerAgent (Qwen2.5-Coder-32B) generates real code.
    Falls back to stub if agent unavailable.
    """
    state = _emit(state, "synthesize_node", GenerationPhase.SYNTHESIZING,
                  {"status": "started", "total": len(state.module_plan)})
    state.current_phase = GenerationPhase.SYNTHESIZING
    file_maps: list[FileMap] = []

    if _PHASE2_AVAILABLE and _LLM_CLIENT:
        import asyncio
        synthesizer = SynthesizerAgent(_LLM_CLIENT, stack_profile=_STACK_PROFILE)
        previously_synthesized: dict[str, str] = {}
        spec_dict = getattr(state, "spec_snapshot", {}) or {}

        for idx, module in enumerate(state.module_plan):
            state = _emit(state, "synthesize_node", GenerationPhase.SYNTHESIZING, {
                "status": "synthesizing",
                "module": module.name,
                "index": idx + 1,
                "total": len(state.module_plan),
            })
            # Respect Groq free-tier rate limits: 60s gap between modules
            # (qwen/qwen3.6-27b allows ~1 large request/min on free tier)
            if idx > 0:
                import time as _time
                logger.info("[node_synthesize] Rate-limit pause 60s before module %s", module.name)
                _time.sleep(60)
            try:
                synth_out = _run_coro(
                    synthesizer.synthesize_module(
                        module=module,
                        spec_dict=spec_dict,
                        previously_synthesized=previously_synthesized,
                        job_id=state.job_id,
                    )
                )
                fm = FileMap(module_id=module.module_id)
                for sf in synth_out.files:
                    fm.files[sf.filename] = sf.content
                    previously_synthesized[sf.filename] = sf.content
                file_maps.append(fm)
                logger.info("[node_synthesize] module=%s files=%d", module.name, len(fm.files))
                # Contract C11: record token usage for per-tenant budget tracking
                _usage = getattr(synth_out, "usage", None) or {}
                _prompt_tok  = int(getattr(_usage, "prompt_tokens",     0) or _usage.get("prompt_tokens",     0) if hasattr(_usage, "get") else getattr(_usage, "prompt_tokens",     0))
                _compl_tok   = int(getattr(_usage, "completion_tokens", 0) or _usage.get("completion_tokens", 0) if hasattr(_usage, "get") else getattr(_usage, "completion_tokens", 0))
                # Heuristic fallback: ~3 chars/token, use synthesized code length
                if _prompt_tok == 0:
                    _prompt_tok = max(200, sum(len(c) for c in fm.files.values()) // 3)
                if _compl_tok == 0:
                    _compl_tok  = max(100, sum(len(c) for c in fm.files.values()) // 3)
                _record_llm_usage(
                    tenant_id        = state.tenant_id,
                    job_id           = state.job_id,
                    model            = "coder-model",
                    prompt_tokens    = _prompt_tok,
                    completion_tokens = _compl_tok,
                )

            except Exception as e:
                logger.error("[node_synthesize] module=%s failed: %s — using stub", module.name, e)
                pkg = module.name.lower()
                file_maps.append(FileMap(
                    module_id=module.module_id,
                    files={
                        f"src/main/java/com/vibeforge/{pkg}/{module.name}.java":
                            f"// SYNTHESIS FAILED: {module.name}",
                    },
                ))
    else:
        # Phase 0 stub fallback
        for idx, module in enumerate(state.module_plan):
            state = _emit(state, "synthesize_node", GenerationPhase.SYNTHESIZING, {
                "status": "synthesizing", "module": module.name,
                "index": idx + 1, "total": len(state.module_plan),
            })
            pkg = module.name.lower()
            file_maps.append(FileMap(
                module_id=module.module_id,
                files={
                    f"src/main/java/com/vibeforge/{pkg}/{module.name}.java":
                        f"// STUB: {module.name}\npublic class {module.name} {{}}",
                    f"src/test/java/com/vibeforge/{pkg}/{module.name}Test.java":
                        f"// STUB test: {module.name}",
                },
            ))

    state.file_maps = file_maps
    state = _emit(state, "synthesize_node", GenerationPhase.SYNTHESIZING,
                  {"status": "complete", "module_count": len(file_maps)})
    return state.model_dump()


# ── Node: assemble_node ────────────────────────────────────────────────────────

def node_assemble(state: GenerationState) -> dict:
    """
    Merge per-module FileMaps. Deterministic conflict detection. Zero LLM.
    Phase 2: uses real Assembler with strict path validation.
    Conflicts FLAGGED in AssemblyResult — never silently overwritten.
    """
    state = _emit(state, "assemble_node", GenerationPhase.ASSEMBLING, {"status": "started"})
    state.current_phase = GenerationPhase.ASSEMBLING

    if _PHASE2_AVAILABLE:
        from agents.generation.synthesizer import FileMapOutput as FMO, SynthesizedFile
        assembler = Assembler()
        # Convert state.file_maps (FileMap Pydantic) to FileMapOutput objects
        fmo_list = []
        for fm in state.file_maps:
            fmo = FMO(module_id=fm.module_id, module_name=fm.module_id)
            for path, code in fm.files.items():
                fmo.files.append(SynthesizedFile(filename=path, content=code))
            fmo_list.append(fmo)

        result = assembler.assemble(
            scaffold_files=state.scaffold_files,
            module_outputs=fmo_list,
        )
        state.assembled_files   = result.assembled_files
        state.assembly_conflicts = result.conflict_paths
        conflicts = result.conflict_paths
    else:
        # Phase 0 fallback
        assembled: dict[str, str] = {}
        conflicts: list[str] = []
        assembled.update(state.scaffold_files)
        for fm in state.file_maps:
            for path, code in fm.files.items():
                if path in assembled and path not in state.scaffold_files:
                    conflicts.append(path)
                    logger.warning("[ASSEMBLE] Conflict: %s", path)
                else:
                    assembled[path] = code
        state.assembled_files   = assembled
        state.assembly_conflicts = conflicts

    state = _emit(state, "assemble_node", GenerationPhase.ASSEMBLING, {
        "status":     "complete" if not conflicts else "conflicts_detected",
        "file_count":  len(state.assembled_files),
        "conflicts":   conflicts,
    })
    return state.model_dump()


# ── Node: gate_node ────────────────────────────────────────────────────────────

def node_gate(state: GenerationState) -> dict:
    """
    7-step QA gate. Phase 0 stub: always passes.
    To exercise the fix loop, flip passed=False below.
    Contract: gate output is machine-readable facts, not LLM opinion.
    """
    state = _emit(state, "gate_node", GenerationPhase.GATING, {
        "status": "started", "iteration": state.fix_iteration})
    state.current_phase = GenerationPhase.GATING
    state.job_status    = JobStatus.GATED

    steps = [
        GateStepResult(step="compile",     passed=True, duration_ms=1100, output="BUILD SUCCESS"),
        GateStepResult(step="unit_tests",  passed=True, duration_ms=3200, coverage_pct=71.4),
        GateStepResult(step="migrations",  passed=True, duration_ms=800),
        GateStepResult(step="api_smoke",   passed=True, duration_ms=2100),
        GateStepResult(step="semgrep",     passed=True, duration_ms=5400),
        GateStepResult(step="trivy_osv",   passed=True, duration_ms=4100, output="0 critical CVEs"),
        GateStepResult(step="gitleaks",    passed=True, duration_ms=600,  output="No secrets found"),
    ]
    all_pass = all(s.passed for s in steps)
    result = GateResult(
        passed=all_pass,
        steps=steps,
        failing_files=[] if all_pass else ["src/main/java/com/vibeforge/stub/Stub.java"],
    )
    state.gate_result = result

    state = _emit(state, "gate_node", GenerationPhase.GATING, {
        "status":    "passed" if all_pass else "failed",
        "iteration": state.fix_iteration,
        "coverage":  71.4,
        "report_id": result.report_id,
    })
    return state.model_dump()


# ── Node: reviewer_node ────────────────────────────────────────────────────────

def node_reviewer(state: GenerationState) -> dict:
    """
    Reads GateReport facts → writes FixPlan.
    Phase 2: ReviewerAgent (Qwen3-8B) maps errors to modules.
    No vibes-based scoring — only machine-readable gate output.
    """
    state = _emit(state, "reviewer_node", GenerationPhase.REVIEWING, {"status": "started"})
    state.current_phase = GenerationPhase.REVIEWING

    gate = state.gate_result
    if not gate or gate.passed:
        return state.model_dump()

    next_iter = state.fix_iteration + 1
    esc_rec   = next_iter >= state.max_fix_iterations

    if _PHASE2_AVAILABLE and _LLM_CLIENT:
        import asyncio
        from agents.generation.assembler import Assembler, AssemblyResult
        from agents.generation.synthesizer import FileMapOutput as FMO, SynthesizedFile

        # Rebuild AssemblyResult from state
        ar = AssemblyResult(
            assembled_files=dict(state.assembled_files),
            scaffold_files=dict(state.scaffold_files),
        )
        # Rebuild file_ownership from assembly_conflicts info (simplified)
        for path in state.assembled_files:
            ar.file_ownership[path] = "__unknown__"

        spec_dict = getattr(state, "spec_snapshot", {}) or {}
        reviewer = ReviewerAgent(_LLM_CLIENT)
        try:
            rev_out = _run_coro(
                reviewer.review(
                    gate_result=gate,
                    assembly_result=ar,
                    spec_dict=spec_dict,
                    fix_iteration=next_iter,
                    max_iterations=state.max_fix_iterations,
                    job_id=state.job_id,
                )
            )
            files_to_fix = [i.file_path for i in rev_out.fix_instructions]
            errors_by_file = {
                i.file_path: i.errors for i in rev_out.fix_instructions
            }
            esc_rec = rev_out.escalation_recommended or esc_rec
        except Exception as e:
            logger.error("[node_reviewer] Phase 2 reviewer failed: %s — fallback", e)
            files_to_fix = gate.failing_files or []
            errors_by_file = {f: ["Gate failure"] for f in files_to_fix}
    else:
        files_to_fix = gate.failing_files or ["src/main/java/com/vibeforge/stub/Stub.java"]
        errors_by_file = {f: ["Compile/test failure — see gate report"] for f in files_to_fix}

    state.fix_plan = FixPlan(
        files_to_fix=files_to_fix,
        errors_by_file=errors_by_file,
        iteration=next_iter,
        escalation_recommended=esc_rec,
        failure_classification="capability_bound" if esc_rec else "mechanical",
    )
    state = _emit(state, "reviewer_node", GenerationPhase.REVIEWING, {
        "status":         "complete",
        "files_to_fix":   files_to_fix,
        "next_iteration": next_iter,
        "escalate":       esc_rec,
    })
    return state.model_dump()


# ── Node: fixer_node ──────────────────────────────────────────────────────────

def node_fixer(state: GenerationState) -> dict:
    """
    Regenerates ONLY the files in the FixPlan.
    CONTRACT C5: prompt ALWAYS contains the concrete GateReport errors.
    Phase 2: FixerAgent (Qwen2.5-Coder-32B) with real errors in-prompt.
    """
    iteration = state.fix_plan.iteration if state.fix_plan else state.fix_iteration + 1
    gate_errors = dict(state.fix_plan.errors_by_file) if state.fix_plan else {}

    state = _emit(state, "fixer_node", GenerationPhase.FIXING, {
        "status":      "started",
        "iteration":   iteration,
        "max":         state.max_fix_iterations,
        "gate_errors": gate_errors,   # C5: ALWAYS present
    })
    state.current_phase = GenerationPhase.FIXING
    state.job_status    = JobStatus.FIXING
    state.fix_iteration = iteration
    state.fix_count     = iteration

    if _PHASE2_AVAILABLE and _LLM_CLIENT and state.fix_plan:
        import asyncio
        from agents.generation.assembler import Assembler, AssemblyResult
        from agents.generation.reviewer_fixer import ReviewerAgent, FixerAgent
        from agents.generation.reviewer_fixer import FileFixInstruction, ReviewerOutput

        fixer = FixerAgent(_LLM_CLIENT, stack_profile=_STACK_PROFILE)
        ar = AssemblyResult(assembled_files=dict(state.assembled_files))

        # Build ReviewerOutput from fix_plan for fixer
        instructions = [
            FileFixInstruction(
                file_path=fpath,
                module_id="unknown",
                errors=errs,
                fix_guidance="Fix the exact errors listed",
                error_source="compile",
            )
            for fpath, errs in gate_errors.items()
        ]
        rev_out = ReviewerOutput(fix_instructions=instructions)

        try:
            fixed_maps = _run_coro(
                fixer.fix(
                    reviewer_output=rev_out,
                    assembly_result=ar,
                    fix_iteration=iteration,
                    job_id=state.job_id,
                )
            )
            assembler = Assembler()
            from agents.generation.synthesizer import FileMapOutput as FMO
            new_result = assembler.apply_fixes(ar, fixed_maps)
            state.assembled_files = new_result.assembled_files
            logger.info("[node_fixer] Phase 2 fixed %d files", len(fixed_maps))
        except Exception as e:
            logger.error("[node_fixer] Phase 2 fixer failed: %s — stub fallback", e)
            if state.fix_plan:
                for fpath in state.fix_plan.files_to_fix:
                    state.assembled_files[fpath] = (
                        f"// FIX FAILED (iteration {iteration}): {fpath}"
                    )
    else:
        # Phase 0 stub fallback
        if state.fix_plan:
            for fpath in state.fix_plan.files_to_fix:
                state.assembled_files[fpath] = (
                    f"// STUB FIXED (iteration {iteration}): {fpath}\n"
                    "// Gate errors were in this prompt — loop converges on facts."
                )

    state = _emit(state, "fixer_node", GenerationPhase.FIXING,
                  {"status": "complete", "iteration": iteration})
    return state.model_dump()


# ── Node: escalation_gate_node ────────────────────────────────────────────────

def node_escalation_gate(state: GenerationState) -> dict:
    """
    Classifies failure as capability-bound vs mechanical.
    Phase 0 stub — pauses for human review.
    Phase 3: Qwen3-8B classification + budget check + PII scan + commercial call.
    CONTRACT C6: escalated output re-enters the gate (no trust shortcut).
    CONTRACT C18: prompt scanned for PII/secrets before leaving perimeter.
    """
    state = _emit(state, "escalation_gate_node", GenerationPhase.ESCALATING,
                  {"status": "evaluating", "fix_iteration": state.fix_iteration})
    state.current_phase = GenerationPhase.ESCALATING
    state.job_status    = JobStatus.PAUSED_HUMAN   # stub: no commercial call yet

    state = _emit(state, "escalation_gate_node", GenerationPhase.ESCALATING,
                  {"status": "paused_human", "reason": "Phase 0 stub — enable Phase 3 for commercial call"})
    return state.model_dump()


# ── Node: deliver_node ────────────────────────────────────────────────────────

def node_deliver(state: GenerationState) -> dict:
    """
    Packages and delivers gate-passing artifacts.
    CONTRACT C7: cache write happens only here (after full gate pass).
    Phase 0 stub. Phase 2: Gitea push + MinIO bundle + Traefik preview.
    """
    state = _emit(state, "deliver_node", GenerationPhase.DELIVERING,
                  {"status": "started", "file_count": len(state.assembled_files)})
    state.current_phase  = GenerationPhase.DONE
    state.gitea_repo_url = f"https://git.vibeforge.io/{state.tenant_id[:8]}/{state.project_id[:8]}"
    state.minio_bundle_key = f"artifacts/{state.tenant_id}/{state.job_id}/bundle.zip"
    state.preview_url    = f"https://preview-{state.job_id[:8]}.vibeforge.io"
    state.sbom_ref       = f"sbom/{state.job_id}.cdx.json"
    state.job_status     = JobStatus.DELIVERED
    state.completed_at   = datetime.now(timezone.utc).isoformat()

    state = _emit(state, "deliver_node", GenerationPhase.DELIVERING, {
        "status":      "complete",
        "preview_url": state.preview_url,
        "gitea_url":   state.gitea_repo_url,
        "file_count":  len(state.assembled_files),
    })
    return state.model_dump()


def node_failed(state: GenerationState) -> dict:
    state.job_status   = JobStatus.FAILED
    state.completed_at = datetime.now(timezone.utc).isoformat()
    state = _emit(state, "failed_node", GenerationPhase.FAILED,
                  {"status": "failed", "error": state.error_message or ""})
    return state.model_dump()


# ── Routing functions ─────────────────────────────────────────────────────────

def _route_gate(state: GenerationState) -> str:
    gate = state.gate_result
    if not gate:                                        return "failed_node"
    if gate.passed:                                     return "deliver_node"
    if state.fix_iteration < state.max_fix_iterations: return "reviewer_node"
    return "escalation_gate_node"


def _route_escalation(state: GenerationState) -> str:
    if state.escalation_approved:
        return "gate_node"    # CONTRACT C6: re-enter gate after escalation
    return "__end__"


# ── Graph builder ─────────────────────────────────────────────────────────────

def build_generation_graph(
    checkpointer=None,
    llm_client=None,
    stack_profile="python_fastapi",
    redis_client=None,    # Contract C11: per-tenant budget counter
    pg_conn=None,         # Contract C11: llm_calls Postgres table
):
    """
    Build and compile the LangGraph StateGraph.
    Pass a PostgresSaver checkpointer for production (Postgres C2 compliance).
    Falls back to MemorySaver for Phase 0 tests.
    llm_client, stack_profile, redis_client, and pg_conn are set as module-level
    globals so node functions can access them without polluting the serialisable
    GenerationState. redis_client and pg_conn are optional — budget tracking
    silently skips if either is None.
    """
    global _LLM_CLIENT, _STACK_PROFILE, _REDIS_CLIENT, _PG_CONN
    _LLM_CLIENT   = llm_client
    _STACK_PROFILE = stack_profile or "python_fastapi"
    _REDIS_CLIENT = redis_client
    _PG_CONN      = pg_conn

    from langgraph.graph import END, START, StateGraph

    g = StateGraph(GenerationState)

    # Register all nodes (names match what tests assert on: e.node == "plan_node")
    g.add_node("plan_node",            node_plan)
    g.add_node("scaffold_node",        node_scaffold)
    g.add_node("synthesize_node",      node_synthesize)
    g.add_node("assemble_node",        node_assemble)
    g.add_node("gate_node",            node_gate)
    g.add_node("reviewer_node",        node_reviewer)
    g.add_node("fixer_node",           node_fixer)
    g.add_node("escalation_gate_node", node_escalation_gate)
    g.add_node("deliver_node",         node_deliver)
    g.add_node("failed_node",          node_failed)

    # Linear backbone
    g.add_edge(START,               "plan_node")
    g.add_edge("plan_node",         "scaffold_node")
    g.add_edge("scaffold_node",     "synthesize_node")
    g.add_edge("synthesize_node",   "assemble_node")
    g.add_edge("assemble_node",     "gate_node")

    # Gate routing
    g.add_conditional_edges("gate_node", _route_gate, {
        "reviewer_node":        "reviewer_node",
        "deliver_node":         "deliver_node",
        "escalation_gate_node": "escalation_gate_node",
        "failed_node":          "failed_node",
    })

    # Fix loop: reviewer → fixer → back to gate
    g.add_edge("reviewer_node", "fixer_node")
    g.add_edge("fixer_node",    "gate_node")

    # Escalation routing: re-enter gate (C6) or end
    g.add_conditional_edges("escalation_gate_node", _route_escalation, {
        "gate_node": "gate_node",
        "__end__":   END,
    })

    g.add_edge("deliver_node", END)
    g.add_edge("failed_node",  END)

    return g.compile(checkpointer=checkpointer)


# ── Public entry point (called by Arq workers and tests) ─────────────────────

async def run_generation_job(
    job_id: str,
    tenant_id: str,
    spec=None,
    postgres_dsn: Optional[str] = None,
    **kwargs,                              # absorbs llm_client, conn, redis_client
) -> GenerationState:
    """
    Async entry point.  Tests call:
        await run_generation_job(job_id=..., tenant_id=..., spec=<ApplicationSpec>)
    Production Arq workers call the same signature.

    Returns a GenerationState whose .events list contains JobEvent wrappers
    so tests can do: {e.node for e in state.events}, {e.phase.value ...}
    """
    from langgraph.checkpoint.memory import MemorySaver

    llm_client = kwargs.get("llm_client")
    stack_profile = kwargs.get("stack_profile", "python_fastapi")

    if postgres_dsn:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            async with AsyncPostgresSaver.from_conn_string(postgres_dsn) as cp:
                await cp.setup()
                return await _invoke(job_id, tenant_id, spec, cp, llm_client, stack_profile)
        except ImportError:
            logger.warning("psycopg not installed — falling back to MemorySaver")

    return await _invoke(job_id, tenant_id, spec, MemorySaver(), llm_client, stack_profile)


async def _invoke(
    job_id: str,
    tenant_id: str,
    spec,
    checkpointer,
    llm_client=None,
    stack_profile: str = "python_fastapi",
) -> GenerationState:
    compiled = build_generation_graph(
        checkpointer=checkpointer,
        llm_client=llm_client,
        stack_profile=stack_profile,
    )
    config   = {"configurable": {"thread_id": job_id}}

    entity_names: list[str] = []
    project_id = ""
    spec_id    = ""
    spec_ver   = 1
    can_hash   = ""

    if spec is not None:
        entity_names = [e.name for e in spec.domain_model.entities]
        project_id   = str(getattr(spec, "project_id", ""))
        spec_id      = str(getattr(spec, "spec_id", ""))
        spec_ver     = getattr(spec, "spec_version", 1)
        can_hash     = getattr(spec, "canonical_hash", "") or ""

    initial = GenerationState(
        job_id=job_id, tenant_id=tenant_id, project_id=project_id,
        spec_id=spec_id, spec_version=spec_ver, canonical_hash=can_hash,
        spec_entity_names=entity_names,
    )

    result = await compiled.ainvoke(initial.model_dump(), config=config)
    final  = GenerationState.model_validate(result)
    return _wrap_events(final)
