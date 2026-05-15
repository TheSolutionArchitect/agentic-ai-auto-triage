"""peer_review node: LLM-assisted code review producing structured findings."""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from src.agents.peer_review import PeerReviewAgent
from src.integrations import runtime
from src.workflow.state import Finding, WorkflowState

log = structlog.get_logger(__name__)


def peer_review(state: WorkflowState) -> dict[str, Any]:
    run_id = state.get("run_id", "unknown")
    log.info("node.peer_review.start", run_id=run_id)

    rt = runtime.get_runtime(run_id)
    llm = rt.get("llm_provider")
    registry = rt.get("tool_registry")
    pr = state.get("pull_request") or {}
    policy_results = state.get("policy_results") or []
    tf_context = state.get("terraform_context") or {}

    if not llm:
        log.warning("node.peer_review.no_llm", run_id=run_id)
        return {"findings": []}

    agent = PeerReviewAgent(llm=llm, tool_registry=registry)
    raw_findings = agent.review(
        pr=pr,
        policy_results=policy_results,
        terraform_context=tf_context,
        changed_files=state.get("changed_files") or [],
    )

    findings: list[Finding] = []
    for raw in raw_findings:
        if not raw.get("id"):
            raw["id"] = str(uuid.uuid4())
        raw.setdefault("resolved", False)
        findings.append(Finding(**{k: v for k, v in raw.items() if k in Finding.__annotations__}))

    log.info("node.peer_review.done", run_id=run_id, findings=len(findings))
    return {"findings": findings}
