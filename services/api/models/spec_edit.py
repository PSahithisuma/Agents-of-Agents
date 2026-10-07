"""
models/spec_edit.py — pure helpers for editing a draft Spec IR (no web or DB imports, unit-testable).

validate_entities(): strict checks on user-supplied entities (names, tables, fields).
apply_entities():    returns (frozen_dict, canonical_hash) for a spec with its extra entities replaced.
"""
from __future__ import annotations

import re
from typing import Any

from models.spec_ir import AppSpec, DomainModel, Entity, EntityField

_FIELD_TYPES = {"string", "int", "uuid", "decimal", "bool"}
_BAD_NAMES = {"String", "Object", "Class", "List", "Map", "Set", "Date", "Record", "Enum", "Entity", "Table",
              "Column", "Id", "Integer", "Long", "Boolean", "CoreEntity"}
_BAD_SQL = {"user", "order", "group", "table", "select", "column", "index", "check", "default", "grant", "limit",
            "offset", "primary", "references", "rule", "session", "transaction", "trigger", "view", "desc", "asc",
            "key", "from", "where", "when", "end", "all", "analyse", "analyze", "both", "case", "cast", "create",
            "current_user", "do", "else", "false", "true", "for", "foreign", "in", "into", "is", "like", "not",
            "null", "on", "only", "or", "returning", "some", "then", "to", "union", "unique", "using", "with"}
_RESERVED_COLS = {"id", "tenant_id", "created_at", "updated_at"}
MAX_ENTITIES = 30
MAX_FIELDS = 40


def validate_entities(items: Any) -> list[dict]:
    """Return a cleaned list of {name, table, fields:[{name,type}]}; raise ValueError listing every problem."""
    errors: list[str] = []
    if not isinstance(items, list) or not items:
        raise ValueError("entities must be a non-empty list")
    if len(items) > MAX_ENTITIES:
        raise ValueError(f"at most {MAX_ENTITIES} entities")
    out, names, tables = [], set(), set()
    for i, it in enumerate(items):
        where = f"entities[{i}]"
        if not isinstance(it, dict):
            errors.append(f"{where}: must be an object")
            continue
        name, table = str(it.get("name", "")).strip(), str(it.get("table", "")).strip()
        if not re.fullmatch(r"[A-Z][A-Za-z0-9]{2,30}", name) or name in _BAD_NAMES:
            errors.append(f"{where}: invalid name {name!r} (PascalCase, 3-31 letters/digits, not a reserved type)")
        elif name in names:
            errors.append(f"{where}: duplicate name {name!r}")
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,40}", table) or table in _BAD_SQL:
            errors.append(f"{where}: invalid table {table!r} (snake_case, not an SQL keyword)")
        elif table in tables:
            errors.append(f"{where}: duplicate table {table!r}")
        fields, cols = [], set()
        raw = it.get("fields")
        if not isinstance(raw, list) or not (1 <= len(raw) <= MAX_FIELDS):
            errors.append(f"{where}: fields must be a list of 1-{MAX_FIELDS}")
            raw = []
        for j, f in enumerate(raw):
            fw = f"{where}.fields[{j}]"
            if not isinstance(f, dict):
                errors.append(f"{fw}: must be an object")
                continue
            fname, ftype = str(f.get("name", "")).strip(), str(f.get("type", "string")).strip()
            if not re.fullmatch(r"[a-z][a-z0-9_]*", fname) or fname in _RESERVED_COLS or fname in _BAD_SQL:
                errors.append(f"{fw}: invalid column name {fname!r}")
                continue
            if fname in cols:
                errors.append(f"{fw}: duplicate column {fname!r}")
                continue
            if ftype not in _FIELD_TYPES and not re.fullmatch(r"raw:[A-Za-z0-9_]+", ftype):
                errors.append(f"{fw}: type must be one of {sorted(_FIELD_TYPES)} or raw:<JavaType>")
                continue
            cols.add(fname)
            fields.append({"name": fname, "type": ftype})
        names.add(name)
        tables.add(table)
        out.append({"name": name, "table": table, "fields": fields})
    if errors:
        raise ValueError("; ".join(errors))
    return out


def apply_entities(spec_data: dict, entities: list[dict]) -> tuple[dict, str]:
    """Rebuild the AppSpec from stored spec_data, replace domain_model.entities, return (frozen dict, hash)."""
    allowed = set(AppSpec.model_fields)
    spec = AppSpec(**{k: v for k, v in spec_data.items() if k in allowed})
    dm = spec.domain_model
    new_dm = DomainModel(
        entities=[
            Entity(name=e["name"], table=e["table"], fields=[EntityField(name=f["name"], type=f["type"]) for f in e["fields"]])
            for e in entities
        ],
        relationships=dm.relationships,
        extensions=dict(dm.extensions or {}),
    )
    spec = spec.model_copy(update={"domain_model": new_dm})
    spec = AppSpec.model_validate(spec.model_dump(exclude={"canonical_hash"}))   # re-validate the final shape
    return spec.freeze(), spec.canonical_hash
