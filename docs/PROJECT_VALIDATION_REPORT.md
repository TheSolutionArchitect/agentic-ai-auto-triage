# Project Validation Report

Date: 2026-05-14  
Project: `agentic-ai-auto-triage` / `agentic-terraform-devops`

## Scope

This review validated the local implementation against the stated Terraform DevOps platform requirements in:

- `docs/agentic-ai-terraform-devops-spec.md`
- `docs/RUNBOOK.md`
- `CLAUDE.md`
- `config/config.json`
- `tools/*.json`
- `src/`
- `tests/`

No source code was changed as part of this review.

## Executive Summary

The project contains a clear architecture scaffold for an agentic Terraform DevOps workflow: LangGraph orchestration, typed workflow state, config-driven tool registration, deterministic approval/risk policies, webhook ingestion, evidence collection, and tests around the policy/config/webhook pieces.

However, the implementation is not yet production-ready and does not fully satisfy the functional requirements. The largest gap is that the core external integrations are scaffolded stubs: repository diff collection, IaC scanning, compliance checks, Slack notification, Jira updates, plan/apply triggering, and monitoring do not currently call live MCP tools. The test suite passes, but it mostly validates the scaffold and policy helpers rather than real end-to-end Terraform review, remediation, deployment, and monitoring behavior.

There are also several correctness and design risks that should be addressed before building on this further, especially state accumulation behavior for list fields and incomplete runtime status handling for human-interrupt workflows.

## Validation Results

| Check | Result | Notes |
|---|---:|---|
| Config validation | Pass | `uv run validate-config` validated 6 enabled tools. |
| Unit/integration tests | Pass | `87 passed`; reported coverage is 53%. |
| Ruff lint | Fail | 89 lint issues, including long lines, unused imports/variables, import sorting, and Python modernization warnings. |
| Mypy strict typing | Fail | 44 type errors across 19 files. |
| Requirement implementation | Partial | Graph and policies exist, but most required external workflow capabilities are stubs. |

## Requirement Coverage

| Requirement Area | Status | Evidence |
|---|---|---|
| Configuration-driven workflow and tool setup | Mostly implemented | `config/config.json`, schemas, and `ToolRegistry` validate and load tool config files. |
| Secrets referenced by environment variables | Mostly implemented | `src/integrations/secrets.py` resolves env vars and redacts resolved values. |
| LangGraph orchestration | Implemented as scaffold | `src/workflow/graph.py` defines the target node sequence and approval interrupts. |
| GitHub webhook ingestion | Implemented for basic events | `push`, `pull_request`, and completed `workflow_run` events are parsed. |
| MCP as tool integration layer | Partial | Registry/allowlist exists, but nodes generally do not invoke MCP tools yet. |
| Repository context collection | Stubbed | `_fetch_pr_files_via_mcp()` returns `([], "")`. |
| Terraform fmt/init/validate/static IaC scans | Stubbed/partial | Branch checks return synthetic pass results; PR checks return empty results. |
| LLM-assisted peer review | Partial | Agent exists, but it receives no real diff/file content unless context collection is implemented. |
| Auto-fix loop | Partial | Policy gate and patch validation exist, but file fetch and commit are stubbed. |
| Jira/ITSM updates | Stubbed | Returns `DEVOPS-PENDING` placeholder issues. |
| Slack notifications | Stubbed | `_post_message()` logs `would_post` and returns no new thread. |
| Human PR and plan approval gates | Partial | Interrupt nodes exist; approval policy exists. API status/resume behavior needs hardening. |
| Terraform plan/apply pipeline | Stubbed | Plan/apply return placeholder GitHub Actions URLs and zero plan summary. |
| Monitoring and incident handling | Stubbed | Queued runs are automatically marked `success`. Failure routing does not create real incidents. |
| Evidence bundle | Partial | Local evidence item construction exists; S3 upload is optional and not verified by tests. |
| Observability/audit | Partial | In-memory audit records exist; no durable state transition logging is wired into graph edges. |

## Findings

### High: Core MCP-based functionality is not implemented

Most business-critical nodes are placeholders. Examples:

- `src/workflow/nodes/repo_context.py:55-60` returns no changed files and no diff.
- `src/workflow/nodes/pr_checks.py:47-48`, `64-65`, and `83` return no scanner, compliance, or provider-doc results.
- `src/workflow/nodes/notify.py:96-100` does not send Slack messages.
- `src/workflow/nodes/deploy.py:116-164` returns placeholder pipeline runs and a zero Terraform plan summary.
- `src/workflow/nodes/monitor.py:67-71` converts non-terminal pipeline runs to `success`.
- `docs/RUNBOOK.md:448-450` explicitly documents that MCP calls are scaffolded stubs.

Impact: The system can appear to complete a DevOps workflow without actually inspecting Terraform changes, running scanners, notifying humans, triggering real plans/applies, or monitoring real deployments.

Recommendation: Implement live MCP calls in thin integration adapters first, then wire nodes to those adapters. Add tests using fake MCP clients that assert the exact tool calls, inputs, and failure behavior.

### High: `findings` state is annotated as append-only but nodes try to replace it

`WorkflowState.findings` uses `Annotated[list[Finding], add]` at `src/workflow/state.py:159`. That means LangGraph appends list updates. But `classify_findings()` says it is replacing findings and returns the whole classified list at `src/workflow/nodes/classify_findings.py:45-46`. `auto_fix()` also returns the full updated findings list at `src/workflow/nodes/auto_fix.py:68-70`.

Impact: In real graph execution, classified/resolved findings can be appended to the existing list instead of replacing it. This can duplicate findings, preserve stale unresolved copies, cause auto-fix loops to repeat, inflate evidence counts, and block/proceed incorrectly.

Recommendation: Split append-only event logs from current-state collections. For current findings, use a replacement reducer or store findings in a dict keyed by finding id. Add an integration test that creates one fixable finding, runs classify/auto-fix, and asserts only one current finding remains with the expected resolved state.

### High: PR review lacks actual file content and diff context

The peer review agent receives `changed_files`, `terraform_context`, and `policy_results`, but repository context currently returns empty data when live MCP is unavailable. It does not pass actual Terraform file content or PR diff to the LLM.

Impact: The LLM cannot meaningfully review Terraform changes, identify file/line issues, or generate reliable findings. A PR with risky Terraform changes may proceed because no findings are produced.

Recommendation: Make PR diff and file content collection a hard prerequisite for PR review. Fail closed for protected environments if diff/file collection fails.

### High: Plan/apply safety gates are present but plan data is not real

`generate_plan()` returns a queued placeholder pipeline and `_collect_plan_summary()` returns zero creates/updates/replaces/destroys with no artifact URL. Plan approval for non-dev environments can therefore be requested with no meaningful plan artifact. For dev, apply can proceed without a real plan artifact.

Impact: The implementation does not satisfy the spec requirement that the Terraform plan be a first-class review artifact. It can mark a workflow successful without validating actual infrastructure changes.

Recommendation: Require a real plan artifact URL and parsed plan summary before plan approval or apply. Treat missing plan artifacts as blocked for every environment, including dev.

### High: Monitoring always reports success for queued runs

`_poll_pipeline_status()` changes any non-terminal pipeline run to `success` at `src/workflow/nodes/monitor.py:67-71`.

Impact: Failed, still-running, canceled, or unknown deployments are treated as successful. This undermines the monitoring and incident requirements.

Recommendation: Preserve `queued`/`running` states and use callback or polling retries. Only route success when the external provider returns a terminal success state.

### Medium: API run status is in-memory and not updated for approval resumes

`_run_status` in `src/app/api.py` is an in-memory dictionary. `submit_approval()` resumes the graph at `src/app/api.py:117-124`, but it does not capture the resumed final state or update `_run_status` with evidence/findings/completion details.

Impact: `/runs/{run_id}` can become stale or incorrect, especially across restarts or after human approvals. This weakens run observability and production operability.

Recommendation: Persist run metadata in the configured state store and update run status after every graph resume. Return whether the graph paused again or completed.

### Medium: Test coverage is broad in count but shallow for critical behavior

The suite passes 87 tests, but coverage is 53%. Several critical modules are barely exercised: API 0%, MCP client 0%, deployment node 24%, evidence 22%, approval nodes 29%, auto-fix agent 29%, monitor 26%.

Impact: Passing tests currently do not prove the product requirements are met. They mainly prove schemas, policy helpers, and simple routing behavior.

Recommendation: Add scenario tests for full PR lifecycle, approval resume, plan blocking, failed scanner behavior, failed deployment monitoring, evidence export, and MCP allowlist enforcement during actual node execution.

### Medium: Strict typing and linting fail

`uv run mypy src/` reports 44 errors across 19 files. `uv run ruff check src/ tests/` reports 89 issues.

Impact: The repo advertises strict mypy and ruff workflows, but they are not currently green. Type mismatches are concentrated around `TypedDict` usage, MCP async APIs, and provider API-key types.

Recommendation: Make lint and mypy required CI checks after correcting current violations. Consider normalizing `TypedDict` values to plain dicts at integration boundaries or using Pydantic models for state payloads.

### Medium: MCP client async usage appears incompatible with current adapter types

Mypy reports likely async/API mismatches in `src/integrations/mcp_client.py`, including `get_tools()` returning a coroutine and `__aexit__` not being awaitable according to installed types.

Impact: Even after nodes start using MCP, the client manager may fail at runtime or expose no tools if the adapter API is being called incorrectly.

Recommendation: Verify against the installed `langchain-mcp-adapters` version and add a focused unit test with a fake `MultiServerMCPClient`.

### Medium: Deployment authority boundary is documented but not fully enforceable in code

The code avoids raw Terraform apply, which is good. But the placeholder apply path does not verify that the plan artifact is non-empty, tied to the approved commit SHA, or protected by external environment rules.

Impact: Once real pipeline calls are added, the system may trigger apply without enough local validation unless these invariants are explicit.

Recommendation: Encode apply preconditions: approved PR SHA, immutable plan artifact, target environment, no blocking findings, and valid plan approval where required.

### Low: Config and README naming are inconsistent

`README.md` describes an incident auto-triage system, while `pyproject.toml`, the runbook, and the spec describe a Terraform DevOps lifecycle platform.

Impact: This can confuse maintainers and reviewers about the project purpose.

Recommendation: Update top-level documentation to consistently describe the Terraform DevOps platform or clearly explain the relationship to incident auto-triage.

### Low: Python version expectations are inconsistent

The runbook says Python 3.11 is required, while the local test run used Python 3.13.2. Ruff also emits Python 3.13-style modernization warnings because `target-version = "py311"` still allows some upgrade rules.

Impact: Behavior may differ between local and CI environments if the exact supported Python versions are not tested.

Recommendation: Define a supported Python matrix and run CI on it, starting with 3.11 and the current local version if desired.

## Positive Observations

- The system has a clean high-level graph matching the intended PR review, approval, plan, apply, monitor, and evidence flow.
- Risk and approval decisions are deterministic rather than delegated to the LLM.
- Tool configuration is schema-validated and allowlist-based.
- Secrets are referenced by environment variable name, not hardcoded values.
- The design correctly keeps non-serializable runtime objects out of LangGraph state.
- The runbook is candid about current MCP stubbing and gives useful local setup instructions.

## Recommended Next Steps

1. Fix the `findings` state reducer issue before extending the review loop.
2. Implement repository diff/file retrieval and make PR review fail closed when unavailable.
3. Implement IaC scanner and compliance MCP calls with deterministic failure behavior by environment.
4. Replace placeholder plan/apply/monitor code with real pipeline integration and terminal-state handling.
5. Harden approval resume and run-status persistence.
6. Add high-value integration tests around the full PR lifecycle and approval resume behavior.
7. Make `ruff` and `mypy` green, then enforce them in CI.
8. Update README/project naming so the stated purpose matches the implemented Terraform platform.

## Overall Assessment

The project is a solid architectural prototype, not a complete implementation of the stated requirements. It is ready for iterative development of the real integrations and workflow hardening, but it should not be used to govern or deploy Terraform infrastructure until the stubbed MCP paths, state reducer issue, plan/apply validation, monitoring, and verification gaps are resolved.
