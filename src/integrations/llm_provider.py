"""LLM provider adapter.

Abstracts OpenAI and Anthropic behind a single interface so the workflow nodes
don't care which model is active. The provider and model are selected from
config.json; a fallback provider is tried automatically on failure.

All methods strip secrets before constructing prompts via secrets.redact().
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Type, TypeVar

import structlog
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from src.integrations.secrets import redact, resolve

log = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

_SYSTEM_BASE = (
    "You are an expert Terraform and cloud infrastructure security reviewer. "
    "You produce structured, accurate outputs and never include secret values, "
    "cloud credentials, API tokens, or Terraform state in your responses."
)


class LLMProvider(ABC):
    """Abstract LLM provider interface used by all workflow agents."""

    @abstractmethod
    def get_model(self) -> BaseChatModel: ...

    def generate_structured_output(
        self,
        schema: Type[T],
        system_prompt: str,
        user_prompt: str,
        context: str = "",
    ) -> T:
        model = self.get_model().with_structured_output(schema)
        messages = [
            SystemMessage(content=redact(_SYSTEM_BASE + "\n\n" + system_prompt)),
            HumanMessage(content=redact(f"{user_prompt}\n\n{context}" if context else user_prompt)),
        ]
        result = model.invoke(messages)
        log.info("llm.structured_output", schema=schema.__name__)
        return result  # type: ignore[return-value]

    def summarize(self, context: str, instruction: str = "") -> str:
        model = self.get_model()
        prompt = instruction or "Summarize the following clearly and concisely."
        messages = [
            SystemMessage(content=_SYSTEM_BASE),
            HumanMessage(content=f"{prompt}\n\n{redact(context)}"),
        ]
        result = model.invoke(messages)
        return str(result.content)

    def classify_findings(
        self,
        findings: list[dict[str, Any]],
        policy: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Ask the LLM to enrich findings; deterministic policy gates are applied
        separately in risk_policy.py — this only adds rationale/context."""
        schema_json = json.dumps(findings, indent=2)
        policy_json = json.dumps(policy, indent=2)
        summary = self.summarize(
            f"Findings:\n{schema_json}\n\nPolicy:\n{policy_json}",
            instruction=(
                "Review these IaC findings against the policy. "
                "For each finding, add a one-sentence 'policy_note' explaining "
                "the relevant policy clause. Return the findings list as JSON."
            ),
        )
        try:
            return json.loads(summary)  # type: ignore[return-value]
        except json.JSONDecodeError:
            return findings

    def propose_fix(
        self,
        diff: str,
        finding: dict[str, Any],
        constraints: list[str],
    ) -> str:
        """Return a unified diff string that fixes *finding* in *diff*."""
        constraints_text = "\n".join(f"- {c}" for c in constraints)
        prompt = (
            f"Finding:\n{json.dumps(finding, indent=2)}\n\n"
            f"Constraints (must not violate any):\n{constraints_text}\n\n"
            f"Original diff:\n{redact(diff)}\n\n"
            "Produce only the corrected unified diff for the affected file. "
            "Do not change any other files. Do not include explanations."
        )
        model = self.get_model()
        messages = [SystemMessage(content=_SYSTEM_BASE), HumanMessage(content=prompt)]
        result = model.invoke(messages)
        return str(result.content)


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str, temperature: float, max_tokens: int, api_key_env: str) -> None:
        from langchain_anthropic import ChatAnthropic

        self._model = ChatAnthropic(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=resolve(api_key_env),  # type: ignore[call-arg]
        )

    def get_model(self) -> BaseChatModel:
        return self._model


class OpenAIProvider(LLMProvider):
    def __init__(self, model: str, temperature: float, max_tokens: int, api_key_env: str) -> None:
        from langchain_openai import ChatOpenAI

        self._model = ChatOpenAI(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=resolve(api_key_env),  # type: ignore[call-arg]
        )

    def get_model(self) -> BaseChatModel:
        return self._model


def build_provider(llm_config: dict[str, Any]) -> LLMProvider:
    """Instantiate primary provider; fall back to secondary on import/auth failure."""
    primary_cfg = llm_config
    fallback_cfg = llm_config.get("fallback")

    try:
        provider = _make_provider(primary_cfg)
        log.info("llm.provider_loaded", provider=primary_cfg["provider"], model=primary_cfg["model"])
        return provider
    except Exception as exc:  # noqa: BLE001
        log.warning("llm.primary_failed", error=str(exc))
        if not fallback_cfg:
            raise
        log.info("llm.using_fallback", provider=fallback_cfg["provider"])
        return _make_provider(fallback_cfg)


def _make_provider(cfg: dict[str, Any]) -> LLMProvider:
    provider_name = cfg["provider"]
    kwargs = dict(
        model=cfg["model"],
        temperature=cfg.get("temperature", 0.1),
        max_tokens=cfg.get("max_output_tokens", 4096),
        api_key_env=cfg["api_key_env"],
    )
    if provider_name == "anthropic":
        return AnthropicProvider(**kwargs)
    if provider_name == "openai":
        return OpenAIProvider(**kwargs)
    raise ValueError(f"Unknown LLM provider: {provider_name!r}")
