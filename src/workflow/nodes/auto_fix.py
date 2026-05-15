"""auto_fix node: apply policy-gated code fixes and commit them to the PR branch."""

from __future__ import annotations

from typing import Any

import structlog

from src.agents.auto_fix import AutoFixAgent
from src.integrations import runtime
from src.policy.risk_policy import RiskPolicy
from src.workflow.state import WorkflowState

log = structlog.get_logger(__name__)


def auto_fix(state: WorkflowState) -> dict[str, Any]:
    run_id = state.get("run_id", "unknown")
    config = state.get("config") or {}
    findings = state.get("findings") or []
    iterations = state.get("auto_fix_iterations", 0)

    rt = runtime.get_runtime(run_id)
    llm = rt.get("llm_provider")
    registry = rt.get("tool_registry")

    log.info("node.auto_fix.start", run_id=run_id, iteration=iterations)

    policy = RiskPolicy(config)
    eligible = policy.auto_fixable_findings(findings)

    if not eligible:
        log.info("node.auto_fix.no_eligible", run_id=run_id)
        return {"auto_fix_iterations": iterations + 1}

    if not llm:
        log.warning("node.auto_fix.no_llm", run_id=run_id)
        return {"auto_fix_iterations": iterations + 1}

    max_files = policy.max_files_per_auto_fix(config)
    agent = AutoFixAgent(llm=llm, tool_registry=registry)

    pr = state.get("pull_request") or {}
    changed_files = state.get("changed_files") or []

    result = agent.apply_fixes(
        findings=eligible,
        pr=pr,
        changed_files=changed_files,
        max_files=max_files,
    )

    resolved_ids = set(result.get("resolved_finding_ids", []))
    updated_findings = []
    for f in findings:
        f = dict(f)  # type: ignore[assignment]
        if f.get("id") in resolved_ids:
            f["resolved"] = True
            f["resolution_notes"] = "Auto-fixed by agent"
        updated_findings.append(f)

    log.info(
        "node.auto_fix.done",
        run_id=run_id,
        resolved=len(resolved_ids),
        files_changed=result.get("files_changed", []),
    )
    return {
        "findings": updated_findings,
        "auto_fix_iterations": iterations + 1,
    }
