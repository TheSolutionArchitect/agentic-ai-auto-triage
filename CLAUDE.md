# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

This project is **pre-implementation**. The full technical specification is in [docs/agentic-ai-terraform-devops-spec.md](docs/agentic-ai-terraform-devops-spec.md). No application code exists yet. Implementation follows the six phases defined in Section 19 of the spec.

## What This System Does

An agentic AI platform that reviews, fixes, governs, deploys, and monitors Terraform infrastructure-as-code changes. LangGraph orchestrates a multi-agent workflow triggered by GitHub webhooks. MCP servers are the exclusive tool integration layer for GitHub, Jira, Confluence, Slack, and Terraform. OpenAI and Anthropic are supported as swappable LLM providers via configuration.

## Commands

Once implementation begins, commands will live under `src/`. Expected entry points based on spec Section 18:

```bash
# Install dependencies (Python 3.11+)
pip install -r requirements.txt

# Run the API service
python -m src.app.api

# Run tests
pytest tests/unit/
pytest tests/integration/

# Validate config on startup (also runs automatically)
python -m src.integrations.tool_registry --validate
```

No `requirements.txt` or runnable code exists yet.

## Intended Repository Layout

```
config/
  config.json              # Global workflow config — single source of truth
  schemas/                 # JSON schemas for config, findings, approvals, evidence
tools/
  github.json              # Per-tool MCP config files (one per integration)
  jira.json
  confluence.json
  slack.json
  terraform.json
  iac_scanner.json
src/
  app/                     # API service + webhook handlers
  workflow/
    graph.py               # LangGraph graph definition
    state.py               # Strongly-typed workflow state (TypedDict)
    nodes/                 # One file per graph node
  agents/                  # LLM-backed agent logic
  integrations/
    mcp_client.py          # MCP session management + allowlist enforcement
    llm_provider.py        # OpenAI/Anthropic adapter interface
    tool_registry.py       # Loads and validates tool configs at startup
    secrets.py             # Env var / secrets manager resolution
  policy/                  # Risk and approval policy engines
  observability/           # Tracing and audit
tests/
  fixtures/                # Historical Terraform PRs used as replay fixtures
```

## Architecture and Key Design Rules

### Configuration-Driven

- `config.json` controls workflow behaviour, model provider, thresholds, loop limits, and which tool configs to load.
- Each tool has its own `tools/<name>.json` — changing one tool config must not require editing another.
- Config is schema-validated at startup; invalid or missing config **fails closed** before any tool is initialized.
- Secrets are never in config or code — only environment variable *names* appear in config files.

### MCP as the Exclusive Tool Boundary

Agents must not call GitHub, Jira, Slack, Confluence, or Terraform APIs directly. All tool calls go through the MCP client layer, which:
- enforces per-agent tool allowlists from the tool JSON configs
- injects auth without exposing credentials to the LLM
- records every call for audit

Each tool namespace (`github.*`, `jira.*`, etc.) is isolated. Agents receive only the tools their role requires.

### LangGraph Workflow

The graph is event-driven: `branch_push`, `pr_opened`, `pr_updated`, `approval_received`, `pipeline_event`, `monitor_event`. Key routing rules:

- The **Orchestrator** node is the only node that decides routing — it reads state, evaluates gates, and delegates; it never does tool work itself.
- The review-and-fix loop (`peer_review → classify_findings → auto_fix → update_itsm → pr_checks`) has a configured iteration cap (`max_auto_fix_iterations` in `config.json`).
- Human approval **pauses** the graph via LangGraph interrupts at two mandatory gates: PR approval and (for non-dev environments) Terraform plan approval.
- Workflow state is strongly typed and checkpointed to PostgreSQL so the graph can pause and resume across process restarts.

### Auto-Fix Policy (from `config.json` `risk_policy`)

| Severity | Min confidence for auto-fix |
|---|---|
| HIGH | 0.85 |
| MEDIUM | 0.70 |
| LOW | 0.60 |

Never auto-fix: `terraform_backend_change`, `state_migration`, `resource_destroy`, `iam_privilege_expansion`, `public_network_exposure`, `provider_major_upgrade`, `production_database_change`. Auto-fix commits must use a bot identity with branch protection — the LLM does not hold merge rights.

### LLM Provider Adapter

Implement `LLMProvider` as an interface with methods: `generate_structured_output`, `summarize`, `classify_findings`, `propose_fix`. OpenAI and Anthropic are both implemented behind this adapter; the active provider is selected from `config.json`. Never pass secrets into model prompts; redact provider tokens, Terraform state content, and cloud credentials before any LLM call.

### Deployment Authority Boundary

The LLM reasons and proposes — it never executes `terraform apply`. The deployment agent triggers a CI/CD pipeline and monitors its status. Apply requires an approved commit SHA, an approved plan artifact, and a least-privilege CI/CD service identity. These must be enforced outside the agent (environment protection rules), not only within it.

## Spec Validation Notes

When implementing, apply these corrections to the spec:

- **`config.json` model ID**: `"gpt-5.4"` in the spec example is not a real OpenAI model. Use `"gpt-4o"` or `"o3"` until superseded by a real release.
- **`merge_or_wait` node**: The spec says it "may" use branch protection — it must *always* defer merges to branch protection; the agent must never merge directly.
- **Policy agent fallback**: When policy tools (OPA/Checkov/etc.) are unavailable, fail closed in production and fail open only in sandbox. Make this explicit in the compliance node implementation.
- **Evidence immutability**: Use object-lock or WORM storage on the S3 evidence bucket — the spec says "immutable evidence bundle" but does not specify the storage mechanism.

## Environment Variables Required

```
OPENAI_API_KEY
ANTHROPIC_API_KEY
GITHUB_MCP_TOKEN
ATLASSIAN_MCP_TOKEN
SLACK_MCP_TOKEN
TFE_TOKEN
LANGGRAPH_POSTGRES_URL
EVIDENCE_BUCKET
EVIDENCE_KMS_KEY_ID
```

Source from a secrets manager (Vault, AWS Secrets Manager, etc.) in production — never hardcode.

## Implementation Phases

| Phase | Focus |
|---|---|
| 1 | Config schemas, config loader, LLM adapter, MCP tool registry, LangGraph state + checkpointer, webhook ingestion |
| 2 | Repo context node, GitHub/Confluence/Jira MCP integration, peer review structured output, PR comments, JIRA updates |
| 3 | Auto-fix policy engine, patch generation with scope guards, Terraform fmt/validate loop, loop limits |
| 4 | LangGraph interrupt/resume, approval UI or Slack/Jira bridge, durable approval recording |
| 5 | CI/CD trigger, plan artifact collection, risk summary, apply monitoring, incident routing, evidence bundle |
| 6 | Full observability, redaction + prompt-injection filters, RBAC, audit export, prompt tuning |
