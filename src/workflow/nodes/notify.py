"""notify node: post concise Slack updates; maintain one thread per workflow run."""

from __future__ import annotations

from typing import Any

import structlog
from src.integrations import runtime

from src.workflow.state import SlackThread, WorkflowState

log = structlog.get_logger(__name__)


def notify(state: WorkflowState) -> dict[str, Any]:
    run_id = state.get("run_id", "unknown")
    config = state.get("config") or {}
    rt = runtime.get_runtime(run_id)
    registry = rt.get("tool_registry")
    status = state.get("status", "running")
    event_type = state.get("event_type", "unknown")

    log.info("node.notify.start", run_id=run_id, status=status)

    notifications = config.get("notifications", {})
    channel = _select_channel(status, notifications)

    message = _build_message(state)
    thread_ts = _post_message(registry, channel, message, state)

    slack_thread = state.get("slack_thread") or {}
    if thread_ts and not slack_thread.get("thread_ts"):
        slack_thread = SlackThread(channel=channel, thread_ts=thread_ts, url="")

    log.info("node.notify.done", run_id=run_id, channel=channel)
    return {"slack_thread": slack_thread}


def _select_channel(status: str, notifications: dict[str, str]) -> str:
    if status in ("failed", "blocked"):
        return notifications.get("incident_channel", "#platform-incidents")
    if status == "waiting_for_human":
        return notifications.get("approval_channel", "#terraform-approvals")
    return notifications.get("default_channel", "#terraform-deployments")


def _build_message(state: WorkflowState) -> str:
    run_id = state.get("run_id", "?")[:8]
    event_type = state.get("event_type", "?")
    status = state.get("status", "?")
    pr = state.get("pull_request") or {}
    env = (state.get("environment") or {}).get("name", "?")
    findings = state.get("findings") or []
    active = [f for f in findings if not f.get("resolved")]
    high = sum(1 for f in active if f.get("severity") == "HIGH")

    lines = [
        f"*Run:* `{run_id}` | *Event:* `{event_type}` | *Env:* `{env}` | *Status:* `{status}`",
    ]
    if pr.get("url"):
        lines.append(f"*PR:* {pr.get('url')} (#{pr.get('number')})")
    if active:
        lines.append(f"*Findings:* {len(active)} active ({high} HIGH)")
    if state.get("status") == "waiting_for_human":
        lines.append(":eyes: *Human review required*")
    if state.get("error"):
        lines.append(f":x: *Error:* {state['error']}")

    jira_issues = state.get("jira_issues") or []
    if jira_issues:
        keys = ", ".join(i.get("key", "") for i in jira_issues[:5])
        lines.append(f"*JIRA:* {keys}")

    return "\n".join(lines)


def _post_message(
    registry: Any,
    channel: str,
    message: str,
    state: WorkflowState,
) -> str | None:
    """Post to Slack via MCP. Returns thread_ts or None."""
    if not registry:
        log.debug("notify.no_registry")
        return None
    try:
        _ = registry.get("slack")
    except KeyError:
        log.debug("notify.slack_not_configured")
        return None

    existing_thread = state.get("slack_thread") or {}
    thread_ts = existing_thread.get("thread_ts")

    # When MCP connected:
    #   if thread_ts: slack.send_message(channel, message, thread_ts=thread_ts)
    #   else: slack.send_message(channel, message) -> returns new thread_ts
    log.debug("notify.would_post", channel=channel, has_thread=bool(thread_ts))
    return thread_ts
