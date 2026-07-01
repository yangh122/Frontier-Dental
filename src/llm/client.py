"""Provider-agnostic LLM fallback wrapper.

Design intent: the LLM is a *fallback*, not the workhorse. This client is
- provider-agnostic: Anthropic Claude or OpenAI GPT, chosen by config.provider,
  so the fallback isn't hard-wired to one vendor,
- gated: a no-op unless llm.enabled and the provider's API key is present
  (deterministic runs work with zero cost / offline),
- budgeted: a hard call cap so a pathological run can't rack up spend,
- observable: every call is logged and counted.

Callers ask for structured JSON and get back a dict (or None on any failure) —
the pipeline must always degrade gracefully to rules-only behavior.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from ..logging_setup import get_logger

log = get_logger("llm")

# provider -> (env var holding the key, default model)
_PROVIDERS = {
    "anthropic": ("ANTHROPIC_API_KEY", "claude-haiku-4-5-20251001"),
    "openai": ("OPENAI_API_KEY", "gpt-4o-mini"),
}


class LLMClient:
    def __init__(self, cfg):
        self.cfg = cfg.llm
        self.provider = (self.cfg.provider or "anthropic").lower()
        self.model = self.cfg.model
        self.calls = 0
        self._client = None
        self._mode: Optional[str] = None  # "anthropic" | "openai"
        self._disabled_reason: Optional[str] = None

        if not self.cfg.enabled:
            self._disabled_reason = "llm.enabled = false"
            return
        if self.provider not in _PROVIDERS:
            self._disabled_reason = f"unknown provider '{self.provider}'"
            log.warning("llm_disabled", reason=self._disabled_reason)
            return

        env_var, default_model = _PROVIDERS[self.provider]
        api_key = os.environ.get(env_var)
        if not api_key:
            self._disabled_reason = f"{env_var} not set"
            log.warning("llm_disabled", reason=self._disabled_reason)
            return
        self.model = self.model or default_model

        try:
            if self.provider == "anthropic":
                import anthropic
                self._client = anthropic.Anthropic(api_key=api_key)
                self._mode = "anthropic"
            else:  # openai
                import openai
                self._client = openai.OpenAI(api_key=api_key)
                self._mode = "openai"
            log.info("llm_ready", provider=self.provider, model=self.model)
        except Exception as e:  # pragma: no cover
            self._disabled_reason = f"{self.provider} import/init failed: {e}"
            log.warning("llm_disabled", reason=self._disabled_reason)

    @property
    def available(self) -> bool:
        return self._client is not None and self.calls < self.cfg.max_fallback_calls

    def extract_json(self, system: str, user: str,
                     max_tokens: int = 1024) -> Optional[dict]:
        """Ask the model for JSON and parse it. Returns None on any problem so
        callers fall back to rules/empty rather than crashing."""
        if self._client is None:
            return None
        if self.calls >= self.cfg.max_fallback_calls:
            log.warning("llm_budget_exhausted", cap=self.cfg.max_fallback_calls)
            return None

        self.calls += 1
        try:
            if self._mode == "anthropic":
                text = self._call_anthropic(system, user, max_tokens)
            else:
                text = self._call_openai(system, user, max_tokens)
            return self._parse_json(text)
        except Exception as e:
            log.warning("llm_call_failed", provider=self.provider, error=str(e))
            return None

    # --- provider calls ---------------------------------------------------
    def _call_anthropic(self, system: str, user: str, max_tokens: int) -> str:
        msg = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if b.type == "text").strip()

    def _call_openai(self, system: str, user: str, max_tokens: int) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},  # force valid JSON
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (resp.choices[0].message.content or "").strip()

    # --- parsing ----------------------------------------------------------
    @staticmethod
    def _parse_json(text: str) -> Optional[dict]:
        # Tolerate ```json fences or surrounding prose.
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip().strip("`").strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
