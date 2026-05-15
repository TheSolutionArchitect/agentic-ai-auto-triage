"""Tool configuration registry.

Loads, validates, and exposes per-tool MCP configurations. Enforces allowlists
so agents can only call tools their role permits. Every tool call is namespaced
as <tool_name>.<method> (e.g. github.pull_requests.list_files).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import jsonschema
import structlog

from src.integrations.secrets import resolve

log = structlog.get_logger(__name__)

_TOOL_SCHEMA_PATH = Path(__file__).parent.parent.parent / "config" / "schemas" / "tool.schema.json"
_CONFIG_ROOT = Path(__file__).parent.parent.parent / "config"


class ToolConfig:
    def __init__(self, name: str, raw: dict[str, Any], config_path: Path) -> None:
        self.name = name
        self.raw = raw
        self.config_path = config_path
        self._allowed: set[str] = set(raw.get("allowed_tools", []))
        self._disallowed: set[str] = set(raw.get("disallowed_tools", []))

    @property
    def server(self) -> dict[str, Any]:
        return self.raw["server"]

    @property
    def transport(self) -> str:
        return self.server["transport"]

    def allowed_tools(self) -> list[str]:
        return sorted(self._allowed - self._disallowed)

    def is_allowed(self, tool_method: str) -> bool:
        """Check whether <tool_method> (e.g. 'pull_requests.list_files') is permitted."""
        if tool_method in self._disallowed:
            return False
        return tool_method in self._allowed

    def get_auth_token(self) -> str | None:
        auth = self.server.get("auth", {})
        token_env = auth.get("token_env")
        if not token_env:
            return None
        return resolve(token_env)

    def get_server_env(self) -> dict[str, str]:
        """Resolve env var references in server.env for stdio-transport tools."""
        raw_env = self.server.get("env", {})
        resolved: dict[str, str] = {}
        for k, env_var_name in raw_env.items():
            try:
                resolved[k] = resolve(env_var_name)
            except RuntimeError:
                log.warning("tool_registry.env_var_missing", tool=self.name, var=env_var_name)
        return resolved


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolConfig] = {}
        self._tool_schema: dict[str, Any] = {}

    def load(self, config: dict[str, Any]) -> None:
        """Load and validate all enabled tool configs referenced by config.json."""
        schema_path = _TOOL_SCHEMA_PATH
        if schema_path.exists():
            self._tool_schema = json.loads(schema_path.read_text())
        else:
            log.warning("tool_registry.schema_missing", path=str(schema_path))

        tools_section = config.get("tools", {})
        for tool_name, tool_entry in tools_section.items():
            if not tool_entry.get("enabled", False):
                log.info("tool_registry.skipped_disabled", tool=tool_name)
                continue
            config_file = Path(tool_entry["config_file"])
            if not config_file.is_absolute():
                config_file = _CONFIG_ROOT.parent / config_file
            self._load_tool(tool_name, config_file)

        log.info("tool_registry.loaded", tools=list(self._tools.keys()))

    def _load_tool(self, name: str, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"Tool config not found: {path}")

        raw = json.loads(path.read_text())

        if self._tool_schema:
            try:
                jsonschema.validate(instance=raw, schema=self._tool_schema)
            except jsonschema.ValidationError as exc:
                raise ValueError(f"Tool config '{name}' failed schema validation: {exc.message}") from exc

        self._tools[name] = ToolConfig(name=name, raw=raw, config_path=path)
        log.info("tool_registry.tool_loaded", tool=name, allowed=len(raw.get("allowed_tools", [])))

    def get(self, tool_name: str) -> ToolConfig:
        if tool_name not in self._tools:
            raise KeyError(f"Tool '{tool_name}' is not registered or not enabled.")
        return self._tools[tool_name]

    def all_tools(self) -> dict[str, ToolConfig]:
        return dict(self._tools)

    def is_tool_call_allowed(self, tool_name: str, method: str) -> bool:
        """Check namespace.method allowance. Returns False for unknown tools."""
        if tool_name not in self._tools:
            return False
        return self._tools[tool_name].is_allowed(method)

    def tools_for_agent(self, agent_name: str) -> dict[str, list[str]]:
        """Return {tool_name: [allowed_methods]} for the given agent role.

        Agent-to-tool mapping is intentionally conservative: agents only receive
        tools they need. Expand here as agent roles evolve.
        """
        agent_tool_map: dict[str, list[str]] = {
            "orchestrator":   ["github", "slack"],
            "repo_context":   ["github"],
            "sca_security":   ["github", "terraform", "iac_scanner"],
            "compliance":     ["confluence", "iac_scanner", "terraform"],
            "peer_review":    ["github", "terraform"],
            "auto_fix":       ["github"],
            "itsm":           ["jira"],
            "deployment":     ["github", "terraform"],
            "monitor":        ["github", "terraform", "jira", "slack"],
            "communication":  ["slack"],
        }
        allowed_tool_names = agent_tool_map.get(agent_name, [])
        return {
            name: self._tools[name].allowed_tools()
            for name in allowed_tool_names
            if name in self._tools
        }


def main() -> None:
    """CLI entry point: validate all tool configs and exit."""
    import argparse

    parser = argparse.ArgumentParser(description="Validate tool registry configs")
    parser.add_argument("--config", default="config/config.json")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"ERROR: config not found at {config_path}", file=sys.stderr)
        sys.exit(1)

    config = json.loads(config_path.read_text())
    registry = ToolRegistry()
    try:
        registry.load(config)
        print(f"OK: {len(registry.all_tools())} tools validated")
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
