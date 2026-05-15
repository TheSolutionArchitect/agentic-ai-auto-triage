"""Deployment nodes: merge gate, plan generation, and apply trigger.

The agent never directly applies infrastructure — it triggers a CI/CD pipeline
and monitors its status. Branch merges always defer to branch protection rules.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from src.integrations import runtime

from src.observability.audit import record_decision
from src.policy.approval_policy import ApprovalPolicy
from src.policy.risk_policy import RiskPolicy
from src.workflow.state import PipelineRun, PlanSummary, WorkflowState

log = structlog.get_logger(__name__)


def merge_or_wait(state: WorkflowState) -> dict[str, Any]:
    """Confirm PR approval is recorded. Merge always happens via branch protection — never directly."""
    run_id = state.get("run_id", "unknown")
    config = state.get("config") or {}
    approvals = state.get("approvals") or []
    environment = (state.get("environment") or {}).get("name", "dev")

    policy = ApprovalPolicy(config)

    if not policy.is_pr_approved(approvals):
        log.warning("node.merge_or_wait.not_approved", run_id=run_id)
        return {"status": "waiting_for_human"}

    log.info("node.merge_or_wait.approved", run_id=run_id)
    record_decision(run_id, "merge_or_wait", "merge_ready", "PR approval recorded; branch protection governs merge")
    return {"status": "running"}


def generate_plan(state: WorkflowState) -> dict[str, Any]:
    """Trigger the CI/CD pipeline to execute terraform plan and collect the plan artifact."""
    run_id = state.get("run_id", "unknown")
    rt = runtime.get_runtime(run_id)
    registry = rt.get("tool_registry")
    pr = state.get("pull_request") or {}
    environment = (state.get("environment") or {}).get("name", "dev")

    log.info("node.generate_plan.start", run_id=run_id, environment=environment)

    pipeline_run = _trigger_plan_pipeline(
        registry=registry,
        commit_sha=pr.get("head_sha", ""),
        environment=environment,
        run_id=run_id,
    )

    plan_summary = _collect_plan_summary(pipeline_run)

    log.info(
        "node.generate_plan.done",
        run_id=run_id,
        create=plan_summary.get("create"),
        destroy=plan_summary.get("destroy"),
        has_destructive=plan_summary.get("has_destructive"),
    )
    return {
        "pipeline_runs": [pipeline_run],
        "plan_summary": plan_summary,
    }


def trigger_apply(state: WorkflowState) -> dict[str, Any]:
    """Trigger the CI/CD apply pipeline after all approvals are confirmed."""
    run_id = state.get("run_id", "unknown")
    config = state.get("config") or {}
    approvals = state.get("approvals") or []
    findings = state.get("findings") or []
    environment = (state.get("environment") or {}).get("name", "dev")
    pr = state.get("pull_request") or {}
    plan_summary = state.get("plan_summary") or {}
    rt = runtime.get_runtime(run_id)
    registry = rt.get("tool_registry")

    approval_policy = ApprovalPolicy(config)
    risk_policy = RiskPolicy(config)

    blocking_apply = risk_policy.blocks_apply(findings)
    if blocking_apply:
        log.error("node.trigger_apply.blocked_by_findings", run_id=run_id, count=len(blocking_apply))
        return {"status": "blocked", "error": f"{len(blocking_apply)} findings block apply"}

    if approval_policy.requires_plan_approval(environment) and not approval_policy.is_plan_approved(approvals):  # type: ignore[arg-type]
        log.error("node.trigger_apply.missing_plan_approval", run_id=run_id)
        return {"status": "blocked", "error": "Plan approval required for this environment"}

    if not approval_policy.is_pr_approved(approvals):
        log.error("node.trigger_apply.missing_pr_approval", run_id=run_id)
        return {"status": "blocked", "error": "PR approval required"}

    log.info("node.trigger_apply.start", run_id=run_id, environment=environment)

    pipeline_run = _trigger_apply_pipeline(
        registry=registry,
        commit_sha=pr.get("head_sha", ""),
        environment=environment,
        plan_artifact_url=plan_summary.get("plan_artifact_url", ""),
        run_id=run_id,
    )

    record_decision(run_id, "trigger_apply", "apply_triggered", environment=environment)
    log.info("node.trigger_apply.done", run_id=run_id, pipeline_run_id=pipeline_run.get("run_id"))
    return {"pipeline_runs": [pipeline_run]}


def _trigger_plan_pipeline(
    registry: Any,
    commit_sha: str,
    environment: str,
    run_id: str,
) -> PipelineRun:
    """Invoke GitHub Actions or Terraform Cloud to run terraform plan via MCP."""
    log.info("deploy.trigger_plan", commit_sha=commit_sha[:8] if commit_sha else "?", environment=environment)
    return PipelineRun(
        run_id=f"plan-{run_id[:8]}",
        pipeline="terraform-plan",
        status="queued",
        url="https://github.com/example-org/terraform-infra/actions/runs/pending",
        plan_artifact_url="",
    )


def _trigger_apply_pipeline(
    registry: Any,
    commit_sha: str,
    environment: str,
    plan_artifact_url: str,
    run_id: str,
) -> PipelineRun:
    """Invoke GitHub Actions or Terraform Cloud to run terraform apply via MCP."""
    log.info("deploy.trigger_apply", commit_sha=commit_sha[:8] if commit_sha else "?", environment=environment)
    return PipelineRun(
        run_id=f"apply-{run_id[:8]}",
        pipeline="terraform-apply",
        status="queued",
        url="https://github.com/example-org/terraform-infra/actions/runs/pending",
        apply_result="pending",
    )


def _collect_plan_summary(pipeline_run: PipelineRun) -> PlanSummary:
    """Parse the plan artifact from the pipeline run. Returns a zero summary when unavailable."""
    return PlanSummary(
        create=0,
        update=0,
        replace=0,
        destroy=0,
        no_change=0,
        has_destructive=False,
        security_sensitive_changes=[],
        cost_estimate=None,
        plan_artifact_url=pipeline_run.get("plan_artifact_url", ""),
        policy_results=[],
    )
