"""Approval nodes: interrupt the graph and collect structured human decisions.

Uses LangGraph's interrupt() to pause execution. The graph resumes when the
API layer receives a callback and calls graph.invoke(Command(resume=decision)).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from langgraph.types import interrupt

from src.observability.audit import record_approval
from src.policy.approval_policy import ApprovalPolicy
from src.workflow.state import Approval, WorkflowState

log = structlog.get_logger(__name__)


def request_pr_approval(state: WorkflowState) -> dict[str, Any]:
    run_id = state.get("run_id", "unknown")
    pr = state.get("pull_request") or {}
    findings = state.get("findings") or []
    jira_issues = state.get("jira_issues") or []
    slack_thread = state.get("slack_thread") or {}

    log.info("node.request_pr_approval.interrupt", run_id=run_id)

    active = [f for f in findings if not f.get("resolved")]
    high_count = sum(1 for f in active if f.get("severity") == "HIGH")
    medium_count = sum(1 for f in active if f.get("severity") == "MEDIUM")

    # Pause the graph — the runtime resumes with a decision dict
    raw_decision: dict[str, Any] = interrupt({
        "type": "pr_approval_request",
        "run_id": run_id,
        "pr_url": pr.get("url"),
        "pr_number": pr.get("number"),
        "head_sha": pr.get("head_sha"),
        "findings_summary": {
            "total": len(active),
            "high": high_count,
            "medium": medium_count,
        },
        "jira_issues": [i.get("key") for i in jira_issues],
        "slack_thread_url": slack_thread.get("url"),
        "instructions": (
            "Review the PR, agent findings, and JIRA issues. "
            "Respond with: {decision, approver, comment, scope='pr_review', expires_at?}"
        ),
    })

    approval = _build_approval(raw_decision, scope="pr_review", run_id=run_id)
    record_approval(run_id, approval)

    log.info(
        "node.request_pr_approval.resumed",
        run_id=run_id,
        decision=approval.get("decision"),
        approver=approval.get("approver"),
    )
    return {"approvals": [approval]}


def request_plan_approval(state: WorkflowState) -> dict[str, Any]:
    run_id = state.get("run_id", "unknown")
    pr = state.get("pull_request") or {}
    plan_summary = state.get("plan_summary") or {}
    environment = (state.get("environment") or {}).get("name", "dev")
    config = state.get("config") or {}

    policy = ApprovalPolicy(config)
    if not policy.requires_plan_approval(environment):  # type: ignore[arg-type]
        log.info("node.request_plan_approval.skipped", run_id=run_id, environment=environment)
        return {}

    log.info("node.request_plan_approval.interrupt", run_id=run_id, environment=environment)

    raw_decision: dict[str, Any] = interrupt({
        "type": "plan_approval_request",
        "run_id": run_id,
        "environment": environment,
        "pr_url": pr.get("url"),
        "plan_summary": plan_summary,
        "plan_artifact_url": plan_summary.get("plan_artifact_url"),
        "instructions": (
            "Review the Terraform plan artifact and risk summary. "
            "Respond with: {decision, approver, comment, scope='terraform_plan', expires_at?}"
        ),
    })

    approval = _build_approval(raw_decision, scope="terraform_plan", run_id=run_id)
    record_approval(run_id, approval)

    log.info(
        "node.request_plan_approval.resumed",
        run_id=run_id,
        decision=approval.get("decision"),
    )
    return {"approvals": [approval]}


def _build_approval(raw: dict[str, Any], scope: str, run_id: str) -> Approval:
    return Approval(
        decision=raw.get("decision", "reject"),
        approver=raw.get("approver", "unknown"),
        comment=raw.get("comment", ""),
        scope=scope,  # type: ignore[arg-type]
        timestamp=raw.get("timestamp", datetime.now(timezone.utc).isoformat()),
        expires_at=raw.get("expires_at", ""),
        run_id=run_id,
    )
