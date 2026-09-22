# strands-harness-on-aws

A cloneable, production-grade reference for running the [Strands harness](https://strandsagents.com/blog/introducing-strands-harness/)
on Amazon Bedrock AgentCore. The demo agent is an **AWS Ops Investigator**: it reads one AWS account and
explains what is broken, what changed, and what it costs, with evidence and without the ability to touch
anything. It is for AWS builders who have a harness agent working on a laptop and now have to put it in
front of other people. "The right way" here means five specific things, and every one of them is a switch
you can read in the code: **read-only is enforced in three independent layers** (an allowlist in the tool,
a deny-only intervention, an IAM deny policy), **one factory builds every agent** (`build_agent()`, so the
local and cloud profiles cannot drift), **no state lives on the microVM** (sessions and memory go to
AgentCore Memory, the workspace is a tempdir), **identity comes from the token** (the actor id is the JWT
`sub`, not a client-supplied string), and **the traces actually appear** in CloudWatch instead of being
silently swallowed by a second exporter.

If you have never built a Strands agent, start at [AWS_AgentLab](https://github.com/iracic82/AWS_AgentLab)
and come back. If you have, [`docs/from-agentlab-to-this.md`](docs/from-agentlab-to-this.md) maps every
step of that lab onto this repo.

## Run it locally in five minutes

You need Python 3.12, [uv](https://docs.astral.sh/uv/), Node 20+, and AWS credentials with Bedrock access
in a region where AgentCore runs. Docker with buildx is optional: `make deploy` builds both arm64 images on
an ARM CodeBuild host inside your account, so Docker is only needed to run the container on your own
machine (PITFALLS P55).

```bash title="manual"
git clone https://github.com/iracic82/strands-harness-on-aws.git
cd strands-harness-on-aws
uv sync
export AWS_PROFILE=<your-profile>      # naming the profile explicitly is not optional, see PITFALLS P30
export AWS_REGION=us-west-2 AWS_DEFAULT_REGION=us-west-2   # both, see PITFALLS P28
```

```bash title="manual"
make setup     # asks STS who you are, writes agentcore/aws-targets.json and .env
make doctor    # 15 checks; every FAIL prints the fix
```

`make setup` is the only thing standing between a fresh clone and a working one. It calls STS for your
account id and writes `agentcore/aws-targets.json` (from the committed `aws-targets.example.json`) and
`.env` (from `.env.example`). Both files are git-ignored, because both carry your account id, and nothing
account-specific is in this repo at all -- `make hygiene` is the test that keeps it that way. Setup is
idempotent: run it as often as you like, and a file you have edited yourself is reported and kept unless
you pass `--force`. If STS cannot answer, it writes nothing and exits 2 with the credential fix.

The region is `--region` if you pass one, otherwise `AWS_REGION`, otherwise `us-west-2`. `make setup`
always passes `--region`, because the Makefile resolves it that way, so your profile's own default region
is not consulted on that path -- run `uv run python scripts/setup.py` directly if that is what you want.

`make doctor` is the whole point of the repo in one command. It checks your Python, uv, Node, and CLI, then
names the account and caller ARN it is about to use, then checks the Anthropic use-case form, the region,
AgentCore reachability, the region pinned into `agentcore/agentcore.json` against the one you are
deploying to, both models with a real one-token `ConverseStream`, CloudWatch Transaction Search, the CDK
bootstrap stack, arm64 buildx (a WARN: the deploy builds remotely), and whether you have accidentally
committed local transcripts.
`doctor.py` exits 0 when every check is PASS, 1 on a WARN, and 2 on a FAIL. `make doctor` runs it with
`--exit-on fail`, which reports 2 for a FAIL and 0 for anything else, so one optional tool missing does
not end the chain above and a broken interpreter still does.

Now run the agent on your laptop. It talks to real Bedrock and real AWS read APIs, but nothing is deployed.

```bash title="manual"
make dev
```

In a second terminal:

```bash title="manual"
make invoke PROMPT="Which AWS account and region am I looking at, and how many Lambda functions are there?"
```

`make invoke` with no `ARN` targets the local server, generates a compliant session id, and prints the
stream. The raw HTTP call it makes is worth seeing once, because it is the same shape AgentCore uses:

```bash title="manual"
curl -N localhost:8080/invocations \
  -H 'Content-Type: application/json' \
  -H 'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: 0123456789abcdef0123456789abcdef01234567' \
  -H 'X-Actor-Id: me' \
  -d '{"prompt":"List the S3 buckets in this account."}'
```

The session id must be at least 33 characters or the request is rejected before the agent runs. In the
cloud the service rejects it; locally the app does, with the same message. See PITFALLS P9.

The local profile keeps the harness's own file-backed session and memory under
`app/ops_investigator/.agent/`, so you can read exactly what the harness persisted. To ask a second
question in the same conversation, reuse the session id that the first invoke printed to stderr:

```bash title="manual"
uv run python scripts/invoke.py --dev --session <the printed id> --prompt "And how many of those are idle?"
```

The local profile is not the cloud profile. It runs under your own AWS credentials, so the IAM deny layer
is whatever your profile allows, and it keeps `shell` (vetted by the guard) and `web_fetch` (not vetted) so
you can explore. Only the deployed runtime has all three layers. See
[`docs/why-each-switch.md`](docs/why-each-switch.md).

## Deploy it

Five commands, then a second deploy because of a chicken-and-egg problem that is worth understanding.

```bash title="manual"
make doctor
DEMO_PASSWORD='<a strong password>' make auth-bootstrap
make plan                 # agentcore deploy --dry-run: shows the CDK diff, changes nothing
make deploy
make status
```

`make auth-bootstrap` creates a Cognito user pool, a public app client with `USER_PASSWORD_AUTH`, and a
demo user, then edits `agentcore/agentcore.json` so both runtimes validate tokens from that pool. It also
pins `REGION` into the runtimes' `AWS_REGION`/`AWS_DEFAULT_REGION` there, which is the only thing that
sets them: the committed file carries a placeholder, and a runtime deployed with the wrong region in it
comes up READY and then fails every invocation (PITFALLS P62). It is idempotent. `make deploy` runs the `@aws/agentcore` CLI, which synthesises CDK, builds two arm64 images on
a native ARM CodeBuild host, pushes them to ECR, and creates the two runtimes plus the memory.

Now the second deploy. The A2A agent card has to advertise its own public URL, and that URL only exists
once the runtime does. So: read the A2A runtime's ARN out of `make status`, build the invocation URL, put
it in the `A2A_PUBLIC_URL` env var of the `OpsInvestigatorA2A` runtime in `agentcore/agentcore.json`, and
deploy again. `scripts/smoke.sh` is the reference implementation of that, and it also runs everything
above, so it is the one-command version of this whole section. It deploys real infrastructure and spends
real money.

```bash title="manual"
AWS_PROFILE=<your-profile> DEMO_PASSWORD='<a strong password>' bash scripts/smoke.sh
```

It runs `doctor`, bootstraps auth, deploys, reads the ARNs out of `status`, writes the A2A URL back,
deploys a second time, runs the demo, and then counts `invoke_agent` spans. On the first real run the first
deploy took about four minutes (both images build in parallel on CodeBuild; the memory is the slow part)
and each later deploy about ninety seconds. Budget ten minutes for the whole script.

Two things about that span count are worth knowing before you read it as a failure. Transaction Search is
enabled per region and takes several minutes after the deploy to start indexing into `aws/spans`, so the
count can be zero while tracing is working perfectly -- which is why the script also counts X-Ray traces
for the same window (PITFALLS P59). And the answer to "which token does the authorizer accept" is the
**access** token, not the ID token: `allowedClients` is matched against the `client_id` claim, which only
the access token carries, and an ID token comes back `401 Claim 'client_id' value mismatch with
configuration` (PITFALLS P60).

Then invoke the deployed runtime with a real token:

```bash title="manual"
export ARN=<the OpsInvestigator ARN from make status>
export CLIENT_ID=<the app client id auth_bootstrap printed>
export TOKEN=$(DEMO_PASSWORD='...' make token)
make invoke ARN="$ARN" TOKEN="$TOKEN" PROMPT="What changed in this account in the last 24 hours?"
```

Before you commit anything, put the placeholders back. `auth_bootstrap` and `smoke.sh` write a real
discovery URL, a real client id, and an A2A URL that contains your account id into `agentcore.json`:

```bash
git checkout agentcore/agentcore.json
```

`uv run python scripts/setup.py --reset-placeholders` does the same to the values without touching the
CLI's original whitespace, which is what you want when the file has other local edits you are keeping.
Either way, `make hygiene` is the check. It fails on an account id, an ARN containing one, a Cognito pool
id, a `/Users/` path, or the repo owner's own profile name, in anything git would publish -- tracked
files and untracked ones that are not ignored -- and on a generated file that is tracked rather than
ignored. gitleaks runs beside it over both history and the working tree. It is part of `make lint`.

## The demo

`scripts/demo.sh` runs the two-session memory demo. Session A states a preference and is thrown away.
AgentCore Memory extracts it asynchronously, which takes minutes and not milliseconds, so the script polls
`retrieve_memory_records` for up to 180 seconds. Session B is a brand new session id for the same actor,
and it recalls the preference.

```bash title="manual"
REGION=us-west-2 ARN="$ARN" CLIENT_ID="$CLIENT_ID" MEMORY_ID="$MEMORY_ID" \
  DEMO_PASSWORD='...' A2A_URL="$A2A_URL" bash scripts/demo.sh
```

Four prompts worth showing live, and what each one proves:

| Prompt | What it shows | Where to point |
|---|---|---|
| "Which account and region are you in? Use `aws_call sts get_caller_identity`." | The tool loop, and that the agent cites account and region before anything else | The `tool_use` and `tool_result` frames in the terminal |
| "Use `aws_call lambda get_function_configuration` on orders-api." | Read-only enforcement. A routine-looking call that is not on the read allowlist reaches the guard and is denied before it is made. (An obviously destructive prompt such as "delete the oldest bucket" never gets this far: the model refuses first, so no `denied` frame appears.) | The `[denied]` frame in the client, and `guard.denied tool=aws_call reason=...` in `make logs` |
| "Read the value of the `/prod/db/password` SSM parameter." | That "read-only" is not enough. `ssm:GetParameter` is a read by name and returns a secret, so it is in the deny table at all three layers | The `denied` frame, then a `tool_result` frame with `status=error`, and the same `guard.denied` line. The call never reaches AWS, which is the point |
| "What did I say my top cost concern was?" (session B) | Long-term memory across sessions, namespaced by the JWT `sub` | The answer itself; `make memory-wipe MEMORY_ID=... ACTOR=...` forgets it again |

The full trace walkthrough is [`docs/reading-the-traces.md`](docs/reading-the-traces.md).

## Architecture

```mermaid
flowchart LR
  subgraph laptop["Your laptop"]
    dev["make dev<br/>local profile"]
    inv["scripts/invoke.py"]
    a2ac["scripts/a2a_client.py"]
  end
  subgraph aws["AWS region"]
    cog["Cognito user pool<br/>CUSTOM_JWT authorizer"]
    subgraph runtime["AgentCore Runtime"]
      http["OpsInvestigator<br/>HTTP + SSE"]
      a2a["OpsInvestigatorA2A<br/>A2A 1.0"]
    end
    mem["AgentCore Memory<br/>USER_PREFERENCE + SEMANTIC"]
    bed["Bedrock<br/>Claude Sonnet 4.6"]
    acct["The account under<br/>investigation, read-only"]
    cw["CloudWatch<br/>GenAI Observability"]
  end
  inv -->|"Bearer JWT"| http
  a2ac -->|"SendMessage"| a2a
  cog -.->|"validates the token"| http
  cog -.->|"validates the token"| a2a
  http --> mem
  a2a --> mem
  http --> bed
  a2a --> bed
  http --> acct
  a2a --> acct
  http -->|"ADOT spans"| cw
  a2a -->|"ADOT spans"| cw
  dev --> bed
```

Both runtimes are the same image content behind two entrypoints and the same `build_agent()`. AgentCore
allows one protocol per runtime, which is the only reason there are two. The full picture, including the
trust boundaries and which control covers each, is [`docs/architecture.md`](docs/architecture.md).

### File map

| Path | What lives there |
|---|---|
| `app/ops_investigator/main.py` | HTTP entrypoint. Three lines; everything is in the package |
| `app/ops_investigator/a2a_main.py` | A2A entrypoint |
| `.../ops_investigator/agent.py` | `build_agent()`, `LIMITS`, the `reader` subagent. The one factory |
| `.../ops_investigator/platform.py` | `detect()` and every backend switch: session, memory, skills, builtin tools, workspace |
| `.../ops_investigator/guard.py` | `ReadOnlyGuard`, the deny-only intervention |
| `.../ops_investigator/auth.py` | Actor id from the JWT `sub` |
| `.../ops_investigator/http_app.py` | Payload schema, agent cache, wall clock, SSE frames, limit handling |
| `.../ops_investigator/a2a_app.py` | `HarnessExecutor`, the A2A 1.0 bridge |
| `.../ops_investigator/sse.py` | The versioned `v: 1` frame schema and the cost estimate |
| `.../ops_investigator/preflight.py` | Startup self-checks, logged to CloudWatch |
| `.../ops_investigator/tools/` | `aws_call`, `logs_insights`, `cost_explorer`, and the shared budget/client helpers |
| `.../skills/investigate-incident/` | The runbook the harness loads on demand |
| `doctor.py` | Standalone preflight. Copy it into any harness project; it only needs boto3 |
| `agentcore/agentcore.json` | Two runtimes, one memory, env vars, IAM, the JWT authorizer |
| `policies/execution-role-deny.json` | The explicit IAM deny list |
| `scripts/` | `auth_bootstrap`, `get_token`, `invoke`, `a2a_client`, `memory_wipe`, `demo.sh`, `smoke.sh` |

## A2A

The second runtime speaks [A2A](https://a2a-protocol.org/) **1.0** over `a2a-sdk` 1.x. Fetch its card:

```bash title="manual"
make a2a-card A2A_URL="$A2A_URL" TOKEN="$TOKEN"
```

1.0 is not 0.3 with a new number on it, and this is the single most expensive surprise in the repo. In 0.3
the card had a top-level `url` and `protocolVersion`, the JSON-RPC method was `message/send`, and the types
were Pydantic models with `kind` discriminators. In 1.0 the transport and version live on each entry of
`supportedInterfaces`, the method is `SendMessage`, every request must carry an `A2A-Version: 1.0` header,
and the types are protobuf messages. Strands' own `A2AServer` still pins the 0.3 SDK, so this runtime
builds its card by hand in `agent_card.py` and runs its own `HarnessExecutor`. `scripts/a2a_client.py`
reads the version off the card and says so plainly if it is talking to a 0.3 server. See PITFALLS P22 and
P40.

Locally the A2A server listens on `0.0.0.0:9000` with **no authenticator at all**. There is no AgentCore
authorizer in front of it, and the card honestly advertises no security scheme when it has no discovery
URL. Do not expose that port.

## Cost

A turn is one question and the tool calls the agent makes to answer it.

| | `est_usd` |
|---|---|
| Measured, one-line answer | ≈ $0.001 |
| Measured, the denied-call turn in [the demo transcript](docs/img/demo-terminal.txt) | ≈ $0.009 |
| Measured, four-cycle investigation with tool calls | ≈ $0.03 |
| What the `Limits` allow at most, for one agent invocation | ≈ $8 |

The first three are what the `metrics` frames reported on the deployment the screenshots above come from.
They are the harness's own estimates against the Bedrock price list and have not been reconciled against a
real invoice. The ≈ $8 figure is not a measurement: it is arithmetic on
`Limits(turns=25, output_tokens=60_000, total_tokens=1_500_000)` in `agent.py`, which the runtime enforces
per invocation and reports in the `metrics` frame as `est_usd`.

The `reader` subagent's cap is **per delegation, not per turn**: the parent can delegate repeatedly, and a
delegate's budget is not counted against the parent's. So ≈ $8 is not a ceiling on what one turn can
spend. Cost Explorer calls bill about a cent each, which is why that tool has a budget of 10 per turn.

To spend less: set `HARNESS_EFFORT=low`, which cuts the thinking budget, or point `HARNESS_MODEL` at
Haiku. To spend more and think harder, `HARNESS_EFFORT=high`. Both are env vars on the runtime in
`agentcore/agentcore.json`, so changing them needs a `make deploy` but not a rebuild. `LIMITS` itself is a
constant in `agent.py`; lowering the cap is a code change on purpose, because it is the last line of
defence.

Set your own billing alarm. Between repeated delegation and a human who keeps re-prompting, nothing in
this repo is a spend ceiling.

## Security posture

The agent cannot write to AWS, and that is checked three times independently.

1. **The tool.** `aws_call` accepts only `describe_/list_/get_/lookup_/search_/query_` operations, minus a
   deny table of reads that return secrets: SSM parameters, S3 object bodies, DynamoDB items, Lambda
   config, EC2 user data, CloudFormation templates, and every CloudWatch Logs read (use `logs_insights`,
   which has its own prefix allowlist and refuses the agent's own groups). Output is capped at 40k
   characters and each turn gets 40 calls.
2. **The intervention.** `ReadOnlyGuard` runs before every tool call and returns `Deny`, never `Confirm`.
   A `Confirm` would pause the turn waiting for a human who is not there (PITFALLS P10).
3. **IAM.** The execution role gets `ReadOnlyAccess` plus `policies/execution-role-deny.json`, an explicit
   deny that mirrors the tool's table. Explicit deny wins over everything.

In the cloud profile there are **no egress-capable tools at all**: `shell`, `web_fetch`, `web_search`,
`write`, and `edit` are pinned off, and the programmatic tool caller may call only the three AWS tools.
That is what makes `networkMode: PUBLIC` defensible.

One thing the agent's read-only posture does **not** cover: the conversation itself is recorded. Strands
emits the system prompt, the user prompt, the model's reasoning, tool arguments and the answer as
`gen_ai.*.message` span events, and they land in the runtime log group with retention *never expire*. There
is no off switch wired up in this release: set a retention policy on both runtime log groups after your
first deploy, which `scripts/smoke.sh` prints the command for (PITFALLS P61).

Identity is the JWT `sub`, taken from the `Authorization` header that the AgentCore authorizer already
validated. Memory namespaces are built from it, so a caller cannot read another caller's memory by
claiming their id. Everyone holding a token from the allowed Cognito client shares one trust domain;
splitting that needs per-user Cognito groups, which this repo does not do.

The attributes this repo sets on its spans carry metadata only: tool names, services, operations, byte
counts, durations, statuses, and ids. The conversation text still reaches CloudWatch Logs through the
Strands span events described above; the SSE stream and that log group are the two places a turn's text
exists.

Full detail in [`PITFALLS.md`](PITFALLS.md) and [`docs/architecture.md`](docs/architecture.md).

## Clean up

```bash title="manual"
make memory-wipe MEMORY_ID="$MEMORY_ID" ACTOR="$ACTOR"     # one actor's records, while the memory exists
make destroy                                               # runtimes, memory, ECR, CodeBuild, the stack
git checkout agentcore/agentcore.json                      # destroy empties it; this is the only way back

aws cognito-idp delete-user-pool --user-pool-id <pool id>  # auth_bootstrap does not delete it
aws logs delete-log-group --log-group-name /aws/bedrock-agentcore/runtimes/<runtimeId>-DEFAULT
aws logs delete-log-group --log-group-name /aws/codebuild/AgentCore-<project>-<target>-container-builder
```

`make memory-wipe` comes first because it needs the memory to still be there. Both variables are required;
to see what would go without deleting it, call the script directly with `--dry-run`.

**`make destroy` takes more with it than its name suggests, and leaves one thing behind that matters.**
It runs `agentcore remove all -y` followed by `agentcore deploy -y`. The `remove` step rewrites
`agentcore/agentcore.json` to an empty project — your two runtimes and your memory are gone *from the
file*, not just from AWS — and the deploy then tears the CloudFormation stack down entirely. Measured on a
real teardown: both runtimes, the memory, **both ECR repositories**, the CodeBuild project, every IAM role
and the stack itself were gone in 286 seconds. `git checkout agentcore/agentcore.json` is what puts the
config back; `--reset-placeholders` cannot, because it only rewrites values in runtimes that are still
there.

What survives, and what to do about it:

- **The log groups — all of them.** The two runtime log groups are created by the AgentCore platform
  rather than by the stack, so nothing in the stack deletes them, and they hold the whole conversation
  with retention *never expire* (PITFALLS P61). The CodeBuild and Lambda build-log groups survive too.
  Delete them by hand, as above; `scripts/smoke.sh` prints the exact names at the end of a run.
- **The Cognito user pool.** `auth_bootstrap` creates it outside CloudFormation and nothing removes it.
- **The two KMS keys** that encrypted the ECR repositories are *scheduled* for deletion, not deleted: AWS
  enforces a waiting period, so expect them in `PendingDeletion` for up to 30 days. Nothing to do.
- **The CDK bootstrap stack** (`CDKToolkit`) and its asset repository are shared with every other CDK
  project in the account. Leave them alone unless you are done with CDK in that account entirely.

## Screenshots

All three were captured from the first real deployment of this repo (Sonnet 4.6 on AgentCore Runtime in
us-west-2). The account id and resource ids are redacted; nothing else is edited.

**The demo, end to end.** Session A stores a preference, session B (new session id, same JWT `sub`) recalls
it after AgentCore Memory has extracted it, then a deny-listed call is refused by `ReadOnlyGuard`. The
transcript is in [`docs/img/demo-terminal.txt`](docs/img/demo-terminal.txt), trimmed where it listed the
account's own resources.

![Two sessions sharing memory, then a denied tool call](docs/img/demo-terminal.png)

**One invocation as a trace.** Fetched from X-Ray with `aws xray batch-get-traces` and rendered as a
timeline: the runtime segment, `invoke_agent`, one `execute_event_loop_cycle` per model round, and the
`chat`, `execute_tool`, and `memory.*` spans under each. The per-call SDK client spans are hidden for
readability. The CloudWatch console shows the same tree under X-Ray traces; the GenAI Observability view
depends on Transaction Search delivering into `aws/spans`, which lagged by more than an hour during the
smoke (PITFALLS P59).

![The span tree of one invocation](docs/img/xray-trace.png)

**The A2A agent card.** `GET /.well-known/agent-card.json` on the A2A runtime, with the Cognito bearer
token, as the JSON the runtime returned: protocol version 1.0, JSON-RPC binding, the Cognito OpenID
security scheme, and the two skills.

![The agent card served by the A2A runtime](docs/img/agent-card.png)

## Learning path

Read in this order:

1. [`docs/how-a-turn-flows.md`](docs/how-a-turn-flows.md) - one request, end to end, with the real frames.
2. [`docs/why-each-switch.md`](docs/why-each-switch.md) - every `create_harness()` kwarg, the default, why
   it breaks on AgentCore, and what we pass instead.
3. [`docs/architecture.md`](docs/architecture.md) - the components and the trust boundaries.
4. [`docs/reading-the-traces.md`](docs/reading-the-traces.md) - spans, attributes, and where cost shows up.
5. [`docs/from-agentlab-to-this.md`](docs/from-agentlab-to-this.md) - if you did the lab.
6. [`PITFALLS.md`](PITFALLS.md) - 62 numbered traps, each with symptom, cause, fix, and the file that
   handles it.

## Configuration

Every knob is an environment variable. On the deployed runtimes they live in `envVars` in
`agentcore/agentcore.json`, so changing one needs a `make deploy` but not a rebuild. Locally, export them.
`make setup` writes a git-ignored `.env` from `.env.example` with your region and account already filled
in, but nothing here loads it for you: there is no dotenv dependency, so source it yourself with
`set -a; . ./.env; set +a` when you want it.

| Variable | Default | What it does |
|---|---|---|
| `HARNESS_MODEL` | `bedrock/global.anthropic.claude-sonnet-4-6` | The model, as a `provider/name` string. Never build a `BedrockModel` yourself (PITFALLS P11) |
| `HARNESS_EFFORT` | `medium` | Thinking budget. One of `auto`, `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`. An unknown value fails at startup with the list |
| `HARNESS_MAX_SECONDS` | `600` | Wall clock for one turn. A value that is not a number logs an error and falls back rather than stopping the container |
| `OPS_LOG_GROUP_PREFIXES` | `/aws/lambda/,/aws/ecs/,/aws/apigateway/,/aws/eks/` | Comma-separated prefixes `logs_insights` may query. The agent's own groups, `/aws/bedrock-agentcore/` and `aws/spans`, are refused regardless of what you set here |
| `ENABLE_AWS_DOCS_MCP` | unset | Set to `1` to attach the AWS documentation MCP server over streamable HTTP, with `continue_on_error` (PITFALLS P13) |
| `MEMORY_OPSINVESTIGATORMEMORY_ID` | injected by CDK | **The profile switch.** Set means cloud, unset means local. You do not set this by hand |
| `AWS_REGION` and `AWS_DEFAULT_REGION` | `us-west-2` | Always set both. botocore reads only the second one (PITFALLS P28) |
| `A2A_PUBLIC_URL` | placeholder | The A2A runtime's own invocation URL, which the agent card advertises. Written back after the first deploy |
| `COGNITO_DISCOVERY_URL` | placeholder | The OIDC discovery document the A2A card names in its security scheme. Written by `auth_bootstrap` |
| `OTEL_TRACES_EXPORTER` | set to `otlp` by ADOT itself | Leave it alone. ADOT sets it before your code runs; setting it to anything else makes the harness fight ADOT for the exporter and you lose your traces (PITFALLS P8, P56) |
| `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT` | unset | Not an off switch for content capture. Unset, ADOT lifts prompt/response content off the span into a separate Gen AI Event log record; set to `true`, spans are exported unmodified, which leaves *more* on the span. Either way the conversation is recorded: Strands emits it as `gen_ai.*.message` span events into the runtime log group (PITFALLS P61) |

## Tested versions

Verified 2026-09-22 on macOS arm64 and in CI on Ubuntu.

| Component | Version |
|---|---|
| `strands-harness` | 0.1.1 |
| `strands-agents[otel]` | 1.56.0 |
| `bedrock-agentcore` | 1.23.1 |
| `@aws/agentcore` (npm CLI) | 0.30.0 |
| `a2a-sdk[http-server]` | 1.1.5 |
| `aws-opentelemetry-distro` | 0.18.x |
| `boto3` / `botocore` | 1.43.99 |
| Python | 3.12 in the image; 3.10+ supported; CI runs 3.12 and 3.14 |
| Default model | `bedrock/global.anthropic.claude-sonnet-4-6` |

The lockfile is committed and CI installs from it with `--frozen`. Dependabot watches both Python projects,
the generated CDK app, and the GitHub Actions, weekly.

## Links

- [Introducing the Strands harness](https://strandsagents.com/blog/introducing-strands-harness/) - the blog post this repo follows.
- [`strands-harness` on PyPI](https://pypi.org/project/strands-harness/) and [`strands-agents`](https://pypi.org/project/strands-agents/).
- [Amazon Bedrock AgentCore documentation](https://docs.aws.amazon.com/bedrock-agentcore/).
- [`@aws/agentcore` CLI](https://www.npmjs.com/package/@aws/agentcore).
- [A2A protocol](https://a2a-protocol.org/).
- [AWS_AgentLab](https://github.com/iracic82/AWS_AgentLab) - start here if you have never built a Strands agent.

## Licence

Apache-2.0. See [`LICENSE`](LICENSE).
