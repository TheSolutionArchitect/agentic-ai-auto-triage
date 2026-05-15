"""LangGraph workflow state definition.

All fields are optional except run_id and event_type so nodes can update
partial slices. Lists use Annotated[list, add] for safe concurrent appends;
scalar fields are replaced on each update.
"""

from __future__ import annotations

import uuid
from operator import add
from typing import Annotated, Any, Literal, Optional

from typing_extensions import TypedDict

EventType = Literal[
    "branch_push",
    "pr_opened",
    "pr_updated",
    "approval_received",
    "pipeline_event",
    "monitor_event",
]

Severity = Literal["HIGH", "MEDIUM", "LOW"]
FixType = Literal["auto_fix_allowed", "human_required", "informational"]
WorkflowStatus = Literal["running", "waiting_for_human", "failed", "completed", "blocked"]
ApprovalDecision = Literal["approve", "reject", "request_changes", "defer", "approve_with_exception"]
ApprovalScope = Literal["pr_review", "terraform_plan", "auto_fix", "exception", "break_glass"]
RiskLevel = Literal["low", "medium", "high"]
Environment = Literal["dev", "test", "stage", "prod"]


class Repository(TypedDict, total=False):
    owner: str
    name: str
    default_branch: str
    full_name: str


class PullRequest(TypedDict, total=False):
    number: int
    url: str
    source_branch: str
    target_branch: str
    head_sha: str
    base_sha: str
    title: str
    author: str
    diff_url: str


class EnvironmentInfo(TypedDict, total=False):
    name: Environment
    risk_level: RiskLevel


class TerraformContext(TypedDict, total=False):
    modules: list[str]
    providers: list[str]
    workspaces: list[str]
    backend_changed: bool
    change_type: str  # "module_change" | "env_change" | "provider_upgrade" | etc.


class Finding(TypedDict, total=False):
    id: str
    file_path: str
    line_ref: str
    severity: Severity
    confidence: float
    category: str
    rationale: str
    fix_recommendation: str
    fix_type: FixType
    source: str
    resolved: bool
    resolution_notes: str
    policy_note: str


class PolicyResult(TypedDict, total=False):
    tool: str
    rule_id: str
    status: Literal["pass", "fail", "error", "skipped"]
    message: str
    file_path: str
    line_ref: str


class PlanSummary(TypedDict, total=False):
    create: int
    update: int
    replace: int
    destroy: int
    no_change: int
    has_destructive: bool
    security_sensitive_changes: list[str]
    cost_estimate: Optional[str]
    plan_artifact_url: str
    policy_results: list[PolicyResult]


class Approval(TypedDict, total=False):
    decision: ApprovalDecision
    approver: str
    comment: str
    scope: ApprovalScope
    timestamp: str
    expires_at: str
    run_id: str


class JiraIssue(TypedDict, total=False):
    key: str
    url: str
    summary: str
    status: str
    finding_id: str


class SlackThread(TypedDict, total=False):
    channel: str
    thread_ts: str
    url: str


class PipelineRun(TypedDict, total=False):
    run_id: str
    pipeline: str
    status: str
    url: str
    plan_artifact_url: str
    apply_result: str
    logs_url: str


class EvidenceItem(TypedDict, total=False):
    type: str
    url: str
    sha: str
    timestamp: str
    description: str


class WorkflowState(TypedDict, total=False):
    # Identity
    run_id: str
    event_type: EventType

    # Source context
    repository: Repository
    pull_request: PullRequest
    environment: EnvironmentInfo
    changed_files: Annotated[list[str], add]
    terraform_context: TerraformContext

    # Review loop
    findings: Annotated[list[Finding], add]
    auto_fix_iterations: int
    policy_results: Annotated[list[PolicyResult], add]

    # Plan
    plan_summary: PlanSummary

    # Approvals and ITSM
    approvals: Annotated[list[Approval], add]
    jira_issues: Annotated[list[JiraIssue], add]

    # Communication
    slack_thread: SlackThread

    # Deployment
    pipeline_runs: Annotated[list[PipelineRun], add]

    # Evidence
    evidence: Annotated[list[EvidenceItem], add]

    # Execution control
    status: WorkflowStatus
    error: Optional[str]
    next_node: Optional[str]

    # Runtime objects (not persisted to checkpointer — transient per-run)
    config: Optional[dict[str, Any]]
    tool_registry: Optional[Any]
    mcp_manager: Optional[Any]
    llm_provider: Optional[Any]


def new_run_state(
    event_type: EventType,
    repository: Repository,
    pull_request: Optional[PullRequest] = None,
    environment: Optional[EnvironmentInfo] = None,
) -> WorkflowState:
    """Factory for a fresh workflow run state."""
    return WorkflowState(
        run_id=str(uuid.uuid4()),
        event_type=event_type,
        repository=repository,
        pull_request=pull_request or {},
        environment=environment or {"name": "dev", "risk_level": "low"},
        changed_files=[],
        terraform_context={},
        findings=[],
        auto_fix_iterations=0,
        policy_results=[],
        plan_summary={},
        approvals=[],
        jira_issues=[],
        slack_thread={},
        pipeline_runs=[],
        evidence=[],
        status="running",
        error=None,
        next_node=None,
        config=None,
        tool_registry=None,
        mcp_manager=None,
        llm_provider=None,
    )
