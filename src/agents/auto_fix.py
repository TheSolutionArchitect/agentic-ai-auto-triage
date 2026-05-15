"""Auto Fix Agent: generate and validate narrowly-scoped Terraform patches."""

from __future__ import annotations

import re
from typing import Any

import structlog
from pydantic import BaseModel, Field

from src.integrations.secrets import redact

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """You are a Terraform auto-remediation agent.
Your task is to produce minimal, targeted fixes for specific IaC findings.

Strict rules:
- Fix only the exact finding described. Do not refactor, rename, or clean up other code.
- Output a unified diff for the single file containing the issue.
- The diff must be valid and apply cleanly with `patch -p1`.
- Never change backend, state, IAM privilege, or provider version unless that is the specific finding.
- If you cannot produce a safe, targeted fix, set applied=false and explain why.
- Never include secrets, credentials, or real values in your output.
"""

_FIX_TEMPLATE = """Finding to fix:
{finding_json}

File content (relevant excerpt):
{file_content}

Constraints:
{constraints}

Return a JSON object:
{{
  "finding_id": string,
  "applied": boolean,
  "patch": string | null,  (unified diff, null if not applied)
  "reason": string,
  "files_changed": [string]
}}
"""


class FixResult(BaseModel):
    finding_id: str
    applied: bool
    patch: str | None = None
    reason: str
    files_changed: list[str] = Field(default_factory=list)


class FixBatch(BaseModel):
    results: list[FixResult]


_NEVER_FIX_CATEGORIES = {
    "terraform_backend_change",
    "state_migration",
    "resource_destroy",
    "iam_privilege_expansion",
    "public_network_exposure",
    "provider_major_upgrade",
    "production_database_change",
}


class AutoFixAgent:
    def __init__(self, llm: Any, tool_registry: Any | None = None) -> None:
        self._llm = llm
        self._registry = tool_registry

    def apply_fixes(
        self,
        findings: list[dict[str, Any]],
        pr: dict[str, Any],
        changed_files: list[str],
        max_files: int = 10,
    ) -> dict[str, Any]:
        resolved_ids: list[str] = []
        all_files_changed: list[str] = []
        patches_applied: list[str] = []

        for finding in findings:
            if finding.get("category") in _NEVER_FIX_CATEGORIES:
                log.warning("auto_fix.skip_never_fix", category=finding.get("category"))
                continue

            file_path = finding.get("file_path", "")
            if len(all_files_changed) >= max_files and file_path not in all_files_changed:
                log.warning("auto_fix.max_files_reached", max_files=max_files)
                break

            file_content = self._fetch_file_content(file_path, pr)
            constraints = self._build_constraints(finding)

            result = self._generate_fix(finding, file_content, constraints)

            if result and result.applied and result.patch:
                if self._validate_patch(result.patch, file_path):
                    self._commit_patch(result.patch, file_path, finding, pr)
                    resolved_ids.append(finding.get("id", ""))
                    all_files_changed.extend(result.files_changed)
                    patches_applied.append(result.patch)
                    log.info("auto_fix.applied", finding_id=finding.get("id"), file=file_path)
                else:
                    log.warning("auto_fix.patch_invalid", finding_id=finding.get("id"))
            else:
                reason = result.reason if result else "LLM returned no result"
                log.info("auto_fix.not_applied", finding_id=finding.get("id"), reason=reason)

        return {
            "resolved_finding_ids": resolved_ids,
            "files_changed": list(set(all_files_changed)),
            "patches_applied": len(patches_applied),
        }

    def _generate_fix(
        self,
        finding: dict[str, Any],
        file_content: str,
        constraints: list[str],
    ) -> FixResult | None:
        import json

        prompt = _FIX_TEMPLATE.format(
            finding_json=json.dumps(finding, indent=2),
            file_content=redact(file_content[:3000]),
            constraints="\n".join(f"- {c}" for c in constraints),
        )
        try:
            output = self._llm.generate_structured_output(
                schema=FixResult,
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=prompt,
            )
            return output
        except Exception as exc:  # noqa: BLE001
            log.error("auto_fix.llm_error", error=str(exc))
            return None

    def _build_constraints(self, finding: dict[str, Any]) -> list[str]:
        constraints = [
            "Do not change any file other than the one containing the finding",
            "Do not modify backend, state, or provider version blocks",
            "Do not expand IAM permissions",
            "The resulting Terraform must pass terraform validate",
        ]
        category = finding.get("category", "")
        if "encryption" in category.lower():
            constraints.append("Ensure encryption is enabled, not just present")
        return constraints

    def _validate_patch(self, patch: str, file_path: str) -> bool:
        """Basic patch sanity checks before committing. Full validate runs in CI."""
        if not patch.strip():
            return False
        if "--- " not in patch or "+++ " not in patch:
            return False
        # Ensure patch only touches expected file
        touched = re.findall(r"^[+-]{3}\s+(\S+)", patch, re.MULTILINE)
        for t in touched:
            clean = t.lstrip("ab/")
            if clean not in (file_path, "/dev/null"):
                log.warning("auto_fix.patch_touches_unexpected_file", file=clean)
                return False
        return True

    def _fetch_file_content(self, file_path: str, pr: dict[str, Any]) -> str:
        """Fetch file content from GitHub via MCP. Returns empty string if unavailable."""
        if not self._registry:
            return ""
        try:
            _ = self._registry.get("github")
            # When MCP connected: call github.repos.get_content(path, ref=pr.head_sha)
        except KeyError:
            pass
        return ""

    def _commit_patch(
        self,
        patch: str,
        file_path: str,
        finding: dict[str, Any],
        pr: dict[str, Any],
    ) -> None:
        """Push the fix to the PR branch via GitHub MCP using bot identity."""
        if not self._registry:
            return
        try:
            _ = self._registry.get("github")
            # When MCP connected: call github.pull_requests.update_branch with patch
            log.debug("auto_fix.would_commit", file=file_path, finding_id=finding.get("id"))
        except KeyError:
            pass
