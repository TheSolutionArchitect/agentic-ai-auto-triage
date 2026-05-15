"""Approval gate policy engine.

Determines whether workflow gates are open based on recorded approvals.
No LLM involvement — all decisions are deterministic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from src.workflow.state import Approval, ApprovalScope, Environment

log = structlog.get_logger(__name__)


class ApprovalPolicy:
    def __init__(self, config: dict[str, Any]) -> None:
        workflow = config.get("workflow", {})
        self._require_pr_approval: bool = workflow.get("require_pr_approval", True)
        self._require_plan_approval: dict[str, bool] = workflow.get(
            "require_plan_approval",
            {"dev": False, "test": True, "stage": True, "prod": True},
        )

    def requires_pr_approval(self) -> bool:
        return self._require_pr_approval

    def requires_plan_approval(self, environment: Environment) -> bool:
        return self._require_plan_approval.get(environment, True)

    def is_pr_approved(self, approvals: list[Approval]) -> bool:
        return self._has_valid_approval(approvals, scope="pr_review")

    def is_plan_approved(self, approvals: list[Approval]) -> bool:
        return self._has_valid_approval(approvals, scope="terraform_plan")

    def _has_valid_approval(self, approvals: list[Approval], scope: ApprovalScope) -> bool:
        for approval in approvals:
            if approval.get("scope") != scope:
                continue
            if approval.get("decision") not in ("approve", "approve_with_exception"):
                continue
            if self._is_expired(approval):
                log.warning("approval_policy.expired", scope=scope, approver=approval.get("approver"))
                continue
            return True
        return False

    def _is_expired(self, approval: Approval) -> bool:
        expires_at = approval.get("expires_at")
        if not expires_at:
            return False
        try:
            expiry = datetime.fromisoformat(expires_at)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) > expiry
        except ValueError:
            return False

    def gate_status(
        self,
        approvals: list[Approval],
        environment: Environment,
        pr_findings_blocking: bool,
    ) -> dict[str, Any]:
        """Return a summary of all gate states for observability."""
        return {
            "pr_gate": {
                "required": self.requires_pr_approval(),
                "approved": self.is_pr_approved(approvals),
                "has_blocking_findings": pr_findings_blocking,
            },
            "plan_gate": {
                "required": self.requires_plan_approval(environment),
                "approved": self.is_plan_approved(approvals),
            },
        }

    def validate_approval_payload(self, payload: dict[str, Any]) -> list[str]:
        """Return a list of validation error messages (empty = valid)."""
        errors: list[str] = []
        for field in ("decision", "approver", "scope", "timestamp"):
            if not payload.get(field):
                errors.append(f"Missing required field: {field}")
        valid_decisions = {"approve", "reject", "request_changes", "defer", "approve_with_exception"}
        if payload.get("decision") not in valid_decisions:
            errors.append(f"Invalid decision: {payload.get('decision')!r}")
        valid_scopes = {"pr_review", "terraform_plan", "auto_fix", "exception", "break_glass"}
        if payload.get("scope") not in valid_scopes:
            errors.append(f"Invalid scope: {payload.get('scope')!r}")
        return errors
