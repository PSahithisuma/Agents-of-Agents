"""
spec_ir.py — VibeForge Application Spec IR (Intermediate Representation)

This is the machine-readable contract between:
  - The option-graph UI (inputs)
  - The database (storage in application_specs.spec_data JSONB)
  - Every generation agent (reads this, writes code)

Design rules:
  P1: Spec is the product. Selections are inputs. Code is a rendering.
  P3: canonical_hash enables semantic caching — same selections → same hash
      → no redundant generation.

The hash is computed over the normalized JSON of the spec (excluding the hash
field itself), so it is deterministic and stable across Python restarts.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from pydantic import BaseModel, computed_field, field_validator


# =============================================================================
# Sub-models — one per logical domain section
# =============================================================================

class EntityField(BaseModel):
    name: str
    type: str                           # "string" | "int" | "uuid" | "bool" | "datetime" | "enum"
    required: bool = True
    unique: bool = False
    enum_values: list[str] = []
    description: Optional[str] = None


class Entity(BaseModel):
    name: str                           # PascalCase e.g. "User", "Order"
    table: Optional[str] = None         # physical table name (pack-defined)
    fields: list[EntityField] = []
    soft_delete: bool = False
    timestamps: bool = True             # created_at / updated_at
    description: Optional[str] = None


class Relationship(BaseModel):
    from_entity: str
    to_entity: str
    cardinality: str                    # "one_to_one" | "one_to_many" | "many_to_many"
    cascade_delete: bool = False


class DomainModel(BaseModel):
    entities: list[Entity] = []
    relationships: list[Relationship] = []
    extensions: dict[str, Any] = {}    # escape hatch for domain-specific metadata


# ---------------------------------------------------------------------------

class Endpoint(BaseModel):
    method: str                         # GET | POST | PUT | PATCH | DELETE
    path: str                           # e.g. "/api/v1/users/{id}"
    description: str = ""
    auth_required: bool = True
    roles: list[str] = []               # [] = any authenticated user


class ApiModel(BaseModel):
    style: str = "rest"                 # "rest" | "graphql" | "grpc"
    version_prefix: str = "/api/v1"
    auth_strategy: str = "jwt"          # "jwt" | "session" | "oauth2" | "api_key"
    rate_limiting: bool = True
    endpoints: list[Endpoint] = []
    extensions: dict[str, Any] = {}


# ---------------------------------------------------------------------------

class UiPage(BaseModel):
    name: str
    route: str
    auth_required: bool = True
    components: list[str] = []


class UiModel(BaseModel):
    framework: str = "react"            # "react" | "nextjs" | "vue" | "svelte"
    styling: str = "tailwind"           # "tailwind" | "css_modules" | "styled_components"
    state_management: str = "zustand"   # "zustand" | "redux" | "context" | "none"
    pages: list[UiPage] = []
    extensions: dict[str, Any] = {}


# ---------------------------------------------------------------------------

class WorkflowStep(BaseModel):
    name: str
    type: str                           # "async_job" | "webhook" | "cron" | "notification"
    description: str = ""
    timeout_seconds: int = 300


class WorkflowSpec(BaseModel):
    async_jobs: bool = True
    notifications: bool = False
    steps: list[WorkflowStep] = []
    extensions: dict[str, Any] = {}


# ---------------------------------------------------------------------------

class Integration(BaseModel):
    name: str                           # e.g. "stripe", "sendgrid", "s3"
    type: str                           # "payment" | "email" | "storage" | "analytics"
    required: bool = True


class IntegrationSpec(BaseModel):
    integrations: list[Integration] = []
    webhooks_enabled: bool = False
    extensions: dict[str, Any] = {}


# ---------------------------------------------------------------------------

class SecuritySpec(BaseModel):
    auth_strategy: str = "jwt"          # mirrors ApiModel.auth_strategy (source of truth here)
    rbac_enabled: bool = True
    roles: list[str] = ["admin", "user"]
    mfa_required: bool = False
    data_classification: str = "internal"  # "public" | "internal" | "confidential" | "restricted"
    pii_fields: list[str] = []          # field names that contain PII → masked in logs
    extensions: dict[str, Any] = {}


# ---------------------------------------------------------------------------

class NonFunctionalReqs(BaseModel):
    db_tier: str = "postgres"           # "postgres" | "mysql" | "sqlite" | "mongodb"
    deploy_target: str = "docker"       # "docker" | "k8s" | "serverless" | "bare_metal"
    expected_rps: int = 100
    sla_uptime_pct: float = 99.9
    cache_strategy: str = "redis"       # "redis" | "memcached" | "none"
    search_enabled: bool = False
    extensions: dict[str, Any] = {}


# ---------------------------------------------------------------------------

class AcceptanceCriterion(BaseModel):
    id: str                             # e.g. "AC-001"
    description: str
    verifiable: bool = True             # can be checked by automated gate
    test_hint: str = ""                 # what the QA gate should test


# =============================================================================
# Top-level AppSpec
# =============================================================================

class AppSpec(BaseModel):
    """
    The frozen, versioned, machine-readable specification of an application.

    Once frozen (frozen_at IS NOT NULL in application_specs table), this model
    is immutable. Any change produces a new version with a new canonical_hash.

    canonical_hash is a SHA-256 of the model's normalized JSON (sorted keys,
    no whitespace). It's a computed field — never stored directly in this model,
    but written into application_specs.canonical_hash by the API on freeze.
    """
    schema_version: str = "1.0"
    app_name: str = ""
    description: str = ""
    vertical: str = "generic"   # banking | ecommerce | logistics | custom | generic

    domain_model:    DomainModel       = DomainModel()
    api_model:       ApiModel          = ApiModel()
    ui_model:        UiModel           = UiModel()
    workflow:        WorkflowSpec      = WorkflowSpec()
    integration:     IntegrationSpec   = IntegrationSpec()
    security:        SecuritySpec      = SecuritySpec()
    nfr:             NonFunctionalReqs = NonFunctionalReqs()
    acceptance:      list[AcceptanceCriterion] = []

    @computed_field  # type: ignore[misc]
    @property
    def canonical_hash(self) -> str:
        """
        Deterministic SHA-256 of the spec content (excluding the hash itself).
        Same option selections → same hash across all Python runtimes and restarts.
        Used for P3 semantic caching: if hash already exists for this tenant,
        return the existing job_id immediately without re-generating.
        """
        payload = self.model_dump(exclude={"canonical_hash"})
        # Sort keys for determinism; separators strip whitespace
        normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(normalized.encode()).hexdigest()

    def freeze(self) -> dict:
        """
        Returns the spec as a plain dict suitable for storing in
        application_specs.spec_data (JSONB). Includes the canonical_hash.

        Also injects validator-compatible aliases so the CompletenessValidator
        (which checks security_model.roles, acceptance_criteria, vertical, stack.backend)
        can read the spec without a schema migration.
        """
        d = self.model_dump()
        # Alias: security → security_model (CompletenessValidator checks security_model.roles)
        d["security_model"] = d.get("security", {})
        # Alias: acceptance → acceptance_criteria (CompletenessValidator checks acceptance_criteria[])
        d["acceptance_criteria"] = d.get("acceptance", [])
        # Add vertical (default "generic"; domain wizard will override in Phase 2+)
        d.setdefault("vertical", "generic")
        # Add stack.backend derived from NFR (CompletenessValidator checks stack.backend)
        nfr = d.get("nfr", {})
        d["stack"] = {
            "backend": nfr.get("db_tier", "postgres"),
            "deploy": nfr.get("deploy_target", "docker"),
        }
        return d
