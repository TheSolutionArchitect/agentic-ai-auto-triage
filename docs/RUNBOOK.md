# RUNBOOK — Agentic AI Terraform DevOps Platform

End-to-end guide for setting up, running, and testing the platform locally.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Environment Variables](#2-environment-variables)
3. [Installation](#3-installation)
4. [Configuration](#4-configuration)
5. [Running the API Service](#5-running-the-api-service)
6. [Running the Tests](#6-running-the-tests)
7. [Simulating a Workflow End-to-End](#7-simulating-a-workflow-end-to-end)
8. [Submitting a Human Approval](#8-submitting-a-human-approval)
9. [Production Checklist](#9-production-checklist)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Prerequisites

| Requirement | Minimum version | Notes |
|---|---|---|
| Python | 3.11 | Required by `pyproject.toml` |
| [UV](https://docs.astral.sh/uv/) | Latest | Package manager — replaces pip/venv |
| Git | Any recent | For webhook integration |
| curl / httpie | Any | For manual API testing |

### Install UV

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# Restart your shell or run:
source ~/.zshrc
```

Verify:

```bash
uv --version
```

---

## 2. Environment Variables

The platform resolves all secrets from environment variables — nothing is hardcoded. Set the variables below before starting the service or running tests.

### Required for Core Operation

```bash
# LLM provider — at least one must be set; Anthropic is the primary provider
export ANTHROPIC_API_KEY="sk-ant-..."       # Primary LLM (claude-sonnet-4-6)
export OPENAI_API_KEY="sk-..."             # Fallback LLM (gpt-4o) — optional if Anthropic works

# GitHub MCP — for PR review, branch checks, and comment posting
export GITHUB_MCP_TOKEN="ghp_..."

# Atlassian MCP — for Jira ticket creation and Confluence doc lookup
export ATLASSIAN_MCP_TOKEN="..."

# Slack MCP — for deployment notifications and approval alerts
export SLACK_MCP_TOKEN="xoxb-..."

# Terraform Cloud / Enterprise — for plan and apply pipeline triggers
export TFE_TOKEN="..."
```

### Required for Production State Persistence

```bash
# PostgreSQL — stores LangGraph workflow checkpoints across restarts
# If not set, the service falls back to in-memory state (lost on restart)
export LANGGRAPH_POSTGRES_URL="postgresql://user:password@host:5432/dbname"

# S3 evidence bucket — stores immutable audit bundles
export EVIDENCE_BUCKET="your-s3-bucket-name"
export EVIDENCE_KMS_KEY_ID="arn:aws:kms:..."    # Optional; encrypts evidence at rest
```

### Required for Webhook Signature Validation

```bash
# Must match the secret configured in your GitHub webhook settings
export GITHUB_WEBHOOK_SECRET="your-webhook-secret"
```

### Optional Overrides

```bash
# API server host/port (defaults: 0.0.0.0 / 8000)
export HOST="127.0.0.1"
export PORT="8000"

# Set to "dev" to enable uvicorn auto-reload
export ENV="dev"
```

### Quick Local Dev Setup (Minimal)

**Running tests only** — tests mock all external services and LLM calls. No real env vars are required; the test fixtures set fake values via `monkeypatch`.

**Running the API service locally** — at minimum you need:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."      # real key — used by the LLM provider
export GITHUB_WEBHOOK_SECRET="dev-secret"  # any string — must match your GitHub webhook config
export GITHUB_MCP_TOKEN="test-token"       # placeholder — MCP calls are stubbed
export ATLASSIAN_MCP_TOKEN="test-token"
export SLACK_MCP_TOKEN="test-token"
export TFE_TOKEN="test-token"
```

> **Tip:** Save these to a `.env` file, add `.env` to `.gitignore`, and load with:
> ```bash
> set -a && source .env && set +a
> ```

---

## 3. Installation

```bash
# Clone the repo and enter the project directory
cd agentic-ai-auto-triage

# Create virtual environment and install all dependencies (runtime + dev)
uv sync

# Verify the environment
uv run python -c "import langgraph; import fastapi; print('OK')"
```

This installs all packages declared in `pyproject.toml`, including dev dependencies (`pytest`, `ruff`, `mypy`, `moto`).

---

## 4. Configuration

All workflow behaviour is controlled by [`config/config.json`](../config/config.json). No code changes are needed for common adjustments.

### Key Settings

| Setting | Location in config.json | Default |
|---|---|---|
| LLM provider | `llm.provider` | `"anthropic"` |
| LLM model | `llm.model` | `"claude-sonnet-4-6"` |
| Max auto-fix iterations | `workflow.max_auto_fix_iterations` | `3` |
| Plan approval required | `workflow.require_plan_approval` | `false` for dev, `true` for test/stage/prod |
| Auto-fix confidence thresholds | `risk_policy.auto_fix` | HIGH=0.85, MEDIUM=0.70, LOW=0.60 |

### Validate Config at Startup

```bash
uv run validate-config
```

This runs the JSON Schema validator against `config/config.json` and all six tool config files under `tools/`. It exits with a non-zero code and a clear error message if anything is missing or malformed.

> **Note:** Config is also validated during workflow execution — the `load_config` node runs schema validation on every webhook-triggered run and raises before any tool is initialized if the config is invalid. The `validate-config` command is a pre-flight check you can run manually without starting the service.

### Tool Config Files

Each integration has its own file in `tools/`:

| File | Integration |
|---|---|
| `tools/github.json` | GitHub MCP — PR reads, comments, Actions triggers |
| `tools/jira.json` | Jira MCP — ticket creation and updates |
| `tools/confluence.json` | Confluence MCP — policy doc lookup |
| `tools/slack.json` | Slack MCP — notifications and alerts |
| `tools/terraform.json` | Terraform Cloud MCP — plan/apply pipelines |
| `tools/iac_scanner.json` | IaC scanner MCP — Checkov/tfsec static analysis |

Edit the `allowed_tools` and `disallowed_tools` arrays in each file to tighten or expand what the agent is allowed to call.

---

## 5. Running the API Service

### Development (in-memory state, auto-reload)

```bash
export ENV="dev"
uv run serve
```

The service binds to `0.0.0.0:8000` by default — accessible at `http://localhost:8000`. To restrict to loopback only, set `HOST=127.0.0.1`. Auto-reloads on file changes.

### Production (PostgreSQL checkpointer)

```bash
export LANGGRAPH_POSTGRES_URL="postgresql://user:password@host:5432/dbname"
uv run serve
```

### Verify the Service is Up

```bash
curl http://localhost:8000/health
# {"status":"ok","timestamp":"2026-05-14T10:00:00+00:00"}
```

### API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `POST` | `/webhooks/github` | Receive GitHub webhook events |
| `POST` | `/approvals/{run_id}` | Submit a human approval decision |
| `GET` | `/runs/{run_id}` | Get workflow run status |
| `GET` | `/runs/{run_id}/evidence` | Get evidence items for a completed run |

---

## 6. Running the Tests

All tests are under `tests/`. The suite requires no running services — integrations are mocked.

### Run the Full Suite

```bash
uv run pytest tests/ -v
```

Expected output: **87 tests pass**, coverage report printed to terminal.

### Run Unit Tests Only

```bash
uv run pytest tests/unit/ -v
```

### Run Integration Tests Only

```bash
uv run pytest tests/integration/ -v
```

### Run a Single Test File

```bash
uv run pytest tests/unit/test_risk_policy.py -v
```

### Run a Single Test by Name

```bash
uv run pytest tests/unit/test_risk_policy.py::TestAutoFixPolicy::test_high_severity_auto_fixable -v
```

### Run with Coverage Report

```bash
uv run pytest tests/ --cov=src --cov-report=html
open htmlcov/index.html   # macOS — opens browser
```

### Test File Index

| File | What it tests |
|---|---|
| `tests/unit/test_risk_policy.py` | Auto-fix gating, blocking rules, never-fix categories, loop limits |
| `tests/unit/test_approval_policy.py` | PR approval, plan approval, expiry, per-environment gates |
| `tests/unit/test_config_loader.py` | JSON Schema validation for all 6 tool config files |
| `tests/unit/test_mcp_allowlist.py` | Tool allowlist enforcement — allowed, disallowed, and unknown tools |
| `tests/unit/test_secret_redaction.py` | Secret registration and redaction from log strings |
| `tests/unit/test_webhook_handlers.py` | GitHub webhook parsing for push, PR, and workflow_run events; HMAC validation |
| `tests/integration/test_workflow.py` | Full LangGraph graph execution with mocked runtime — routing, policy gates, state transitions |

### Linting and Type Checking

```bash
# Lint with ruff
uv run ruff check src/ tests/

# Auto-fix lint issues
uv run ruff check --fix src/ tests/

# Type check with mypy
uv run mypy src/
```

---

## 7. Simulating a Workflow End-to-End

With the API service running (`uv run serve`), you can drive the full workflow via curl.

### Simulate a Branch Push Event

```bash
PAYLOAD='{
  "ref": "refs/heads/feature/add-storage-bucket",
  "repository": {"owner": {"login": "example-org"}, "name": "terraform-infra"},
  "commits": [
    {"added": [], "modified": ["modules/storage/main.tf"], "removed": []}
  ],
  "head_commit": {"id": "abc123def456"}
}'

SIGNATURE=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" | awk '{print "sha256="$2}')

curl -s -X POST http://localhost:8000/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: push" \
  -H "X-Hub-Signature-256: $SIGNATURE" \
  -d "$PAYLOAD"
```

Expected response:
```json
{"status": "accepted", "run_id": "<uuid>"}
```

### Simulate a PR Opened Event

```bash
PAYLOAD='{
  "action": "opened",
  "pull_request": {
    "number": 42,
    "html_url": "https://github.com/example-org/terraform-infra/pull/42",
    "head": {"sha": "abc123def456", "ref": "feature/add-storage-bucket"},
    "base": {"ref": "main"}
  },
  "repository": {"owner": {"login": "example-org"}, "name": "terraform-infra"}
}'

SIGNATURE=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" | awk '{print "sha256="$2}')

curl -s -X POST http://localhost:8000/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-Hub-Signature-256: $SIGNATURE" \
  -d "$PAYLOAD"
```

Save the `run_id` from the response — you'll need it to submit approvals.

### Check Run Status

```bash
RUN_ID="<uuid-from-response>"
curl -s http://localhost:8000/runs/$RUN_ID | python3 -m json.tool
```

Typical statuses: `queued` → `running` → `waiting_for_human` → `running` → `completed`.

---

## 8. Submitting a Human Approval

The workflow pauses at two gates — PR approval and (for non-dev environments) Terraform plan approval — and waits for a human decision.

### Approve a PR

```bash
RUN_ID="<uuid>"

curl -s -X POST http://localhost:8000/approvals/$RUN_ID \
  -H "Content-Type: application/json" \
  -d '{
    "decision": "approve",
    "approver": "your.email@example.com",
    "scope": "pr_review",
    "comment": "LGTM — reviewed storage encryption settings"
  }'
```

### Approve a Terraform Plan

```bash
curl -s -X POST http://localhost:8000/approvals/$RUN_ID \
  -H "Content-Type: application/json" \
  -d '{
    "decision": "approve",
    "approver": "your.email@example.com",
    "scope": "terraform_plan",
    "comment": "Plan reviewed — no unexpected resource destroys"
  }'
```

### Reject (Block) a PR or Plan

```bash
curl -s -X POST http://localhost:8000/approvals/$RUN_ID \
  -H "Content-Type: application/json" \
  -d '{
    "decision": "reject",
    "approver": "your.email@example.com",
    "scope": "pr_review",
    "comment": "Security finding not resolved"
  }'
```

---

## 9. Production Checklist

Before deploying to a production environment, verify the following:

- [ ] `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`) set in the runtime environment / secrets manager
- [ ] `GITHUB_WEBHOOK_SECRET` matches the secret configured in GitHub → Settings → Webhooks
- [ ] `LANGGRAPH_POSTGRES_URL` set — without it, state is lost on process restart
- [ ] `EVIDENCE_BUCKET` set and the service IAM role has `s3:PutObject` permission on that bucket
- [ ] S3 bucket has Object Lock (WORM) enabled for evidence immutability
- [ ] `EVIDENCE_KMS_KEY_ID` set for evidence encryption at rest
- [ ] GitHub repository name and owner in `tools/github.json` match your actual repository
- [ ] `config.json` → `workflow.allowed_target_branches` matches your branch strategy
- [ ] Tool `disallowed_tools` lists reviewed — especially `terraform.json` which blocks `apply_run` directly
- [ ] The CI/CD service identity used for apply has least-privilege IAM (not the agent's identity)
- [ ] Branch protection rules on `main` enforce required reviewers — the agent never merges directly
- [ ] All `tools/*.json` MCP server URLs updated from placeholder to real MCP server endpoints
- [ ] OpenTelemetry endpoint configured (set `OTEL_EXPORTER_OTLP_ENDPOINT`) if using distributed tracing
- [ ] `uv run validate-config` exits 0

---

## 10. Troubleshooting

### `RuntimeError: Required secret '...' is not set`

The named environment variable is missing. Set it and restart the service. See [Section 2](#2-environment-variables).

### `Config validation failed` on startup

Run `uv run validate-config` to see the exact schema violation. Common causes: missing required fields in `config.json`, or a tool JSON file references a non-existent `allowed_tools` entry.

### Graph paused — workflow stuck at `waiting_for_human`

This is expected behaviour at approval gates. Submit an approval via `POST /approvals/{run_id}` as shown in [Section 8](#8-submitting-a-human-approval).

### `TypeError: Type is not msgpack serializable`

Non-serializable objects (ToolRegistry, LLMProvider) must not be stored in LangGraph state. These are managed by `src/integrations/runtime.py` (the process-level runtime store). If you add new node code that stores a custom object in `state`, move it to `runtime.set_runtime()` instead.

### `structlog` raises `ValueError: 'event' is a reserved keyword`

Do not pass `event=` as a keyword argument to any structlog logger call. Use a different key name (e.g., `event_name=`, `action=`).

### Tests fail with `ANTHROPIC_API_KEY not set`

Tests mock out the LLM provider — they do not call real APIs. If this error appears, a test is not properly patching `build_provider`. Check that `monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")` is present in the failing test fixture.

### MCP tool calls return empty / do nothing

All MCP tool implementations are currently scaffolded stubs (marked with `# When MCP connected:` comments). To activate real integrations, implement the actual MCP client calls in each node file (`src/workflow/nodes/`) and ensure the corresponding MCP server URL in `tools/<name>.json` is reachable.

### `uv: command not found`

UV was not installed or the shell PATH was not reloaded. Re-run the UV install command from [Section 1](#1-prerequisites) and open a new terminal.
