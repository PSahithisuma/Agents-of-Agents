"""
VibeForge — Option-Graph Engine
================================
YAML-driven deterministic spec patch evaluator. Zero LLM.
Every checkbox click → validated spec_delta with provenance.

Public API (what tests expect):
    engine = OptionGraphEngine.from_pack_dir(pack_dir)
    context = EligibilityContext(selected_options={"add_reviews"})
    delta   = engine.evaluate_selection("featured_reviews", context, spec)
    status  = engine.get_section_status("catalog", context)
    impact  = engine.get_live_impact(context, spec)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── YAML schema models ────────────────────────────────────────────────────────

class EligibilityRule(BaseModel):
    rule_type: str = "none"          # none | requires_option | conflicts_with | requires_vertical
    option_id: Optional[str] = None  # for requires_option / conflicts_with
    vertical: Optional[str] = None   # for requires_vertical

class SpecBinding(BaseModel):
    json_path: str                   # e.g. "domain_model.entities"
    operation: str = "append_if_missing"  # set | append_if_missing | remove
    value: Any = None

class OptionDefinition(BaseModel):
    option_id: str
    label: str
    description: str = ""
    eligibility_rules: list[EligibilityRule] = Field(default_factory=list)
    spec_bindings: list[SpecBinding] = Field(default_factory=list)

class SectionYAML(BaseModel):
    section_id: str
    title: str = ""      # test fixtures use 'title'
    label: str = ""      # production packs use 'label'
    prerequisites: list[str] = Field(default_factory=list)
    options: list[OptionDefinition] = Field(default_factory=list)

    @property
    def display_title(self) -> str:
        return self.title or self.label

class OptionGraph(BaseModel):
    sections: list[SectionYAML] = Field(default_factory=list)


# ── Eligibility context ───────────────────────────────────────────────────────

@dataclass
class EligibilityContext:
    """Holds the set of currently selected option IDs and vertical/BM context."""
    selected_options: set[str] = field(default_factory=set)
    vertical: Optional[str] = None
    business_models: list[str] = field(default_factory=list)


# ── Delta returned by evaluate_selection ─────────────────────────────────────

@dataclass
class OptionDelta:
    """
    What the engine returns per checkbox click.
    Caller passes this to spec.apply_delta() — never applied automatically.
    Includes impact numbers for the live impact panel.
    """
    option_id: str
    patch: dict[str, Any]                  # ready to merge into spec dict
    provenance_entries: list[Any]          # FieldProvenance records
    new_entity_count: int = 0
    new_endpoint_count: int = 0
    amendment_id: str = ""
    impact_summary: str = ""

    # Make it compatible with ApplicationSpec.apply_delta()
    def to_spec_delta(self):
        """Convert to the SpecDelta format expected by ApplicationSpec."""
        from core.spec_ir import SpecDelta, ProvenanceEntry
        entries = []
        for p in self.provenance_entries:
            if isinstance(p, dict):
                entries.append(ProvenanceEntry(
                    json_path=p.get("json_path", ""),
                    source_type="option_selection",
                    source_id=self.option_id,
                    value_snapshot=str(p.get("value", ""))[:200],
                ))
            else:
                entries.append(p)
        return SpecDelta(
            amendment_id=self.amendment_id or f"opt_{self.option_id}",
            patch=self.patch,
            provenance_entries=entries,
            impact_summary=self.impact_summary,
            new_entity_count=self.new_entity_count,
            new_endpoint_count=self.new_endpoint_count,
        )


# ── JSON patch helpers ────────────────────────────────────────────────────────

def _get_nested(d: dict, dotpath: str) -> Any:
    """Navigate a.b.c → d['a']['b']['c'], returning None if missing."""
    parts = dotpath.split(".")
    cur = d
    for p in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _set_nested(d: dict, dotpath: str, value: Any) -> None:
    """Set a.b.c = value, creating intermediate dicts as needed."""
    parts = dotpath.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _apply_binding(patch: dict, binding: SpecBinding) -> tuple[int, int]:
    """
    Apply one spec binding to the patch dict.
    Returns (new_entity_count, new_endpoint_count) contributed by this binding.
    """
    new_entities = 0
    new_endpoints = 0

    if binding.operation == "set":
        _set_nested(patch, binding.json_path, binding.value)

    elif binding.operation == "append_if_missing":
        # Ensure the list exists in the patch
        existing = _get_nested(patch, binding.json_path)
        if existing is None:
            _set_nested(patch, binding.json_path, [])
            existing = _get_nested(patch, binding.json_path)

        # Check if value already present (by 'name' field for entities/endpoints)
        value = binding.value
        if isinstance(value, dict) and isinstance(existing, list):
            name_key = "name" if "name" in value else "path" if "path" in value else None
            already_present = False
            if name_key:
                already_present = any(
                    isinstance(item, dict) and item.get(name_key) == value.get(name_key)
                    for item in existing
                )
            if not already_present:
                existing.append(value)
                # Count impact
                if "entities" in binding.json_path:
                    new_entities += 1
                if "endpoints" in binding.json_path:
                    new_endpoints += 1

    elif binding.operation == "remove":
        existing = _get_nested(patch, binding.json_path)
        if isinstance(existing, list) and binding.value in existing:
            existing.remove(binding.value)

    return new_entities, new_endpoints


# ── Engine ────────────────────────────────────────────────────────────────────

class OptionGraphEngine:
    """
    Loads a domain pack's option_graphs/ YAML files and evaluates
    user checkbox selections into deterministic SpecDeltas.
    """

    def __init__(self, graph: OptionGraph):
        self.graph = graph
        # Flat lookup: option_id → OptionDefinition
        self._options: dict[str, OptionDefinition] = {
            opt.option_id: opt
            for section in graph.sections
            for opt in section.options
        }
        # section_id → SectionYAML
        self._sections: dict[str, SectionYAML] = {
            s.section_id: s for s in graph.sections
        }

    @classmethod
    def from_pack_dir(cls, pack_dir: str | Path) -> "OptionGraphEngine":
        """
        Load all YAML files from <pack_dir>/option_graphs/.
        Files are sorted alphabetically so load order is deterministic.
        Falls back to loading YAML files directly from pack_dir if
        option_graphs/ subdirectory doesn't exist (for test fixtures).
        """
        pack_path = Path(pack_dir)
        og_dir = pack_path / "option_graphs"

        if not og_dir.exists():
            # Allow tests to pass pack_dir that IS the option_graphs dir
            og_dir = pack_path

        yaml_files = sorted(og_dir.glob("*.yaml")) + sorted(og_dir.glob("*.yml"))
        if not yaml_files:
            raise FileNotFoundError(f"No YAML files found in {og_dir}")

        all_sections: list[SectionYAML] = []
        for yf in yaml_files:
            with open(yf) as f:
                data = yaml.safe_load(f)
            if data and "section_id" in data:
                all_sections.append(SectionYAML(**data))
            elif data and "sections" in data:
                # Multi-section file
                for s in data["sections"]:
                    all_sections.append(SectionYAML(**s))

        graph = OptionGraph(sections=all_sections)
        logger.info("Loaded option graph: %d sections, %d options",
                    len(graph.sections),
                    sum(len(s.options) for s in graph.sections))
        return cls(graph)

    # ── Eligibility check ─────────────────────────────────────────────────────

    def _is_eligible(self, option_id: str, context: EligibilityContext) -> tuple[bool, str]:
        """
        Returns (eligible, reason).
        Evaluates all eligibility_rules for the option.
        """
        option = self._options.get(option_id)
        if not option:
            return False, f"Unknown option_id: {option_id}"

        for rule in option.eligibility_rules:
            rt = rule.rule_type

            if rt == "none":
                continue  # no restriction

            elif rt == "requires_option":
                req = rule.option_id
                if req and req not in context.selected_options:
                    return False, f"Requires '{req}' to be selected first"

            elif rt == "conflicts_with":
                conflict = rule.option_id
                if conflict and conflict in context.selected_options:
                    return False, f"Conflicts with '{conflict}'"

            elif rt == "requires_vertical":
                v = rule.vertical
                if v and context.vertical != v:
                    return False, f"Only available for vertical '{v}'"

        return True, ""

    # ── Core evaluation ───────────────────────────────────────────────────────

    def evaluate_selection(
        self,
        option_id: str,
        context: EligibilityContext,
        spec,                          # ApplicationSpec (avoid circular import)
    ) -> OptionDelta:
        """
        Evaluate a checkbox selection and return an OptionDelta.

        Raises ValueError if the option is not eligible given the current context.
        Never mutates the spec — caller must call spec.apply_delta(delta.to_spec_delta()).
        """
        option = self._options.get(option_id)
        if not option:
            raise ValueError(f"Unknown option_id: '{option_id}'")

        eligible, reason = self._is_eligible(option_id, context)
        if not eligible:
            raise ValueError(f"Option '{option_id}' is not eligible: {reason}")

        # Build the patch dict and count impact
        patch: dict[str, Any] = {}
        provenance_entries: list[dict] = []
        total_entities = 0
        total_endpoints = 0

        for binding in option.spec_bindings:
            new_e, new_ep = _apply_binding(patch, binding)
            total_entities += new_e
            total_endpoints += new_ep
            provenance_entries.append({
                "json_path": binding.json_path,
                "operation": binding.operation,
                "value": binding.value,
            })

        impact_parts = []
        if total_entities:
            impact_parts.append(f"+{total_entities} {'entity' if total_entities == 1 else 'entities'}")
        if total_endpoints:
            impact_parts.append(f"+{total_endpoints} {'endpoint' if total_endpoints == 1 else 'endpoints'}")

        return OptionDelta(
            option_id=option_id,
            patch=patch,
            provenance_entries=provenance_entries,
            new_entity_count=total_entities,
            new_endpoint_count=total_endpoints,
            amendment_id=f"opt_{option_id}",
            impact_summary=f"Added: {option.label}" + (f" ({', '.join(impact_parts)})" if impact_parts else ""),
        )

    # ── Section status ────────────────────────────────────────────────────────

    def get_section_status(self, section_id: str, context: EligibilityContext) -> dict[str, Any]:
        """
        Returns the current status of a section for the Spec Sheet UI.
        {
            "section_id": "...",
            "title": "...",
            "unlocked": bool,
            "options": {
                "option_id": {"eligible": bool, "reason": str, "selected": bool}
            }
        }
        """
        section = self._sections.get(section_id)
        if not section:
            raise ValueError(f"Unknown section_id: '{section_id}'")

        # Section is unlocked when all prerequisite options are selected
        unlocked = all(p in context.selected_options for p in section.prerequisites)

        options_status: dict[str, dict] = {}
        for opt in section.options:
            eligible, reason = self._is_eligible(opt.option_id, context)
            options_status[opt.option_id] = {
                "label": opt.label,
                "eligible": eligible,
                "reason": reason,
                "selected": opt.option_id in context.selected_options,
            }

        return {
            "section_id": section_id,
            "title": section.display_title,
            "unlocked": unlocked,
            "options": options_status,
        }

    # ── Live impact panel ─────────────────────────────────────────────────────

    def get_live_impact(self, context: EligibilityContext, spec) -> dict[str, int]:
        """
        Returns the numbers shown in the live impact panel.
        Derived purely from current spec state — no model calls.
        """
        return {
            "entity_count": len(spec.domain_model.entities),
            "endpoint_count": len(spec.api_model.endpoints),
            "screen_count": len(spec.ui_model.screens),
            "connector_count": len(getattr(spec.integration_model, "connectors", None) or getattr(spec.integration_model, "integrations", [])),
        }
