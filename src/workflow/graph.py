"""LangGraph workflow graph assembly.

Defines the full directed graph for the Terraform agentic DevOps workflow.
Every routing decision goes through a conditional edge — nodes never redirect.
Human approval points pause the graph using LangGraph interrupts and resume
when the API receives a callback.
"""

from __future__ import annotations

from typing import Any

import structlog
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from src.workflow.nodes.approvals import request_plan_approval, request_pr_approval
from src.workflow.nodes.auto_fix import auto_fix
from src.workflow.nodes.branch_checks import run_branch_checks
from src.workflow.nodes.classify_findings import classify_findings
from src.workflow.nodes.deploy import generate_plan, merge_or_wait, trigger_apply
from src.workflow.nodes.evidence import finalize_evidence
from src.workflow.nodes.initialize_tools import initialize_tools
from src.workflow.nodes.load_config import load_config
from src.workflow.nodes.monitor import monitor_deployment, route_after_monitor
from src.workflow.nodes.notify import notify
from src.workflow.nodes.peer_review import peer_review
from src.workflow.nodes.pr_checks import run_pr_checks
from src.workflow.nodes.repo_context import collect_repo_context
from src.workflow.nodes.review_loop_gate import (
    ROUTE_CONTINUE_LOOP,
    ROUTE_FAIL,
    ROUTE_PROCEED,
    ROUTE_WAIT_HUMAN,
    review_loop_gate,
)
from src.workflow.nodes.route_event import (
    ROUTE_APPROVAL,
    ROUTE_BRANCH_PUSH,
    ROUTE_MONITOR,
    ROUTE_PIPELINE,
    ROUTE_PR,
    ROUTE_UNKNOWN,
    route_event,
)
from src.workflow.nodes.update_itsm import update_itsm
from src.workflow.state import WorkflowState

log = structlog.get_logger(__name__)

# ── Sentinel node names ──────────────────────────────────────────────────────
N_LOAD_CONFIG = "load_config"
N_INIT_TOOLS = "initialize_tools"
N_ROUTE_EVENT = "route_event"
N_REPO_CONTEXT = "collect_repo_context"
N_BRANCH_CHECKS = "run_branch_checks"
N_PR_CHECKS = "run_pr_checks"
N_PEER_REVIEW = "peer_review"
N_CLASSIFY = "classify_findings"
N_AUTO_FIX = "auto_fix"
N_UPDATE_ITSM = "update_itsm"
N_LOOP_GATE = "review_loop_gate"
N_PR_APPROVAL = "request_pr_approval"
N_MERGE_OR_WAIT = "merge_or_wait"
N_GENERATE_PLAN = "generate_plan"
N_PLAN_APPROVAL = "request_plan_approval"
N_TRIGGER_APPLY = "trigger_apply"
N_MONITOR = "monitor_deployment"
N_EVIDENCE = "finalize_evidence"
N_NOTIFY = "notify"


def build_graph(checkpointer: Any = None) -> Any:
    """Compile and return the LangGraph StateGraph.

    Args:
        checkpointer: LangGraph checkpointer (AsyncPostgresSaver or MemorySaver).
                      When None, MemorySaver is used (dev/test only).
    """
    if checkpointer is None:
        checkpointer = MemorySaver()
        log.warning("graph.using_memory_saver", note="Use AsyncPostgresSaver in production")

    graph = StateGraph(WorkflowState)

    # ── Register all nodes ───────────────────────────────────────────────────
    graph.add_node(N_LOAD_CONFIG, load_config)
    graph.add_node(N_INIT_TOOLS, initialize_tools)
    graph.add_node(N_ROUTE_EVENT, _noop)          # routing only — no state update
    graph.add_node(N_REPO_CONTEXT, collect_repo_context)
    graph.add_node(N_BRANCH_CHECKS, run_branch_checks)
    graph.add_node(N_PR_CHECKS, run_pr_checks)
    graph.add_node(N_PEER_REVIEW, peer_review)
    graph.add_node(N_CLASSIFY, classify_findings)
    graph.add_node(N_AUTO_FIX, auto_fix)
    graph.add_node(N_UPDATE_ITSM, update_itsm)
    graph.add_node(N_LOOP_GATE, _noop)            # routing only
    graph.add_node(N_PR_APPROVAL, request_pr_approval)
    graph.add_node(N_MERGE_OR_WAIT, merge_or_wait)
    graph.add_node(N_GENERATE_PLAN, generate_plan)
    graph.add_node(N_PLAN_APPROVAL, request_plan_approval)
    graph.add_node(N_TRIGGER_APPLY, trigger_apply)
    graph.add_node(N_MONITOR, monitor_deployment)
    graph.add_node(N_EVIDENCE, finalize_evidence)
    graph.add_node(N_NOTIFY, notify)

    # ── Entry: always load config then init tools ────────────────────────────
    graph.add_edge(START, N_LOAD_CONFIG)
    graph.add_edge(N_LOAD_CONFIG, N_INIT_TOOLS)
    graph.add_edge(N_INIT_TOOLS, N_ROUTE_EVENT)

    # ── Event routing ────────────────────────────────────────────────────────
    graph.add_conditional_edges(
        N_ROUTE_EVENT,
        route_event,
        {
            ROUTE_BRANCH_PUSH: N_REPO_CONTEXT,
            ROUTE_PR: N_REPO_CONTEXT,
            ROUTE_APPROVAL: N_MONITOR,       # approval callbacks wake up via Command(resume=)
            ROUTE_PIPELINE: N_MONITOR,
            ROUTE_MONITOR: N_MONITOR,
            ROUTE_UNKNOWN: N_NOTIFY,
        },
    )

    # ── Branch push flow ─────────────────────────────────────────────────────
    # branch_push: context → branch checks → notify → END
    graph.add_edge(N_BRANCH_CHECKS, N_NOTIFY)

    # ── PR flow: repo context → checks → review loop ─────────────────────────
    graph.add_conditional_edges(
        N_REPO_CONTEXT,
        _route_after_context,
        {
            "pr": N_PR_CHECKS,
            "branch": N_BRANCH_CHECKS,
        },
    )
    graph.add_edge(N_PR_CHECKS, N_PEER_REVIEW)
    graph.add_edge(N_PEER_REVIEW, N_CLASSIFY)
    graph.add_edge(N_CLASSIFY, N_LOOP_GATE)

    # ── Review-and-fix loop ──────────────────────────────────────────────────
    graph.add_conditional_edges(
        N_LOOP_GATE,
        review_loop_gate,
        {
            ROUTE_CONTINUE_LOOP: N_AUTO_FIX,
            ROUTE_WAIT_HUMAN: N_UPDATE_ITSM,   # human review after ITSM update
            ROUTE_PROCEED: N_UPDATE_ITSM,
            ROUTE_FAIL: N_NOTIFY,
        },
    )
    graph.add_edge(N_AUTO_FIX, N_UPDATE_ITSM)
    graph.add_conditional_edges(
        N_UPDATE_ITSM,
        _route_after_itsm,
        {
            "loop": N_PR_CHECKS,               # continue review loop
            "proceed": N_PR_APPROVAL,           # all clear → PR approval gate
            "wait": N_NOTIFY,                  # human review needed → notify + wait
        },
    )

    # ── PR approval gate (human interrupt) ────────────────────────────────────
    graph.add_edge(N_PR_APPROVAL, N_NOTIFY)
    graph.add_conditional_edges(
        N_NOTIFY,
        _route_after_notify,
        {
            "merge": N_MERGE_OR_WAIT,
            "wait": END,                       # graph paused; resumes on approval callback
            "done": N_EVIDENCE,
            "failed": N_EVIDENCE,
            "continue": END,
        },
    )

    # ── Merge → plan → plan approval → apply ─────────────────────────────────
    graph.add_edge(N_MERGE_OR_WAIT, N_GENERATE_PLAN)
    graph.add_edge(N_GENERATE_PLAN, N_PLAN_APPROVAL)
    graph.add_edge(N_PLAN_APPROVAL, N_TRIGGER_APPLY)
    graph.add_edge(N_TRIGGER_APPLY, N_MONITOR)

    # ── Monitor → success or incident ────────────────────────────────────────
    graph.add_conditional_edges(
        N_MONITOR,
        route_after_monitor,
        {
            "success": N_EVIDENCE,
            "failed": N_NOTIFY,               # post incident notification
        },
    )

    # ── Evidence finalization ─────────────────────────────────────────────────
    graph.add_edge(N_EVIDENCE, END)

    compiled = graph.compile(checkpointer=checkpointer, interrupt_before=[N_PR_APPROVAL, N_PLAN_APPROVAL])
    log.info("graph.compiled", nodes=list(graph.nodes.keys()))
    return compiled


async def build_graph_postgres(connection_string: str) -> Any:
    """Build the graph with a PostgreSQL checkpointer for production use."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with await AsyncPostgresSaver.from_conn_string(connection_string) as checkpointer:
        await checkpointer.setup()
        return build_graph(checkpointer=checkpointer)


# ── Routing helper functions ──────────────────────────────────────────────────

def _noop(state: WorkflowState) -> dict[str, Any]:
    """Placeholder for routing-only nodes that make no state changes."""
    return {}


def _route_after_context(state: WorkflowState) -> str:
    event = state.get("event_type", "")
    return "pr" if event in ("pr_opened", "pr_updated") else "branch"


def _route_after_itsm(state: WorkflowState) -> str:
    findings = state.get("findings") or []
    config = state.get("config") or {}
    iterations = state.get("auto_fix_iterations", 0)

    from src.policy.risk_policy import RiskPolicy
    policy = RiskPolicy(config)

    # Still fixable items and below loop limit → continue loop
    if (
        policy.auto_fixable_findings(findings)
        and not policy.is_loop_limit_reached(iterations, config)
    ):
        return "loop"

    # Blocking findings remain → human review needed
    if policy.blocks_merge(findings):
        return "wait"

    return "proceed"


def _route_after_notify(state: WorkflowState) -> str:
    status = state.get("status", "running")
    event = state.get("event_type", "")

    if status == "completed":
        return "done"
    if status in ("failed", "blocked"):
        return "failed"
    if status == "waiting_for_human":
        return "wait"

    # After PR approval interrupt resumes → proceed to merge
    approvals = state.get("approvals") or []
    if any(a.get("scope") == "pr_review" for a in approvals):
        return "merge"

    # Branch push flow completes after notify
    if event == "branch_push":
        return "continue"

    return "wait"
