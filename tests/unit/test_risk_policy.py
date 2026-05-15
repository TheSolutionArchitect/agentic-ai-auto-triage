"""Tests for deterministic risk policy engine."""

import json
from pathlib import Path

import pytest

from src.policy.risk_policy import RiskPolicy
from src.workflow.state import Finding

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def config() -> dict:
    cfg_path = Path(__file__).parent.parent.parent / "config" / "config.json"
    return json.loads(cfg_path.read_text())


@pytest.fixture
def findings() -> list[Finding]:
    return json.loads((FIXTURES / "sample_findings.json").read_text())


@pytest.fixture
def policy(config) -> RiskPolicy:
    return RiskPolicy(config)


class TestAutoFixEligibility:
    def test_high_confidence_encryption_is_fixable(self, policy, findings):
        enc = next(f for f in findings if f["category"] == "encryption_disabled")
        assert policy.is_auto_fixable(enc)

    def test_public_network_is_never_fixable(self, policy, findings):
        net = next(f for f in findings if f["category"] == "public_network_exposure")
        assert not policy.is_auto_fixable(net)

    def test_iam_privilege_is_never_fixable(self, policy, findings):
        iam = next(f for f in findings if f["category"] == "iam_privilege_expansion")
        assert not policy.is_auto_fixable(iam)

    def test_medium_confidence_above_threshold_is_fixable(self, policy, findings):
        tags = next(f for f in findings if f["category"] == "missing_tags")
        assert policy.is_auto_fixable(tags)

    def test_low_confidence_below_threshold_is_not_fixable(self, policy):
        low_confidence = Finding(
            id="test",
            file_path="main.tf",
            severity="MEDIUM",
            confidence=0.50,  # below 0.70 threshold
            category="missing_tags",
            rationale="test",
            fix_recommendation="test",
            fix_type="auto_fix_allowed",
        )
        assert not policy.is_auto_fixable(low_confidence)

    def test_human_required_finding_is_not_fixable(self, policy):
        f = Finding(
            id="test",
            file_path="main.tf",
            severity="HIGH",
            confidence=0.99,
            category="encryption_disabled",
            rationale="test",
            fix_recommendation="test",
            fix_type="human_required",  # explicitly blocked
        )
        assert not policy.is_auto_fixable(f)

    def test_never_fix_category_blocks_auto_fix(self, policy):
        for category in [
            "terraform_backend_change",
            "state_migration",
            "resource_destroy",
            "iam_privilege_expansion",
            "public_network_exposure",
            "provider_major_upgrade",
            "production_database_change",
        ]:
            f = Finding(
                id="test",
                file_path="main.tf",
                severity="LOW",
                confidence=1.0,
                category=category,
                rationale="test",
                fix_recommendation="test",
                fix_type="auto_fix_allowed",
            )
            assert not policy.is_auto_fixable(f), f"Expected {category} to be blocked"


class TestMergeGating:
    def test_high_severity_blocks_merge(self, policy, findings):
        blocking = policy.blocks_merge(findings)
        blocking_severities = {f["severity"] for f in blocking}
        assert "HIGH" in blocking_severities

    def test_resolved_finding_does_not_block(self, policy, findings):
        resolved = [dict(f, resolved=True) for f in findings]
        assert policy.blocks_merge(resolved) == []

    def test_medium_does_not_block_merge_by_default(self, policy):
        medium_only = [Finding(
            id="test",
            file_path="main.tf",
            severity="MEDIUM",
            confidence=0.9,
            category="missing_tags",
            rationale="test",
            fix_recommendation="test",
            fix_type="human_required",
            resolved=False,
        )]
        assert policy.blocks_merge(medium_only) == []


class TestLoopLimits:
    def test_loop_limit_reached(self, policy, config):
        max_iter = config["workflow"]["max_auto_fix_iterations"]
        assert policy.is_loop_limit_reached(max_iter, config)
        assert not policy.is_loop_limit_reached(max_iter - 1, config)

    def test_auto_fixable_filtered(self, policy, findings):
        fixable = policy.auto_fixable_findings(findings)
        for f in fixable:
            assert f["fix_type"] == "auto_fix_allowed"
            assert f["category"] not in {
                "public_network_exposure", "iam_privilege_expansion"
            }
