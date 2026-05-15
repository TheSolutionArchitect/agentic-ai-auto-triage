"""FastAPI application: webhook ingestion, approval callbacks, and run status."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import structlog
import uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from langgraph.types import Command

from src.app.webhook_handlers import (
    WebhookValidationError,
    parse_approval_callback,
    parse_github_event,
    validate_github_signature,
)
from src.observability.tracing import setup_tracing
from src.workflow.graph import build_graph

log = structlog.get_logger(__name__)

_graph: Any = None
_run_status: dict[str, dict[str, Any]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _graph
    setup_tracing()

    postgres_url = os.environ.get("LANGGRAPH_POSTGRES_URL", "")
    if postgres_url:
        from src.workflow.graph import build_graph_postgres
        _graph = await build_graph_postgres(postgres_url)
        log.info("api.graph_ready", checkpointer="postgres")
    else:
        _graph = build_graph()
        log.info("api.graph_ready", checkpointer="memory")

    yield
    log.info("api.shutdown")


app = FastAPI(
    title="Agentic Terraform DevOps",
    version="0.1.0",
    description="LangGraph-powered Terraform review, fix, and deployment pipeline",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    event_name = request.headers.get("X-GitHub-Event", "")

    try:
        validate_github_signature(body, signature)
    except WebhookValidationError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    try:
        initial_state = parse_github_event(event_name, payload)
    except Exception as exc:  # noqa: BLE001
        log.error("webhook.parse_error", event_name=event_name, error=str(exc))
        raise HTTPException(status_code=422, detail=f"Payload parse error: {exc}")

    if initial_state is None:
        return JSONResponse({"status": "ignored", "event": event_name})

    run_id = initial_state["run_id"]
    _run_status[run_id] = {"status": "queued", "run_id": run_id}
    background_tasks.add_task(_run_workflow, initial_state)

    log.info("webhook.accepted", event_name=event_name, run_id=run_id)
    return JSONResponse({"status": "accepted", "run_id": run_id}, status_code=202)


@app.post("/approvals/{run_id}")
async def submit_approval(run_id: str, request: Request) -> JSONResponse:
    """Receive a human approval decision and resume the paused graph."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    body["run_id"] = run_id
    try:
        decision = parse_approval_callback(body)
    except WebhookValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if _graph is None:
        raise HTTPException(status_code=503, detail="Graph not initialized")

    config = {"configurable": {"thread_id": run_id}}
    try:
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _graph.invoke(Command(resume=decision), config=config),
        )
        log.info("approval.resumed", run_id=run_id, decision=decision.get("decision"))
        return JSONResponse({"status": "resumed", "run_id": run_id})
    except Exception as exc:  # noqa: BLE001
        log.error("approval.resume_failed", run_id=run_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Resume failed: {exc}")


@app.get("/runs/{run_id}")
async def get_run_status(run_id: str) -> JSONResponse:
    """Return the current status of a workflow run."""
    status = _run_status.get(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")
    return JSONResponse(status)


@app.get("/runs/{run_id}/evidence")
async def get_run_evidence(run_id: str) -> JSONResponse:
    """Return the evidence items for a completed run."""
    status = _run_status.get(run_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")
    return JSONResponse({"run_id": run_id, "evidence": status.get("evidence", [])})


async def _run_workflow(initial_state: dict[str, Any]) -> None:
    """Execute the LangGraph workflow in the background."""
    run_id = initial_state.get("run_id", "unknown")
    config = {"configurable": {"thread_id": run_id}}

    try:
        _run_status[run_id] = {"status": "running", "run_id": run_id}
        log.info("workflow.start", run_id=run_id)

        final_state = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _graph.invoke(initial_state, config=config),
        )

        _run_status[run_id] = {
            "status": final_state.get("status", "completed"),
            "run_id": run_id,
            "evidence": final_state.get("evidence", []),
            "findings_count": len(final_state.get("findings", [])),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        log.info("workflow.complete", run_id=run_id, status=_run_status[run_id]["status"])

    except Exception as exc:  # noqa: BLE001
        log.error("workflow.failed", run_id=run_id, error=str(exc))
        _run_status[run_id] = {
            "status": "failed",
            "run_id": run_id,
            "error": str(exc),
        }


def main() -> None:
    uvicorn.run(
        "src.app.api:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("ENV", "prod") == "dev",
        log_level="info",
    )


if __name__ == "__main__":
    main()
