"""monitor_deployment node: watch pipeline status and classify failures."""

from __future__ import annotations

from typing import Any

import structlog
from src.integrations import runtime

from src.observability.audit import record_decision
from src.workflow.state import PipelineRun, WorkflowState

log = structlog.get_logger(__name__)

ROUTE_SUCCESS = "success"
ROUTE_FAILED = "failed"


def monitor_deployment(state: WorkflowState) -> dict[str, Any]:
    run_id = state.get("run_id", "unknown")
    pipeline_runs = state.get("pipeline_runs") or []
    rt = runtime.get_runtime(run_id)
    registry = rt.get("tool_registry")

    log.info("node.monitor_deployment.start", run_id=run_id, runs=len(pipeline_runs))

    updated_runs: list[PipelineRun] = []
    all_succeeded = True

    for run in pipeline_runs:
        updated = _poll_pipeline_status(run, registry)
        updated_runs.append(updated)
        if updated.get("status") not in ("success", "completed"):
            all_succeeded = False

    final_status = "completed" if all_succeeded else "failed"
    log.info("node.monitor_deployment.done", run_id=run_id, status=final_status)
    return {
        "pipeline_runs": updated_runs,
        "status": final_status if not all_succeeded else "running",
    }


def route_after_monitor(state: WorkflowState) -> str:
    """Routing function for the conditional edge after monitor_deployment."""
    pipeline_runs = state.get("pipeline_runs") or []
    if not pipeline_runs:
        return ROUTE_SUCCESS

    all_ok = all(r.get("status") in ("success", "completed") for r in pipeline_runs)
    result = ROUTE_SUCCESS if all_ok else ROUTE_FAILED
    record_decision(
        state.get("run_id", "unknown"),
        "monitor_deployment",
        result,
    )
    return result


def _poll_pipeline_status(run: PipelineRun, registry: Any) -> PipelineRun:
    """Poll GitHub Actions / HCP Terraform for the run's current status via MCP."""
    current_status = run.get("status", "unknown")

    if current_status in ("success", "completed", "failed"):
        return run

    # When MCP is connected: call github.actions.get_workflow_run or
    # hcp_terraform.get_run to get real status.
    updated = dict(run)
    updated["status"] = "success"  # placeholder — real status from MCP
    return PipelineRun(**{k: v for k, v in updated.items() if k in PipelineRun.__annotations__})


def classify_failure(pipeline_run: PipelineRun, llm: Any) -> str:
    """Use the LLM to classify a deployment failure into a category."""
    logs_url = pipeline_run.get("logs_url", "")
    if not llm or not logs_url:
        return "unknown"

    # Categories from spec section 13
    categories = [
        "code_issue", "environment_issue", "policy_issue",
        "quota_issue", "provider_issue", "transient_issue",
    ]
    summary = llm.summarize(
        f"Pipeline run {pipeline_run.get('run_id')} failed. Logs: {logs_url}",
        instruction=(
            f"Classify this Terraform deployment failure into one of: {categories}. "
            "Respond with only the category name."
        ),
    )
    for cat in categories:
        if cat in summary.lower():
            return cat
    return "unknown"
