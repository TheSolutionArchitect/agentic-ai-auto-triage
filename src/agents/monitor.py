"""Monitor Agent: classify failures and route them to the correct recovery path."""

from __future__ import annotations

from typing import Any

import structlog

from src.workflow.state import JiraIssue

log = structlog.get_logger(__name__)

FAILURE_CATEGORIES = [
    "code_issue",
    "environment_issue",
    "policy_issue",
    "quota_issue",
    "provider_issue",
    "transient_issue",
]

_SYSTEM_PROMPT = """You are a cloud infrastructure SRE.
Given a deployment failure, classify it into one of the following categories:
code_issue, environment_issue, policy_issue, quota_issue, provider_issue, transient_issue.

Respond with only the category name. No explanation.
"""


class MonitorAgent:
    def __init__(self, llm: Any, tool_registry: Any | None = None) -> None:
        self._llm = llm
        self._registry = tool_registry

    def classify_failure(self, pipeline_run: dict[str, Any]) -> str:
        logs_url = pipeline_run.get("logs_url", "")
        run_id = pipeline_run.get("run_id", "?")

        if not self._llm:
            return "unknown"

        context = (
            f"Pipeline: {pipeline_run.get('pipeline')}\n"
            f"Status: {pipeline_run.get('status')}\n"
            f"Apply result: {pipeline_run.get('apply_result')}\n"
            f"Logs: {logs_url}"
        )
        try:
            category = self._llm.summarize(
                context=context,
                instruction=f"Classify this failure into one of: {FAILURE_CATEGORIES}. Reply with only the category.",
            ).strip().lower()
            for cat in FAILURE_CATEGORIES:
                if cat in category:
                    log.info("monitor_agent.classified", run_id=run_id, category=cat)
                    return cat
        except Exception as exc:  # noqa: BLE001
            log.error("monitor_agent.classify_failed", error=str(exc))
        return "unknown"

    def create_incident(
        self,
        run_id: str,
        pipeline_run: dict[str, Any],
        failure_category: str,
        pr: dict[str, Any],
    ) -> JiraIssue | None:
        """Create a JIRA incident for the deployment failure."""
        if not self._registry:
            return None
        try:
            jira_cfg = self._registry.get("jira")
            project_key = jira_cfg.raw.get("project_key", "DEVOPS")
        except KeyError:
            return None

        summary = (
            f"[INCIDENT] Terraform apply failure ({failure_category}) — "
            f"PR #{pr.get('number', '?')} run {pipeline_run.get('run_id', '?')}"
        )
        log.info("monitor_agent.create_incident", run_id=run_id, category=failure_category)

        # When MCP connected: call jira.create_issue with Incident type
        return JiraIssue(
            key=f"{project_key}-INCIDENT-PENDING",
            url=f"https://jira.example.com/browse/{project_key}-INCIDENT-PENDING",
            summary=summary,
            status="open",
            finding_id="",
        )

    def needs_rollback(self, failure_category: str) -> bool:
        """Return True when the failure likely requires state recovery."""
        return failure_category in ("code_issue", "policy_issue")
