"""
VibeForge — Assembler + Conflict Checker (Phase 2)
====================================================
Merges per-module FileMaps into one project tree.
Zero LLM. Pure deterministic Python.

Contract: if two synthesizers emit the same file path, the conflict
is FLAGGED in the AssemblyResult — NEVER silently overwritten.
The gate reads conflicts from AssemblyResult and fails the build.

Phase 2 decision: sequential synthesis.
  - No two synthesizers run at the same time
  - Conflicts are rare (only when models hallucinate duplicate paths)
  - Conflict detection is still required — models can still conflict

Why conflicts are flagged, not auto-resolved:
  Auto-resolution requires understanding intent. The Reviewer (Qwen3-8B)
  is better placed to decide which file wins — or whether both are wrong.
  The Assembler has no such judgment — it is deterministic by contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional

from agents.generation.synthesizer import FileMapOutput, SynthesizedFile

logger = logging.getLogger(__name__)


# ── Assembly result ────────────────────────────────────────────────────────────

@dataclass
class ConflictRecord:
    """Records one filename conflict between two synthesized modules."""
    file_path: str
    first_owner_module_id: str
    conflict_module_id: str
    first_content_preview: str     # first 100 chars of the first version
    conflict_content_preview: str  # first 100 chars of the conflicting version

    def __str__(self) -> str:
        return (
            f"CONFLICT: {self.file_path}\n"
            f"  First written by:  {self.first_owner_module_id}\n"
            f"  Conflicting module: {self.conflict_module_id}"
        )


@dataclass
class AssemblyResult:
    """
    The complete assembled project.
    Passed to the QA Gate which reads conflicts and fails if any exist.
    """
    assembled_files: dict[str, str] = field(default_factory=dict)
    # {filename: module_id} — tracks which module owns each file
    file_ownership: dict[str, str] = field(default_factory=dict)
    conflicts: list[ConflictRecord] = field(default_factory=list)
    scaffold_files: dict[str, str] = field(default_factory=dict)
    total_file_count: int = 0
    total_module_count: int = 0
    assembly_notes: list[str] = field(default_factory=list)

    @property
    def has_conflicts(self) -> bool:
        return len(self.conflicts) > 0

    @property
    def conflict_paths(self) -> list[str]:
        return [c.file_path for c in self.conflicts]

    def summary(self) -> dict:
        return {
            "total_files": self.total_file_count,
            "total_modules": self.total_module_count,
            "conflicts": len(self.conflicts),
            "conflict_paths": self.conflict_paths,
            "scaffold_files": len(self.scaffold_files),
        }


# ── Assembler ──────────────────────────────────────────────────────────────────

class Assembler:
    """
    Merges per-module FileMaps into one project tree.
    Zero LLM — pure deterministic Python.

    Usage:
        assembler = Assembler()
        result = assembler.assemble(
            scaffold_files={"Dockerfile": "...", "pom.xml": "..."},
            module_outputs=[fileMapOutput1, fileMapOutput2, ...],
        )
        if result.has_conflicts:
            # Gate will catch this — don't try to auto-resolve
            pass
    """

    # File paths that the scaffold always provides and synthesizers must never overwrite.
    # If a synthesizer emits one of these, it's a conflict even with the scaffold.
    SCAFFOLD_PROTECTED_PATHS = {
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "pyproject.toml",
        "setup.py",
        "requirements.txt",
        "Dockerfile",
        "docker-compose.yml",
        ".github/workflows/ci.yml",
        "README.md",
    }

    def assemble(
        self,
        scaffold_files: dict[str, str],
        module_outputs: list[FileMapOutput],
        strict_path_validation: bool = True,
    ) -> AssemblyResult:
        """
        Merge scaffold files and all module FileMaps into one tree.

        Args:
            scaffold_files:           Files from the Copier template (zero LLM)
            module_outputs:           Ordered list of FileMapOutput from Synthesizer
            strict_path_validation:   If True, validates paths are well-formed

        Returns:
            AssemblyResult with assembled_files and any conflicts flagged
        """
        result = AssemblyResult(
            scaffold_files=dict(scaffold_files),
            total_module_count=len(module_outputs),
        )

        # Step 1: lay scaffold files (these are the foundation)
        for path, content in scaffold_files.items():
            normalized = self._normalize_path(path)
            result.assembled_files[normalized] = content
            result.file_ownership[normalized] = "__scaffold__"
            logger.debug("[Assembler] scaffold: %s", normalized)

        # Step 2: merge module files in build order
        for file_map in module_outputs:
            module_id = file_map.module_id
            for synth_file in file_map.files:
                path = synth_file.filename
                normalized = self._normalize_path(path)

                # Validate path shape
                if strict_path_validation:
                    error = self._validate_path(normalized, module_id)
                    if error:
                        result.assembly_notes.append(error)
                        logger.warning("[Assembler] Path validation: %s", error)
                        continue  # skip invalid paths — gate will catch via compile

                # Check conflict with scaffold
                if normalized in self.SCAFFOLD_PROTECTED_PATHS:
                    conflict = ConflictRecord(
                        file_path=normalized,
                        first_owner_module_id="__scaffold__",
                        conflict_module_id=module_id,
                        first_content_preview=result.assembled_files.get(normalized, "")[:100],
                        conflict_content_preview=synth_file.content[:100],
                    )
                    result.conflicts.append(conflict)
                    logger.error("[Assembler] CONFLICT with scaffold: %s (from %s)", normalized, module_id)
                    continue  # keep scaffold version, record conflict

                # Check conflict with another module
                if normalized in result.assembled_files and \
                   result.file_ownership.get(normalized) != "__scaffold__":
                    first_owner = result.file_ownership[normalized]
                    conflict = ConflictRecord(
                        file_path=normalized,
                        first_owner_module_id=first_owner,
                        conflict_module_id=module_id,
                        first_content_preview=result.assembled_files[normalized][:100],
                        conflict_content_preview=synth_file.content[:100],
                    )
                    result.conflicts.append(conflict)
                    logger.error(
                        "[Assembler] CONFLICT: %s written by %s AND %s",
                        normalized, first_owner, module_id,
                    )
                    # Keep the FIRST writer's version, record the conflict
                    # The Reviewer decides which one is correct
                    continue

                # No conflict — write to tree
                result.assembled_files[normalized] = synth_file.content
                result.file_ownership[normalized] = module_id
                logger.debug("[Assembler] %s → %s", module_id, normalized)

        result.total_file_count = len(result.assembled_files)

        # Log summary
        if result.has_conflicts:
            logger.error(
                "[Assembler] Assembly complete with %d CONFLICTS: %s",
                len(result.conflicts),
                result.conflict_paths,
            )
        else:
            logger.info(
                "[Assembler] Assembly complete: %d files, %d modules, 0 conflicts",
                result.total_file_count,
                result.total_module_count,
            )

        return result

    def apply_fixes(
        self,
        current_result: AssemblyResult,
        fixed_file_maps: list[FileMapOutput],
    ) -> AssemblyResult:
        """
        Apply Fixer output — regenerates ONLY the files in the FixPlan.
        Called by the Fixer node, not the main assembly path.

        Contract C5: only FixPlan files are regenerated.
        All other files stay exactly as they were.
        """
        # Clone the current assembled tree
        new_result = AssemblyResult(
            assembled_files=dict(current_result.assembled_files),
            file_ownership=dict(current_result.file_ownership),
            scaffold_files=dict(current_result.scaffold_files),
            total_module_count=current_result.total_module_count,
            assembly_notes=list(current_result.assembly_notes),
        )
        # Clear only the conflicts — fixer may have resolved them
        new_result.conflicts = []

        files_replaced = 0
        for file_map in fixed_file_maps:
            for synth_file in file_map.files:
                normalized = self._normalize_path(synth_file.filename)
                new_result.assembled_files[normalized] = synth_file.content
                new_result.file_ownership[normalized] = f"__fixer_{file_map.module_id}__"
                files_replaced += 1
                logger.info("[Assembler:fix] replaced %s", normalized)

        new_result.total_file_count = len(new_result.assembled_files)
        new_result.assembly_notes.append(
            f"Fixer replaced {files_replaced} files"
        )
        return new_result

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_path(path: str) -> str:
        """
        Normalize a file path to forward-slash POSIX style.
        Strips leading slashes and dots.
        Windows backslashes are converted.
        """
        normalized = path.replace("\\", "/").strip("/").strip("./")
        # Collapse any double slashes
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        return normalized

    @staticmethod
    def _validate_path(path: str, module_id: str) -> Optional[str]:
        """
        Returns an error string if the path is invalid, else None.
        Catches common model hallucination patterns.
        """
        if not path:
            return f"Module {module_id}: emitted empty file path"

        if len(path) > 300:
            return f"Module {module_id}: path too long ({len(path)} chars): {path[:80]}"

        # Must have a file extension
        if "." not in PurePosixPath(path).name:
            return f"Module {module_id}: path has no extension: {path}"

        # Must not contain dangerous patterns
        dangerous = ["..", "~", "$", "%", "&&", "|", ";"]
        for d in dangerous:
            if d in path:
                return f"Module {module_id}: dangerous pattern '{d}' in path: {path}"

        return None

    def get_files_for_module(
        self,
        result: AssemblyResult,
        module_id: str,
    ) -> dict[str, str]:
        """
        Return all files owned by a specific module.
        Used by the Reviewer to map GateReport errors back to modules.
        """
        return {
            path: content
            for path, content in result.assembled_files.items()
            if result.file_ownership.get(path) == module_id
        }

    def get_module_for_file(
        self,
        result: AssemblyResult,
        file_path: str,
    ) -> Optional[str]:
        """
        Return the module_id that owns a given file path.
        Used by the Reviewer to map a failing file back to its module.
        """
        normalized = self._normalize_path(file_path)
        return result.file_ownership.get(normalized)
