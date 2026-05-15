"""Compliance Agent: retrieve policy standards from Confluence and evaluate findings."""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """You are a cloud infrastructure compliance analyst.
Given a set of IaC findings and relevant policy text from the organisation's
Confluence standards, add a concise policy_note to each finding explaining
which policy clause it violates (or confirm it does not violate any).

Rules:
- policy_note must cite the specific policy clause/section.
- Do not invent policies that are not in the provided text.
- Never include credentials or state values in your output.
"""


class ComplianceAgent:
    def __init__(self, llm: Any, tool_registry: Any | None = None) -> None:
        self._llm = llm
        self._registry = tool_registry

    def annotate_findings(
        self,
        findings: list[dict[str, Any]],
        terraform_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Retrieve relevant Confluence policies and annotate findings with policy_note."""
        if not findings:
            return findings

        policy_text = self._fetch_policy_text(terraform_context)
        if not policy_text:
            log.info("compliance_agent.no_policy_text")
            return findings

        try:
            annotated = self._llm.classify_findings(findings, {"policy_text": policy_text})
            log.info("compliance_agent.annotated", count=len(annotated))
            return annotated
        except Exception as exc:  # noqa: BLE001
            log.error("compliance_agent.failed", error=str(exc))
            return findings

    def _fetch_policy_text(self, terraform_context: dict[str, Any]) -> str:
        """Retrieve applicable Confluence pages for the changed Terraform resources."""
        if not self._registry:
            return ""
        try:
            _ = self._registry.get("confluence")
            # When MCP connected:
            #   pages = confluence.search(query="Terraform security standards")
            #   return combined text of top N pages
        except KeyError:
            log.info("compliance_agent.confluence_not_configured")
        return ""
