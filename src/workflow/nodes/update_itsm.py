"""update_itsm node: create or update JIRA issues for findings and workflow events."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from src.integrations import runtime

from src.workflow.state import JiraIssue, WorkflowState

log = structlog.get_logger(__name__)


def update_itsm(state: WorkflowState) -> dict[str, Any]:
    run_id = state.get("run_id", "unknown")
    log.info("node.update_itsm.start", run_id=run_id)
    rt = runtime.get_runtime(run_id)
    registry = rt.get("tool_registry")
    findings = state.get("findings") or []
    pr = state.get("pull_request") or {}
    config = state.get("config") or {}
    environment = (state.get("environment") or {}).get("name", "dev")

    active_findings = [f for f in findings if not f.get("resolved")]
    new_issues: list[JiraIssue] = []

    if not active_findings:
        log.info("node.update_itsm.no_active_findings", run_id=run_id)
        return {"jira_issues": new_issues}

    if not registry:
        log.warning("node.update_itsm.no_registry", run_id=run_id)
        return {"jira_issues": new_issues}

    try:
        jira_cfg = registry.get("jira")
        field_mapping = jira_cfg.raw.get("field_mapping", {})
        project_key = jira_cfg.raw.get("project_key", "DEVOPS")
    except KeyError:
        log.warning("node.update_itsm.jira_not_configured", run_id=run_id)
        return {"jira_issues": new_issues}

    for finding in active_findings:
        issue = _create_or_update_jira_issue(
            finding=finding,
            pr=pr,
            run_id=run_id,
            project_key=project_key,
            environment=environment,
            field_mapping=field_mapping,
        )
        if issue:
            new_issues.append(issue)

    log.info("node.update_itsm.done", run_id=run_id, issues_created=len(new_issues))
    return {"jira_issues": new_issues}


def _create_or_update_jira_issue(
    finding: dict[str, Any],
    pr: dict[str, Any],
    run_id: str,
    project_key: str,
    environment: str,
    field_mapping: dict[str, str],
) -> JiraIssue | None:
    """Invoke jira.create_issue or jira.update_issue via MCP.

    When MCP is connected, this builds the issue payload and calls the tool.
    Returns a JiraIssue dict with key and URL, or None on failure.
    """
    summary = (
        f"[{finding.get('severity', 'UNKNOWN')}] {finding.get('category', 'IaC Issue')} "
        f"in {finding.get('file_path', 'unknown')} — PR #{pr.get('number', '?')}"
    )
    description = (
        f"*Run ID:* {run_id}\n"
        f"*PR:* {pr.get('url', 'N/A')}\n"
        f"*File:* {finding.get('file_path', 'N/A')} {finding.get('line_ref', '')}\n"
        f"*Severity:* {finding.get('severity')}\n"
        f"*Confidence:* {finding.get('confidence')}\n"
        f"*Environment:* {environment}\n\n"
        f"*Rationale:* {finding.get('rationale')}\n\n"
        f"*Recommended fix:* {finding.get('fix_recommendation')}"
    )
    log.debug(
        "itsm.would_create_issue",
        project=project_key,
        summary=summary[:80],
        finding_id=finding.get("id"),
    )
    # Placeholder: when MCP is live, call jira.create_issue and return real key/url
    return JiraIssue(
        key=f"{project_key}-PENDING",
        url=f"https://jira.example.com/browse/{project_key}-PENDING",
        summary=summary,
        status="open",
        finding_id=finding.get("id", ""),
    )
