"""Tests for approval gate policy engine."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.policy.approval_policy import ApprovalPolicy
from src.workflow.state import Approval


@pytest.fixture
def config() -> dict:
    return json.loads(
        (Path(__file__).parent.parent.parent / "config" / "config.json").read_text()
    )


@pytest.fixture
def policy(config) -> ApprovalPolicy:
    return ApprovalPolicy(config)


def _make_approval(scope: str, decision: str = "approve", expires_delta_hours: int = 24) -> Approval:
    return Approval(
        decision=decision,
        approver="reviewer@example.com",
        scope=scope,
        timestamp=datetime.now(timezone.utc).isoformat(),
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=expires_delta_hours)).isoformat(),
        run_id="test-run",
    )


class TestPRApprovalGate:
    def test_no_approvals_is_not_approved(self, policy):
        assert not policy.is_pr_approved([])

    def test_valid_pr_approval_is_approved(self, policy):
        approval = _make_approval(scope="pr_review")
        assert policy.is_pr_approved([approval])

    def test_rejected_decision_is_not_approved(self, policy):
        approval = _make_approval(scope="pr_review", decision="reject")
        assert not policy.is_pr_approved([approval])

    def test_wrong_scope_is_not_approved(self, policy):
        approval = _make_approval(scope="terraform_plan")
        assert not policy.is_pr_approved([approval])

    def test_expired_approval_is_not_valid(self, policy):
        expired = _make_approval(scope="pr_review", expires_delta_hours=-1)
        assert not policy.is_pr_approved([expired])

    def test_approve_with_exception_is_valid(self, policy):
        approval = _make_approval(scope="pr_review", decision="approve_with_exception")
        assert policy.is_pr_approved([approval])


class TestPlanApprovalGate:
    def test_prod_requires_plan_approval(self, policy):
        assert policy.requires_plan_approval("prod") is True

    def test_dev_does_not_require_plan_approval(self, policy):
        assert policy.requires_plan_approval("dev") is False

    def test_stage_requires_plan_approval(self, policy):
        assert policy.requires_plan_approval("stage") is True

    def test_valid_plan_approval(self, policy):
        approval = _make_approval(scope="terraform_plan")
        assert policy.is_plan_approved([approval])

    def test_pr_approval_does_not_satisfy_plan_gate(self, policy):
        approval = _make_approval(scope="pr_review")
        assert not policy.is_plan_approved([approval])


class TestPayloadValidation:
    def test_valid_payload(self, policy):
        payload = {
            "decision": "approve",
            "approver": "user@example.com",
            "scope": "pr_review",
            "timestamp": "2026-05-14T10:00:00Z",
        }
        assert policy.validate_approval_payload(payload) == []

    def test_missing_required_field(self, policy):
        payload = {"decision": "approve", "approver": "user@example.com", "scope": "pr_review"}
        errors = policy.validate_approval_payload(payload)
        assert any("timestamp" in e for e in errors)

    def test_invalid_decision(self, policy):
        payload = {
            "decision": "yes_please",
            "approver": "user@example.com",
            "scope": "pr_review",
            "timestamp": "2026-05-14T10:00:00Z",
        }
        errors = policy.validate_approval_payload(payload)
        assert any("decision" in e for e in errors)

    def test_invalid_scope(self, policy):
        payload = {
            "decision": "approve",
            "approver": "user@example.com",
            "scope": "rubber_stamp",
            "timestamp": "2026-05-14T10:00:00Z",
        }
        errors = policy.validate_approval_payload(payload)
        assert any("scope" in e for e in errors)
