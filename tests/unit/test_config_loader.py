"""Tests for config loading and schema validation."""

import json
import copy
from pathlib import Path

import jsonschema
import pytest

CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "config.json"
SCHEMA_PATH = Path(__file__).parent.parent.parent / "config" / "schemas" / "config.schema.json"
TOOL_SCHEMA_PATH = Path(__file__).parent.parent.parent / "config" / "schemas" / "tool.schema.json"
TOOLS_DIR = Path(__file__).parent.parent.parent / "tools"


@pytest.fixture
def config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


@pytest.fixture
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def tool_schema() -> dict:
    return json.loads(TOOL_SCHEMA_PATH.read_text())


class TestConfigSchema:
    def test_valid_config_passes_schema(self, config, schema):
        jsonschema.validate(instance=config, schema=schema)

    def test_missing_version_fails(self, config, schema):
        bad = copy.deepcopy(config)
        del bad["version"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=schema)

    def test_missing_llm_fails(self, config, schema):
        bad = copy.deepcopy(config)
        del bad["llm"]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=schema)

    def test_invalid_environment_fails(self, config, schema):
        bad = copy.deepcopy(config)
        bad["workflow"]["default_environment"] = "staging"  # not in enum
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=schema)

    def test_invalid_auto_fix_threshold_fails(self, config, schema):
        bad = copy.deepcopy(config)
        bad["risk_policy"]["auto_fix"]["HIGH"] = 1.5  # > 1.0
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=schema)

    def test_max_auto_fix_iterations_bounds(self, config, schema):
        bad = copy.deepcopy(config)
        bad["workflow"]["max_auto_fix_iterations"] = 0  # below minimum
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(instance=bad, schema=schema)


class TestToolSchemas:
    @pytest.mark.parametrize("tool_file", list(TOOLS_DIR.glob("*.json")))
    def test_each_tool_config_passes_schema(self, tool_schema, tool_file):
        tool_config = json.loads(tool_file.read_text())
        jsonschema.validate(instance=tool_config, schema=tool_schema)

    def test_github_allowed_tools_non_empty(self):
        github = json.loads((TOOLS_DIR / "github.json").read_text())
        assert len(github["allowed_tools"]) > 0

    def test_terraform_disallows_destructive_tools(self):
        terraform = json.loads((TOOLS_DIR / "terraform.json").read_text())
        disallowed = terraform.get("disallowed_tools", [])
        assert any("apply" in t or "delete" in t for t in disallowed)


class TestConfigValues:
    def test_provider_is_anthropic_or_openai(self, config):
        assert config["llm"]["provider"] in ("anthropic", "openai")

    def test_fallback_uses_different_provider(self, config):
        primary = config["llm"]["provider"]
        fallback = config["llm"]["fallback"]["provider"]
        assert primary != fallback

    def test_prod_requires_plan_approval(self, config):
        assert config["workflow"]["require_plan_approval"]["prod"] is True

    def test_dev_does_not_require_plan_approval(self, config):
        assert config["workflow"]["require_plan_approval"]["dev"] is False

    def test_high_auto_fix_threshold_most_restrictive(self, config):
        thresholds = config["risk_policy"]["auto_fix"]
        assert thresholds["HIGH"] >= thresholds["MEDIUM"] >= thresholds["LOW"]
