"""initialize_tools node: build ToolRegistry and LLM provider; register in runtime store."""

from __future__ import annotations

from typing import Any

import structlog

from src.integrations import runtime
from src.integrations.llm_provider import build_provider
from src.integrations.tool_registry import ToolRegistry
from src.workflow.state import WorkflowState

log = structlog.get_logger(__name__)


def initialize_tools(state: WorkflowState) -> dict[str, Any]:
    config = state.get("config")
    if not config:
        raise RuntimeError("initialize_tools called before load_config — config is missing")

    run_id = state.get("run_id", "unknown")
    log.info("node.initialize_tools.start", run_id=run_id)

    registry = ToolRegistry()
    registry.load(config)

    llm = build_provider(config["llm"])

    # Store in process-level runtime store (not in state — not serializable)
    runtime.set_runtime(run_id, tool_registry=registry, llm_provider=llm)

    log.info(
        "node.initialize_tools.done",
        run_id=run_id,
        tools=list(registry.all_tools().keys()),
        llm_provider=config["llm"]["provider"],
    )
    return {}  # No state mutation — runtime objects are outside the checkpoint
