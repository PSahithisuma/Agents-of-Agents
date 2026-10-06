"""
VibeForge — LLM Client Adapter
================================
The single place where model calls are wired.

Phase 1 (laptop, no GPU server):
    - Ollama running locally with qwen3:8b
    - Falls back gracefully if Ollama is not running

Phase 2+ (GPU server):
    - LiteLLM proxy routing to vLLM
    - Same interface — zero agent code changes

Public API:
    client = OllamaClient()           # Phase 1 local dev
    client = LiteLLMClient(base_url)  # Phase 2+ production

Both implement:
    async def __call__(model, system, user) -> str

Agents import make_llm_client() and get whatever is configured.
Contract C11: every call is logged with job/conversation correlation id.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ── Model aliases ─────────────────────────────────────────────────────────────
# Agents use these aliases — never hardcode model names in agent code.
# Swap the underlying model here without touching agents.

MODEL_ALIASES = {
    # Phase 1 laptop: Ollama running qwen3:8b
    "agent-model":  os.getenv("AGENT_MODEL",  "qwen3:8b"),
    "coder-model":  os.getenv("CODER_MODEL",  "qwen2.5-coder:14b"),  # 14B on laptop
    "gate-model":   os.getenv("GATE_MODEL",   "qwen3:8b"),
}


# ── Ollama client (Phase 1 — local dev, laptop) ───────────────────────────────

class OllamaClient:
    """
    Async client for Ollama running locally.
    Install: https://ollama.com → ollama pull qwen3:8b

    Usage:
        client = OllamaClient()
        response = await client(model="agent-model", system="...", user="...")
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def __call__(
        self,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.1,
        job_id: str = "",
    ) -> str:
        """
        Call Ollama's chat/completions endpoint.
        Model aliases are resolved here.
        """
        resolved_model = MODEL_ALIASES.get(model, model)

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        payload = {
            "model": resolved_model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": 2048,
            },
            "format": "json",   # ask Ollama to constrain to JSON output
        }

        logger.info(
            "[LLM] model=%s job_id=%s user_chars=%d",
            resolved_model, job_id or "none", len(user),
        )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as http:
                resp = await http.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["message"]["content"]

                logger.info(
                    "[LLM] model=%s response_chars=%d",
                    resolved_model, len(content),
                )
                return content

        except httpx.ConnectError:
            raise RuntimeError(
                f"Cannot connect to Ollama at {self.base_url}. "
                "Is Ollama running? Start it with: ollama serve"
            )
        except httpx.TimeoutException:
            raise RuntimeError(
                f"Ollama call timed out after {self.timeout}s. "
                f"Model {resolved_model} may still be loading."
            )

    async def is_available(self) -> bool:
        """Check if Ollama is running and the agent model is available."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                resp = await http.get(f"{self.base_url}/api/tags")
                if resp.status_code != 200:
                    return False
                models = [m["name"] for m in resp.json().get("models", [])]
                agent_model = MODEL_ALIASES["agent-model"]
                available = any(
                    agent_model.split(":")[0] in m for m in models
                )
                if not available:
                    logger.warning(
                        "[LLM] Ollama running but %s not found. "
                        "Run: ollama pull %s",
                        agent_model, agent_model,
                    )
                return available
        except Exception:
            return False


# ── LiteLLM client (Phase 2+ — GPU server) ───────────────────────────────────

class LiteLLMClient:
    """
    Async client for the LiteLLM proxy (Phase 2+).
    The proxy routes model aliases to the correct vLLM instance.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:4000",
        api_key: str = "vibeforge",
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    async def __call__(
        self,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.1,
        job_id: str = "",
    ) -> str:
        """
        Call the LiteLLM proxy (OpenAI-compatible /v1/chat/completions).
        Retries with exponential backoff on 429 rate-limit errors (Groq free tier).
        """
        import asyncio
        import re as _re

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        payload = {
            "model": model,   # LiteLLM resolves alias to actual model
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 8192,
            "response_format": {"type": "json_object"},
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if job_id:
            headers["X-Job-Id"] = job_id

        # 429 backoff schedule: immediate → 15s → 30s → 60s
        backoff_delays = [0, 15, 30, 60]

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as http:
                for attempt, delay in enumerate(backoff_delays):
                    if delay:
                        logger.warning(
                            "[LiteLLMClient] 429 rate limit on %s — retry %d/%d in %ds",
                            model, attempt, len(backoff_delays) - 1, delay,
                        )
                        await asyncio.sleep(delay)

                    resp = await http.post(
                        f"{self.base_url}/v1/chat/completions",
                        json=payload,
                        headers=headers,
                    )

                    if resp.status_code == 429 and attempt < len(backoff_delays) - 1:
                        continue  # wait and retry

                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"] or ""
                    # Strip <think>...</think> reasoning blocks from Qwen3 thinking models
                    content = _re.sub(
                        r"<think>.*?</think>", "", content, flags=_re.DOTALL
                    ).strip()
                    return content

        except httpx.ConnectError:
            raise RuntimeError(
                f"Cannot connect to LiteLLM proxy at {self.base_url}. "
                "Is the proxy running?"
            )


# ── Factory function ──────────────────────────────────────────────────────────

def make_llm_client(mode: str = "auto"):
    """
    Factory that returns the right client for the environment.

    mode="auto"    → checks environment, uses LiteLLM if configured, else Ollama
    mode="ollama"  → always use Ollama (local dev)
    mode="litellm" → always use LiteLLM proxy (production)
    mode="mock"    → returns a mock that echoes back JSON (for offline testing)

    Environment variables:
        LITELLM_BASE_URL  → enables LiteLLM mode (e.g. http://gpu-server:4000)
        OLLAMA_BASE_URL   → override Ollama URL (default: http://localhost:11434)
    """
    if mode == "mock" or os.getenv("VIBEFORGE_MOCK_LLM"):
        return _MockClient()

    if mode == "litellm" or os.getenv("LITELLM_BASE_URL"):
        base_url = os.getenv("LITELLM_BASE_URL", "http://localhost:4000")
        logger.info("[LLM] Using LiteLLM proxy at %s", base_url)
        return LiteLLMClient(base_url=base_url)

    # Default: Ollama for local dev
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    logger.info("[LLM] Using Ollama at %s", ollama_url)
    return OllamaClient(base_url=ollama_url)


class _MockClient:
    """
    Offline mock client — echoes a minimal valid JSON response.
    Used when VIBEFORGE_MOCK_LLM=1 or mode="mock".
    Useful for running the full pipeline without a model.
    """
    async def __call__(self, model: str, system: str, user: str, **kwargs) -> str:
        return json.dumps({
            "summary": "Mock response — no model running",
            "patch": {},
            "impact_summary": "Mock mode — set VIBEFORGE_MOCK_LLM=0 to use real model",
            "new_entity_count": 0,
            "new_endpoint_count": 0,
            "confidence": 0.0,
            "grounded_sources": [],
            "operation": "add",
            "section": "domain_model",
            "compliance_implications": [],
            "consistency_warnings": [],
            "gap_questions": [],
            "analysis_summary": "Mock mode",
        })
