# From AWS_AgentLab to this repo

[AWS_AgentLab](https://github.com/iracic82/AWS_AgentLab) teaches the raw Strands SDK: you construct an
`Agent`, hand it tools, attach MCP servers, put a Gateway in front of some of them, and deploy with the
Python starter toolkit. This repo is the same journey with the harness doing the parts you were doing by
hand, and with the deployment path that has replaced the one the lab uses.

If you did the lab, this page is the diff.

## The mapping

| In the lab | Here | Why it changed |
|---|---|---|
| `Agent(model=..., tools=[...], system_prompt=...)` constructed where you need it | One `build_agent(session_id, actor_id)` calling `create_harness()` | The harness brings a tuned agent loop, context management, prompt caching, skills, and a tool set. You configure it rather than assemble it. One factory means the local and cloud paths cannot drift |
| A `BedrockModel(...)` you build and pass | A `provider/name` **string**, `bedrock/global.anthropic.claude-sonnet-4-6` | Constructing the model yourself means owning its boto config, and a `boto_client_config` silently drops the 120 second read timeout (P11). The string path keeps the harness's effort mapping, thinking budget, and cache points |
| `@tool` functions you wrote, called directly | The same `@tool` functions, plus harness built-ins you now have to *pin* | The harness ships `shell`, `write`, `edit`, `web_fetch` and more, on by default. A `builtin_tools` mapping **edits** that set rather than replacing it, which is P21 and the easiest thing here to get wrong |
| A system prompt string | `instructions=`, appended to the harness's own contract | The harness has a prompt of its own that you are adding to, not replacing. `AGENTS.md` is injected separately each turn by the `environment` plugin |
| Conversation state in memory, or a file you managed | `session=AgentCoreMemorySessionManager(...)` in cloud, the harness file store locally | The microVM is ephemeral and `/app` is read-only for the runtime user (P2) |
| Nothing, or a vector store you wired yourself | `memory=` harness stores over AgentCore Memory, with AgentCore strategies extracting | Long-term memory that survives the session, namespaced by the caller's identity. The trap is turning on *both* writers (P3) |
| MCP servers as a first-class step | `mcp_servers=`, opt-in behind `ENABLE_AWS_DOCS_MCP=1`, streamable HTTP, `continue_on_error` | A stalled MCP call hangs the whole agent (P13), and spawning `uvx` at cold start costs you the cold start. Same mechanism, defensive defaults |
| AgentCore Gateway in front of tools | Not used | A non-goal for this version. Gateway is a separate trust boundary and deserves its own treatment. The three AWS tools are in-process and allowlisted |
| `agentcore configure` then `agentcore launch` (Python starter toolkit) | `npx @aws/agentcore deploy` against a committed `agentcore/agentcore.json`, generating a CDK app you own | The Python toolkit is deprecated (P1), and both install a binary called `agentcore`, so the old one shadows the new one on PATH (P20). The CDK output is real infrastructure-as-code in your repo |
| An identity you passed in, or a fixed user | The JWT `sub` from the allowlisted `Authorization` header | The generated template reads `context.user_id`, which does not exist, so every caller shares one namespace (P24) |
| Print statements and a terminal | Versioned SSE frames, structured logs, ADOT spans in CloudWatch | The stream has a schema so a client can be written against it, and the traces need three separate settings to appear at all (P8) |
| Run it and see | `make doctor` first | Fourteen checks, each with the fix in the failure. Most of this repo's value is in the checks that fail before you spend money |

## What the harness gives you that you were writing yourself

Worth naming, because it is the reason to switch: the agent loop with tool-use cycles, automatic context
compaction, prompt caching, a skills mechanism that loads runbooks on demand, subagent delegation with its
own tool profile, offloading of oversized tool results, effort levels that map to thinking budgets, and the
`environment` plugin that injects `AGENTS.md` each turn. In the lab, each of those was either code you wrote
or a thing you did without.

## What is deliberately not carried over

**The Python starter toolkit.** `bedrock-agentcore-starter-toolkit` is deprecated. Every `agentcore` call
in this repo goes through `npx -y @aws/agentcore@0.30.0` from the repo root, so PATH cannot pick the wrong
binary for you. `doctor.py` detects the old toolkit by its Click-style usage banner and tells you how to
uninstall it.

**Managing your own CodeBuild project.** The starter toolkit's build path had you own a CodeBuild project
for the container build. Here the generated CDK stack owns the build: it runs on a native ARM64 CodeBuild
image, so the plain `docker build` inside it is already `linux/arm64` and the whole arm64 class of failures
(P16) never reaches you. You never configure it, and `make plan` shows you what it will do before it does
it.

**Gateway, outbound OAuth, Code Interpreter, and Browser.** All out of scope for this version, each for the
same reason: they open a boundary that needs its own analysis, and this repo's claim is that everything in
it has been thought through.

**Any write operation.** The lab's agents can act. This one cannot, and three independent layers say so.
See [architecture.md](architecture.md), boundary 3.

## The order to read the code in

If you knew your way around the lab, this is the shortest path to knowing your way around here.

1. `app/ops_investigator/ops_investigator/agent.py`. Forty lines of `create_harness()`. This is the whole
   agent.
2. `app/ops_investigator/ops_investigator/platform.py`. Every switch, and the one branch between local and
   cloud. [why-each-switch.md](why-each-switch.md) is the annotated version of this file.
3. `app/ops_investigator/ops_investigator/http_app.py`. What a hosted runtime has to do that a script does
   not: validate, authenticate, cache, bound, stream, and never throw.
4. `doctor.py`. Standalone and dependency-free on purpose. Copy it into your own harness project; it is the
   part of this repo most likely to save you an afternoon.

Then [PITFALLS.md](../PITFALLS.md), which is fifty-one reasons the first three files look the way they do.
