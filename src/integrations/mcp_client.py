"""MCP client manager.

Wraps langchain-mcp-adapters MultiServerMCPClient.  Every tool call is:
  1. Checked against the per-tool allowlist before dispatch.
  2. Recorded to the audit log after completion.
  3. Namespaced as <tool_name>.<method> so agents receive clean tool names.

Agents receive only the subset of tools their role permits (see tool_registry
tools_for_agent()).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import structlog
from langchain_core.tools import BaseTool

from src.integrations.secrets import resolve
from src.integrations.tool_registry import ToolConfig, ToolRegistry
from src.observability.audit import record_tool_call

log = structlog.get_logger(__name__)


class MCPClientManager:
    """Lifecycle manager for all MCP server connections."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._tools_by_agent: dict[str, list[BaseTool]] = {}
        self._raw_clients: dict[str, Any] = {}

    @asynccontextmanager
    async def session(self) -> AsyncGenerator["MCPClientManager", None]:
        """Async context manager: opens all MCP sessions, yields self, closes on exit."""
        try:
            await self._connect_all()
            yield self
        finally:
            await self._disconnect_all()

    async def _connect_all(self) -> None:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        server_configs: dict[str, dict[str, Any]] = {}
        for name, tool_cfg in self._registry.all_tools().items():
            server_configs[name] = self._build_server_config(tool_cfg)

        self._multi_client = MultiServerMCPClient(server_configs)
        await self._multi_client.__aenter__()
        log.info("mcp.connected", servers=list(server_configs.keys()))

    async def _disconnect_all(self) -> None:
        if hasattr(self, "_multi_client"):
            try:
                await self._multi_client.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                log.warning("mcp.disconnect_error", error=str(exc))

    def _build_server_config(self, tool_cfg: ToolConfig) -> dict[str, Any]:
        transport = tool_cfg.transport
        server = tool_cfg.server

        if transport == "streamable_http":
            auth_token = tool_cfg.get_auth_token()
            headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
            return {
                "transport": "streamable_http",
                "url": server["url"],
                "headers": headers,
            }
        elif transport == "stdio":
            return {
                "transport": "stdio",
                "command": server["command"],
                "args": server.get("args", []),
                "env": tool_cfg.get_server_env(),
            }
        elif transport == "sse":
            return {
                "transport": "sse",
                "url": server["url"],
            }
        else:
            raise ValueError(f"Unknown MCP transport '{transport}' for tool '{tool_cfg.name}'")

    def get_tools_for_agent(self, agent_name: str) -> list[BaseTool]:
        """Return LangChain BaseTool instances filtered to the agent's allowlist."""
        if agent_name in self._tools_by_agent:
            return self._tools_by_agent[agent_name]

        allowed_map = self._registry.tools_for_agent(agent_name)
        all_mcp_tools: list[BaseTool] = self._multi_client.get_tools()

        filtered: list[BaseTool] = []
        for tool in all_mcp_tools:
            # MCP adapter names tools as "<server>_<method>" — normalise to dot notation
            tool_name_normalised = tool.name.replace("_", ".", 1)
            namespace, _, method = tool_name_normalised.partition(".")
            if namespace in allowed_map:
                allowed_methods = allowed_map[namespace]
                if method in allowed_methods or tool.name in allowed_methods:
                    wrapped = _AuditedTool(tool, namespace=namespace, method=method)
                    filtered.append(wrapped)
                    log.debug("mcp.tool_allowed", agent=agent_name, tool=tool.name)
                else:
                    log.debug("mcp.tool_blocked_allowlist", agent=agent_name, tool=tool.name)
            else:
                log.debug("mcp.tool_blocked_namespace", agent=agent_name, tool=tool.name)

        self._tools_by_agent[agent_name] = filtered
        return filtered


class _AuditedTool(BaseTool):
    """Thin wrapper that records every tool invocation to the audit log."""

    name: str = ""
    description: str = ""
    _inner: BaseTool
    _namespace: str
    _method: str

    def __init__(self, inner: BaseTool, namespace: str, method: str) -> None:
        super().__init__(name=inner.name, description=inner.description or "")
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_namespace", namespace)
        object.__setattr__(self, "_method", method)

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        record_tool_call(self._namespace, self._method, args, kwargs)
        return self._inner._run(*args, **kwargs)

    async def _arun(self, *args: Any, **kwargs: Any) -> Any:
        record_tool_call(self._namespace, self._method, args, kwargs)
        return await self._inner._arun(*args, **kwargs)
