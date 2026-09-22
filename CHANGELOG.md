# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## 0.1.0 (2026-09-22)

Initial release.

### Added

- **`build_agent()`**, one `create_harness()` call configured for a `local` or a `cloud` profile, switched
  by a single environment variable. `platform.detect()` owns every backend decision: session, memory,
  skills, built-in tools, workspace, model, and effort.
- **AgentCore HTTP runtime** (`OpsInvestigator`) with a versioned `v: 1` SSE frame schema, a payload
  schema, a per-session agent cache with turn locking and actor ownership, a per-pull wall clock, and a
  bounded closing turn on a budget trip. Every request ends with exactly one `done` frame.
- **AgentCore A2A runtime** (`OpsInvestigatorA2A`) speaking A2A **1.0** on `a2a-sdk` 1.x, with a
  hand-built agent card at `/.well-known/agent-card.json` and a `HarnessExecutor` over the same
  `build_agent()`. Every failure is reported as a task state.
- **Three read-only AWS tools**: `aws_call` (operation allowlist, secret-bearing deny table, JMESPath
  `query`, 40k output cap, 40 calls per turn), `logs_insights` (log-group prefix allowlist, bounded
  window), and `cost_explorer` (10 calls per turn).
- **`ReadOnlyGuard`**, a deterministic `InterventionHandler` that returns `Deny` or `Proceed` and never
  `Confirm`, plus a `reader` subagent with its own capped limits and the same guard.
- **AgentCore Memory** with `USER_PREFERENCE` and `SEMANTIC` strategies namespaced by the caller's JWT
  `sub`, a single writer and a single reader, and `scripts/memory_wipe.py` as the admin escape hatch.
- **Cognito `CUSTOM_JWT` inbound auth** on both runtimes, bootstrapped idempotently by
  `scripts/auth_bootstrap.py`, with the actor id taken from the token rather than the request body.
- **Explicit IAM deny policy** (`policies/execution-role-deny.json`) attached alongside `ReadOnlyAccess`,
  mirroring the tool-layer deny table, with the ECR carve-out that keeps the image pull working.
- **`doctor.py`**, a standalone 14-check preflight that needs only boto3 and prints the fix for every WARN
  and FAIL. Drop it into any harness project.
- **In-container preflight** logged to CloudWatch, with `/ping` reporting busy when a hard check fails.
- **CloudWatch GenAI Observability** through ADOT, with `session.id`, `user.id`, `gen_ai.request.model`,
  and `harness.effort` on every span, and metadata-only capture.
- **Operator scripts**: `setup.py` (`make setup`: account detection and the two git-ignored files a fresh
  clone needs), `invoke.py` (three transports, SSE rendering), `get_token.py`, `a2a_client.py`,
  `memory_wipe.py`, `demo.sh` (the two-session memory demo), and `smoke.sh` (the full owner-run
  end-to-end check).
- **A public-repo hygiene gate**: `make hygiene` fails on an account id, an ARN, a Cognito pool id, a
  `/Users/` path, or the owner's own profile name in anything git would publish -- tracked files *and*
  untracked ones that are not ignored -- and on a generated file that is tracked rather than ignored.
  gitleaks runs beside it over both history and the working tree, in CI and as a pre-commit hook, and a
  `fresh-clone` CI job proves the clone runs with nothing but the repo. There is no allowlisted path:
  the design history under `docs/superpowers/` uses the same placeholders as everything else.
- **Two arm64 images** built by the generated CDK stack on a native ARM CodeBuild host, from a
  digest-pinned base.
- **CI** on Python 3.12 and 3.14: ruff, pytest, gitleaks, an arm64 buildx build of both images, and a
  fresh-clone run of the whole suite, and a docs gate (`scripts/verify_docs.py`) that runs every runnable
  command block and parses every Mermaid diagram with mermaid-cli. 347 tests, no network.
- **Documentation**: `README.md`, `PITFALLS.md` (61 entries), and a five-part learning path under `docs/`.

- **Screenshots** under `docs/img/` from the first real deployment: the two-session memory demo with a
  denied call, one invocation as an X-Ray span timeline, and the A2A agent card. Ids redacted, nothing else
  edited.

### Pinned versions

| Component | Version |
|---|---|
| `strands-harness` | 0.1.1 |
| `strands-agents[otel]` | 1.56.0 |
| `bedrock-agentcore` | 1.23.1 |
| `@aws/agentcore` (npm CLI) | 0.30.0 |
| `@aws/agentcore-cdk` | 0.1.0-alpha.53 |
| `a2a-sdk[http-server]` | 1.1.5 |
| `aws-opentelemetry-distro` | ~0.18.0 |
| `boto3` / `botocore` | 1.43.99 |
| Starlette | 1.6.0 |
| Base image | `public.ecr.aws/docker/library/python:3.12-slim-trixie`, digest-pinned |
| Python | 3.12 in the image; 3.10+ supported |

### Known limitations

- **The `reader` subagent's limits are per delegation.** A delegate's budget is not counted against the
  parent's, so repeated delegation can take one turn past the parent's `Limits`. Set a billing alarm.
- **The A2A runtime publishes one artifact at the end of a turn.** Streaming callers get status updates in
  the meantime, but not incremental artifact chunks.
- **Locally, the A2A server on port 9000 has no authenticator.** There is no AgentCore authorizer in front
  of it, and the card correctly advertises no security scheme without a discovery URL.
- **The runtime log groups keep the whole conversation, with no expiry, and nothing turns that off.**
  Strands emits the system prompt, user prompt, reasoning, tool arguments and answers as `gen_ai.*.message`
  span events into `/aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT`, which AgentCore creates with
  retention *never expire* (PITFALLS P61). The control this release ships is retention:
  `scripts/smoke.sh` prints the `aws logs put-retention-policy` command with the real group names. Strands'
  redaction switch (`OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` with
  `gen_ai_unredacted_attributes`) is scoped by its own docstring to latest-convention span attributes
  rather than these legacy events, is **untested here**, and is a follow-up for the next release.

### Notes

- **A tool denial is reported as a `denied` frame.** `ReadOnlyGuard`'s `Deny` reaches the caller as a
  `denied{tool, reason}` SSE frame, sourced from the SDK's `tool_cancel_event`, ahead of the
  `tool_result` frame with `status=error` for the same call.
- **`tool_result` frames come from a hook, not the stream.** `agent.stream_async()` never yields a tool
  result in `strands-agents` 1.56, because `ToolResultEvent` is not a callback event. A
  `ToolResultRecorder` hook on `AfterToolCallEvent` records every finished call instead, and the runtimes
  drain it between stream events. See PITFALLS P51.
- The default model is `bedrock/global.anthropic.claude-sonnet-4-6`, not Sonnet 5. The Claude 5 family is
  entitled separately and was refused on the account this was built against. See PITFALLS P29.
- Cost figures in the README (≈ $1 per turn, ≈ $5 per demo, ≈ $8 hard cap) are estimates and have not been
  reconciled against an invoice.
- `make setup` detects the account and region and writes `agentcore/aws-targets.json` and `.env`. Neither
  is committed; `make hygiene` (gitleaks plus `tests/test_repo_hygiene.py`) is what keeps it that way.
