"""Audit log: immutable record of every tool call, decision, and state transition.

Each entry is written to structlog (stdout/file) and optionally uploaded to
the S3 evidence bucket as NDJSON when finalize_evidence() is called.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_audit_entries: list[dict[str, Any]] = []
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_tool_call(namespace: str, method: str, args: Any, kwargs: Any) -> None:
    entry = {
        "type": "tool_call",
        "timestamp": _now(),
        "tool": f"{namespace}.{method}",
        "args_repr": repr(args)[:200],
        "kwargs_keys": list(kwargs.keys()) if isinstance(kwargs, dict) else [],
    }
    _append(entry)
    log.info("audit.tool_call", tool=entry["tool"])


def record_decision(
    run_id: str,
    node: str,
    decision: str,
    rationale: str = "",
    **extra: Any,
) -> None:
    entry = {
        "type": "decision",
        "timestamp": _now(),
        "run_id": run_id,
        "node": node,
        "decision": decision,
        "rationale": rationale,
        **extra,
    }
    _append(entry)
    log.info("audit.decision", run_id=run_id, node=node, decision=decision)


def record_approval(run_id: str, approval: dict[str, Any]) -> None:
    entry = {
        "type": "approval",
        "timestamp": _now(),
        "run_id": run_id,
        **approval,
    }
    _append(entry)
    log.info("audit.approval", run_id=run_id, scope=approval.get("scope"), decision=approval.get("decision"))


def record_state_transition(run_id: str, from_node: str, to_node: str) -> None:
    entry = {
        "type": "state_transition",
        "timestamp": _now(),
        "run_id": run_id,
        "from": from_node,
        "to": to_node,
    }
    _append(entry)
    log.info("audit.transition", run_id=run_id, from_node=from_node, to_node=to_node)


def get_audit_trail(run_id: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        if run_id:
            return [e for e in _audit_entries if e.get("run_id") == run_id]
        return list(_audit_entries)


def _append(entry: dict[str, Any]) -> None:
    with _lock:
        _audit_entries.append(entry)


def export_to_s3(run_id: str, bucket: str, kms_key_id: str | None = None) -> str | None:
    """Upload this run's audit trail as NDJSON to S3. Returns the S3 URL or None on error."""
    import boto3

    entries = get_audit_trail(run_id)
    if not entries:
        return None

    ndjson = "\n".join(json.dumps(e) for e in entries)
    key = f"audit/{run_id}/audit-trail.ndjson"

    try:
        s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        put_kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": ndjson.encode(),
            "ContentType": "application/x-ndjson",
        }
        if kms_key_id:
            put_kwargs["ServerSideEncryption"] = "aws:kms"
            put_kwargs["SSEKMSKeyId"] = kms_key_id

        s3.put_object(**put_kwargs)
        url = f"s3://{bucket}/{key}"
        log.info("audit.exported_to_s3", run_id=run_id, url=url)
        return url
    except Exception as exc:  # noqa: BLE001
        log.error("audit.s3_export_failed", run_id=run_id, error=str(exc))
        return None
