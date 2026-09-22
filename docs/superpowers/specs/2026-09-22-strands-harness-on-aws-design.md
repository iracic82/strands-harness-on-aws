# strands-harness-on-aws — Design Spec

Date: 2026-09-22
Status: approved by owner, pending reviewer pass
Owner: Igor Racic (iracic82)

## 1. Purpose

A cloneable, production-grade reference for running the **Strands harness**
(`strands-harness`, the one-call `create_harness()` agent introduced in
https://strandsagents.com/blog/introducing-strands-harness/) on AWS the right way:
locally in five minutes, then on **Amazon Bedrock AgentCore Runtime** with
**AgentCore Memory** and **CloudWatch GenAI Observability**, with every known
pitfall either fixed in code or caught by a preflight check before it costs the
user an afternoon.

It is the successor to https://github.com/iracic82/AWS_AgentLab (a step-by-step
lab on the raw Strands SDK). This repo is not a tutorial: it is a golden path
plus a `doctor`, meant to be demoed live and copied into real projects.

### Non-goals (v1)

- AgentCore Gateway, Identity (inbound OAuth), Code Interpreter, Browser.
- TypeScript harness parity.
- Multi-region or multi-account deployment.
- Any write/mutating AWS operation by the agent.

## 2. Decisions already made

| Decision | Choice | Why |
|---|---|---|
| Repo shape | Golden path + standalone `doctor.py` | Demo-able and reusable |
| Demo domain | **AWS Ops Investigator** (read-only account diagnosis) | Relatable to every AWS builder, safe to run live, no fake data |
| AgentCore depth | Runtime + Memory + Observability | Fixes the real traps without doubling scope |
| Wiring | **Harness-first**: one `create_harness()` factory, backends switched by env | Keeps the harness's tuned defaults (the point of the blog) |
| Build | **Container** (arm64) via the new `@aws/agentcore` CLI (CDK) | Headroom for ~205 MB deps; teaches the arch trap deliberately |
| Deploy tool | `@aws/agentcore` npm CLI 0.30+, `agentcore.json`, CDK | The Python `bedrock-agentcore-starter-toolkit` is deprecated |
| Default model | `bedrock/global.anthropic.claude-sonnet-5`, effort `medium` | Harness default (Opus 5 / high) is costly for a demo; env-overridable |
| Default region | `us-west-2` | Owner's account already runs AgentCore there |
| Language | Python 3.12 in the image (CLI template), 3.10+ supported | Harness requires >=3.10; tested on 3.12 and 3.14 |
| Licence | Apache-2.0 | Matches upstream |

## 3. Pitfall inventory (what the repo must handle)

Verified 2026-09-22 against `strands-harness` 0.1.1, `strands-agents` 1.56.0,
`bedrock-agentcore` 1.23.1, `@aws/agentcore` 0.30.0, and the owner's account.

| # | Pitfall | Evidence | Handling |
|---|---|---|---|
| P1 | Python starter toolkit / old `agentcore` CLI is deprecated; tutorials point the wrong way | AgentCore runtime guide "Migration" section | `doctor` detects old toolkit, README uses only the npm CLI |
| P2 | Harness persists sessions, memory, skills under `./.agent/*`; AgentCore working dir (`/app`) is read-only for the non-root user and the microVM is ephemeral | Harness `defaults.py`; CLI skills fetcher comment ("runtime working directory is read-only") | `platform.py` switches to AgentCore Memory and a `/tmp` workspace; preflight asserts writability |
| P3 | Two overlapping memory systems (harness file extractor on Haiku + AgentCore strategies) | Harness `resolve_memory`; `AgentCoreMemorySessionManager.retrieval_config` | Single writer: AgentCore strategies extract; harness extraction off in cloud |
| P4 | Hidden second model: Haiku 4.5 (global profile) used for `web_fetch` and memory extraction | Harness `models.py` `_WEB_FETCH_MODELS` | `doctor` checks Haiku availability; documented |
| P5 | No `AWS_REGION` -> empty model list / silent failures | harness-sdk issue #4481 | `doctor` + preflight hard-fail with fix text |
| P6 | Default model is Opus 5 at effort `high` (extended thinking) | Harness `defaults.py` | Sonnet 5 / medium default, `HARNESS_MODEL`, `HARNESS_EFFORT` env |
| P7 | AgentCore Runtime is regional; account may have it enabled in one region only | Owner account: runtimes in us-west-2, none in us-east-1 | `doctor` probes control plane in the chosen region |
| P8 | CloudWatch Transaction Search must be enabled once per region or traces never appear; ADOT env vars required in the container | AgentCore observability doc | `doctor` checks Transaction Search; Dockerfile keeps `opentelemetry-instrument`; env set by CDK |
| P9 | Runtime session ID must be >= 33 chars | AgentCore runtime guide | `main.py` normalises/pads; client scripts generate UUID4 hex x2 |
| P10 | A persisted pending interrupt (approval) wedges a resumed session | harness-sdk issues #4470, #4202 | Read-only agent uses deny (not ask) interventions; resumed sessions with pending interrupt are logged and dismissed |
| P11 | `BedrockModel` drops default read timeout when `boto_client_config` is passed | issue #4394 | Explicit `Config(read_timeout=..., retries=...)` |
| P12 | Unknown `limits` keys are silently ignored | issue #4354 | Limits set through typed config; unit test asserts they apply |
| P13 | Stalled MCP tool call hangs the agent | issue #4403 | MCP servers opt-in, `continue_on_error`, tool timeouts |
| P14 | Sync `add_memory` writes can be lost | issue #4418 | Agent invoked via `stream_async` only; flush on shutdown |
| P15 | Guardrail ID without version silently sends no guardrail | issue #4204 | Not used in v1; documented in PITFALLS |
| P16 | arm64-only containers ("exec format error") | AgentCore runtime guide | CDK builds `--platform linux/arm64`; `doctor` checks buildx |
| P17 | Global inference profiles need `bedrock:InvokeModel*` on the foundation-model ARNs in every routed region | Bedrock cross-region inference docs | Execution role policy covers `arn:aws:bedrock:*::foundation-model/*` + the profile ARNs |
| P18 | Version drift: harness 0.1.1 is one day old, `pydantic-monty` pinned exactly, CLI templates target Python 3.14 | PyPI metadata | `uv.lock` committed; CI installs from lock; README states tested versions |
| P19 | `web_search` is off on Bedrock Converse; naming it raises; Exa fallback sends queries to a third party | Harness README | `web_search` left off; documented |
| P20 | Old `agentcore` Python binary shadows the npm one on PATH | Owner's machine | `doctor` warns when `agentcore --version` is not the npm CLI |

## 4. Architecture

```
laptop                                   AWS (us-west-2)
──────                                   ───────────────────────────────────────────
make dev ─► agentcore dev (:8080) ─┐     agentcore deploy (CDK) ─► ECR (arm64 image)
                                   │                            └► AgentCore Runtime
make invoke ───────────────────────┼────────────────────────────►   └ microVM per session
                                   │                                  main.py (SSE)
                                   ▼                                  └ build_agent()
                            build_agent()                                └ create_harness()
                             └ create_harness()                              ├ Bedrock (Sonnet 5, cached)
                                ├ session: disk ./.agent/sessions            ├ session: AgentCoreMemorySessionManager
                                ├ memory:  file store + Haiku extractor      ├ memory: AgentCore Memory (strategies extract)
                                ├ skills:  ./app/.../skills                  ├ skills: baked into image
                                └ tools: built-ins + aws_call/logs/cost      └ workspace: /tmp/workspace
                                                                            traces ─► ADOT ─► CloudWatch GenAI Observability
```

### 4.1 Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `app/ops_investigator/main.py` | `BedrockAgentCoreApp` entrypoint; payload validation; per-session agent cache; SSE streaming; `/ping` | `agent.py`, `platform.py`, `preflight.py` |
| `agent.py` | `build_agent(session_id, actor_id) -> strands.Agent` via `create_harness()`; instructions; tool list; interventions | `platform.py`, `tools/` |
| `platform.py` | `Runtime` dataclass: `is_agentcore`, `region`, `memory_id`, `workspace`; factories `session_manager()`, `memory_option()`, `skills_option()` | `bedrock_agentcore.memory.integrations.strands`, env |
| `preflight.py` | Startup checks, structured log lines, hard-fail only on unrecoverable config | boto3 |
| `tools/aws_call.py` | Read-only boto3 tool: allowlisted verbs (`Describe*`, `List*`, `Get*`, `Lookup*`, `Search*`, `Query*`), 30 s timeout, structured error text | boto3 |
| `tools/logs_insights.py` | Start + poll a CloudWatch Logs Insights query with a bounded window | boto3 |
| `tools/cost_explorer.py` | `GetCostAndUsage` grouped by service for a date range | boto3 |
| `skills/investigate-incident/SKILL.md` | Runbook the model loads on demand | — |
| `AGENTS.md` (app) | Injected by the harness `environment` plugin each turn | — |
| `doctor.py` (repo root) | Standalone preflight, boto3 only, exit code = worst status | boto3, subprocess |
| `agentcore/` | `agentcore.json`, `aws-targets.json`, CDK app (generated, committed, customised for env vars + role policies) | `@aws/agentcore`, CDK |
| `Makefile` | `doctor dev invoke test deploy logs smoke destroy` | all of the above |
| `scripts/demo.sh` | Two-session demo proving memory survival | `agentcore invoke` |
| `tests/` | unit (no network) + `live` marked tests | pytest, botocore Stubber |

### 4.2 `build_agent()` (the only agent definition)

```python
def build_agent(session_id: str, actor_id: str) -> Agent:
    rt = platform.detect()
    return create_harness(
        model=rt.model,                      # HARNESS_MODEL, default bedrock/global.anthropic.claude-sonnet-5
        effort=rt.effort,                    # HARNESS_EFFORT, default "medium"
        instructions=OPS_INSTRUCTIONS,
        tools=[aws_call, logs_insights, cost_explorer],
        mcp_servers=rt.mcp_servers,          # AWS docs MCP when ENABLE_AWS_DOCS_MCP=1, else None
        builtin_tools={"subagent": {"max_depth": 1}},   # web_search stays off on Bedrock (P19)
        session=rt.session_option(session_id, actor_id),
        memory=rt.memory_option(actor_id),
        skills=rt.skills_option(),
        interventions=READ_ONLY_POLICY,      # deny mutating tool calls; no "ask" (P10)
        caching="auto",
        context_manager="auto",
    )
```

`platform.detect()` returns local backends when `MEMORY_*_ID` is absent and
AgentCore backends when present. Model construction goes through the harness
`provider/name` string so effort mapping and cache points stay the harness's
responsibility; the boto read-timeout fix (P11) is applied by passing a
`BedrockModel` instance only when `HARNESS_BOTO_READ_TIMEOUT` is set, in which case
`effort` is configured on the instance (harness ignores `effort` for instances).

### 4.3 Memory policy (P3) — to be confirmed in implementation

Preferred: `memory={"stores": create_agentcore_memory_stores(memory_id, actor_id,
session_id, namespaces=[facts, preferences], extraction=False)}` so the harness
`search_memory` tool reads AgentCore records and the harness runs no extractor;
AgentCore strategies (SEMANTIC, USER_PREFERENCE, SUMMARIZATION) are the single
writer via the session manager's events.

Fallback if the store cannot satisfy the harness `MemoryManager` writable
topology: `memory=False` plus a custom `search_memory` tool over
`retrieve_memory_records`. Whichever is used, `PITFALLS.md` P3 records the reason.

Locally the harness defaults stand (file store + Haiku extractor) so the demo
shows both modes.

### 4.4 Data flow per turn

1. Client calls `InvokeAgentRuntime` with `runtimeSessionId` (>= 33 chars) and a
   JSON payload `{"prompt": "...", "actor_id": "..."}` (actor defaults to
   `anonymous-<session>`; a JWT `sub` wins when Identity is added later).
2. `main.py` validates, resolves the agent from a per-process cache keyed by
   `session_id/actor_id` (microVM is per session anyway), and calls
   `agent.stream_async(prompt)`.
3. Harness loop: Bedrock Converse with cache points; tool calls through the
   read-only interventions; subagent (depth 1) for wide searches; context
   manager offloads bulky tool output to the `/tmp` workspace.
4. Events are yielded as SSE; `main.py` filters to content deltas and tool
   events.
5. Session manager writes user/assistant events to AgentCore Memory; strategies
   extract asynchronously; the next session's first turn gets facts injected via
   `retrieval_config`.
6. Spans exported by ADOT (`opentelemetry-instrument`) to CloudWatch; the harness
   telemetry module stays idle when `OTEL_TRACES_EXPORTER` is handled by ADOT
   (verify no double export in the smoke test).

### 4.5 IAM

- **Execution role** (created by CDK): AgentCore defaults + `bedrock:InvokeModel`,
  `bedrock:InvokeModelWithResponseStream` on `arn:aws:bedrock:*::foundation-model/*`
  and the two global inference profiles + AWS managed `ReadOnlyAccess` +
  `logs:StartQuery/GetQueryResults` + `ce:GetCostAndUsage` (the last two are inside
  ReadOnlyAccess; listed for clarity) + memory data-plane actions on the memory ARN.
- **Deployer** (human, my-profile): CDK bootstrap, ECR push, AgentCore control plane.
  `doctor` verifies with dry `List*` calls; it does not attempt IAM simulation.

## 5. Error handling

| Situation | Behaviour |
|---|---|
| Missing region / memory id in cloud | `preflight` raises at import with the fix; container exits; CDK surfaces CREATE_FAILED with the message |
| Model not accessible | `preflight` warns (structured), `/ping` healthy, first invoke returns a clear error event |
| AWS tool `AccessDenied` / throttle / timeout | Tool returns `{"error": ..., "hint": ...}` text; no exception to the loop |
| Mutating verb requested | Tool refuses before boto3; intervention denies shell calls matching `aws ... (create|put|delete|update|modify|terminate|run|start|stop)` |
| Short session id | Padded deterministically (`sha256` hex) and logged |
| Pending interrupt on resumed session | Logged, dismissed with an empty response; turn proceeds |
| MCP server down | `continue_on_error: true`; agent runs without those tools |
| Memory write failure | Logged; turn still returns; `preflight` result visible in logs |

## 6. Testing

- **Unit (pytest, no network)**: `platform.detect()` under both env shapes;
  payload validation and session-id padding; `aws_call` allowlist rejects every
  mutating verb in a table of 40 real operation names; `doctor` checks using
  botocore `Stubber`; limits config round-trip (P12).
- **Live (marked `live`, skipped without creds)**: start `agentcore dev`, send
  three prompts, assert session and memory files exist locally.
- **Cloud smoke (`make smoke`, manual)**: `agentcore deploy` to us-west-2, two
  sessions (`A` states a fact, `B` recalls it), assert one `invoke_agent` span in
  CloudWatch for the runtime, then `agentcore remove all && agentcore deploy` to
  tear down. Run by the owner with my-profile before publishing.
- **CI (GitHub Actions)**: ruff, unit tests on 3.12 and 3.14, `docker buildx build
  --platform linux/arm64` (no push, no deploy).

## 7. Documentation deliverables

- `README.md`: what/why, 5-minute local quickstart, deploy in 3 commands, demo
  script, architecture diagram, cost note, cleanup.
- `PITFALLS.md`: P1–P20 as symptom -> cause -> fix -> where in code.
- `doctor.py --help` and every FAIL message carry the fix inline.
- CHANGELOG with the pinned upstream versions.

## 8. Publishing

GitHub `iracic82/strands-harness-on-aws`, Apache-2.0, README links the harness
blog and AWS_AgentLab. No push until the owner says so and the cloud smoke has
passed in the my-profile account.

## 9. Open items to resolve during implementation

1. P3 memory policy: store-backed vs fallback (see 4.3).
2. Whether the CDK L3 construct exposes execution-role policy additions in
   `agentcore.json` or needs a CDK-side patch (`agentcore/.llm-context/agentcore.ts`
   is the schema to check).
3. Whether ADOT auto-instrumentation and the harness `StrandsTelemetry` double
   export; decide which one owns the exporter.
4. Exact env var name the CDK stack injects for the memory id
   (template shows `MEMORY_<NAME>_ID`).
