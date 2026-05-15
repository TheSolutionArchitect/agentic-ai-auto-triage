"""Tests for MCP tool allowlist enforcement in ToolRegistry."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.integrations.tool_registry import ToolConfig, ToolRegistry


@pytest.fixture
def config() -> dict:
    return json.loads(
        (Path(__file__).parent.parent.parent / "config" / "config.json").read_text()
    )


@pytest.fixture
def registry(config, monkeypatch) -> ToolRegistry:
    monkeypatch.setenv("GITHUB_MCP_TOKEN", "test-github-token")
    monkeypatch.setenv("ATLASSIAN_MCP_TOKEN", "test-atlassian-token")
    monkeypatch.setenv("SLACK_MCP_TOKEN", "test-slack-token")
    monkeypatch.setenv("TFE_TOKEN", "test-tfe-token")
    r = ToolRegistry()
    r.load(config)
    return r


class TestToolRegistryLoading:
    def test_all_enabled_tools_are_loaded(self, registry, config):
        enabled_tools = [name for name, cfg in config["tools"].items() if cfg["enabled"]]
        for tool in enabled_tools:
            assert tool in registry.all_tools()

    def test_unknown_tool_raises_key_error(self, registry):
        with pytest.raises(KeyError):
            registry.get("nonexistent_tool")


class TestAllowlistEnforcement:
    def test_allowed_github_tool(self, registry):
        assert registry.is_tool_call_allowed("github", "pull_requests.list_files")

    def test_disallowed_github_tool(self, registry):
        assert not registry.is_tool_call_allowed("github", "repos.delete")

    def test_unknown_tool_is_not_allowed(self, registry):
        assert not registry.is_tool_call_allowed("unknown_tool", "some.method")

    def test_terraform_disallows_destructive_ops(self, registry):
        assert not registry.is_tool_call_allowed("terraform", "hcp_terraform.delete_workspace")

    def test_terraform_allows_read_ops(self, registry):
        assert registry.is_tool_call_allowed("terraform", "hcp_terraform.list_workspaces")

    def test_confluence_disallows_write(self, registry):
        assert not registry.is_tool_call_allowed("confluence", "confluence.create_page")

    def test_confluence_allows_read(self, registry):
        assert registry.is_tool_call_allowed("confluence", "confluence.get_page")


class TestAgentToolFiltering:
    def test_communication_agent_has_only_slack(self, registry):
        tools = registry.tools_for_agent("communication")
        assert "slack" in tools
        assert "terraform" not in tools
        assert "jira" not in tools

    def test_itsm_agent_has_only_jira(self, registry):
        tools = registry.tools_for_agent("itsm")
        assert "jira" in tools
        assert "slack" not in tools
        assert "github" not in tools

    def test_auto_fix_agent_has_only_github(self, registry):
        tools = registry.tools_for_agent("auto_fix")
        assert "github" in tools
        assert "terraform" not in tools

    def test_unknown_agent_gets_no_tools(self, registry):
        tools = registry.tools_for_agent("unknown_agent_role")
        assert tools == {}
