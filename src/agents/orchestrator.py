"""Orchestrator Agent: owns all workflow routing decisions.

The orchestrator reads state, evaluates gates, and delegates to nodes.
It never performs direct tool work — that belongs to the specialized nodes.
"""

from __future__ import annotations

from typing import Any

import structlog

from src.observability.audit import record_decision
from src.policy.approval_policy import ApprovalPolicy
from src.policy.risk_policy import RiskPolicy
from src.workflow.state import WorkflowState

log = structlog.get_logger(__name__)


class Orchestrator:
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._risk = RiskPolicy(config)
        self._approval = ApprovalPolicy(config)

    def should_trigger_pr_flow(self, state: WorkflowState) -> bool:
        event = state.get("event_type", "")
        return event in ("pr_opened", "pr_updated")

    def should_enter_review_loop(self, state: WorkflowState) -> bool:
        changed_files = state.get("changed_files") or []
        return bool(changed_files)

    def should_auto_fix(self, state: WorkflowState) -> bool:
        findings = state.get("findings") or []
        return bool(self._risk.auto_fixable_findings(findings))

    def is_loop_exhausted(self, state: WorkflowState) -> bool:
        return self._risk.is_loop_limit_reached(
            state.get("auto_fix_iterations", 0), self._config
        )

    def has_blocking_findings(self, state: WorkflowState) -> bool:
        findings = state.get("findings") or []
        return bool(self._risk.blocks_merge(findings))

    def pr_approval_satisfied(self, state: WorkflowState) -> bool:
        if not self._approval.requires_pr_approval():
            return True
        return self._approval.is_pr_approved(state.get("approvals") or [])

    def plan_approval_satisfied(self, state: WorkflowState) -> bool:
        environment = (state.get("environment") or {}).get("name", "dev")
        if not self._approval.requires_plan_approval(environment):  # type: ignore[arg-type]
            return True
        return self._approval.is_plan_approved(state.get("approvals") or [])

    def gate_status(self, state: WorkflowState) -> dict[str, Any]:
        environment = (state.get("environment") or {}).get("name", "dev")
        findings = state.get("findings") or []
        return self._approval.gate_status(
            approvals=state.get("approvals") or [],
            environment=environment,  # type: ignore[arg-type]
            pr_findings_blocking=bool(self._risk.blocks_merge(findings)),
        )

    def log_gate_status(self, state: WorkflowState) -> None:
        gates = self.gate_status(state)
        run_id = state.get("run_id", "unknown")
        record_decision(run_id, "orchestrator", "gate_status_check", gates=str(gates))
        log.info("orchestrator.gate_status", run_id=run_id, gates=gates)
