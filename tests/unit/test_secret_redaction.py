"""Tests for secret redaction to prevent credential leakage in LLM prompts."""

import os

import pytest

from src.integrations import secrets


@pytest.fixture(autouse=True)
def clear_redaction_state():
    """Reset module-level redaction state between tests."""
    secrets._REDACT_PATTERNS.clear()
    secrets._RESOLVED_VALUES.clear()
    yield
    secrets._REDACT_PATTERNS.clear()
    secrets._RESOLVED_VALUES.clear()


class TestRedaction:
    def test_resolved_secret_is_redacted_in_text(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "super-secret-token-xyz")
        secrets.resolve("MY_SECRET")
        result = secrets.redact("The token is super-secret-token-xyz and nothing else")
        assert "super-secret-token-xyz" not in result
        assert "[REDACTED]" in result

    def test_unresolved_value_is_not_redacted(self):
        text = "This string has no secrets"
        assert secrets.redact(text) == text

    def test_multiple_secrets_are_all_redacted(self, monkeypatch):
        monkeypatch.setenv("SECRET_A", "token-aaa")
        monkeypatch.setenv("SECRET_B", "token-bbb")
        secrets.resolve("SECRET_A")
        secrets.resolve("SECRET_B")
        text = "A: token-aaa and B: token-bbb in one string"
        result = secrets.redact(text)
        assert "token-aaa" not in result
        assert "token-bbb" not in result

    def test_redact_dict_recurses(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "my-db-password")
        secrets.resolve("SECRET_KEY")
        data = {
            "connection": {"password": "my-db-password", "host": "localhost"},
            "tags": ["my-db-password", "safe-value"],
        }
        result = secrets.redact_dict(data)
        assert result["connection"]["password"] == "[REDACTED]"
        assert result["connection"]["host"] == "localhost"
        assert result["tags"][0] == "[REDACTED]"
        assert result["tags"][1] == "safe-value"

    def test_missing_secret_raises_runtime_error(self, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        with pytest.raises(RuntimeError, match="MISSING_VAR"):
            secrets.resolve("MISSING_VAR")

    def test_optional_secret_returns_none_when_missing(self, monkeypatch):
        monkeypatch.delenv("OPTIONAL_VAR", raising=False)
        assert secrets.resolve_optional("OPTIONAL_VAR") is None

    def test_secret_not_registered_twice(self, monkeypatch):
        monkeypatch.setenv("DEDUP_SECRET", "same-value")
        secrets.resolve("DEDUP_SECRET")
        secrets.resolve("DEDUP_SECRET")
        assert len(secrets._REDACT_PATTERNS) == 1
