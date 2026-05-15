"""GitHub webhook parsing and HMAC signature validation.

Each incoming webhook is parsed into a WorkflowState seed dict that
the API layer passes directly to the LangGraph runner.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

import structlog

from src.workflow.state import (
    Environment,
    EnvironmentInfo,
    EventType,
    PullRequest,
    Repository,
    RiskLevel,
    WorkflowState,
    new_run_state,
)

log = structlog.get_logger(__name__)

_DEFAULT_ENVIRONMENT: Environment = "dev"
_RISK_MAP: dict[Environment, RiskLevel] = {
    "dev": "low",
    "test": "medium",
    "stage": "medium",
    "prod": "high",
}


class WebhookValidationError(ValueError):
    pass


def validate_github_signature(body: bytes, signature_header: str | None) -> None:
    """Verify the GitHub HMAC-SHA256 webhook signature.

    Raises WebhookValidationError if the signature is missing or invalid.
    Skips validation if GITHUB_WEBHOOK_SECRET is not set (dev mode only).
    """
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        log.warning("webhook.signature_check_skipped", reason="GITHUB_WEBHOOK_SECRET not set")
        return

    if not signature_header:
        raise WebhookValidationError("Missing X-Hub-Signature-256 header")

    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header):
        raise WebhookValidationError("Webhook signature mismatch")


def parse_github_event(event_name: str, payload: dict[str, Any]) -> WorkflowState | None:
    """Convert a raw GitHub webhook payload into a WorkflowState.

    Returns None for events the workflow doesn't handle (e.g. issue comments).
    """
    action = payload.get("action", "")
    log.info("webhook.parse", event_name=event_name, action=action)

    if event_name == "push":
        return _parse_push_event(payload)
    elif event_name == "pull_request":
        return _parse_pr_event(payload, action)
    elif event_name == "workflow_run":
        return _parse_workflow_run_event(payload)
    else:
        log.info("webhook.ignored", event_name=event_name)
        return None


def _parse_push_event(payload: dict[str, Any]) -> WorkflowState | None:
    ref = payload.get("ref", "")
    if not ref.startswith("refs/heads/"):
        return None

    branch = ref.removeprefix("refs/heads/")
    repo_data = payload.get("repository", {})

    repo = Repository(
        owner=repo_data.get("owner", {}).get("login", ""),
        name=repo_data.get("name", ""),
        default_branch=repo_data.get("default_branch", "main"),
        full_name=repo_data.get("full_name", ""),
    )
    env = _infer_environment(branch)

    state = new_run_state(
        event_type="branch_push",
        repository=repo,
        environment=env,
    )
    changed: list[str] = []
    for commit in payload.get("commits", []):
        changed.extend(commit.get("added", []))
        changed.extend(commit.get("modified", []))
    state["changed_files"] = list(dict.fromkeys(changed))
    return state


def _parse_pr_event(payload: dict[str, Any], action: str) -> WorkflowState | None:
    if action not in ("opened", "synchronize", "reopened"):
        return None

    event_type: EventType = "pr_opened" if action == "opened" else "pr_updated"
    pr_data = payload.get("pull_request", {})
    repo_data = payload.get("repository", {})

    repo = Repository(
        owner=repo_data.get("owner", {}).get("login", ""),
        name=repo_data.get("name", ""),
        default_branch=repo_data.get("default_branch", "main"),
        full_name=repo_data.get("full_name", ""),
    )

    pr = PullRequest(
        number=pr_data.get("number"),
        url=pr_data.get("html_url", ""),
        source_branch=pr_data.get("head", {}).get("ref", ""),
        target_branch=pr_data.get("base", {}).get("ref", "main"),
        head_sha=pr_data.get("head", {}).get("sha", ""),
        base_sha=pr_data.get("base", {}).get("sha", ""),
        title=pr_data.get("title", ""),
        author=pr_data.get("user", {}).get("login", ""),
    )

    target_branch = pr.get("target_branch", "main")
    env = _infer_environment(target_branch)

    return new_run_state(event_type=event_type, repository=repo, pull_request=pr, environment=env)


def _parse_workflow_run_event(payload: dict[str, Any]) -> WorkflowState | None:
    """Parse a completed GitHub Actions workflow run into a pipeline_event."""
    run_data = payload.get("workflow_run", {})
    if run_data.get("status") != "completed":
        return None

    repo_data = payload.get("repository", {})
    repo = Repository(
        owner=repo_data.get("owner", {}).get("login", ""),
        name=repo_data.get("name", ""),
        default_branch=repo_data.get("default_branch", "main"),
        full_name=repo_data.get("full_name", ""),
    )
    env = _infer_environment(run_data.get("head_branch", "main"))
    state = new_run_state(event_type="pipeline_event", repository=repo, environment=env)
    return state


def _infer_environment(branch_or_target: str) -> EnvironmentInfo:
    env: Environment = _DEFAULT_ENVIRONMENT
    if "prod" in branch_or_target:
        env = "prod"
    elif "stage" in branch_or_target or "staging" in branch_or_target:
        env = "stage"
    elif "test" in branch_or_target:
        env = "test"
    return EnvironmentInfo(name=env, risk_level=_RISK_MAP[env])


def parse_approval_callback(body: dict[str, Any]) -> dict[str, Any]:
    """Parse an approval decision callback from Slack/UI and validate its shape."""
    required = {"run_id", "decision", "approver", "scope"}
    missing = required - body.keys()
    if missing:
        raise WebhookValidationError(f"Approval callback missing fields: {missing}")

    valid_decisions = {"approve", "reject", "request_changes", "defer", "approve_with_exception"}
    if body["decision"] not in valid_decisions:
        raise WebhookValidationError(f"Invalid decision: {body['decision']!r}")

    return body
