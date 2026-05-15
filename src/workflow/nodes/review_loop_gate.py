"""review_loop_gate node: decide whether to continue auto-fix, wait for human, or proceed.

Returns a routing key consumed by the graph's conditional edge.
"""

from __future__ import annotations

import structlog

from src.observability.audit import record_decision
from src.policy.risk_policy import RiskPolicy
from src.workflow.state import WorkflowState

log = structlog.get_logger(__name__)

ROUTE_CONTINUE_LOOP = "continue_loop"
ROUTE_WAIT_HUMAN = "wait_human"
ROUTE_PROCEED = "proceed"
ROUTE_FAIL = "fail"


def review_loop_gate(state: WorkflowState) -> str:
    run_id = state.get("run_id", "unknown")
    config = state.get("config") or {}
    findings = state.get("findings") or []
    iterations = state.get("auto_fix_iterations", 0)

    policy = RiskPolicy(config)

    if policy.is_loop_limit_reached(iterations, config):
        log.warning("node.review_loop_gate.loop_limit_reached", run_id=run_id, iterations=iterations)
        record_decision(run_id, "review_loop_gate", ROUTE_WAIT_HUMAN, "Loop limit reached")
        return ROUTE_WAIT_HUMAN

    still_auto_fixable = policy.auto_fixable_findings(findings)
    if still_auto_fixable:
        log.info("node.review_loop_gate.continue_loop", run_id=run_id, remaining=len(still_auto_fixable))
        record_decision(run_id, "review_loop_gate", ROUTE_CONTINUE_LOOP)
        return ROUTE_CONTINUE_LOOP

    blocking = policy.blocks_merge(findings)
    if blocking:
        log.info("node.review_loop_gate.blocking_findings", run_id=run_id, blocking=len(blocking))
        record_decision(
            run_id,
            "review_loop_gate",
            ROUTE_WAIT_HUMAN,
            f"{len(blocking)} blocking finding(s) require human review",
        )
        return ROUTE_WAIT_HUMAN

    log.info("node.review_loop_gate.proceed", run_id=run_id)
    record_decision(run_id, "review_loop_gate", ROUTE_PROCEED, "No blocking findings remain")
    return ROUTE_PROCEED
