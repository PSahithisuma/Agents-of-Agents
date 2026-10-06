"""
VibeForge — Synthesizer Agent (Phase 2)
=========================================
Per-module code generation using Qwen2.5-Coder-32B.

Called once per module in build order (sequential — Phase 2 decision).
Output is a FileMap only — a dict of {filename: code_string}.
The Synthesizer never writes to disk. The Assembler merges FileMaps.

What goes into every synthesis prompt (Contract C5 source):
  1. spec_slice  — only the spec sections this module reads
  2. interfaces  — method signatures from scaffold stubs (what this module must implement)
  3. conventions — naming, layering, annotation patterns from the stack profile
  4. exemplar    — one gold example from the stack profile's exemplars/ folder
  5. output_schema — the exact FileMap JSON schema the model must return

What NEVER goes into the prompt:
  - The full spec (too long, most is irrelevant to this module)
  - Code from other modules (the Synthesizer sees only its own interfaces)
  - Free-form instructions not from the spec (Contract C16)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import BaseModel, Field

from agents.harness.structured_output import StructuredOutputHarness, HarnessError
from agents.generation.planner import PlannedModule

logger = logging.getLogger(__name__)


# ── Output schema — what the model must return ─────────────────────────────────

class SynthesizedFile(BaseModel):
    """One generated source file."""
    filename: str = Field(
        ...,
        description="Full relative path from project root. "
                    "e.g. 'src/main/java/com/app/product/ProductEntity.java'"
    )
    content: str = Field(
        ...,
        description="Complete file content — compilable, no placeholders, no TODOs"
    )
    language: str = Field(
        default="java",
        description="java | python | typescript | csharp | go | sql"
    )


class FileMapOutput(BaseModel):
    """
    The Synthesizer's complete output for one module.
    This is a FILE MAP — not merged yet. The Assembler merges all FileMaps.
    """
    module_id: str
    module_name: str
    files: list[SynthesizedFile] = Field(
        default_factory=list,
        description="All files generated for this module. Typically 2-4 files."
    )
    synthesis_notes: str = Field(
        default="",
        description="Any decisions made during synthesis worth logging"
    )


# ── Stack conventions per profile ─────────────────────────────────────────────
# These are read from the stack profile's conventions.md in production.
# In Phase 2, we inline the most critical ones here as a bootstrap.

STACK_CONVENTIONS: dict[str, str] = {
    "java_spring": """\
Stack: Java 21 + Spring Boot 3 + JPA/Hibernate + PostgreSQL

Naming conventions:
- Entities: @Entity, @Table(name="snake_case"), suffix Entity
- Repositories: extend JpaRepository<Entity, UUID>, suffix Repository
- Services: @Service, @Transactional, suffix Service
- Controllers: @RestController, @RequestMapping("/api/v1/..."), suffix Controller
- DTOs: suffix Request (input) or Response (output)

Field rules:
- Primary key: @Id @GeneratedValue(strategy=GenerationType.UUID) UUID id
- Timestamps: @CreatedDate Instant createdAt; @LastModifiedDate Instant updatedAt
- Money: BigDecimal (never double/float)
- Soft delete: Boolean deletedAt (Instant null = not deleted)

Validation: Jakarta Bean Validation (@NotNull, @NotBlank, @Size, @Min, @Max)
Security: @PreAuthorize on service methods, not controllers
PII fields: @Convert(converter = EncryptedStringConverter.class)
""",

    "python_fastapi": """\
Stack: Python 3.12 + FastAPI + SQLAlchemy 2.0 + PostgreSQL + Pydantic v2

Naming conventions:
- Models (DB): SQLAlchemy DeclarativeBase, snake_case table names, suffix Model
- Schemas (API): Pydantic BaseModel, suffix Schema (input) or Response (output)
- Repositories: class suffix Repository, takes Session as constructor arg
- Services: class suffix Service, takes Repository as constructor arg
- Routers: APIRouter with prefix="/api/v1/...", suffix _router

Field rules:
- Primary key: id: uuid.UUID = Field(default_factory=uuid.uuid4)
- Timestamps: created_at: datetime = Field(default_factory=datetime.utcnow)
- Money: Decimal (from decimal import Decimal)
- Soft delete: deleted_at: Optional[datetime] = None

Validation: Pydantic field validators, @field_validator
Security: Depends(get_current_user) on protected routes
""",

    "dotnet": """\
Stack: C# 12 + ASP.NET Core 8 + Entity Framework Core 8 + PostgreSQL

Naming conventions:
- Entities: PascalCase, suffix Entity or just the domain name, no suffix
- DbContext: AppDbContext with DbSet<T> properties
- Repositories: interface IEntityRepository, class EntityRepository
- Services: interface IEntityService, class EntityService
- Controllers: suffix Controller, [ApiController], [Route("api/v1/...")]

Field rules:
- Primary key: Guid Id { get; private set; } = Guid.NewGuid()
- Timestamps: DateTime CreatedAt { get; private set; } = DateTime.UtcNow
- Money: decimal (not double)
- Soft delete: DateTime? DeletedAt { get; private set; }

Validation: Data Annotations or FluentValidation
Security: [Authorize] attribute + Policy-based authorization
""",
}

# ── Stack exemplars (gold few-shot examples) ───────────────────────────────────
# One entity + repository example per stack.
# In production these come from the stack profile's exemplars/ folder.

ENTITY_EXEMPLAR: dict[str, str] = {
    "java_spring": """\
// EXEMPLAR: CustomerEntity.java
@Entity
@Table(name = "customers")
@EntityListeners(AuditingEntityListener.class)
public class CustomerEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.UUID)
    private UUID id;

    @NotBlank
    @Size(max = 200)
    @Convert(converter = EncryptedStringConverter.class)  // PII
    @Column(name = "full_name", nullable = false)
    private String fullName;

    @NotBlank
    @Email
    @Column(name = "email", nullable = false, unique = true)
    private String email;

    @CreatedDate
    @Column(name = "created_at", updatable = false)
    private Instant createdAt;

    @LastModifiedDate
    @Column(name = "updated_at")
    private Instant updatedAt;

    @Column(name = "deleted_at")
    private Instant deletedAt;

    // Getters and setters omitted for brevity in exemplar
}
""",
    "python_fastapi": """\
# EXEMPLAR: customer_model.py
class CustomerModel(Base):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    full_name: Mapped[str] = mapped_column(
        EncryptedString(200), nullable=False  # PII
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
""",
    "dotnet": """\
// EXEMPLAR: Customer.cs
public class Customer
{
    public Guid Id { get; private set; } = Guid.NewGuid();

    [MaxLength(200)]
    public string FullName { get; private set; } = string.Empty;  // PII - encrypted

    [MaxLength(254)]
    [EmailAddress]
    public string Email { get; private set; } = string.Empty;

    public DateTime CreatedAt { get; private set; } = DateTime.UtcNow;
    public DateTime UpdatedAt { get; private set; } = DateTime.UtcNow;
    public DateTime? DeletedAt { get; private set; }

    private Customer() { }  // EF Core
}
""",
}

# ── System prompt ──────────────────────────────────────────────────────────────

_SYNTHESIZER_SYSTEM = """\
You are the VibeForge Synthesizer. You generate production-quality source code
for one module of an application.

Critical rules:
1. Output ONLY valid JSON matching the FileMapOutput schema. No preamble.
2. Every file must be COMPLETE and COMPILABLE. No TODOs, no placeholders,
   no "// implement later" comments.
3. Follow the stack conventions EXACTLY — naming, annotations, patterns.
4. The exemplar shows the exact style expected. Match it precisely.
5. Generate 2-4 files per module (entity/model, repository, test at minimum).
6. Test files must test the REAL behavior, not just "assert true".
7. Never invent fields not in the spec_slice. Never omit required fields.
8. PII fields must have the encryption annotation shown in conventions.
9. Money fields must use the correct type (BigDecimal/Decimal/decimal).
10. All database fields map to the correct column types.
"""


# ── Synthesizer ────────────────────────────────────────────────────────────────

class SynthesizerAgent:
    """
    Qwen2.5-Coder-32B synthesizer: one module → FileMap.

    Called sequentially for each module in build order.
    Contract C5: gate errors always in the Fixer prompt (not here —
    the Fixer re-calls this agent with errors injected).
    """

    MODEL = "coder-model"   # → Qwen2.5-Coder-32B via LiteLLM (Phase 2 GPU server)
                             # → Qwen2.5-Coder-14B via Ollama (Phase 1 laptop fallback)

    def __init__(self, llm_client, stack_profile: str = "java_spring"):
        self._harness = StructuredOutputHarness(llm_client)
        self._stack = stack_profile

    async def synthesize_module(
        self,
        module: PlannedModule,
        spec_dict: dict[str, Any],
        previously_synthesized: Optional[dict[str, str]] = None,
        gate_errors: Optional[dict[str, list[str]]] = None,
        job_id: str = "",
    ) -> FileMapOutput:
        """
        Synthesize one module.

        Args:
            module:                 The PlannedModule to synthesize
            spec_dict:              Full frozen spec (we extract the slice here)
            previously_synthesized: Interface stubs from earlier modules (for type references)
            gate_errors:            If this is a fix attempt, the real compiler/test errors
            job_id:                 For correlation logging

        Returns:
            FileMapOutput with the generated files
        """
        spec_slice = self._extract_spec_slice(module, spec_dict)
        interfaces = self._extract_interfaces(module, previously_synthesized or {})
        prompt = self._build_prompt(module, spec_slice, interfaces, gate_errors)

        try:
            output, meta = await self._harness.call(
                output_schema=FileMapOutput,
                user_message=prompt,
                system_prompt=_SYNTHESIZER_SYSTEM,
                model=self.MODEL,
                context_tag=f"synthesizer:{module.module_id}:{job_id or 'unknown'}",
                max_attempts=3,
            )
            output.module_id = module.module_id
            output.module_name = module.name

            logger.info(
                "[Synthesizer] job=%s module=%s files=%d repaired=%s",
                job_id, module.name, len(output.files), meta.repaired,
            )
            return output

        except HarnessError as e:
            logger.error(
                "[Synthesizer] Module %s failed all harness attempts: %s",
                module.name, e,
            )
            # Return stub files — gate will catch them and fixer will retry
            return self._stub_fallback(module)

    # ── Prompt builder ─────────────────────────────────────────────────────────

    def _build_prompt(
        self,
        module: PlannedModule,
        spec_slice: dict[str, Any],
        interfaces: str,
        gate_errors: Optional[dict[str, list[str]]],
    ) -> str:
        parts = []

        # Stack conventions (from profile)
        conventions = STACK_CONVENTIONS.get(self._stack, STACK_CONVENTIONS["java_spring"])
        parts.append(f"## Stack conventions\n{conventions}")

        # Exemplar — gold few-shot
        exemplar = ENTITY_EXEMPLAR.get(self._stack, "")
        if exemplar and module.module_type == "entity":
            parts.append(f"## Style exemplar (match this exactly)\n{exemplar}")

        # Spec slice — only what this module needs
        parts.append(
            f"## Spec slice for module '{module.name}'\n"
            f"{json.dumps(spec_slice, indent=2)}"
        )

        # Interface stubs from previously synthesized modules
        if interfaces:
            parts.append(f"## Interface stubs from earlier modules\n{interfaces}")

        # Gate errors — ALWAYS present when this is a fix attempt (Contract C5)
        if gate_errors:
            parts.append("## GATE ERRORS — fix these exactly (do not invent fixes)")
            for filepath, errors in gate_errors.items():
                parts.append(f"\nFile: {filepath}")
                for err in errors:
                    parts.append(f"  ERROR: {err}")

        # Task
        parts.append(
            f"\n## Task\n"
            f"Generate the '{module.name}' module (type: {module.module_type}).\n"
            f"Module ID: {module.module_id}\n"
            f"Dependencies: {module.dependencies or 'none'}\n\n"
            f"Return ONLY a FileMapOutput JSON with all files for this module.\n"
            f"module_id must be '{module.module_id}'.\n"
            f"module_name must be '{module.name}'.\n"
        )

        return "\n\n".join(parts)

    # ── Spec slice extractor ───────────────────────────────────────────────────

    def _extract_spec_slice(
        self,
        module: PlannedModule,
        spec: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Extract only the spec sections this module needs.
        Prevents the prompt from including irrelevant spec sections.
        """
        slice_dict: dict[str, Any] = {}

        # Always include stack info
        slice_dict["stack"] = spec.get("stack", {})
        slice_dict["vertical"] = spec.get("vertical", "")

        # Add sections from spec_paths declared in the module plan
        for path in module.spec_paths:
            parts = path.split(".")
            src = spec
            dst = slice_dict
            for i, part in enumerate(parts):
                if i == len(parts) - 1:
                    # Leaf — handle both dict and list[dict] with name matching
                    src_val = src.get(part) if isinstance(src, dict) else None
                    if src_val is not None:
                        if isinstance(src_val, list):
                            # Filter by entity name if the path specifies one
                            # e.g. spec_paths=["domain_model.entities.Product"]
                            # "Product" is a filter, not a real dict key
                            entity_filter = parts[i] if len(parts) > 2 else None
                            if entity_filter and entity_filter[0].isupper():
                                src_val = [
                                    e for e in src_val
                                    if e.get("name") == entity_filter
                                ]
                        dst[part] = src_val
                else:
                    src = src.get(part, {}) if isinstance(src, dict) else {}
                    dst = dst.setdefault(part, {})

        # Always add security roles for auth annotations
        slice_dict["security_model"] = {
            "roles": spec.get("security_model", {}).get("roles", [])
        }

        # Add compliance frameworks for annotation decisions
        slice_dict["compliance"] = {
            "frameworks": spec.get("compliance_model", {}).get("frameworks", [])
        }

        return slice_dict

    # ── Interface extractor ────────────────────────────────────────────────────

    @staticmethod
    def _extract_interfaces(
        module: PlannedModule,
        previously_synthesized: dict[str, str],
    ) -> str:
        """
        Extract method signatures from previously synthesized modules
        that this module depends on.

        The full code is too large for the prompt — we extract just the
        public interface (class name, method signatures, return types).
        This is enough for the Synthesizer to use the dependency correctly.
        """
        if not module.dependencies or not previously_synthesized:
            return ""

        interfaces: list[str] = []
        for dep_id in module.dependencies:
            for filename, code in previously_synthesized.items():
                if dep_id.lower().replace("_", "") in filename.lower().replace("_", ""):
                    # Extract just the class declaration and method signatures
                    lines = code.split("\n")
                    sig_lines = []
                    in_class = False
                    brace_depth = 0

                    for line in lines:
                        stripped = line.strip()
                        if "class " in stripped or "interface " in stripped:
                            in_class = True
                        if in_class:
                            brace_depth += stripped.count("{") - stripped.count("}")
                            # Include class declaration and public method signatures
                            if (
                                "public " in stripped
                                or "class " in stripped
                                or "interface " in stripped
                                or stripped.startswith("@")
                            ) and "{" not in stripped or "class" in stripped:
                                sig_lines.append(line)
                            if brace_depth == 0 and in_class and stripped == "}":
                                break

                    if sig_lines:
                        interfaces.append(
                            f"// Interface from {filename.split('/')[-1]}:\n"
                            + "\n".join(sig_lines[:30])  # cap at 30 lines
                        )
                    break

        return "\n\n".join(interfaces)

    # ── Stub fallback ──────────────────────────────────────────────────────────

    def _stub_fallback(self, module: PlannedModule) -> FileMapOutput:
        """
        If synthesis fails all 3 harness attempts, return a stub file map.
        The gate will catch this (compile failure) and the Fixer will retry
        with the real compiler error in-prompt.
        """
        lang_ext = {
            "java_spring": "java",
            "python_fastapi": "py",
            "dotnet": "cs",
        }.get(self._stack, "java")

        pkg_path = {
            "java_spring": f"src/main/java/com/app/{module.name.lower()}/{module.name}",
            "python_fastapi": f"app/{module.name.lower()}/{module.name.lower()}",
            "dotnet": f"src/{module.name}",
        }.get(self._stack, f"src/{module.name}")

        return FileMapOutput(
            module_id=module.module_id,
            module_name=module.name,
            files=[
                SynthesizedFile(
                    filename=f"{pkg_path}.{lang_ext}",
                    content=f"// SYNTHESIS FAILED: {module.name}\n// Gate will catch this and Fixer will retry with real errors",
                    language=lang_ext,
                )
            ],
            synthesis_notes="Fallback stub — synthesis failed all harness attempts",
        )
