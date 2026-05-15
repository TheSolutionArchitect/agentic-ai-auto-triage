# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This System Does

An agentic AI platform that reviews, auto-fixes, governs, deploys, and monitors Terraform IaC changes. LangGraph orchestrates a stateful multi-agent workflow triggered by GitHub webhooks. MCP servers are the exclusive tool integration layer for GitHub, Jira, Confluence, Slack, and Terraform. Anthropic (primary) and OpenAI (fallback) are supported as swappable LLM providers via `config.json`.

## Commands

```bash
# Install all deps (runtime + dev) using UV
uv sync

# Start the API service (dev mode with auto-reload)
ENV=dev uv run serve

# Validate config + all tool JSON files at startup
uv run validate-config

# Run all tests
uv run pytest tests/ -v

# Run a single test file
uv run pytest tests/unit/test_risk_policy.py -v

# Run a single test by name
uv run pytest tests/unit/test_risk_policy.py::TestAutoFixPolicy::test_high_severity_auto_fixable -v

# Lint and type check
uv run ruff check src/ tests/
uv run mypy src/
```

See [docs/RUNBOOK.md](docs/RUNBOOK.md) for environment variable setup, API usage, and production checklist.

## Architecture

### Request Flow

```
GitHub Webhook → POST /webhooks/github
    → parse_github_event()        # validates HMAC, parses event
    → build_graph().invoke()       # async, in background task
        → load_config             # reads config/config.json, validates schema
        → initialize_tools        # builds ToolRegistry + LLMProvider, stores in runtime store
        → route_event             # dispatches to branch_push or PR flow
```

### Graph Node Sequence (PR flow)

```
route_event → collect_repo_context → run_pr_checks → peer_review → classify_findings
    → review_loop_gate ──► auto_fix ──► update_itsm ──► (loop back or proceed)
                      └──► [INTERRUPT] request_pr_approval
    → notify → merge_or_wait → generate_plan
    → [INTERRUPT] request_plan_approval
    → trigger_apply → monitor_deployment → finalize_evidence → END
```

### Runtime Store Pattern (critical)

LangGraph's MemorySaver/PostgresSaver cannot serialize `ToolRegistry` or `LLMProvider`. These objects are stored in a process-level dict in `src/integrations/runtime.py`, keyed by `run_id`:

```python
# initialize_tools node — stores after creation
runtime.set_runtime(run_id, tool_registry=registry, llm_provider=llm)
return {}  # never put non-serializable objects in returned state

# every other node — retrieves
rt = runtime.get_runtime(run_id)
registry = rt.get("tool_registry")
llm = rt.get("llm_provider")
```

If you add a new node that needs tools or the LLM, always use `runtime.get_runtime()` — never read from `state.get("tool_registry")` (that field is never populated).

### Human Interrupts

The graph pauses at two mandatory gates using `langgraph.types.interrupt()`:

- `request_pr_approval` — always required
- `request_plan_approval` — skipped for `dev` environment, required for `test`/`stage`/`prod`

The graph resumes when `POST /approvals/{run_id}` is called, which calls `graph.invoke(Command(resume=decision), config={"configurable": {"thread_id": run_id}})`.

### Policy Engines (deterministic — no LLM)

**`src/policy/risk_policy.py` — `RiskPolicy`:**
- `auto_fixable_findings(findings)` — must have `fix_type == "auto_fix_allowed"`, not in `never_auto_fix_categories`, and `confidence >= threshold` for severity
- `blocks_merge(findings)` — active (unresolved) HIGH findings
- `blocks_apply(findings)` — active HIGH + unapproved destroy findings
- `is_loop_limit_reached(iterations, config)` — compares against `max_auto_fix_iterations`

**`src/policy/approval_policy.py` — `ApprovalPolicy`:**
- `requires_plan_approval(environment)` — from `config.json workflow.require_plan_approval`
- `is_pr_approved(approvals)` — finds valid (non-expired) approval with `scope == "pr_review"`
- `is_plan_approved(approvals)` — finds valid approval with `scope == "terraform_plan"`

### Review-and-Fix Loop

After `classify_findings`, `review_loop_gate` returns a routing key:
- `continue_loop` → `auto_fix` → `update_itsm` → back to `run_pr_checks`
- `wait_human` → `update_itsm` → `notify` → END (paused)
- `proceed` → `update_itsm` → `request_pr_approval`
- `fail` → `notify` → END

Loop iteration is tracked in `state["auto_fix_iterations"]` (int, incremented by the `auto_fix` node). The cap is `config.workflow.max_auto_fix_iterations` (default: 3).

### Auto-Fix Safety

`src/agents/auto_fix.py` enforces `_NEVER_FIX_CATEGORIES` before any LLM call. Patches are validated with `_validate_patch()` — a unified diff that touches files outside the PR scope is rejected. Fix confidence is required to meet the per-severity threshold from `config.json`.

### MCP Tool Allowlists

`src/integrations/tool_registry.py` loads each `tools/<name>.json` at startup. Every node receives only the tool namespaces declared for its agent role. The `is_tool_call_allowed(tool_name, method)` method is the enforcement point — it's called by `_AuditedTool` in `mcp_client.py` before dispatch. All MCP calls are currently scaffolded stubs marked `# When MCP connected:`.

### State Shape

`WorkflowState` is a `TypedDict(total=False)`. All list fields use `Annotated[list[X], add]` (operator.add) so nodes safely return partial updates that are appended, not replaced. Scalar fields are replaced. Nodes return only the keys they modify — never a full state copy.

### structlog Constraint

Do not pass `event=` as a keyword argument to any structlog call — `event` is reserved. Use `event_name=`, `action=`, or similar.

## Key Files

| File | Role |
|---|---|
| [src/workflow/graph.py](src/workflow/graph.py) | Full graph assembly — all nodes, edges, and routing |
| [src/workflow/state.py](src/workflow/state.py) | `WorkflowState` TypedDict + all inner types + `new_run_state()` |
| [src/integrations/runtime.py](src/integrations/runtime.py) | Process-level store for non-serializable objects |
| [src/policy/risk_policy.py](src/policy/risk_policy.py) | Deterministic auto-fix and blocking decisions |
| [src/policy/approval_policy.py](src/policy/approval_policy.py) | Environment-aware approval gate enforcement |
| [src/workflow/nodes/approvals.py](src/workflow/nodes/approvals.py) | `interrupt()` calls and approval construction |
| [src/workflow/nodes/review_loop_gate.py](src/workflow/nodes/review_loop_gate.py) | Routing function for the review-and-fix loop |
| [src/app/api.py](src/app/api.py) | FastAPI app — webhook ingestion and approval callback |
| [config/config.json](config/config.json) | Single source of truth for all workflow configuration |

## Configuration Rules

- `config.json` is schema-validated at startup against `config/schemas/config.schema.json`. Invalid config fails closed.
- Each tool has its own `tools/<name>.json`. Tool configs are also schema-validated.
- Secrets are resolved by name from environment variables in `src/integrations/secrets.py` — only env var *names* appear in config files.
- The `llm.provider` field selects the active LLM. The `llm.fallback` block is tried automatically if the primary fails.

## Deployment Authority Boundary

The LLM proposes; CI/CD executes. `trigger_apply` and `generate_plan` trigger external pipelines and return `queued` status — they never call `terraform apply` directly. Apply requires an approved commit SHA and plan artifact enforced via environment protection rules outside the agent.

`merge_or_wait` never merges directly — it records that PR approval is in place and defers to GitHub branch protection rules.

## Test Structure

```
tests/unit/
  test_risk_policy.py          # 15 tests — all auto-fix and blocking scenarios
  test_approval_policy.py      # PR approval, plan approval, expiry, per-env gates
  test_config_loader.py        # JSON Schema validation for all 6 tool configs
  test_mcp_allowlist.py        # Allowlist enforcement — allowed, disallowed, unknown
  test_secret_redaction.py     # Secret registration and redaction
  test_webhook_handlers.py     # HMAC validation, push/PR/pipeline event parsing
tests/integration/
  test_workflow.py             # Full graph execution with mocked runtime
```

Integration tests pre-seed the runtime store via `runtime.set_runtime()` and patch `build_provider` + `ToolRegistry` to avoid real credential requirements. Do not require a running API or real MCP servers.
