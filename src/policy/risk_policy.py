"""Deterministic risk policy engine.

All auto-fix and merge/apply gate decisions are made here — not by the LLM.
The LLM produces findings with severity and confidence; this module decides
whether those findings permit automated remediation or block progression.
"""

from __future__ import annotations

from typing import Any

import structlog

from src.workflow.state import Finding, Severity

log = structlog.get_logger(__name__)


class RiskPolicy:
    def __init__(self, config: dict[str, Any]) -> None:
        risk = config.get("risk_policy", {})
        self._thresholds: dict[Severity, float] = {
            "HIGH":   risk.get("auto_fix", {}).get("HIGH", 0.85),
            "MEDIUM": risk.get("auto_fix", {}).get("MEDIUM", 0.70),
            "LOW":    risk.get("auto_fix", {}).get("LOW", 0.60),
        }
        self._never_fix: set[str] = set(risk.get("never_auto_fix_categories", []))
        self._block_merge_on: set[str] = set(risk.get("block_merge_on", ["HIGH"]))
        self._block_apply_on: set[str] = set(risk.get("block_apply_on", ["HIGH", "unapproved_destroy"]))

    def is_auto_fixable(self, finding: Finding) -> bool:
        """Return True only when the finding may be auto-fixed per policy.

        Rules (all must pass):
        - fix_type must be "auto_fix_allowed"
        - category must not be in never_auto_fix_categories
        - confidence must meet or exceed the severity threshold
        """
        if finding.get("fix_type") != "auto_fix_allowed":
            return False
        category = finding.get("category", "")
        if category in self._never_fix:
            log.info("risk_policy.auto_fix_blocked_category", category=category)
            return False
        severity: Severity = finding.get("severity", "LOW")  # type: ignore[assignment]
        confidence: float = finding.get("confidence", 0.0)
        threshold = self._thresholds.get(severity, 1.0)
        if confidence < threshold:
            log.info(
                "risk_policy.auto_fix_blocked_confidence",
                severity=severity,
                confidence=confidence,
                threshold=threshold,
            )
            return False
        return True

    def auto_fixable_findings(self, findings: list[Finding]) -> list[Finding]:
        return [f for f in findings if self.is_auto_fixable(f)]

    def human_required_findings(self, findings: list[Finding]) -> list[Finding]:
        return [f for f in findings if not self.is_auto_fixable(f) and not f.get("resolved")]

    def blocks_merge(self, findings: list[Finding]) -> list[Finding]:
        """Return findings that must be resolved before PR merge is permitted."""
        active = [f for f in findings if not f.get("resolved")]
        blocked = []
        for f in active:
            severity = f.get("severity", "LOW")
            if severity in self._block_merge_on:
                blocked.append(f)
        return blocked

    def blocks_apply(self, findings: list[Finding]) -> list[Finding]:
        """Return findings that must be resolved before terraform apply is permitted."""
        active = [f for f in findings if not f.get("resolved")]
        blocked = []
        for f in active:
            severity = f.get("severity", "LOW")
            category = f.get("category", "")
            if severity in self._block_apply_on or category == "unapproved_destroy":
                blocked.append(f)
        return blocked

    def max_auto_fix_iterations(self, config: dict[str, Any]) -> int:
        return config.get("workflow", {}).get("max_auto_fix_iterations", 3)

    def max_files_per_auto_fix(self, config: dict[str, Any]) -> int:
        return config.get("workflow", {}).get("max_files_per_auto_fix", 10)

    def is_loop_limit_reached(self, iterations: int, config: dict[str, Any]) -> bool:
        return iterations >= self.max_auto_fix_iterations(config)
