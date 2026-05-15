"""collect_repo_context node: gather PR diff, file list, and Terraform context via GitHub MCP."""

from __future__ import annotations

import re
from typing import Any

import structlog
from src.integrations import runtime

from src.workflow.state import TerraformContext, WorkflowState

log = structlog.get_logger(__name__)

_TF_FILE_PATTERN = re.compile(r"\.tf(vars)?$")
_BACKEND_PATTERN = re.compile(r'terraform\s*\{.*?backend\s+"', re.DOTALL)
_PROVIDER_PATTERN = re.compile(r'provider\s+"([^"]+)"')
_MODULE_PATTERN = re.compile(r'module\s+"([^"]+)"')


def collect_repo_context(state: WorkflowState) -> dict[str, Any]:
    run_id = state.get("run_id", "unknown")
    log.info("node.collect_repo_context.start", run_id=run_id)

    pr = state.get("pull_request") or {}
    rt = runtime.get_runtime(run_id)
    registry = rt.get("tool_registry")

    changed_files: list[str] = []
    tf_files: list[str] = []
    raw_diff = ""

    if registry:
        try:
            changed_files, raw_diff = _fetch_pr_files_via_mcp(registry, pr)
            tf_files = [f for f in changed_files if _TF_FILE_PATTERN.search(f)]
        except Exception as exc:  # noqa: BLE001
            log.warning("node.collect_repo_context.mcp_error", error=str(exc))

    terraform_context = _classify_tf_context(tf_files, raw_diff)

    log.info(
        "node.collect_repo_context.done",
        run_id=run_id,
        changed_files=len(changed_files),
        tf_files=len(tf_files),
        change_type=terraform_context.get("change_type"),
    )
    return {
        "changed_files": changed_files,
        "terraform_context": terraform_context,
    }


def _fetch_pr_files_via_mcp(registry: Any, pr: dict[str, Any]) -> tuple[list[str], str]:
    """Invoke GitHub MCP tool to list PR files. Returns (file_paths, combined_diff)."""
    # When MCP client is live, this would call:
    #   github.pull_requests.list_files(owner=..., repo=..., pull_number=...)
    # For now, return empty to allow graph to proceed without live credentials.
    return [], ""


def _classify_tf_context(tf_files: list[str], diff: str) -> TerraformContext:
    modules: list[str] = []
    providers: list[str] = []
    backend_changed = False

    for match in _MODULE_PATTERN.finditer(diff):
        m = match.group(1)
        if m not in modules:
            modules.append(m)

    for match in _PROVIDER_PATTERN.finditer(diff):
        p = match.group(1)
        if p not in providers:
            providers.append(p)

    if _BACKEND_PATTERN.search(diff):
        backend_changed = True

    change_type = _infer_change_type(tf_files, backend_changed)

    return TerraformContext(
        modules=modules,
        providers=providers,
        workspaces=[],
        backend_changed=backend_changed,
        change_type=change_type,
    )


def _infer_change_type(tf_files: list[str], backend_changed: bool) -> str:
    if backend_changed:
        return "backend_change"
    if any("modules/" in f for f in tf_files):
        return "module_change"
    if any("provider" in f or "versions" in f for f in tf_files):
        return "provider_upgrade"
    if tf_files:
        return "env_change"
    return "unknown"
