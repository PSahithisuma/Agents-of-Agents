"""
domain_packs/v0/options.py — Option Graph for VibeForge Domain Pack v0

The option graph is the structured set of choices a user makes in the UI.
Each key maps to a typed OptionDef that constrains valid values and provides
display metadata. The compiler (compiler.py) maps these selections deterministically
to an AppSpec.

P1: "Spec is the product; selection is the input."
This is the materialisation of that principle — the option graph IS the product UI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class OptionDef:
    """Definition of a single option in the graph."""
    key: str
    label: str
    choices: list[str]
    default: str
    description: str = ""
    # Options that become visible/required only when a parent has a certain value
    depends_on: dict[str, str] = field(default_factory=dict)  # {parent_key: parent_value}


# =============================================================================
# The 8 option keys for Domain Pack v0
# =============================================================================
OPTION_GRAPH: dict[str, OptionDef] = {

    # ── 1. Authentication strategy ─────────────────────────────────────────
    "auth_strategy": OptionDef(
        key="auth_strategy",
        label="Authentication Strategy",
        choices=["jwt", "session", "oauth2", "api_key"],
        default="jwt",
        description=(
            "How users authenticate with the generated API. "
            "JWT is stateless and works well with microservices. "
            "Session suits traditional server-rendered apps. "
            "OAuth2 enables third-party login (Google, GitHub). "
            "API Key is for machine-to-machine integrations."
        ),
    ),

    # ── 2. Database tier ───────────────────────────────────────────────────
    "db_tier": OptionDef(
        key="db_tier",
        label="Database",
        choices=["postgres", "mysql", "sqlite", "mongodb"],
        default="postgres",
        description=(
            "The primary data store for the generated application. "
            "Postgres is recommended for production workloads. "
            "SQLite is zero-config for prototypes."
        ),
    ),

    # ── 3. UI framework ────────────────────────────────────────────────────
    "ui_framework": OptionDef(
        key="ui_framework",
        label="Frontend Framework",
        choices=["react", "nextjs", "vue", "svelte", "none"],
        default="react",
        description=(
            "Frontend technology for the generated app. "
            "Choose 'none' for API-only (no UI generated)."
        ),
    ),

    # ── 4. API style ───────────────────────────────────────────────────────
    "api_style": OptionDef(
        key="api_style",
        label="API Style",
        choices=["rest", "graphql", "grpc"],
        default="rest",
        description=(
            "The API contract style. REST for broad compatibility. "
            "GraphQL for flexible querying. gRPC for high-performance services."
        ),
    ),

    # ── 5. Deployment target ───────────────────────────────────────────────
    "deploy_target": OptionDef(
        key="deploy_target",
        label="Deployment Target",
        choices=["docker", "k8s", "serverless", "bare_metal"],
        default="docker",
        description=(
            "Infrastructure target. Docker Compose for local/simple prod. "
            "Kubernetes for orchestrated scale. Serverless for event-driven workloads."
        ),
    ),

    # ── 6. Async jobs / background processing ─────────────────────────────
    "async_jobs": OptionDef(
        key="async_jobs",
        label="Background Jobs",
        choices=["yes", "no"],
        default="yes",
        description=(
            "Whether the app needs background job processing "
            "(e.g. email sending, report generation, data pipelines)."
        ),
    ),

    # ── 7. Compliance tier ─────────────────────────────────────────────────
    "compliance_tier": OptionDef(
        key="compliance_tier",
        label="Compliance Level",
        choices=["none", "gdpr", "hipaa", "pci"],
        default="none",
        description=(
            "Compliance requirements affect PII handling, audit logging, "
            "data retention, and encryption requirements in the generated code."
        ),
    ),

    # ── 8. Search capability ───────────────────────────────────────────────
    "search_enabled": OptionDef(
        key="search_enabled",
        label="Full-Text Search",
        choices=["yes", "no"],
        default="no",
        description=(
            "Enable full-text search in the generated app "
            "(adds Elasticsearch or Postgres tsvector depending on db_tier)."
        ),
    ),
}


def validate_selections(selections: dict[str, str]) -> dict[str, str]:
    """
    Validates a dict of user selections against OPTION_GRAPH.
    Fills in defaults for any missing keys.
    Raises ValueError if any value is not in the allowed choices.
    Returns a complete, validated selection dict.
    """
    result: dict[str, str] = {}
    errors: list[str] = []

    for key, opt in OPTION_GRAPH.items():
        value = selections.get(key, opt.default)
        if value not in opt.choices:
            errors.append(
                f"'{key}': '{value}' is not valid. "
                f"Allowed: {opt.choices}"
            )
        result[key] = value

    if errors:
        raise ValueError(f"Invalid option selections:\n" + "\n".join(f"  • {e}" for e in errors))

    return result
