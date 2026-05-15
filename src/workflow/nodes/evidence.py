"""finalize_evidence node: collect all artifacts and upload the evidence bundle to S3."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

from src.observability.audit import export_to_s3, get_audit_trail
from src.workflow.state import EvidenceItem, WorkflowState

log = structlog.get_logger(__name__)


def finalize_evidence(state: WorkflowState) -> dict[str, Any]:
    run_id = state.get("run_id", "unknown")
    config = state.get("config") or {}
    log.info("node.finalize_evidence.start", run_id=run_id)

    artifact_cfg = config.get("state", {}).get("artifact_store", {})
    bucket = artifact_cfg.get("bucket_env", "")
    kms_key = artifact_cfg.get("kms_key_env", "")

    from src.integrations.secrets import resolve_optional
    resolved_bucket = resolve_optional(bucket) if bucket else None
    resolved_kms = resolve_optional(kms_key) if kms_key else None

    evidence_items: list[EvidenceItem] = []

    evidence_items.extend(_collect_pr_evidence(state))
    evidence_items.extend(_collect_findings_evidence(state))
    evidence_items.extend(_collect_pipeline_evidence(state))
    evidence_items.extend(_collect_approval_evidence(state))

    if resolved_bucket:
        audit_url = export_to_s3(run_id, resolved_bucket, resolved_kms)
        if audit_url:
            evidence_items.append(EvidenceItem(
                type="audit_trail",
                url=audit_url,
                timestamp=datetime.now(timezone.utc).isoformat(),
                description="Complete audit trail (NDJSON)",
            ))

        bundle_url = _upload_evidence_bundle(
            run_id=run_id,
            evidence_items=evidence_items,
            state=state,
            bucket=resolved_bucket,
            kms_key=resolved_kms,
        )
        if bundle_url:
            evidence_items.append(EvidenceItem(
                type="evidence_bundle",
                url=bundle_url,
                timestamp=datetime.now(timezone.utc).isoformat(),
                description="Consolidated evidence bundle (JSON)",
            ))

    log.info("node.finalize_evidence.done", run_id=run_id, items=len(evidence_items))
    return {"evidence": evidence_items, "status": "completed"}


def _collect_pr_evidence(state: WorkflowState) -> list[EvidenceItem]:
    pr = state.get("pull_request") or {}
    if not pr:
        return []
    return [EvidenceItem(
        type="pull_request",
        url=pr.get("url", ""),
        sha=pr.get("head_sha", ""),
        timestamp=datetime.now(timezone.utc).isoformat(),
        description=f"PR #{pr.get('number')} — {pr.get('title', '')}",
    )]


def _collect_findings_evidence(state: WorkflowState) -> list[EvidenceItem]:
    findings = state.get("findings") or []
    if not findings:
        return []
    high = sum(1 for f in findings if f.get("severity") == "HIGH")
    resolved = sum(1 for f in findings if f.get("resolved"))
    return [EvidenceItem(
        type="findings_summary",
        url="",
        timestamp=datetime.now(timezone.utc).isoformat(),
        description=f"{len(findings)} findings ({high} HIGH, {resolved} resolved)",
    )]


def _collect_pipeline_evidence(state: WorkflowState) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            type="pipeline_run",
            url=run.get("url", ""),
            timestamp=datetime.now(timezone.utc).isoformat(),
            description=f"Pipeline: {run.get('pipeline')} — status: {run.get('status')}",
        )
        for run in (state.get("pipeline_runs") or [])
    ]


def _collect_approval_evidence(state: WorkflowState) -> list[EvidenceItem]:
    return [
        EvidenceItem(
            type="approval",
            url="",
            timestamp=a.get("timestamp", ""),
            description=f"{a.get('scope')} — {a.get('decision')} by {a.get('approver')}",
        )
        for a in (state.get("approvals") or [])
    ]


def _upload_evidence_bundle(
    run_id: str,
    evidence_items: list[EvidenceItem],
    state: WorkflowState,
    bucket: str,
    kms_key: str | None,
) -> str | None:
    """Serialize the full state snapshot + evidence index to S3."""
    try:
        import boto3
        import os

        safe_state = {
            k: v for k, v in state.items()
            if k not in ("config", "tool_registry", "mcp_manager", "llm_provider")
        }
        bundle = {
            "run_id": run_id,
            "evidence": evidence_items,
            "state_snapshot": safe_state,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        key = f"evidence/{run_id}/bundle.json"
        s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        put_kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": json.dumps(bundle, default=str).encode(),
            "ContentType": "application/json",
        }
        if kms_key:
            put_kwargs["ServerSideEncryption"] = "aws:kms"
            put_kwargs["SSEKMSKeyId"] = kms_key
        s3.put_object(**put_kwargs)
        return f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001
        log.error("evidence.bundle_upload_failed", run_id=run_id, error=str(exc))
        return None
