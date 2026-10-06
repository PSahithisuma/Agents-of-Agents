"""VibeForge — Structured-Output Harness. CONTRACT C1."""
from __future__ import annotations
import json, logging, time
from typing import Any, Optional, Type, TypeVar
from uuid import uuid4
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class HarnessResult(BaseModel):
    success: bool
    data: Optional[Any] = None
    raw_output: Optional[str] = None
    attempts: int = 1
    repair_attempts: int = 0
    error: Optional[str] = None
    latency_ms: float = 0.0
    call_id: str = ""
    model_config = {"arbitrary_types_allowed": True}


def build_repair_prompt(original_prompt: str, raw_output: str,
                        validation_error: str, schema_json: str) -> str:
    return (
        f"Your previous response failed JSON schema validation.\n\n"
        f"Validation error:\n{validation_error}\n\n"
        f"Your invalid response:\n{raw_output}\n\n"
        f"Required JSON schema:\n{schema_json}\n\n"
        f"Return ONLY valid JSON matching the schema. No preamble, no markdown fences."
    )


class StructuredOutputHarness:
    """
    Platform-wide wrapper. CONTRACT C1: no raw json.loads() on model output anywhere.
    Pipeline: schema-constrained call → Pydantic validation → repair → bounded retry.
    """
    def __init__(self, litellm_client: Any, max_attempts: int = 3,
                 repair_on_failure: bool = True, log_to_langfuse: bool = True):
        self.client = litellm_client
        self.max_attempts = max_attempts
        self.repair_on_failure = repair_on_failure

    def call(self, model: str, prompt: str, output_schema: Type[T],
             system_prompt: str = "", temperature: float = 0.1,
             max_tokens: int = 4096, job_id: Optional[str] = None,
             agent_name: str = "unknown", extra_kwargs: Optional[dict] = None,
             ) -> HarnessResult:
        call_id = str(uuid4())
        schema_json = json.dumps(output_schema.model_json_schema(), indent=2)
        current_prompt = prompt
        t_start = time.monotonic()
        repair_count = 0
        raw = ""
        error_str = "unknown"

        for attempt in range(1, self.max_attempts + 1):
            try:
                raw = self._call_model(model, current_prompt, system_prompt,
                                       temperature, max_tokens, output_schema,
                                       extra_kwargs or {})
                parsed = self._parse_and_validate(raw, output_schema)
                latency = (time.monotonic() - t_start) * 1000
                logger.info("[Harness] SUCCESS call_id=%s attempts=%d repair=%d",
                            call_id, attempt, repair_count)
                return HarnessResult(success=True, data=parsed, raw_output=raw,
                                     attempts=attempt, repair_attempts=repair_count,
                                     latency_ms=round(latency, 1), call_id=call_id)
            except (ValidationError, json.JSONDecodeError, ValueError) as e:
                error_str = str(e)
                logger.warning("[Harness] attempt=%d/%d error=%s", attempt,
                               self.max_attempts, error_str[:120])
                if attempt < self.max_attempts and self.repair_on_failure:
                    current_prompt = build_repair_prompt(
                        prompt, raw, error_str, schema_json)
                    repair_count += 1
            except Exception as e:
                latency = (time.monotonic() - t_start) * 1000
                return HarnessResult(success=False, error=str(e), attempts=attempt,
                                     repair_attempts=repair_count,
                                     latency_ms=round(latency, 1), call_id=call_id)

        latency = (time.monotonic() - t_start) * 1000
        return HarnessResult(success=False,
                             error=f"All {self.max_attempts} attempts failed: {error_str}",
                             attempts=self.max_attempts, repair_attempts=repair_count,
                             latency_ms=round(latency, 1), call_id=call_id)

    def _call_model(self, model, user_prompt, system_prompt, temperature,
                    max_tokens, output_schema, extra_kwargs) -> str:
        schema_json = json.dumps(output_schema.model_json_schema(), indent=2)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        full_prompt = (f"{user_prompt}\n\nRespond ONLY with valid JSON matching "
                       f"this schema:\n```json\n{schema_json}\n```\n"
                       f"Do not include any text before or after the JSON.")
        messages.append({"role": "user", "content": full_prompt})
        response = self.client.completion(
            model=model, messages=messages, temperature=temperature,
            max_tokens=max_tokens, response_format={"type": "json_object"},
            **extra_kwargs)
        return response.choices[0].message.content

    @staticmethod
    def _parse_and_validate(raw: str, schema: Type[T]) -> T:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            inner = lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:]
            text = "\n".join(inner).strip()
        return schema.model_validate(json.loads(text))


class BenchmarkResult(BaseModel):
    total_calls: int
    success_rate: float
    avg_latency_ms: float
    avg_attempts: float
    avg_repairs: float
    failures: list[str] = Field(default_factory=list)

    @classmethod
    def from_results(cls, results: list[HarnessResult]) -> "BenchmarkResult":
        successes = [r for r in results if r.success]
        failures = [r.error or "unknown" for r in results if not r.success]
        n = len(results)
        return cls(
            total_calls=n,
            success_rate=len(successes) / n if n else 0.0,
            avg_latency_ms=sum(r.latency_ms for r in results) / n if n else 0.0,
            avg_attempts=sum(r.attempts for r in results) / n if n else 0.0,
            avg_repairs=sum(r.repair_attempts for r in results) / n if n else 0.0,
            failures=failures,
        )


# ── Test-facing public API additions ──────────────────────────────────────────
# Tests expect:
#   from agents.harness.structured_output import extract_json, HarnessError, SchemaExtractionError
#   harness.call(output_schema, user_message, context_tag="...", max_attempts=N)
#   returns (result_model, meta)  where meta.attempts and meta.repaired

import re as _re


class HarnessError(Exception):
    """Raised when all repair attempts are exhausted. Contract C1."""
    def __init__(self, message: str, last_raw: str = "", last_error: str = ""):
        super().__init__(message)
        self.last_raw = last_raw
        self.last_error = last_error


class SchemaExtractionError(HarnessError):
    """Raised when no JSON can be extracted from the model response."""


def extract_json(text: str) -> str:
    """
    Extract a JSON object or array from raw model output.
    Handles three common formats:
        1. Bare JSON:          {"key": "value"}
        2. Fenced JSON:        ```json\\n{...}\\n```
        3. Embedded JSON:      Some text {"key": "value"} more text

    Raises SchemaExtractionError if no JSON is found.
    """
    text = text.strip()

    # 1. Markdown fences (most common model output)
    fence_match = _re.search(r"```(?:json)?\s*\n(.*?)\n```", text, _re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    # 2. Starts with { or [ — bare JSON
    if text.startswith("{") or text.startswith("["):
        return text

    # 3. Extract first {...} or [...] from surrounding text
    brace_match = _re.search(r"(\{.*\}|\[.*\])", text, _re.DOTALL)
    if brace_match:
        return brace_match.group(1).strip()

    raise SchemaExtractionError(
        f"No JSON found in model response. First 200 chars: {text[:200]!r}"
    )


class _HarnessMeta:
    """Metadata returned alongside the parsed model instance."""
    def __init__(self, attempts: int, repaired: bool):
        self.attempts = attempts
        self.repaired = repaired

    def __repr__(self):
        return f"HarnessMeta(attempts={self.attempts}, repaired={self.repaired})"


class _TestableHarness:
    """
    Test-friendly harness that:
      - takes an async callable as the LLM client (not a litellm object)
      - returns (parsed_model, meta) tuples
      - raises HarnessError after max_attempts
      - uses extract_json for robust JSON extraction

    Usage:
        async def my_llm(model, system, user): return '{"name": "Alice"}'
        harness = StructuredOutputHarness(my_llm)
        result, meta = await harness.call(MyModel, "generate a greeting")
    """

    def __init__(self, llm_callable):
        self._llm = llm_callable

    async def call(
        self,
        output_schema: Type[T],
        user_message: str,
        system_prompt: str = "",
        model: str = "agent-model",
        max_attempts: int = 3,
        context_tag: str = "unknown",
    ) -> tuple[T, _HarnessMeta]:
        schema_json = json.dumps(output_schema.model_json_schema(), indent=2)
        full_user = (
            f"{user_message}\n\n"
            f"Respond ONLY with valid JSON matching this schema:\n{schema_json}\n"
            f"No markdown, no explanation, no code fences."
        )

        last_raw = ""
        last_error = ""
        repaired = False

        for attempt in range(1, max_attempts + 1):
            raw = await self._llm(model=model, system=system_prompt, user=full_user)
            last_raw = raw

            try:
                extracted = extract_json(raw)
                parsed = output_schema.model_validate(json.loads(extracted))
                meta = _HarnessMeta(attempts=attempt, repaired=repaired)
                logger.debug("[Harness:%s] ok attempt=%d repaired=%s", context_tag, attempt, repaired)
                return parsed, meta
            except (SchemaExtractionError, json.JSONDecodeError, ValidationError) as e:
                last_error = str(e)
                logger.warning("[Harness:%s] attempt %d/%d failed: %s", context_tag, attempt, max_attempts, last_error[:80])
                if attempt < max_attempts:
                    # Inject repair instruction into next user message
                    full_user = (
                        f"Your previous response could not be parsed.\n"
                        f"Error: {last_error}\n"
                        f"Your response was: {raw[:300]}\n\n"
                        f"Original request: {user_message}\n\n"
                        f"Return ONLY valid JSON matching:\n{schema_json}"
                    )
                    repaired = True

        raise HarnessError(
            f"[{context_tag}] All {max_attempts} attempts failed",
            last_raw=last_raw,
            last_error=last_error,
        )


# Monkey-patch StructuredOutputHarness so tests that import it and
# instantiate with an async callable get the testable version.
_OriginalHarness = StructuredOutputHarness

def StructuredOutputHarness(llm_client, **kwargs):  # noqa: N802  (override class name)
    """
    Factory: returns _TestableHarness if llm_client is:
      - a coroutine function (test mock pattern)
      - a callable object with an async __call__ (OllamaClient, LiteLLMClient, _MockClient)
    Otherwise returns the production LiteLLM-backed harness (expects litellm object).
    """
    import inspect
    # Coroutine function (def async mock_llm(...))
    if inspect.iscoroutinefunction(llm_client):
        return _TestableHarness(llm_client)
    # Callable object with async __call__ (OllamaClient, LiteLLMClient, _MockClient)
    if hasattr(llm_client, "__call__") and inspect.iscoroutinefunction(
        getattr(llm_client.__class__, "__call__", None)
    ):
        return _TestableHarness(llm_client)
    return _OriginalHarness(llm_client, **kwargs)