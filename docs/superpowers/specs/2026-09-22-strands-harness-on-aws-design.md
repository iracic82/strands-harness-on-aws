# strands-harness-on-aws — Design Spec (v2)

Date: 2026-09-22
Status: v2, post review (Backend Architect, Security Architect, Multi-Agent Systems Architect); pending owner sign-off
Owner: Igor Racic (iracic82)

## 1. Purpose

A cloneable, production-grade reference for running the **Strands harness**
(`strands-harness`, the one-call `create_harness()` agent from
https://strandsagents.com/blog/introducing-strands-harness/) on AWS the right way:
locally in five minutes, then on **Amazon Bedrock AgentCore Runtime** with
**AgentCore Memory**, **JWT inbound auth**, an **A2A endpoint with a spec-1.0 agent
card**, and **CloudWatch GenAI Observability**. Every known pitfall is either fixed
in code or caught by a preflight check before it costs the user an afternoon.

Successor to https://github.com/iracic82/AWS_AgentLab (a lab on the raw Strands SDK).
This repo is a golden path plus a `doctor`, meant to be demoed live and copied into
real projects.

### Non-goals (v1)

- AgentCore Gateway, outbound OAuth, Code Interpreter, Browser.
- The AgentCore **`harnesses[]` resource** (`AWS::BedrockAgentCore::Harness`, a
  declarative AWS product). Unrelated to `strands-harness`; named here so readers do
  not conflate them.
- TypeScript harness parity.
- VPC network mode, multi-region, multi-account.
- Any write/mutating AWS operation by the agent.
- Human-in-the-loop approval (the seam is left; see 6.3).

## 2. Decisions

| Decision | Choice | Why |
|---|---|---|
| Repo shape | Golden path + standalone `doctor.py` | Demo-able and reusable |
| Demo domain | **AWS Ops Investigator**, read-only account diagnosis | Relatable, safe live, no fake data |
| AgentCore depth | Runtime + Memory + Observability + JWT inbound auth + A2A runtime | Real production traps covered; Gateway/Identity-outbound deferred |
| Wiring | **Harness-first**: one `build_agent()` -> `create_harness()`, backends switched by env | Keeps the harness's tuned defaults |
| Build | **Container** (arm64) via `@aws/agentcore` CLI 0.30+ (CDK) | ~205 MB deps; teaches the arch trap |
| Deploy tool | npm `@aws/agentcore`, `agentcore.json`; generated CDK kept pristine | Python starter toolkit is deprecated |
| Inbound auth | **`CUSTOM_JWT` via Cognito**; actor id from JWT `sub` | Closes actor spoofing of memory; IAM SigV4 documented as alternative |
| Protocols | Runtime 1: HTTP (SSE). Runtime 2: A2A on `a2a-sdk` 1.x, card at `/.well-known/agent-card.json`, `protocolVersion` 1.0 | AgentCore allows one protocol per runtime; Strands `A2AServer` is 0.3-only |
| Cloud tool profile | `read` + AWS tools + programmatic tool caller + fixed `reader` subagent. **No shell, no web_fetch, no write/edit** in cloud | Removes the exfiltration channel on public network mode |
| Network mode | PUBLIC (non-goal: VPC) | Simplicity; safe only because of the tool profile above |
| Default model | `bedrock/global.anthropic.claude-sonnet-5`, effort `medium` (reader: `low`) | Harness default Opus 5 / high is costly; env-overridable |
| Limits | `Limits(turns=25, output_tokens=60_000, total_tokens=1_500_000)`; wall clock 600 s; reader `Limits(12, 20_000, 400_000)` | Cost ceiling ≈ $8/turn worst case, ≈ $1/turn typical |
| Default region | `us-west-2` | Owner's account already runs AgentCore there |
| Python | 3.12 in the image; 3.10+ supported; `runtimeVersion: PYTHON_3_12` | Harness >= 3.10; tested 3.12 and 3.14 |
| Licence | Apache-2.0 | Matches upstream |

## 3. Pitfall inventory

Verified 2026-09-22 against `strands-harness` 0.1.1, `strands-agents` 1.56.0,
`bedrock-agentcore` 1.23.1, `@aws/agentcore` 0.30.0 (`@aws/agentcore-cdk`
0.1.0-alpha.53), `a2a-sdk` 1.1.5, and account 123456789012.

| # | Pitfall | Evidence | Handling |
|---|---|---|---|
| P1 | Python starter toolkit / old `agentcore` CLI deprecated; tutorials point the wrong way | Runtime guide "Migration" | `doctor` detects old toolkit on PATH; README uses only npm CLI |
| P2 | Harness persists sessions/memory/skills under `./.agent/*`; container `/app` is read-only for the non-root user; microVM ephemeral | `defaults.py`; CLI skills fetcher comment | `platform.py` switches to AgentCore Memory; `os.chdir(workspace)` under `tempfile.gettempdir()`; preflight asserts writability |
| P3 | Two memory writers (harness Haiku extractor + AgentCore strategies) **and** two readers (session-manager `retrieval_config` + harness injection) | `resolve_memory`, `session_manager.py:843-910` | Single writer: AgentCore strategies. Single reader: harness stores over AgentCore (`create_agentcore_memory_stores(extraction=False)`), `retrieval_config=None` |
| P4 | Hidden second model: Haiku 4.5 for `web_fetch` + memory extraction | `models.py` `_WEB_FETCH_MODELS` | Cloud profile drops `web_fetch`, extraction off -> no second model in cloud; `doctor` still checks Haiku for local mode |
| P5 | No `AWS_REGION` -> empty model list / silent failures | issue #4481 | `doctor` + preflight hard-fail with fix text |
| P6 | Default Opus 5 / effort high | `defaults.py` | Sonnet 5 / medium; `HARNESS_MODEL`, `HARNESS_EFFORT` |
| P7 | AgentCore Runtime regional; account enabled per region | Owner account: us-west-2 only | `doctor` probes control plane in chosen region |
| P8 | CloudWatch Transaction Search must be enabled once per region; ADOT env required; harness telemetry must not own the exporter | Observability doc; `strands/telemetry/config.py:111-114` | `doctor` checks Transaction Search (`xray:GetTraceSegmentDestination`); Dockerfile `CMD opentelemetry-instrument`; `OTEL_TRACES_EXPORTER` **unset** in container |
| P9 | `runtimeSessionId` >= 33 chars; service rejects short ids before the container | Runtime guide | `main.py` rejects < 33 (400) in dev mode; scripts generate compliant ids |
| P10 | A policy **string** passed to `interventions` becomes an LLM classifier that pauses with an interrupt; pending interrupts persist with the session and wedge resume | `strands_harness/interventions.py:45-65`, `hitl.py:75-84`, issues #4470/#4202 | `ReadOnlyGuard(InterventionHandler)` returning `Deny`; never `Confirm`; unit test asserts no `Confirm` |
| P11 | `BedrockModel` with `boto_client_config` drops the 120 s default read timeout | issue #4394, `bedrock.py:270-290` | String model path is default; `HARNESS_BOTO_READ_TIMEOUT` escape hatch replicates `_bedrock()` kwargs, unit test diffs |
| P12 | `Limits` accepts only `turns`, `output_tokens`, `total_tokens`; other keys silently ignored | issue #4354, `agent.py:1850-1870` | Typed `Limits(...)`; test `set(LIMITS) <= set(Limits.__annotations__)`; stub-model trip test |
| P13 | Stalled MCP tool call hangs the agent | issue #4403 | MCP opt-in, `continue_on_error`, streamable-HTTP endpoint (no `uvx` at cold start) |
| P14 | Fire-and-forget memory writes lost on sync path; sync session manager blocks the stream | issue #4418; `session_manager.py:943-951` | `stream_async` only; `AgentCoreMemoryConfig(async_mode=True)`; `session_manager.close()` in lifespan |
| P15 | Guardrail ID without version silently sends none | issue #4204 | Not used; documented |
| P16 | arm64-only containers ("exec format error") | Runtime guide | CDK builds arm64; `doctor` checks buildx; CI builds arm64 |
| P17 | Global inference profiles need model permissions in every routed region | Bedrock docs | Already granted by the L3 runtime role (`foundation-model/*`, `inference-profile/*`); documented |
| P18 | Version drift: harness 1 day old, `pydantic-monty` exact pin, CLI templates say Python 3.14 | PyPI | `uv.lock` committed, CI from lock, base image pinned by digest, Dependabot |
| P19 | `web_search` off on Bedrock; naming it raises; Exa fallback ships queries to a third party | Harness README | Off; documented |
| P20 | Old Python `agentcore` binary shadows npm one on PATH | Owner's Mac | `doctor` warns |
| P21 | `builtin_tools` **mapping** edits defaults (shell, write, edit, web_fetch stay on); only a list or `{"*": False, ...}` pins | `options.py:80-94` | Cloud: `{"*": False, "read": True, "programmatic_tool_caller": {...}}` |
| P22 | Strands `A2AServer` pins `a2a-sdk` 0.3.x (spec 0.3: `kind` discriminators, `supportsAuthenticatedExtendedCard`); A2A spec is 1.0 (`/.well-known/agent-card.json`, `capabilities.extendedAgentCard`); AWS A2A guide still shows 0.3 shapes | `strands-py/pyproject.toml`; a2a-protocol.org Appendix A | A2A runtime on `a2a-sdk` 1.x with own executor; client script handles both shapes |
| P23 | Offloaded tool results go to `tempfile.mkdtemp` when a `SessionManager` instance is passed; restored history in a new microVM references dead files | `strands_harness/agent.py:494-500` | `filter_restored_tool_context=True`; document artifacts are per-microVM |
| P24 | `RequestContext` has no `user_id`; CLI template's `getattr(context, 'user_id', ...)` is always the default | `bedrock_agentcore/runtime/context.py:17` | Actor from JWT `sub` in the allowlisted `Authorization` header |
| P25 | `GetFoundationModelAvailability` is ambiguous (Sonnet 5 shows `agreementAvailability: NOT_AVAILABLE` yet `AUTHORIZED`); only a real call proves access | Owner account probe | `doctor` performs a 1-token `Converse` on each required model |
| P26 | Managed `ReadOnlyAccess` reads secrets: SSM SecureString (via `aws/ssm` key), S3 bodies, DynamoDB rows, Lambda/ECS env, EC2 user-data, CFN templates, the agent's own log groups | Security review | Explicit Deny statements via `additionalPolicies`; tool-level (service, operation) allowlist |
| P27 | Strategy extraction is asynchronous; a two-session demo without a wait flakes | memorystore README | `demo.sh` polls `retrieve_memory_records` (180 s) before session B |

## 4. Architecture

```
laptop                                     AWS us-west-2 (account 123456789012 for the owner)
──────                                     ───────────────────────────────────────────────────────────
make dev ─► agentcore dev :8080            agentcore deploy (CDK) ─► ECR (arm64) ─► AgentCore Runtime
make invoke / demo.sh                        ├─ Runtime "OpsInvestigator"   HTTP :8080  CUSTOM_JWT (Cognito)
                                             ├─ Runtime "OpsInvestigatorA2A" A2A :9000   CUSTOM_JWT (Cognito)
        build_agent()                        ├─ Memory "OpsInvestigatorMemory" (USER_PREFERENCE, SEMANTIC*)
         └ create_harness()                  └─ env: MEMORY_OPSINVESTIGATORMEMORY_ID, AWS_REGION, HARNESS_*
            local profile                          both runtimes ─► build_agent() cloud profile
            ├ session: ./.agent/sessions             ├ session: AgentCoreMemorySessionManager (events only, async)
            ├ memory:  file store + Haiku            ├ memory:  harness stores over AgentCore (read; no extractor)
            ├ skills:  app/.../skills                ├ skills:  baked in image
            ├ tools: harness defaults + AWS tools    ├ tools: read + aws_call/logs_insights/cost_explorer
            └ interventions: ReadOnlyGuard           │         + programmatic_tool_caller(allowed) + reader subagent
                                                     ├ interventions: ReadOnlyGuard (Deny only)
                                                     └ workspace: $TMPDIR/workspace (cwd)
                                             traces ─► ADOT (opentelemetry-instrument) ─► CloudWatch GenAI Observability
Cognito user pool + app client (scripts/auth_bootstrap.py) ─► discovery URL + client id into agentcore.json
```

### 4.1 Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `app/ops_investigator/main.py` | `BedrockAgentCoreApp` HTTP entrypoint; payload schema; JWT actor extraction; LRU agent cache keyed by session; per-session lock; versioned SSE events; wall-clock timeout; limit-trip handling; lifespan close | `agent.py`, `platform.py`, `preflight.py`, `auth.py` |
| `app/ops_investigator/a2a_main.py` | A2A 1.0 server (`a2a-sdk` 1.x Starlette app) with `HarnessExecutor` bridging `message/send` and `message/stream` to `build_agent().stream_async`; serves the hand-written card | `agent.py`, `platform.py`, `auth.py`, `agent_card.py` |
| `agent_card.py` | Builds the 1.0 `AgentCard`: `protocolVersion`, `name`, `description`, `url`, `version`, `capabilities{streaming, pushNotifications:false, extendedAgentCard:false}`, `defaultInputModes/OutputModes` (`text/plain`), `skills[]` (ids `investigate-incident`, `cost-review`, `inventory`), `securitySchemes{cognito: openIdConnect(discoveryUrl)}`, `security[{cognito: []}]`, `provider` | env (`A2A_PUBLIC_URL`, `COGNITO_DISCOVERY_URL`) |
| `agent.py` | `build_agent(session_id, actor_id, profile) -> Agent` via `create_harness()`; `LIMITS`; `ReadOnlyGuard`; `reader` subagent via `make_subagent`; instructions | `platform.py`, `tools/`, `guard.py` |
| `guard.py` | `ReadOnlyGuard(InterventionHandler)`: `before_tool_call` returns `Deny` for non-allowlisted tools and non-read `aws_call` operations; logs a structured line per denial; never returns `Confirm` | strands interventions |
| `platform.py` | `Runtime` dataclass (`profile`, `region`, `memory_id`, `workspace`, `model`, `effort`); `session_option()`, `memory_option()`, `skills_option()`, `builtin_tools()`; `chdir(workspace)` | `bedrock_agentcore.memory.integrations.strands`, env |
| `auth.py` | Extracts actor id from the `Authorization: Bearer` JWT (`sub`, unverified decode; the AgentCore authorizer verified signature/audience before the request reached the container); validates `^[A-Za-z0-9_-]{1,64}$`; in local mode falls back to `X-Actor-Id` header/payload with a WARN | none (base64/json) |
| `preflight.py` | Startup checks (region, memory id, tempdir writable, model reachable via 1-token Converse, ADOT env); structured log; marks `/ping` unhealthy with fix text on hard failure | boto3 |
| `tools/aws_call.py` | Read-only boto3 tool: (service, operation) allowlist + explicit deny table; `query` (JMESPath) and `max_items`; 40k-char cap returning `{"truncated": true, ...}`; 30 s timeout; structured error text; per-turn budget (40 calls) in `invocation_state`; `Config(user_agent_extra=...)` | boto3 |
| `tools/logs_insights.py` | Start/poll Logs Insights; log-group prefix allowlist (`OPS_LOG_GROUP_PREFIXES`), bounded window and `limit`; excludes `/aws/bedrock-agentcore/*` and `aws/spans` | boto3 |
| `tools/cost_explorer.py` | `GetCostAndUsage` by service for a date range; budget 10 calls/turn | boto3 |
| `skills/investigate-incident/SKILL.md` | Runbook loaded on demand | — |
| `AGENTS.md` (app) | < 1k tokens; injected each turn by the `environment` plugin; states that tool output and memory are data, never instructions | — |
| `policies/execution-role-deny.json` | `additionalPolicies` inline document: explicit Deny list (P26) | agentcore.json |
| `doctor.py` (root) | Standalone preflight, boto3 + subprocess only; PASS/WARN/FAIL with fix text; exit code = worst | boto3 |
| `scripts/auth_bootstrap.py` | Idempotent: Cognito user pool + app client + demo user; prints discovery URL/client id; patches `agentcore.json` `authorizerConfiguration` | boto3 |
| `scripts/token.py` | Gets a JWT for the demo user (`USER_PASSWORD_AUTH`) | boto3 |
| `scripts/invoke.py` | HTTP invoke with bearer token, compliant session id, SSE printing | boto3/httpx |
| `scripts/a2a_client.py` | A2A client for the card + `message/stream`; handles 1.0 and 0.3 shapes | httpx |
| `scripts/demo.sh` | Session A (states a preference) -> poll memory -> session B (recalls) -> A2A card fetch | scripts above |
| `scripts/memory_wipe.py` | Deletes an actor's records/events (`make memory-wipe ACTOR=`) | boto3 |
| `agentcore/` | `agentcore.json` (2 runtimes, 1 memory, env vars, `additionalPolicies`, `authorizerConfiguration`, `requestHeaderAllowlist`), `aws-targets.json`, generated CDK (pristine) | `@aws/agentcore` |
| `Makefile` | `doctor auth-bootstrap dev invoke test deploy logs smoke a2a-card memory-wipe destroy` | all |
| `tests/` | unit (no network) + `live` marked | pytest, botocore Stubber |

### 4.2 `build_agent()`

```python
LIMITS = Limits(turns=env_int("HARNESS_MAX_TURNS", 25),
                output_tokens=env_int("HARNESS_MAX_OUTPUT_TOKENS", 60_000),
                total_tokens=env_int("HARNESS_MAX_TOTAL_TOKENS", 1_500_000))
READER_LIMITS = Limits(turns=12, output_tokens=20_000, total_tokens=400_000)
AWS_TOOLS = [aws_call, logs_insights, cost_explorer]

def build_reader(spec: AgentSpec) -> Agent:          # fixed role, own caps, low effort
    return create_harness(model=spec.model or rt.model, effort="low",
        instructions=spec.instructions, tools=AWS_TOOLS,
        builtin_tools={"*": False, "read": True}, session=False, memory=False, skills=False,
        interventions=[ReadOnlyGuard()], limits=READER_LIMITS,
        trace_attributes={"session.id": rt.session_id, "agent.role": "reader"})

reader = make_subagent(builder=build_reader,
    presets={"reader": Preset(instructions=READER_PROMPT, context="none",
                              description="reads and summarises AWS state for one narrow question")},
    instructions=Fixed(None), model=Inherit(), max_depth=1)

def build_agent(session_id: str, actor_id: str) -> Agent:
    rt = platform.detect(session_id, actor_id)       # chdir(workspace) happens here
    return create_harness(
        model=rt.model, effort=rt.effort, instructions=OPS_INSTRUCTIONS,
        tools=[*AWS_TOOLS, reader],
        mcp_servers=rt.mcp_servers,                  # streamable-HTTP AWS docs MCP when ENABLE_AWS_DOCS_MCP=1
        builtin_tools=rt.builtin_tools(),            # cloud: {"*": False, "read": True,
                                                     #   "programmatic_tool_caller": {"allowed_tools": [...AWS_TOOLS names], "timeout": 60}}
                                                     # local: {"subagent": False, "web_search": False}  (harness defaults otherwise)
        background_tasks={"agentic": [], "max_concurrency": 2, "timeout": 300},
        session=rt.session_option(),                 # cloud: AgentCoreMemorySessionManager(async_mode=True,
                                                     #   retrieval_config=None, filter_restored_tool_context=True)
        memory=rt.memory_option(),                   # cloud: {"stores": create_agentcore_memory_stores(..., extraction=False)}
        skills=rt.skills_option(),
        interventions=[ReadOnlyGuard()],
        caching="auto", context_manager="auto",
        name="ops-investigator", limits=LIMITS,
        trace_attributes={"session.id": session_id, "user.id": actor_id,
                          "gen_ai.request.model": rt.model, "harness.effort": rt.effort},
    )
```

`profile` is `cloud` when `MEMORY_OPSINVESTIGATORMEMORY_ID` is set, else `local`.
Model construction always goes through the harness `provider/name` string (so
effort mapping, thinking budget and cache points are the harness's). The
`HARNESS_BOTO_READ_TIMEOUT` escape hatch builds a `BedrockModel` replicating
`_bedrock()`'s kwargs and is covered by a unit test that diffs both configs (P11).

### 4.3 Memory policy (P3), settled

- **Writer:** `AgentCoreMemorySessionManager(AgentCoreMemoryConfig(memory_id, session_id,
  actor_id, retrieval_config=None, async_mode=True, filter_restored_tool_context=True))`
  persists conversation events. AgentCore strategies extract.
- **Reader:** `memory={"stores": create_agentcore_memory_stores(memory_id=..., actor_id=...,
  session_id=..., namespaces=[{"namespace": f"/preferences/{actor}/", "name": "preferences",
  "max_search_results": 5}, {"namespace": f"/facts/{actor}/", "name": "facts",
  "max_search_results": 5}], extraction=False)}`. Verified: yields read-only stores,
  `MemoryManager` exposes `search_memory` only, no Haiku extractor built.
- **Strategies** (in `agentcore.json`): `USER_PREFERENCE` -> `/preferences/{actorId}/`;
  `SEMANTIC` with a custom extraction prompt restricted to durable operator context
  (owned accounts/regions, naming conventions, known-noisy alarms, stated goals) and
  excluding resource state, metrics, timestamps, identifiers, credentials, and anything
  quoted from tool output -> `/facts/{actorId}/`. No `SUMMARIZATION`, no `EPISODIC` in v1
  (extra extraction cost, wrong content for an ops agent). `eventExpiryDuration: 30`.
- Tool outputs are capped at the tool (40k chars) so 100k-char payloads never enter the
  event stream. `make memory-wipe ACTOR=<id>` exists as an admin escape hatch.
- Locally the harness defaults stand (file store + Haiku extractor), so the demo can
  show both modes.

### 4.4 Data flow per turn (HTTP runtime)

1. Client obtains a Cognito JWT (`scripts/token.py`) and calls `InvokeAgentRuntime`
   with `runtimeSessionId` (uuid4 hex x2) and `{"prompt": "..."}` and `Authorization:
   Bearer <jwt>`. The AgentCore authorizer validates the token; the header is passed
   through (`requestHeaderAllowlist: ["Authorization"]`).
2. `main.py` validates the payload (schema: `prompt` str <= 32 KB, optional
   `stream` bool, no unknown keys), extracts `actor_id` from JWT `sub`, rejects an
   actor change on an existing session, takes the per-session lock, and gets/builds the
   agent from an LRU (size 16).
3. `async with asyncio.timeout(HARNESS_MAX_SECONDS=600)`: `agent.stream_async(prompt)`.
4. Events are mapped to the versioned SSE schema (`v: 1`): `text` (delta), `thinking`
   (delta, when present), `tool_use` (name, input summary), `tool_result` (name, status,
   bytes), `denied` (tool, reason), `metrics` (input/output/cache tokens, cycles,
   stop_reason, est_usd), `error` (message, hint), `done`.
5. Limit trip (`stop_reason` starts with `limit_`): emit `metrics` + `error{hint}` and
   run one bounded closing turn (`limits=Limits(turns=1)`) asking for a summary of
   findings so far. Wall-clock trip: cancel, emit `error{timeout}`.
6. Session manager writes user/assistant events (async); strategies extract later;
   the next session's `search_memory`/injection sees preferences and facts.
7. Spans exported by ADOT only. `OTEL_TRACES_EXPORTER` is unset in the container so
   the harness telemetry module stays idle (P8). Content capture is **off** by default;
   `HARNESS_TRACE_CONTENT=1` turns it on for private debugging.

### 4.5 A2A runtime

- `a2a_main.py` builds an `a2a-sdk` 1.x Starlette app on `0.0.0.0:9000` with
  `HarnessExecutor(AgentExecutor)`: `execute()` maps the incoming message parts to a
  prompt, calls `build_agent(context_id, actor_id).stream_async`, streams
  `TaskStatusUpdateEvent`/`TaskArtifactUpdateEvent`, and completes the task; `cancel()`
  cancels the stream. Task store: in-memory (microVM per session).
- Card served at `/.well-known/agent-card.json` from `agent_card.py`; `url` is
  `A2A_PUBLIC_URL` (the AgentCore invocation URL for this runtime), injected by env
  after first deploy (`make a2a-card` prints it and patches the env var).
- Auth: same Cognito `CUSTOM_JWT` authorizer; the card advertises it in
  `securitySchemes`/`security`. Actor id from JWT `sub` as in HTTP.
- `scripts/a2a_client.py` fetches the card, negotiates by `protocolVersion`, sends
  `message/stream`, prints events. Documented pitfall P22.

### 4.6 IAM

- **Execution role** (generated by CDK, not edited): Bedrock invoke on foundation
  models and inference profiles, logs, X-Ray, memory data plane (all auto-wired).
  `additionalPolicies` in `agentcore.json`: `arn:aws:iam::aws:policy/ReadOnlyAccess`
  **plus** `policies/execution-role-deny.json` with explicit Deny on:
  `secretsmanager:GetSecretValue`, `ssm:GetParameter*`, `kms:Decrypt`,
  `kms:GenerateDataKey*`, `s3:GetObject*`, `dynamodb:GetItem|BatchGetItem|Scan|Query|
  PartiQLSelect`, `lambda:GetFunction|GetFunctionConfiguration|GetLayerVersion`,
  `ec2:GetPasswordData|GetConsoleOutput|GetConsoleScreenshot|DescribeInstanceAttribute`,
  `cloudformation:GetTemplate`, `ecs:DescribeTaskDefinition`,
  `codebuild:BatchGetProjects`, `apigateway:GET` on `/apikeys*`,
  `cognito-idp:AdminGetUser|ListUsers`, `sqs:ReceiveMessage`, `kinesis:GetRecords|
  GetShardIterator`, `sts:AssumeRole|GetFederationToken|GetSessionToken`, and
  `logs:GetLogEvents|FilterLogEvents|StartQuery|GetQueryResults` on
  `log-group:/aws/bedrock-agentcore/*` and `log-group:aws/spans*`.
- `aws_call` mirrors the deny table at the tool layer (defence in depth); unit test
  asserts every denied operation is refused by the tool.
- **Deployer** (human): CDK bootstrap (`doctor` checks `CDKToolkit` stack), ECR push,
  AgentCore control plane, Cognito admin for `auth_bootstrap.py`.
- Log groups: 30-day retention (set via CDK context if exposed, else documented).

## 5. Error handling

| Situation | Behaviour |
|---|---|
| Missing region / memory id in cloud | `preflight` logs the fix, `/ping` reports unhealthy; container does not crash-loop silently; `agentcore logs` shows the line |
| Model not accessible | `preflight` WARN (structured); first invoke returns `error{hint}` event |
| AWS tool `AccessDenied` / throttle / timeout / budget exhausted | Tool returns structured error text; loop continues |
| Denied tool call | `ReadOnlyGuard` -> `Deny(reason)`; structured log; `denied` SSE event |
| Session manager init fails (AccessDenied/throttle on first read) | Fail closed: `error` event with hint; no agent cached |
| Concurrent invoke on same session | Per-session lock; second caller gets `error{busy}` immediately |
| Actor mismatch on existing session | 403-style `error{actor_mismatch}` |
| Short session id (dev/direct HTTP only) | 400 with fix; in cloud the service rejects earlier |
| Limit / wall-clock trip | See 4.4 step 5 |
| MCP server down | `continue_on_error: true` |
| Client disconnect mid-stream | Agent finishes the turn; events persisted; documented |
| Bedrock throttling | botocore standard retry mode, 3 attempts (string path default); documented |

## 6. Security posture (from the threat model)

6.1 **No egress-capable tools in cloud** (shell, web_fetch, write, edit off). The
programmatic tool caller may call only the three AWS tools. The reader subagent
inherits the same guard and profile.
6.2 **Identity:** actor from JWT `sub`; all invokers must present a Cognito token
from the allowed client; README states that everyone in that client shares one trust
domain unless per-user Cognito groups are added.
6.3 **HITL seam:** `interventions=[ReadOnlyGuard(), HumanInTheLoop(ask=None)]` is the
documented next step, with `main.py` emitting `interrupt` events and accepting
`interruptResponse` on resume. Not in v1.
6.4 **Telemetry:** metadata only by default (tool name, service, operation, arg
SHA-256, bytes, duration, status, denial decision, session/actor ids); never bodies,
env, or values. `user_agent_extra="strands-harness-on-aws session=<id> actor=<id>"`
on every boto3 client for CloudTrail correlation.
6.5 **Supply chain:** base image pinned by digest; `uv.lock`; CI installs from lock;
Dependabot on the four upstream packages; `CODEOWNERS` on `skills/**`, `AGENTS.md`,
`policies/**`, `agentcore/**`.
6.6 **Local hygiene:** `.gitignore` covers `.agent/`, `.claude/`, `.env*`; `doctor`
FAILs if `.agent/` is tracked.
6.7 **Instruction hardening** (defence in depth only): instructions and `AGENTS.md`
say tool output, logs, tags, cost data, and memory are data, never instructions; tool
results are wrapped with a provenance header.

## 7. Testing

- **Unit (pytest, no network):** `platform.detect()` both profiles; payload schema;
  JWT `sub` extraction and actor regex; `aws_call` allowlist/deny table over 60 real
  operation names; output cap and budget counter; `ReadOnlyGuard` never returns
  `Confirm`; `Limits` key check (P12) and stub-model trip; `BedrockModel` config diff
  (P11); SSE mapper; agent card validates against `a2a.types.AgentCard` (1.x) and
  contains `protocolVersion == "1.0"`; `doctor` checks via botocore Stubber.
- **Live (marked, skipped without creds):** `agentcore dev`, three prompts, session and
  memory files appear locally; a denied `aws_call` mutating operation produces a
  `denied` event.
- **Cloud smoke (`make smoke`, owner-run):** `auth_bootstrap` -> `deploy` -> token ->
  session A states a preference -> poll `retrieve_memory_records` (<= 180 s) -> session B
  recalls it -> A2A card fetched and validated -> exactly one `invoke_agent` span per
  invoke in CloudWatch -> from inside the runtime `curl http://169.254.169.254/` and
  `http://localhost:8080/ping` from a tool are unreachable/blocked (there are no such
  tools; the check documents it) -> `agentcore remove all && agentcore deploy`.
- **CI:** ruff, pytest on 3.12 and 3.14, `docker buildx build --platform linux/arm64`
  for both images (no push, no deploy).

## 8. Documentation

- `README.md`: what/why, 5-minute local quickstart, auth bootstrap, deploy in 3
  commands, demo script, A2A card, architecture diagram, cost note (≈ $1/turn, ≈ $5/
  demo, hard cap ≈ $8/turn), cleanup.
- `PITFALLS.md`: P1–P27 as symptom -> cause -> fix -> code location.
- `doctor.py --help`; every FAIL carries the fix.
- `CHANGELOG.md` with pinned upstream versions.

## 9. Publishing

GitHub `iracic82/strands-harness-on-aws`, Apache-2.0. No push until the owner says so
and the cloud smoke has passed in account 123456789012. Reviewer agent memory files
under `.claude/` stay untracked.

## 10. Open items (implementation-time verification)

Resolved against a real deployment in `us-west-2` on 2026-09-22 (the first cloud smoke), except 10.5.

1. **`requestHeaderAllowlist` — confirmed, and it is an allowlist in the strict sense.** The key name is
   right, and with `["Authorization"]` the JWT reached `context.request_headers`: the runtime log line for
   a real invoke reads `turn start session=<id> actor=<the JWT sub>`, so `auth.actor_from_headers` derived
   the actor from the token and never fell back. What the spec did not anticipate is that AgentCore drops
   every header *not* named, which broke A2A outright until `A2A-Version` was added to the A2A runtime's
   list. See PITFALLS P52.
2. **ADOT content capture — the reading was backwards.** `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT` is an
   opt-*out*: `amazon/opentelemetry/distro/_utils.py` treats it as false by default and only `"true"` turns
   capture off. ADOT additionally sets `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` when
   `AGENT_OBSERVABILITY_ENABLED=true`, which is the AgentCore default. So prompt and response content is
   captured into traces unless you opt out, which is a privacy decision worth making deliberately rather
   than a wiring detail. Related: ADOT also sets `OTEL_TRACES_EXPORTER` itself (PITFALLS P56).
3. **`a2a-sdk` 1.x alongside `strands-agents` — confirmed, no conflict.** `uv.lock` resolves `a2a-sdk`
   1.1.5 with `strands-agents[otel]` 1.56.0, and both images built and ran with that lock.
4. **Log-group retention — manual, and there is no CDK context key for the part that matters.** The two
   runtime log groups (`/aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT`) are created by the AgentCore
   platform, not by the stack, and come out with retention *never expire*. The log groups CDK does own are
   set for it (the two container-build Lambdas get 731 days); the CodeBuild project's group also never
   expires. Setting retention on the runtime groups is a `PutRetentionPolicy` you make yourself after the
   first deploy.
5. **Bedrock pricing — still open.** `sse.PRICES` remains the unverified estimate it always was, and it is
   keyed by family (`sonnet`/`opus`/`haiku`), not by a specific version. Note the deployed model is
   `global.anthropic.claude-sonnet-4-6`, not Sonnet 5, so the item as written no longer names what runs
   here. For scale, the metrics frames from the smoke reported roughly $0.0008 for a one-line answer and
   about $0.03 for a four-cycle investigation with tool calls. Check the published price list before
   quoting any of this in the README.
