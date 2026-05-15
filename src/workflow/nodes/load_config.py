"""load_config node: validate config.json and fail closed on any error."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import structlog

from src.workflow.state import WorkflowState

log = structlog.get_logger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent.parent.parent / "config" / "config.json"
_SCHEMA_PATH = Path(__file__).parent.parent.parent.parent / "config" / "schemas" / "config.schema.json"


def load_config(state: WorkflowState) -> dict[str, Any]:
    """Load and schema-validate config.json. Returns {config: ...} or raises."""
    log.info("node.load_config.start", run_id=state.get("run_id"))

    config_path = _CONFIG_PATH
    schema_path = _SCHEMA_PATH

    if not config_path.exists():
        raise FileNotFoundError(f"config.json not found at {config_path}")

    config = json.loads(config_path.read_text())

    if schema_path.exists():
        schema = json.loads(schema_path.read_text())
        try:
            jsonschema.validate(instance=config, schema=schema)
        except jsonschema.ValidationError as exc:
            raise ValueError(f"config.json failed schema validation: {exc.message}") from exc
    else:
        log.warning("node.load_config.schema_missing", path=str(schema_path))

    log.info("node.load_config.done", run_id=state.get("run_id"))
    return {"config": config}
