"""route_event node: central dispatch based on event_type.

Returns a routing key consumed by the conditional edge in graph.py.
The orchestrator owns all routing — individual nodes never redirect flow.
"""

from __future__ import annotations

import structlog

from src.workflow.state import WorkflowState

log = structlog.get_logger(__name__)

# Routing key constants consumed by graph.py conditional edges
ROUTE_BRANCH_PUSH = "branch_push"
ROUTE_PR = "pr"
ROUTE_APPROVAL = "approval_received"
ROUTE_PIPELINE = "pipeline_event"
ROUTE_MONITOR = "monitor_event"
ROUTE_UNKNOWN = "unknown"


def route_event(state: WorkflowState) -> str:
    event_type = state.get("event_type", "")
    run_id = state.get("run_id", "unknown")
    log.info("node.route_event", run_id=run_id, event_type=event_type)

    if event_type == "branch_push":
        return ROUTE_BRANCH_PUSH
    elif event_type in ("pr_opened", "pr_updated"):
        return ROUTE_PR
    elif event_type == "approval_received":
        return ROUTE_APPROVAL
    elif event_type == "pipeline_event":
        return ROUTE_PIPELINE
    elif event_type == "monitor_event":
        return ROUTE_MONITOR
    else:
        log.warning("node.route_event.unknown", run_id=run_id, event_type=event_type)
        return ROUTE_UNKNOWN
