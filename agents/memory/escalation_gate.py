"""
VibeForge — Escalation Gate (Phase 3)
=======================================
Called when all 3 local fix iterations have failed.
Fires one commercial model call (Claude/GPT/Grok) per job maximum.

Pre-conditions (ALL must pass before any call is made):
  1. fix_iteration >= 3   — local loop exhausted
  2. tenant_consent       — tenant explicitly enabled escalation
  3. budget_ok            — remaining budget >= escalation cost estimate
  4. pii_scan_passed      — prompt scanned, no PII/secrets found (Contract C18)

Post-conditions:
  - Output re-enters the sandbox gate (Contract C6 — no trust shortcut)
  - Escalation memory is updated with the problem signature
  - One call per job maximum (escalation_used flag)

Contracts:
  C6:  Escalated output re-enters the sandbox gate. Never trusted directly.
  C18: Pre-egress PII scan. No data leaves the perimeter without passing scan.

Prompt budget: ≤ 30K tokens (enforced by compressor before call).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── Escalation decision ────────────────────────────────────────────────────────

class EscalationVerdict(str, Enum):
    APPROVED        = "approved"        # all preconditions met, call fired
    BLOCKED_CONSENT = "blocked_consent" # tenant hasn't enabled escalation
    BLOCKED_BUDGET  = "blocked_budget"  # insufficient budget
    BLOCKED_PII     = "blocked_pii"     # PII found in prompt (Contract C18)
    BLOCKED_LIMIT   = "blocked_limit"   # already escalated this job
    BLOCKED_ITER    = "blocked_iter"    # fix loop not exhausted yet


@dataclass
class EscalationContext:
    """Everything the gate needs to make an escalation decision."""
    job_id: str
    tenant_id: str

    # Fix loop state
    fix_iteration: int = 0
    max_fix_iterations: int = 3

    # Tenant permissions
    tenant_escalation_enabled: bool = False
    tenant_external_llm_consent: bool = False

    # Budget
    budget_remaining: float = 0.0
    escalation_cost_estimate: float = 5.0   # USD, conservative estimate

    # Already escalated this job?
    escalation_used: bool = False

    # The failing files and their errors (input to the prompt)
    failing_files: list[str] = field(default_factory=list)
    errors_by_file: dict[str, list[str]] = field(default_factory=dict)
    assembled_files: dict[str, str] = field(default_factory=dict)

    # Stack context
    stack_profile: str = "java_spring"
    vertical: str = ""


@dataclass
class EscalationResult:
    """Output of the escalation gate."""
    verdict: EscalationVerdict
    reason: str

    # Populated when verdict == APPROVED
    fixed_files: dict[str, str] = field(default_factory=dict)
    model_used: str = ""
    tokens_used: int = 0
    cost_usd: float = 0.0
    pii_scan_passed: bool = False
    prompt_tokens: int = 0

    decided_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def approved(self) -> bool:
        return self.verdict == EscalationVerdict.APPROVED


# ── PII Scanner — Contract C18 ─────────────────────────────────────────────────

class PIIScanner:
    """
    Pre-egress scanner. Runs on the prompt before it leaves the perimeter.
    Contract C18: no data leaves without passing this scan.

    Detects:
    - Email addresses
    - Phone numbers (Indian and international)
    - Aadhaar numbers (12-digit)
    - PAN numbers (Indian tax ID format)
    - Credit card numbers (16-digit sequences)
    - AWS/GCP/Azure key patterns
    - JWT tokens
    - Private key headers
    """

    # Patterns that indicate PII or secrets
    PATTERNS = {
        "email":          re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),
        "phone_in":       re.compile(r'\b[6-9]\d{9}\b'),
        "phone_intl":     re.compile(r'\+\d{1,3}[\s-]?\d{6,12}\b'),
        "aadhaar":        re.compile(r'\b\d{4}\s?\d{4}\s?\d{4}\b'),
        "pan":            re.compile(r'\b[A-Z]{5}\d{4}[A-Z]{1}\b'),
        "credit_card":    re.compile(r'\b(?:\d[ -]?){15,16}\b'),
        "aws_key":        re.compile(r'AKIA[0-9A-Z]{16}'),
        "gcp_key":        re.compile(r'AIza[0-9A-Za-z\-_]{35}'),
        "jwt":            re.compile(r'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'),
        "private_key":    re.compile(r'-----BEGIN (?:RSA )?PRIVATE KEY-----'),
        "db_password":    re.compile(r'(?i)password\s*=\s*[\'"][^\'"]{8,}[\'"]'),
        "api_key_assign": re.compile(r'(?i)(?:api_key|apikey|secret_key)\s*=\s*[\'"][^\'"]{16,}[\'"]'),
    }

    # Allow-listed patterns (false positives from generated code)
    ALLOWLIST_PATTERNS = [
        re.compile(r'AKIA[0-9A-Z]{16}.*example'),    # example AWS key
        re.compile(r'test@example\.com'),              # test email
        re.compile(r'user@vibeforge\.io'),             # platform email
        re.compile(r'password\s*=\s*[\'"]?\$\{'),     # env var reference
        re.compile(r'password\s*=\s*[\'"]?<'),        # placeholder
    ]

    def scan(self, text: str) -> tuple[bool, list[str]]:
        """
        Scan text for PII/secrets.

        Returns:
            (passed, findings) where passed=True means no PII found.
            findings is a list of human-readable descriptions of what was found.
        """
        findings: list[str] = []

        for pattern_name, pattern in self.PATTERNS.items():
            matches = pattern.findall(text)
            if not matches:
                continue

            # Filter out allow-listed patterns
            real_matches = []
            for match in matches:
                match_str = match if isinstance(match, str) else str(match)
                is_allowlisted = any(
                    al.search(match_str) for al in self.ALLOWLIST_PATTERNS
                )
                if not is_allowlisted:
                    real_matches.append(match_str[:20] + "..." if len(match_str) > 20 else match_str)

            if real_matches:
                findings.append(
                    f"{pattern_name}: {len(real_matches)} instance(s) detected "
                    f"(sample: {real_matches[0]})"
                )

        passed = len(findings) == 0
        return passed, findings


# ── Prompt compressor — keeps prompt ≤ 30K tokens ─────────────────────────────

class PromptCompressor:
    """
    Compresses the escalation prompt to stay within the 30K token budget.
    Rough rule: 1 token ≈ 4 chars.
    """

    MAX_TOKENS = 30_000
    MAX_CHARS = MAX_TOKENS * 4   # conservative estimate

    def compress(
        self,
        errors_by_file: dict[str, list[str]],
        file_contents: dict[str, str],
        stack_profile: str,
        vertical: str,
    ) -> tuple[str, int]:
        """
        Build a compact escalation prompt within the token budget.

        Returns:
            (prompt_text, estimated_token_count)
        """
        parts = [
            f"You are fixing code generation failures for a {vertical} application "
            f"on {stack_profile} stack.",
            "",
            "## Failing files and exact errors",
        ]

        # Add errors (always included — they are the core of the prompt)
        for fpath, errors in errors_by_file.items():
            parts.append(f"\n### {fpath}")
            for err in errors[:10]:   # max 10 errors per file
                parts.append(f"  ERROR: {err}")

        parts.append("\n## Current file contents (for context)")

        # Add file contents in priority order — smallest files first to fit more
        sorted_files = sorted(
            file_contents.items(),
            key=lambda x: len(x[1]),
        )

        current_chars = sum(len(p) for p in parts)
        budget_for_files = self.MAX_CHARS - current_chars - 500  # 500 char buffer

        for fpath, content in sorted_files:
            if fpath not in errors_by_file:
                continue  # only include failing files

            file_section = f"\n### {fpath}\n```\n{content}\n```"
            if current_chars + len(file_section) > self.MAX_CHARS:
                # Truncate content to fit
                available = budget_for_files - current_chars
                if available > 200:
                    truncated = content[:available]
                    file_section = f"\n### {fpath}\n```\n{truncated}\n... [truncated]\n```"
                    parts.append(file_section)
                    current_chars += len(file_section)
                break

            parts.append(file_section)
            current_chars += len(file_section)

        parts.append(
            "\n## Task\n"
            "Fix all the errors above. Return ONLY the corrected file contents "
            "as a JSON object: {\"filename\": \"complete corrected content\"}. "
            "No explanation. No markdown. Pure JSON."
        )

        prompt = "\n".join(parts)
        estimated_tokens = len(prompt) // 4
        return prompt, estimated_tokens


# ── Commercial model adapter ───────────────────────────────────────────────────

class CommercialModelAdapter:
    """
    Calls the commercial model (Claude/GPT/Grok) via LiteLLM.
    Model selection is configuration-driven — not hardcoded.

    In tests: replaced with a mock.
    In production: calls api.anthropic.com or api.openai.com via LiteLLM proxy.
    """

    def __init__(
        self,
        litellm_client=None,
        model: str = "claude-opus-4-6",
        max_tokens: int = 8192,
    ):
        self._client = litellm_client
        self._model = model
        self._max_tokens = max_tokens

    async def call(self, prompt: str, job_id: str = "") -> tuple[str, int, float]:
        """
        Fire the commercial model call.

        Returns:
            (response_text, tokens_used, cost_usd)
        """
        if self._client is None:
            raise RuntimeError(
                "CommercialModelAdapter: no LiteLLM client configured. "
                "Set LITELLM_BASE_URL and configure commercial model routing."
            )

        import httpx

        logger.info(
            "[EscalationGate] Firing commercial model call. "
            "model=%s job=%s prompt_chars=%d",
            self._model, job_id, len(prompt),
        )

        async with httpx.AsyncClient(timeout=120.0) as http:
            resp = await http.post(
                f"{self._client.base_url}/v1/chat/completions",
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": self._max_tokens,
                    "temperature": 0.1,
                },
                headers={
                    "Authorization": f"Bearer {self._client.api_key}",
                    "Content-Type": "application/json",
                    "X-Job-Id": job_id,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        content = data["choices"][0]["message"]["content"]
        tokens_used = data.get("usage", {}).get("total_tokens", 0)

        # Rough cost estimate (varies by model — update when pricing changes)
        cost_per_1k = {"claude-opus-4-6": 0.015, "gpt-4o": 0.005}.get(self._model, 0.01)
        cost_usd = (tokens_used / 1000) * cost_per_1k

        logger.info(
            "[EscalationGate] Commercial call complete. tokens=%d cost=$%.4f",
            tokens_used, cost_usd,
        )
        return content, tokens_used, cost_usd

    def parse_fixed_files(self, response: str) -> dict[str, str]:
        """
        Parse the commercial model's response into a {filename: content} dict.
        Handles both raw JSON and markdown-fenced JSON.
        """
        import json

        text = response.strip()

        # Strip markdown fences
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
            text = text.strip()

        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return {k: v for k, v in parsed.items() if isinstance(v, str)}
        except (json.JSONDecodeError, ValueError):
            pass

        logger.warning("[EscalationGate] Could not parse commercial model response as JSON")
        return {}


# ── Escalation Gate ─────────────────────────────────────────────────────────────

class EscalationGate:
    """
    The commercial model escalation gate.

    Call flow:
        1. Check all preconditions (fast, zero LLM)
        2. Run PII scan on the prompt (Contract C18)
        3. Compress prompt to ≤30K tokens
        4. Fire commercial model call
        5. Parse fixed files from response
        6. Return EscalationResult — caller re-enters sandbox gate (Contract C6)

    One call per job. escalation_used flag prevents re-escalation.
    """

    def __init__(
        self,
        commercial_adapter: Optional[CommercialModelAdapter] = None,
        pii_scanner: Optional[PIIScanner] = None,
        compressor: Optional[PromptCompressor] = None,
    ):
        self._adapter = commercial_adapter or CommercialModelAdapter()
        self._scanner = pii_scanner or PIIScanner()
        self._compressor = compressor or PromptCompressor()

    async def evaluate(self, ctx: EscalationContext) -> EscalationResult:
        """
        Evaluate whether to escalate and fire the commercial call if approved.

        Contract C6:  Output goes back to caller — caller must re-run sandbox gate.
        Contract C18: PII scan runs before any data leaves the perimeter.
        """

        # ── Precondition checks (ordered cheapest first) ──────────────────────

        if ctx.escalation_used:
            return EscalationResult(
                verdict=EscalationVerdict.BLOCKED_LIMIT,
                reason="Escalation already used for this job. One call per job maximum.",
            )

        if ctx.fix_iteration < ctx.max_fix_iterations:
            return EscalationResult(
                verdict=EscalationVerdict.BLOCKED_ITER,
                reason=f"Fix iteration {ctx.fix_iteration} < {ctx.max_fix_iterations}. "
                       "Local fix loop not exhausted.",
            )

        if not ctx.tenant_escalation_enabled:
            return EscalationResult(
                verdict=EscalationVerdict.BLOCKED_CONSENT,
                reason="Tenant has not enabled escalation. "
                       "Enable in tenant settings to allow commercial model calls.",
            )

        if not ctx.tenant_external_llm_consent:
            return EscalationResult(
                verdict=EscalationVerdict.BLOCKED_CONSENT,
                reason="Tenant has not given consent for data to be sent to external LLMs. "
                       "Consent required per Contract C18.",
            )

        if ctx.budget_remaining < ctx.escalation_cost_estimate:
            return EscalationResult(
                verdict=EscalationVerdict.BLOCKED_BUDGET,
                reason=f"Budget remaining (${ctx.budget_remaining:.2f}) < "
                       f"estimated escalation cost (${ctx.escalation_cost_estimate:.2f}).",
            )

        # ── Build prompt ──────────────────────────────────────────────────────

        # Only include failing files in the prompt
        failing_contents = {
            fpath: content
            for fpath, content in ctx.assembled_files.items()
            if fpath in ctx.failing_files
        }

        prompt, estimated_tokens = self._compressor.compress(
            errors_by_file=ctx.errors_by_file,
            file_contents=failing_contents,
            stack_profile=ctx.stack_profile,
            vertical=ctx.vertical,
        )

        # ── PII scan — Contract C18 ───────────────────────────────────────────

        pii_passed, pii_findings = self._scanner.scan(prompt)

        if not pii_passed:
            logger.error(
                "[EscalationGate] PII scan FAILED — escalation blocked. "
                "job=%s findings=%s", ctx.job_id, pii_findings,
            )
            return EscalationResult(
                verdict=EscalationVerdict.BLOCKED_PII,
                reason=f"Pre-egress PII scan failed (Contract C18). "
                       f"Findings: {', '.join(pii_findings)}. "
                       "Prompt must not contain PII before leaving perimeter.",
                pii_scan_passed=False,
                prompt_tokens=estimated_tokens,
            )

        logger.info(
            "[EscalationGate] PII scan passed. Firing commercial call. "
            "job=%s tokens=%d", ctx.job_id, estimated_tokens,
        )

        # ── Fire commercial call ──────────────────────────────────────────────

        try:
            response, tokens_used, cost_usd = await self._adapter.call(
                prompt=prompt,
                job_id=ctx.job_id,
            )
        except Exception as e:
            logger.error("[EscalationGate] Commercial call failed: %s", e)
            return EscalationResult(
                verdict=EscalationVerdict.BLOCKED_BUDGET,  # treat as budget issue
                reason=f"Commercial model call failed: {str(e)[:200]}",
                pii_scan_passed=True,
                prompt_tokens=estimated_tokens,
            )

        # ── Parse response ────────────────────────────────────────────────────

        fixed_files = self._adapter.parse_fixed_files(response)

        logger.info(
            "[EscalationGate] Escalation complete. "
            "job=%s fixed_files=%d tokens=%d cost=$%.4f",
            ctx.job_id, len(fixed_files), tokens_used, cost_usd,
        )

        return EscalationResult(
            verdict=EscalationVerdict.APPROVED,
            reason=f"Commercial model ({self._adapter._model}) called successfully.",
            fixed_files=fixed_files,
            model_used=self._adapter._model,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            pii_scan_passed=True,
            prompt_tokens=estimated_tokens,
        )
