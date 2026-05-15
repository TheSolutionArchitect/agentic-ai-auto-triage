"""Peer Review Agent: LLM-assisted code review with structured finding output."""

from __future__ import annotations

import json
from typing import Any

import structlog
from pydantic import BaseModel, Field, field_validator

from src.integrations.secrets import redact

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """You are a senior Terraform and cloud infrastructure security engineer.
Your task is to review Terraform code changes and produce a structured list of findings.

Rules:
- Never duplicate findings already covered by deterministic scanner results (unless adding materially useful context).
- Every finding must have a file_path, severity, confidence (0.0–1.0), category, rationale, and fix_recommendation.
- Severity: HIGH = security/availability/compliance/cost/data-loss risk; MEDIUM = meaningful risk; LOW = style/convention.
- Confidence: how certain you are the finding is real and the fix is correct.
- fix_type: "auto_fix_allowed" only for narrow, deterministic changes; "human_required" for anything ambiguous.
- Never include secrets, credentials, or Terraform state content in your output.
"""

_USER_TEMPLATE = """PR #{pr_number} — branch: {source_branch} → {target_branch}

Changed Terraform files:
{changed_files}

Terraform context:
{tf_context}

Scanner and compliance results (do not duplicate, only add context):
{policy_results}

Produce findings as a JSON array. Each finding must match this schema:
{{
  "file_path": string,
  "line_ref": string | null,
  "severity": "HIGH" | "MEDIUM" | "LOW",
  "confidence": float (0.0–1.0),
  "category": string,
  "rationale": string,
  "fix_recommendation": string,
  "fix_type": "auto_fix_allowed" | "human_required" | "informational"
}}

Return ONLY the JSON array, no explanation.
"""


class FindingSchema(BaseModel):
    file_path: str
    line_ref: str | None = None
    severity: str = Field(pattern="^(HIGH|MEDIUM|LOW)$")
    confidence: float = Field(ge=0.0, le=1.0)
    category: str
    rationale: str
    fix_recommendation: str
    fix_type: str = Field(pattern="^(auto_fix_allowed|human_required|informational)$")

    @field_validator("file_path")
    @classmethod
    def strip_path(cls, v: str) -> str:
        return v.strip()


class FindingsOutput(BaseModel):
    findings: list[FindingSchema]


class PeerReviewAgent:
    def __init__(self, llm: Any, tool_registry: Any | None = None) -> None:
        self._llm = llm
        self._registry = tool_registry

    def review(
        self,
        pr: dict[str, Any],
        policy_results: list[dict[str, Any]],
        terraform_context: dict[str, Any],
        changed_files: list[str],
    ) -> list[dict[str, Any]]:
        user_prompt = _USER_TEMPLATE.format(
            pr_number=pr.get("number", "?"),
            source_branch=pr.get("source_branch", "?"),
            target_branch=pr.get("target_branch", "main"),
            changed_files="\n".join(changed_files) if changed_files else "(none)",
            tf_context=json.dumps(terraform_context, indent=2),
            policy_results=self._format_policy_results(policy_results),
        )

        try:
            output = self._llm.generate_structured_output(
                schema=FindingsOutput,
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=redact(user_prompt),
            )
            findings = [f.model_dump() for f in output.findings]
            log.info("peer_review_agent.findings", count=len(findings))
            return findings
        except Exception as exc:  # noqa: BLE001
            log.error("peer_review_agent.failed", error=str(exc))
            return []

    def _format_policy_results(self, results: list[dict[str, Any]]) -> str:
        if not results:
            return "(no scanner results)"
        lines = []
        for r in results:
            status = r.get("status", "?")
            tool = r.get("tool", "?")
            rule = r.get("rule_id", "?")
            file_path = r.get("file_path", "")
            msg = r.get("message", "")
            lines.append(f"[{status.upper()}] {tool}/{rule} {file_path}: {msg}")
        return "\n".join(lines)
