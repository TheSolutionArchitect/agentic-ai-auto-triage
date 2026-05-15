"""classify_findings node: apply risk policy to produce the fix plan."""

from __future__ import annotations

from typing import Any

import structlog

from src.policy.risk_policy import RiskPolicy
from src.workflow.state import Finding, WorkflowState

log = structlog.get_logger(__name__)


def classify_findings(state: WorkflowState) -> dict[str, Any]:
    run_id = state.get("run_id", "unknown")
    config = state.get("config") or {}
    findings = state.get("findings") or []

    log.info("node.classify_findings.start", run_id=run_id, findings=len(findings))

    policy = RiskPolicy(config)

    # Stamp each finding with its fix_type based on deterministic policy
    classified: list[Finding] = []
    for f in findings:
        f = dict(f)  # type: ignore[assignment]
        if policy.is_auto_fixable(f):  # type: ignore[arg-type]
            f["fix_type"] = "auto_fix_allowed"
        elif f.get("severity") == "LOW" and f.get("confidence", 0) < 0.5:
            f["fix_type"] = "informational"
        else:
            f["fix_type"] = "human_required"
        classified.append(Finding(**{k: v for k, v in f.items() if k in Finding.__annotations__}))

    auto_fixable = policy.auto_fixable_findings(classified)
    blocking = policy.blocks_merge(classified)

    log.info(
        "node.classify_findings.done",
        run_id=run_id,
        auto_fixable=len(auto_fixable),
        blocking_merge=len(blocking),
    )
    # Replace findings with classified set (not append, so reset the list)
    return {"findings": classified}
