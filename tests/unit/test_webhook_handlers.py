"""Tests for GitHub webhook parsing and signature validation."""

import hashlib
import hmac
import json
import os
from pathlib import Path

import pytest

from src.app.webhook_handlers import (
    WebhookValidationError,
    parse_approval_callback,
    parse_github_event,
    validate_github_signature,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def pr_payload() -> dict:
    return json.loads((FIXTURES / "sample_pr_webhook.json").read_text())


class TestSignatureValidation:
    def test_valid_signature_passes(self, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
        body = b'{"action": "opened"}'
        sig = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        validate_github_signature(body, sig)  # should not raise

    def test_invalid_signature_raises(self, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
        with pytest.raises(WebhookValidationError):
            validate_github_signature(b"body", "sha256=wrongsig")

    def test_missing_signature_raises(self, monkeypatch):
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
        with pytest.raises(WebhookValidationError):
            validate_github_signature(b"body", None)

    def test_no_secret_skips_validation(self, monkeypatch):
        monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
        validate_github_signature(b"body", None)  # should not raise


class TestPREventParsing:
    def test_opened_event_produces_pr_opened_state(self, pr_payload):
        state = parse_github_event("pull_request", pr_payload)
        assert state is not None
        assert state["event_type"] == "pr_opened"

    def test_synchronize_produces_pr_updated(self, pr_payload):
        pr_payload["action"] = "synchronize"
        state = parse_github_event("pull_request", pr_payload)
        assert state is not None
        assert state["event_type"] == "pr_updated"

    def test_closed_event_is_ignored(self, pr_payload):
        pr_payload["action"] = "closed"
        state = parse_github_event("pull_request", pr_payload)
        assert state is None

    def test_pr_state_has_correct_pr_number(self, pr_payload):
        state = parse_github_event("pull_request", pr_payload)
        assert state["pull_request"]["number"] == 42

    def test_pr_state_has_correct_repo(self, pr_payload):
        state = parse_github_event("pull_request", pr_payload)
        assert state["repository"]["name"] == "terraform-infra"
        assert state["repository"]["owner"] == "example-org"

    def test_pr_targeting_main_defaults_to_dev_env(self, pr_payload):
        # main branch → default environment
        state = parse_github_event("pull_request", pr_payload)
        assert state["environment"]["name"] == "dev"


class TestPushEventParsing:
    def test_push_to_branch_produces_state(self):
        payload = {
            "ref": "refs/heads/feature/my-change",
            "commits": [{"added": ["main.tf"], "modified": [], "removed": []}],
            "repository": {
                "name": "terraform-infra",
                "full_name": "example-org/terraform-infra",
                "default_branch": "main",
                "owner": {"login": "example-org"},
            },
        }
        state = parse_github_event("push", payload)
        assert state is not None
        assert state["event_type"] == "branch_push"

    def test_push_to_tags_is_ignored(self):
        payload = {
            "ref": "refs/tags/v1.0.0",
            "commits": [],
            "repository": {"name": "r", "full_name": "o/r", "default_branch": "main", "owner": {"login": "o"}},
        }
        state = parse_github_event("push", payload)
        assert state is None


class TestApprovalCallbackParsing:
    def test_valid_callback_returns_dict(self):
        payload = {
            "run_id": "test-run",
            "decision": "approve",
            "approver": "user@example.com",
            "scope": "pr_review",
        }
        result = parse_approval_callback(payload)
        assert result["decision"] == "approve"

    def test_missing_decision_raises(self):
        payload = {"run_id": "test-run", "approver": "user@example.com", "scope": "pr_review"}
        with pytest.raises(WebhookValidationError, match="decision"):
            parse_approval_callback(payload)

    def test_invalid_decision_raises(self):
        payload = {
            "run_id": "test-run",
            "decision": "LGTM",
            "approver": "user@example.com",
            "scope": "pr_review",
        }
        with pytest.raises(WebhookValidationError, match="Invalid decision"):
            parse_approval_callback(payload)
