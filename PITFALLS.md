# PITFALLS

Fifty-one numbered traps between a Strands harness agent that works on your laptop and one that works on
Amazon Bedrock AgentCore. P1 to P27 were found while designing this repo and are fixed in its code. P28 to
P51 were found while building it, which is to say the hard way.

Every entry has the same four fields. **Where in this repo** names the file and function that handles it,
so you can read the fix rather than trust this page.

Verified 2026-09-22 against `strands-harness` 0.1.1, `strands-agents` 1.56.0, `bedrock-agentcore` 1.23.1,
`@aws/agentcore` 0.30.0, `a2a-sdk` 1.1.5, `botocore` 1.43.99, Starlette 1.6.0, Python 3.12.

## Contents

- [Toolchain and deploy](#toolchain-and-deploy): P1, P16, P18, P20, P39, P44, P47
- [Region, models, and access](#region-models-and-access): P5, P6, P7, P17, P25, P28, P29, P30
- [State, memory, and sessions](#state-memory-and-sessions): P2, P3, P14, P23, P27, P37, P42, P43, P48
- [Tools and the agent loop](#tools-and-the-agent-loop): P4, P10, P11, P12, P13, P19, P21, P33, P34, P50
- [Runtime, streaming, and protocols](#runtime-streaming-and-protocols): P9, P22, P24, P35, P36, P40, P41, P45, P46, P51
- [Security and IAM](#security-and-iam): P15, P26, P31, P32
- [Observability and operations](#observability-and-operations): P8, P38, P49

---

## Toolchain and deploy

### P1. The Python starter toolkit is deprecated and most tutorials still use it

**Symptom.** You follow a blog post, run `agentcore configure` then `agentcore launch`, and end up with a
project layout that matches nothing in the current AgentCore documentation, or a deploy that cannot be
reproduced from your repo.
**Cause.** `bedrock-agentcore-starter-toolkit` (pip) has been superseded by `@aws/agentcore` (npm), which
generates a real CDK project you own. Both install a binary called `agentcore`.
**Fix.** Use only the npm CLI, pinned, through `npx`, from the repo root. Never install the Python one.
**Where in this repo.** `Makefile` (the `AGENTCORE = npx -y @aws/agentcore@0.30.0` variable, used by every
target); `doctor.py:_agentcore_cli_check` detects the deprecated toolkit by its Click-style usage banner
and WARNs with the uninstall command -- a WARN and not a FAIL, because `npx` makes PATH irrelevant, so a
shadowed or missing global CLI stops nothing here. Pinned by `tests/test_scripts.py::test_makefile_only_reaches_the_agentcore_cli_through_npx_from_the_repo_root`.

### P16. AgentCore Runtime is arm64 only

**Symptom.** The runtime never becomes READY, and the container log says `exec format error`.
**Cause.** You built an amd64 image on an amd64 laptop or CI runner. AgentCore runs arm64 microVMs.
**Fix.** Build with `--platform linux/arm64` everywhere, and check that buildx can do it before you start.
**Where in this repo.** `app/ops_investigator/Dockerfile` (the CDK builds it on a native ARM CodeBuild
host, so the plain `docker build` there is already arm64); `.github/workflows/ci.yml` builds both images
with `--platform linux/arm64`; `doctor.py:_docker_arm64_check` FAILs with the `binfmt` install command.

### P18. Version drift, in a stack where everything is days old

**Symptom.** A clean clone stops working a week later, or CI resolves a different harness than your laptop.
**Cause.** The harness is new, `pydantic-monty` is pinned exactly upstream, and the CLI's own templates
disagree with the runtime about the Python version.
**Fix.** Commit the lockfile, install from it everywhere, pin the base image by digest, and let Dependabot
open the upgrade PRs rather than discovering them at deploy time.
**Where in this repo.** `uv.lock` committed; `.github/workflows/ci.yml` uses `uv sync --frozen`;
`app/ops_investigator/Dockerfile` pins `python:3.12-slim-trixie` by SHA-256 digest;
`.github/dependabot.yml`.

### P20. A stale `agentcore` binary shadows the one you meant to run

**Symptom.** `agentcore --version` prints something that does not look like the npm CLI, or `agentcore
deploy` cannot find your project.
**Cause.** The deprecated Python toolkit installs `~/.local/bin/agentcore`, which comes before
`/opt/homebrew/bin/agentcore` on a normal macOS PATH. This was live on the machine this repo was built on.
**Fix.** Call the CLI as `npx -y @aws/agentcore@<version>` so PATH cannot decide for you, and uninstall the
old toolkit.
**Where in this repo.** `Makefile` (`AGENTCORE`); `scripts/smoke.sh` does the same; `doctor.py:_agentcore_cli_check`.

### P39. `agentcore status --json` is a flat `resources[]`, and two flags in the docs do not exist

**Symptom.** Your script reads `status["runtimes"]` and gets `KeyError`. Or `agentcore deploy --plan` and
`agentcore logs --agent <name>` both fail with unknown-option errors.
**Cause.** CLI 0.30.0 returns `{success, projectName, targetName, targetRegion, resources[], deployedState,
logPath}`, where `resources` is a flat array of `{resourceType, name, deploymentState, detail}`. Before the
first deploy every entry is `local-only` and carries no ARN at all. The real flags are `deploy --dry-run`
and `logs --runtime <name>`.
**Fix.** Parse the flat array by `name`, and fall back to scanning the whole document for an ARN that
belongs to the named runtime. Fail loudly when neither works rather than deploying against an empty string.
**Where in this repo.** `Makefile` targets `plan` (`deploy --dry-run`), `status` (`status --json`), `logs`
(`logs --runtime`); `scripts/smoke.sh` (the inline `runtime_arn`/`memory_id` parser and the `${ARN:?}`
guards).

### P44. The committed `discoveryUrl` placeholder has to be a real URL

**Symptom.** `agentcore validate` fails on a config you have not deployed yet:
`discoveryUrl: Must be a valid URL` / `must use HTTPS` / `must end with '/.well-known/openid-configuration'`.
**Cause.** `OidcDiscoveryUrlSchema` validates the field at synth time, so a bare `REPLACED_BY_...` token
cannot sit there waiting for a bootstrap script.
**Fix.** Make the placeholder a schema-valid URL that merely *contains* the token, and have the bootstrap
script edit the JSON structurally instead of doing a string replace. A blind token replace would put the
user pool id where the client id belongs.
**Where in this repo.** `agentcore/agentcore.json` (`https://cognito-idp.us-west-2.amazonaws.com/REPLACED_BY_auth_bootstrap/.well-known/openid-configuration`);
`scripts/auth_bootstrap.py:apply_auth` and `:reset_placeholders`; pinned by
`tests/test_agentcore_json.py::test_jwt_authorizer_placeholders_are_valid_oidc_discovery_urls`.

### P47. `additionalPolicies` JSON files are read from local disk at synth time

**Symptom.** Deploy fails with `ENOENT` on your policy file, or the policy silently does not apply.
**Cause.** An entry starting with `arn:` is attached as a managed policy. Anything else is resolved
relative to the runtime's `codeLocation`, read with `readFileSync` on the machine running CDK, and attached
as an inline policy. The file must exist in the repo at that relative path. It does **not** need to be in
the image, and should not be.
**Fix.** Keep the policy beside the code it applies to, and exclude it from the build context.
**Where in this repo.** `agentcore/agentcore.json` (`"additionalPolicies": [..., "policies/execution-role-deny.json"]`)
resolves to `app/ops_investigator/policies/execution-role-deny.json`; `policies/execution-role-deny.json`
at the root is the copy humans read, and
`tests/test_agentcore_json.py::test_root_policy_copy_matches_the_deployed_one` keeps the two identical.

---

## Region, models, and access

### P5. No region means an empty model list and silent failures

**Symptom.** The agent starts, then every model call fails, or the harness reports no models at all.
**Cause.** The harness resolves models per region and has nothing to resolve against without one.
**Fix.** Hard-fail at startup with the exact env var to set, rather than failing per request.
**Where in this repo.** `platform.detect` raises `RuntimeConfigError` when the cloud profile has no region;
`preflight.run_preflight` reports it as a FAIL check; `doctor.py:_region_set_check`. See also P28, which is
the half of this that costs people an afternoon.

### P6. The harness default is Opus 5 at effort `high`

**Symptom.** A demo that should cost a dollar costs fifteen.
**Cause.** `create_harness()` defaults are tuned for a developer at a terminal, not for a hosted service.
**Fix.** Set an explicit model and effort, and make both env vars so they can change without a rebuild.
**Where in this repo.** `platform.DEFAULT_MODEL` (`bedrock/global.anthropic.claude-sonnet-4-6`) and
`platform.DEFAULT_EFFORT` (`medium`), read from `HARNESS_MODEL` and `HARNESS_EFFORT` in `platform.detect`;
set on both runtimes in `agentcore/agentcore.json`; the `reader` subagent is pinned to `low` in
`agent.build_reader`.

### P7. AgentCore Runtime is enabled per account per region

**Symptom.** Every control-plane call in your favourite region fails, and the error does not say "not
enabled here".
**Cause.** AgentCore Runtime is enabled per account per region, and the control plane returns a generic
error rather than "not enabled here".
**Fix.** Probe the control plane in the region you chose, before you build anything.
**Where in this repo.** `doctor.py:_agentcore_region_check` calls `list_agent_runtimes(maxResults=1)` and
FAILs with "pick a supported region".

### P17. Global inference profiles route to regions you did not think about

**Symptom.** A model that works from your laptop fails from the runtime, intermittently.
**Cause.** A `global.` inference profile can route a request to any region in the profile, and the calling
role needs model permissions in each.
**Fix.** Nothing to do here, but know why: the CDK-generated execution role already grants
`foundation-model/*` and `inference-profile/*`. If you narrow that role by hand, narrow it per region.
**Where in this repo.** Documented only. The execution role is generated by the L3 construct and is not
edited; `policies/execution-role-deny.json` only ever *denies*.

### P25. `GetFoundationModelAvailability` does not tell you whether you can call the model

**Symptom.** The API says `AUTHORIZED`, and the call still fails. Or it says `NOT_AVAILABLE` for a model
you have been using.
**Cause.** Availability, agreement status, and entitlement are three different things, and the API mixes
them.
**Fix.** Treat `agreementAvailability.status != AVAILABLE` as a definite no, and prove a yes with a real
one-token `ConverseStream`. Streaming, not plain `Converse`: that is the call the harness makes, and an
account can be refused the streaming form of a model whose `Converse` succeeds.
**Where in this repo.** `doctor.py:_model_check` checks availability first and then calls
`doctor.py:_converse`; `preflight.converse_probe` does the same inside the container. Pinned by
`tests/test_doctor.py::test_model_availability_not_available_is_fail_before_any_converse_call`.

### P28. `AWS_REGION` alone does not give a boto3 client a region

**Symptom.** You export `AWS_REGION=us-west-2`, and your calls land somewhere else entirely. Measured on
botocore 1.43.99:

```
AWS_REGION=us-west-2          ->  boto3.Session().region_name == 'ca-central-1'   (the profile's region)
AWS_DEFAULT_REGION=eu-west-1  ->  boto3.Session().region_name == 'eu-west-1'
```

**Cause.** botocore resolves the default region from `AWS_DEFAULT_REGION` and the profile config. It does
not read `AWS_REGION` at all. Clients that pass `region_name=` are unaffected, which is why this hides:
your own code is fine, and the library's internal client (the harness's Bedrock client, built without an
explicit region) is the one that wanders off.
**Fix.** Always set both variables, everywhere.
**Where in this repo.** Both are in `envVars` for both runtimes in `agentcore/agentcore.json`; the
`Makefile` exports both through `REGION_ENV`; `scripts/demo.sh` and `scripts/smoke.sh` export both for
every child; `platform.detect`'s error text explains why. Pinned by
`tests/test_agentcore_json.py` (the two must be equal for every runtime).

### P29. The Claude 5 family is gated per account, and `agreementAvailability` is the signal

**Symptom.** `ConverseStream` on `global.anthropic.claude-sonnet-5` returns `AccessDeniedException` with
"not available for this account", on an account where other Anthropic models stream fine.
**Cause.** Newer model families are entitled separately. On the account this repo was built against,
`sonnet-5`, `opus-5`, `fable-5-1` and `opus-4-8` were all refused with
`agreementAvailability: NOT_AVAILABLE`, while `sonnet-4-6`, `sonnet-4-5`, `haiku-4-5` and `opus-4-5`
worked. This is why the default model here is Sonnet 4.6 and not Sonnet 5.
**Fix.** Read `agreementAvailability` before spending a turn, report the model id and the console path in
the failure, and keep the model in an env var so switching costs nothing.
**Where in this repo.** `platform.DEFAULT_MODEL`; `doctor.py:_model_availability` and `_availability_fix`;
`preflight._model_fix` recognises the "not available for this account" text and names an available model.

### P30. The Anthropic use-case form, and the wrong-profile trap that hides it

**Symptom.** Every Anthropic model returns `ResourceNotFoundException: Model use case details have not
been submitted for this account`, in an account where the Bedrock console shows the models as enabled.
**Cause.** Anthropic models on Bedrock need a one-time use-case form per account. `bedrock:GetUseCaseForModelAccess`
returns `{formData: ...}` when it has been submitted and `ResourceNotFoundException` when it has not.
The trap around it is worse: with no `AWS_PROFILE` exported, every call goes to your *default* profile's
account, which is usually not the one you are deploying into. Half a day was spent here debugging a model
entitlement that was fine all along.
**Fix.** Check the form explicitly, and print the account id and caller ARN *first*, before any other AWS
check, so a wrong profile is the first thing you see and not the last thing you deduce.
**Where in this repo.** `doctor.py:_credentials_check` runs first and short-circuits the rest on failure,
reporting `account=... arn=... profile=...`; `doctor.py:_use_case_form_check` FAILs with the console path.
Pinned by `tests/test_doctor.py::test_use_case_form_runs_right_after_credentials` and
`::test_credentials_pass_reports_account_arn_and_profile`.

---

## State, memory, and sessions

### P2. The harness writes to the current directory, and the container will not let it

**Symptom.** `PermissionError` or `Read-only file system` on the first turn, writing `./.agent/sessions/...`.
**Cause.** Harness defaults persist sessions, memory, and fetched skills under the cwd. In the image `/app`
is root-owned and the process runs as an unprivileged user, and the microVM filesystem is ephemeral anyway,
so even a successful write would be pointless.
**Fix.** In the cloud profile, `chdir` into a writable tempdir and put durable state in AgentCore Memory.
Assert the tempdir is writable at startup instead of finding out mid-turn.
**Where in this repo.** `platform.detect` sets `workspace` to `$TMPDIR/ops-workspace` in cloud;
`platform.enter_workspace` creates it, copies `AGENTS.md` into it (the harness `environment` plugin reads
that from cwd), and chdirs; `preflight.run_preflight` has the `tempdir` check. See P32 for the image side.

### P3. Two memory writers and two memory readers

**Symptom.** Duplicate or contradictory memories, unexplained Haiku spend, and the agent "remembering"
things nobody asked it to.
**Cause.** Four independent mechanisms are on by default: the harness's own extractor, the harness's own
injection, the AgentCore session manager's `retrieval_config`, and AgentCore's server-side strategies.
**Fix.** Pick one writer and one reader. Here: AgentCore strategies write, the harness stores read.
**Where in this repo.** `platform.session_option` passes `retrieval_config=None` (so the session manager
does not also inject); `platform.memory_option` builds stores with `extraction=False` (so no Haiku
extractor is constructed); `agentcore/agentcore.json` declares `USER_PREFERENCE -> /preferences/{actorId}/`
and `SEMANTIC -> /facts/{actorId}/`. Pinned by `tests/test_agent.py::test_cloud_session_and_memory_options`.
See also P48.

### P14. Memory writes are fire-and-forget on the sync path, and the sync manager blocks the stream

**Symptom.** Events written during a turn never appear. Or the SSE stream stalls in bursts while the
session manager flushes.
**Cause.** The sync path drops in-flight writes when the turn ends, and doing the writes inline blocks the
event loop that is producing the stream.
**Fix.** `async_mode=True` and `stream_async` only, plus an explicit `close()` on shutdown so the last
events are flushed.
**Where in this repo.** `platform.session_option` (`async_mode=True`); `http_app._turn` and
`a2a_app._text_chunks` both use `agent.stream_async`; `agent_cache._close` calls `session_manager.close()`
off the event loop, and `agent_cache.AgentCache.drain_closes` waits for those at shutdown. See P43 for the
consequence.

### P23. Offloaded tool results point at a tempdir that no longer exists

**Symptom.** After a session resumes in a new microVM, the agent references a document it cannot open.
**Cause.** When a `SessionManager` instance is passed, large tool results are offloaded to
`tempfile.mkdtemp`. The restored history keeps the reference; the files are gone.
**Fix.** `filter_restored_tool_context=True`, and treat offloaded artifacts as per-microVM by design.
**Where in this repo.** `platform.session_option`.

### P27. Strategy extraction is asynchronous, so a two-session demo flakes

**Symptom.** Session B does not recall what session A said, when you run the demo fast.
**Cause.** AgentCore strategies extract long-term records in the background. Minutes, not milliseconds.
**Fix.** Poll `retrieve_memory_records` on the target namespace before starting session B.
**Where in this repo.** `scripts/demo.sh` polls for up to 180 seconds and prints what it found before
continuing.

### P37. The LRU can close a session manager whose turn is still running

**Symptom.** A live stream dies mid-answer, and a second turn interleaves on the same session id.
**Cause.** A plain LRU evicts the oldest entry. The oldest entry can be mid-turn.
**Fix.** Evict only entries whose turn lock is free, never the entry just handed to a caller, and run over
size rather than evicting a live session. Do the eviction synchronously, with the blocking close as a
background task: an `await` between inserting an entry and returning it lets a concurrent miss evict the
brand new entry before its caller ever holds its lock.
**Where in this repo.** `agent_cache.AgentCache._evict` (the `protect=` argument and the `not lk.locked()`
filter). Pinned by `tests/test_http_app.py::test_cache_never_evicts_a_session_that_is_mid_turn` and
`::test_cache_does_not_evict_a_concurrent_caller_while_closing_a_victim`.

### P42. The AgentCore session manager does network I/O in its constructor

**Symptom.** A unit test against a fake memory id raises `ValidationException: Invalid memoryId`. In
production, the event loop stalls for the length of two AgentCore API calls on the first request of a
session.
**Cause.** `AgentCoreMemorySessionManager.__init__` calls `read_session`, which is `ListEvents`, and for a
new session `create_session`, which is `CreateEvent`.
**Fix.** Build the agent in a worker thread, never on the event loop, and fail closed with a readable error
if the build raises. In tests, patch `read_session` rather than the class, so the real constructor and the
real config object stay under assertion with zero API calls.
**Where in this repo.** `agent_cache.AgentCache.get_or_create` uses `asyncio.to_thread(factory)`;
`http_app._handle` and `a2a_app.HarnessExecutor._turn` both turn a build failure into an error frame or a
failed task. Pinned by `tests/test_http_app.py::test_agent_cache_builds_off_the_event_loop`.

### P43. `async_mode=True` makes the synchronous call illegal

**Symptom.** `agent("...")` raises `RuntimeError` from the hook registry.
**Cause.** The async session manager's hooks cannot run on the sync path. This is a hard constraint, not a
preference: choosing `async_mode=True` for P14 forecloses `agent(...)` entirely.
**Fix.** Use `stream_async` or `invoke_async` everywhere, including in the places you would normally take a
shortcut, like a closing turn after a budget trip.
**Where in this repo.** Every call site: `http_app._turn`, `a2a_app._text_chunks`, and both closing turns.

### P48. There is no field for a custom memory extraction prompt

**Symptom.** You design a careful SEMANTIC extraction prompt and find nowhere to put it.
**Cause.** `MemoryStrategy` in CLI 0.30.0 and `@aws/agentcore-cdk` 0.1.0-alpha.53 exposes exactly `type`,
`name`, `description`, `namespaceTemplates`, and `reflectionNamespaceTemplates`. No prompt field exists.
**Fix.** Deploy with the default strategy, then override the prompt out of band:
`bedrock-agentcore-control update-memory` with a `memoryStrategies.modifyMemoryStrategies[].configuration.extraction`
override, or the console. CDK does not manage that sub-field, so a redeploy should not revert it, though
that last part has not been verified against a live memory.
**Where in this repo.** `agentcore/agentcore.json` `memories[0].strategies` carries the default SEMANTIC
strategy. The narrow extraction scope the design called for is enforced at the other end instead: tool
output is capped at 40k characters in `tools/aws_call.py:_cap` so oversized payloads never enter the event
stream, and `scripts/memory_wipe.py` is the escape hatch.

---

## Tools and the agent loop

### P4. There is a second model you did not choose

**Symptom.** Haiku charges on a bill for an agent configured to use Sonnet.
**Cause.** The harness uses Haiku 4.5 for `web_fetch` summarisation and for memory extraction.
**Fix.** In cloud, drop `web_fetch` and turn extraction off, and there is no second model. Locally the
harness defaults stand, so `doctor` still checks Haiku access.
**Where in this repo.** `platform.builtin_tools` pins `web_fetch` off in cloud; `platform.memory_option`
passes `extraction=False`; `doctor.py:DEFAULT_SMALL` probes Haiku 4.5 as `model_small`.

### P10. A policy *string* in `interventions` becomes an LLM classifier that pauses the turn

**Symptom.** A turn stops and never resumes. The session is then wedged: the pending interrupt persists
with the session, so resuming hits it again.
**Cause.** `interventions="never delete anything"` is not a rule, it is a prompt. The harness builds a
classifier from it that returns `Confirm`, which raises an interrupt and waits for a human. In a headless
runtime nobody is there.
**Fix.** Write a deterministic `InterventionHandler` that returns `Deny` or `Proceed` and never `Confirm`.
**Where in this repo.** `guard.ReadOnlyGuard.before_tool_call` returns only `Deny` or `Proceed`. Pinned by
`tests/test_guard.py::test_never_confirms`, which asserts no code path can produce a `Confirm`.

### P11. A `boto_client_config` on `BedrockModel` silently drops the 120 second read timeout

**Symptom.** Long tool-heavy turns die on a read timeout that used to be fine.
**Cause.** Passing your own `Config` replaces the harness's, including the read timeout it set for you.
**Fix.** Do not construct `BedrockModel` at all. Pass the model as a `provider/name` string and let the
harness build the client, which also keeps its effort mapping, thinking budget, and cache points.
**Where in this repo.** `agent.build_agent` passes `model=rt.model`, a string, and there is no
`boto_client_config` anywhere in the repo. That is the fix: the escape hatch was designed and then not
built, because nothing needed it.

### P12. `Limits` silently ignores keys it does not know

**Symptom.** You set `Limits(max_tool_calls=10)` and the agent makes fifty.
**Cause.** Only `turns`, `output_tokens`, and `total_tokens` exist. Anything else is accepted and dropped.
**Fix.** Use the typed constructor, and pin the key set with a test that compares against the dataclass
annotations so an upstream rename fails loudly.
**Where in this repo.** `agent.LIMITS` and `agent.READER_LIMITS`. Pinned by
`tests/test_agent.py::test_limits_keys_are_valid`.

### P13. One stalled MCP tool call hangs the whole agent

**Symptom.** The turn never ends, and no error is reported.
**Cause.** An MCP server that accepts the call and never answers has no client-side deadline.
**Fix.** Make MCP opt-in, use a streamable-HTTP endpoint rather than spawning `uvx` at cold start, and set
`continue_on_error` so a dead server degrades instead of blocking.
**Where in this repo.** `platform.detect` builds `mcp_servers` only when `ENABLE_AWS_DOCS_MCP=1`, with
`{"transport": "streamable-http", "continue_on_error": True}`. Pinned by
`tests/test_platform.py::test_mcp_servers_opt_in`. The runtime's own wall clock is the backstop.

### P19. `web_search` is not available on Bedrock, and naming it raises

**Symptom.** `create_harness(...)` raises at construction because you listed a tool that does not exist for
this provider. Or, worse, the Exa fallback engages and your queries go to a third party.
**Cause.** `web_search` is provider-specific and Bedrock does not offer it, so `create_harness` raises when
it is named, and the harness's Exa fallback is a third-party egress path.
**Fix.** Pin it off explicitly rather than leaving it to the default.
**Where in this repo.** `platform.builtin_tools` sets `"web_search": False` in local and `"*": False` in
cloud. Pinned by `tests/test_agent.py::test_local_builtin_tools_drop_subagent_web_search_write_edit`.

### P21. A `builtin_tools` **mapping** edits the defaults; it does not replace them

**Symptom.** You pass `builtin_tools={"read": True}` expecting one tool, and the agent still has `shell`,
`write`, `edit`, and `web_fetch`.
**Cause.** A mapping is applied as a patch over the default set. Only a list, or a mapping starting with
`{"*": False}`, pins the set.
**Fix.** Start with `{"*": False}` in any environment where the tool surface is a security boundary.
**Where in this repo.** `platform.builtin_tools` returns `{"*": False, "read": True,
"programmatic_tool_caller": {...}}` in cloud, and a patch (`subagent`, `web_search`, `write`, `edit` off)
in local, where the guard still vets what remains. Pinned by
`tests/test_agent.py::test_cloud_builtin_tools_pinned`.

### P33. `Limits` are per invocation, and the subagent tool passes none

**Symptom.** The parent agent respects its budget, and a delegate it spawns runs away.
**Cause.** `Limits` is a `stream_async(...)` argument, not agent state. The harness's subagent tool calls
`child.stream_async(prompt, invocation_state=..., cancel_signal=...)` with no `limits`, and the `Agent`
constructor has no `limits` keyword to set a default with.
**Fix.** Wrap the child's `stream_async` so an uncapped call gets a floor, while a caller that passes its
own limits keeps them.
**Where in this repo.** `agent._cap_stream` (a `functools.wraps` wrapper doing `kw.setdefault("limits",
limits)`), applied in `agent.build_reader`. Pinned by `tests/test_agent.py::test_reader_stream_async_is_capped`
and `::test_reader_caller_supplied_limits_win`.

### P34. `InterventionHandler.name` is abstract

**Symptom.** `TypeError: Can't instantiate abstract class ReadOnlyGuard` on a class that implements
`before_tool_call` and nothing else.
**Cause.** `name` is an abstract member of the base class, and subclassing does not give you one.
**Fix.** A plain class attribute is enough.
**Where in this repo.** `guard.ReadOnlyGuard` declares `name = "read_only_guard"`.

### P50. Re-exporting a `@tool` in `__init__.py` shadows the module it came from

**Symptom.** `from ops_investigator.tools import aws_call` gives you the tool object, and
`import ops_investigator.tools.aws_call` in a test gives you the same tool object, so patching a function
inside the module does nothing.
**Cause.** `from .aws_call import aws_call` in `__init__.py` binds the name `aws_call` in the package
namespace to the function, shadowing the submodule of the same name. Separately, `@tool` wraps your
function, so the original is reachable as `._tool_func` and not as the decorated name.
**Fix.** Keep the convenient re-export, and have tests reach the module through
`importlib.import_module("ops_investigator.tools.aws_call")`.
**Where in this repo.** `app/ops_investigator/ops_investigator/tools/__init__.py`; the tests in
`tests/test_aws_call.py` import the submodule explicitly.

---

## Runtime, streaming, and protocols

### P9. `runtimeSessionId` must be at least 33 characters

**Symptom.** A 400 from the service before your container is even reached, on a session id that looks fine.
**Cause.** AgentCore enforces a 33 character floor. A `uuid4().hex` is 32.
**Fix.** Generate ids that clear the floor, and reject short ones locally with the same message the service
would give, so dev and cloud behave alike.
**Where in this repo.** `scripts/invoke.py:new_session_id` returns 40 characters;
`runtime_common.MIN_SESSION_LEN = 33`; `http_app._handle` and `a2a_app.HarnessExecutor.execute` both reject
short ids (the A2A context id *is* the session id). Pinned by
`tests/test_scripts.py::test_new_session_id_clears_agentcores_floor`.

### P22. Strands' `A2AServer` speaks A2A 0.3, and the spec is at 1.0

**Symptom.** Your card validates against nothing, or a 1.0 client cannot talk to your 0.3 server.
**Cause.** `strands-agents` pins `a2a-sdk` 0.3.x, whose card has a top-level `url` and `protocolVersion`
and whose types carry `kind` discriminators. A2A 1.0 moved the transport and version onto each entry of
`supportedInterfaces` and renamed `supportsAuthenticatedExtendedCard` to `capabilities.extendedAgentCard`.
The AWS A2A guide still shows 0.3 shapes.
**Fix.** Run the 1.x SDK with your own executor and build the card by hand. Make the client read the
version off the card and say so rather than guess.
**Where in this repo.** `agent_card.build_agent_card`; `a2a_app.HarnessExecutor`;
`scripts/a2a_client.py:card_protocol_version`. Pinned by `tests/test_agent_card.py::test_card_is_spec_1_0_shaped`
and `::test_card_has_no_0_3_fields`. See P40 for the wire details.

### P24. `RequestContext` has no `user_id`, so the CLI template's actor is always the default

**Symptom.** Every caller shares one memory namespace, and nobody notices until two people use the demo.
**Cause.** The generated template reads `getattr(context, "user_id", "default")`. `RequestContext` has
`session_id`, `request_headers`, and `request`. There is no `user_id` and there never was, so the default
always wins.
**Fix.** Take the actor from the JWT `sub` in the allowlisted `Authorization` header. The AgentCore
authorizer already validated signature and audience before the container saw it, so an unverified decode is
correct here and a second verification would only add a JWKS dependency.
**Where in this repo.** `auth.actor_from_headers` and `auth._jwt_claims`; `requestHeaderAllowlist:
["Authorization"]` in `agentcore/agentcore.json`; the actor is validated against
`^[A-Za-z0-9_-]{1,64}$` because memory namespaces are built from it. A2A reads the same header out of
`call_context.state["headers"]` (`a2a_app._headers`).

### P35. Real `tool_use_stream` events carry a partial JSON string, and `ToolResult` has no name

**Symptom.** Your SSE mapper emits a dozen `tool_use` frames for one tool call, each with garbage input,
and every `tool_result` frame has an empty name.
**Cause.** `current_tool_use.input` arrives as a *growing JSON string*, one event per delta, not as a dict.
And a `ToolResult` carries `toolUseId`, `status`, and `content`, but no tool name.
**Fix.** Make the mapper stateful for the length of a turn: keep a `toolUseId -> name` map so results can
be named, and emit exactly one `tool_use` frame per call, the first time its input parses as a complete
JSON object.
**Where in this repo.** `sse.EventMapper` (one instance per turn, created in `http_app._turn`). Pinned by
`tests/test_sse.py::test_streaming_tool_use_input_string_and_tool_result_name_resolution` and
`tests/test_http_app.py::test_invoke_uses_one_event_mapper_per_turn`. The name map is the fallback rather
than the usual path now: results reach the mapper from the after-tool hook (P51), which knows the name.

### P36. A timeout that spans a `yield` fires at the consumer, not at you

**Symptom.** The wall clock trips and the caller gets no timeout frame, the turn lock stays held forever,
and the log shows `RuntimeError` from a `GeneratorExit`. Reproduced, not theorised.
**Cause.** `async with asyncio.timeout(...)` around a loop that yields measures real time correctly, but if
it fires while the generator is suspended at a `yield`, the cancellation is delivered to whoever is
consuming the generator. Your `finally` block never runs, and in an ASGI server the exception surfaces
somewhere unhelpful.
**Fix.** Never let a timeout scope cross a `yield`. Compute a deadline once, and apply
`asyncio.wait_for(stream.__anext__(), remaining)` to each individual pull. As a bonus this works on Python
3.10, where `asyncio.timeout` does not exist and `asyncio.TimeoutError` is not the builtin `TimeoutError`.
**Where in this repo.** `http_app._handle` (the `deadline`/`remaining` loop) and `a2a_app._collect`. Both
re-raise a timeout that fired before the deadline, so a tool's own timeout is reported as itself. Pinned by
`tests/test_http_app.py::test_invoke_wall_clock_with_a_slow_consumer` and
`tests/test_a2a_app.py::test_the_timeout_does_not_swallow_someone_elses_timeout`.

### P40. A2A 1.0 on `a2a-sdk` 1.x: protobuf types, `SendMessage`, and a required version header

**Symptom.** A client that worked against a 0.3 server gets `-32601 Method not found`, or
`-32009 A2A version '0.3' is not supported by this handler`.
**Cause.** Three separate changes at once. The JSON-RPC method names are the gRPC ones (`SendMessage`,
`SendStreamingMessage`, `GetTask`, `CancelTask`); `message/send` is 0.3 and is only routed with
`enable_v0_3_compat=True`. Every handler entry point is decorated with `@validate_version`, and a missing
`A2A-Version` header is read as `0.3`. And the types are protobuf messages, so a map read does not create
the key: `requirement.schemes["cognito"]` alone produces `securityRequirements: [{}]`, and you need
`.SetInParent()`.
**Fix.** Speak 1.0 end to end, leave compat off, let the SDK client set the header, and build protobuf maps
with `SetInParent`.
**Where in this repo.** `a2a_app.build_app` calls `create_jsonrpc_routes(handler, "/")` with compat off;
`agent_card.build_agent_card` uses `SetInParent()`; `scripts/a2a_client.py` uses the SDK client, which
sends the header. Pinned by `tests/test_a2a_app.py::test_a_0_3_client_is_answered_with_an_error_not_a_turn`.

### P41. Starlette 1.6 removed `on_shutdown`

**Symptom.** Your shutdown hook never runs, or `@app.route` raises a deprecation that becomes an error.
**Cause.** Starlette 1.6 dropped `on_shutdown` in favour of the lifespan context manager, and deprecated
the `@app.route` decorator in favour of an explicit routes list.
**Fix.** Put teardown after the `yield` in an `asynccontextmanager` lifespan, and register routes as
`Route(...)` objects.
**Where in this repo.** `a2a_app.build_app` (the `lifespan` function that awaits `handler.aclose()` and
`CACHE.drain_closes()`, and the `Route("/ping", ...)` entry). Pinned by
`tests/test_a2a_app.py::test_shutdown_closes_the_request_handler`.

### P45. SigV4 deletes any `Authorization` header before it signs

**Symptom.** You add a bearer token to a boto3 client and the runtime still rejects you as unauthenticated.
**Cause.** `botocore/auth.py`, `_modify_request_before_signing`, removes any existing `Authorization`
header so it can write its own. A bearer token and SigV4 cannot coexist on one request.
**Fix.** Build the client with `signature_version=UNSIGNED` when a token is in play, and add the header on
the `before-sign` event, which is emitted even for unsigned clients and is the last hook before the request
goes out. Or just use plain HTTPS against the same data-plane URL, which is what a `CUSTOM_JWT` runtime
expects anyway.
**Where in this repo.** `scripts/invoke.py:bearer_client` and `:boto3_config` do the first;
`scripts/invoke.py:invocations_url` plus `--transport https` does the second. Pinned by
`tests/test_scripts.py::test_the_bearer_token_survives_on_the_boto3_transport` and
`::test_boto3_is_unsigned_only_when_a_bearer_token_is_in_play`.

### P46. The A2A card lives on the path, not after the query string

**Symptom.** Fetching the card from a deployed A2A runtime 404s, or returns the invoke endpoint's error.
**Cause.** A deployed runtime is addressed as `.../runtimes/<arn>/invocations?qualifier=DEFAULT`. Appending
`/.well-known/agent-card.json` to that string puts the well-known path after the query. The service's own
`GetAgentCard` request URI is `/runtimes/{arn}/invocations/.well-known/agent-card.json`.
**Fix.** Split the URL, extend the *path*, keep the query. Then build the client from the card you already
fetched so the SDK does not resolve it a second time and guess differently.
**Where in this repo.** `scripts/a2a_client.py:card_url_for`. Pinned by
`tests/test_scripts.py::test_card_url_puts_the_well_known_path_on_the_path_not_the_query`.

### P51. `stream_async` never yields a tool result, so build the frame from `AfterToolCallEvent`

**Symptom.** Your SSE stream shows `tool_use` and then text, and the `tool_result` frame your mapper
handles never appears. The mapper branch for it is dead code and nothing tells you.
**Cause.** In `strands-agents` 1.56 `ToolResultEvent.is_callback_event` is `False`, so the agent loop
consumes it and never hands it to a `stream_async` consumer. A real tool-calling turn yields
`init_event_loop`, `start`, `start_event_loop`, raw `event` chunks, `tool_use_stream`, `message`, `data`
and `result`, plus `tool_cancel_event` when an intervention denies a call, but never a tool result.
`tool_cancel_event` *is* a callback event (P35's sibling, the `denied` frame), which makes the absence of
the result easy to miss.
**Fix.** Take tool results from the hook system instead: a `HookProvider` that subscribes to
`AfterToolCallEvent` fires for every finished call, successful or not, with both the `ToolUse` (so you get
the name) and the `ToolResult`. Buffer them and drain the buffer between stream events, so the frame still
lands in the order a reader expects.
**Where in this repo.** `tool_events.ToolResultRecorder`, passed to `create_harness(hooks=[...])` in
`agent.build_agent`/`build_reader` and attached to the returned agent; drained by `http_app._turn` and
(to bound its growth on a cached agent, since A2A publishes one text artifact) by `a2a_app._text_chunks`.
Pinned by `tests/test_tool_events.py` and
`tests/test_http_app.py::test_invoke_emits_a_tool_result_frame_from_the_after_tool_hook`. One consequence
worth knowing before you assume every result has a `tool_use` frame ahead of it: on the cloud profile
`programmatic_tool_caller` runs its inner calls through `ToolExecutor._stream` itself and consumes those
events, so the hook fires for calls whose `tool_use_stream` events never reached the runtime, and they
reach the wire as `tool_result` frames with no preceding `tool_use` frame. A denial in there is reported
the same way: `AfterToolCallEvent` carries `cancel_message`, so the recorder replays the cancel ahead of
the result and the caller still gets `denied` then `tool_result` with `status=error`. For a call the model
made directly the SDK streams its own `tool_cancel_event` as well, and `EventMapper` dedupes the two by
`toolUseId`, so that path still produces exactly one `denied` frame.

---

## Security and IAM

### P15. A guardrail ID without a version silently applies no guardrail

**Symptom.** You configure a Bedrock guardrail, and nothing is ever filtered.
**Cause.** The guardrail identifier needs a version alongside it. Without one the request carries none, and
no error says so.
**Fix.** Always pass the version. Not used in this repo, documented so that adding one later does not
repeat the trap.
**Where in this repo.** Nowhere. Deliberately: the read-only tool surface is the control here.

### P26. Managed `ReadOnlyAccess` reads secrets

**Symptom.** Your "read-only" agent can fetch an SSM SecureString, an S3 object body, a DynamoDB row, a
Lambda's environment variables, EC2 user data, a CloudFormation template, and its own conversation logs.
**Cause.** `ReadOnlyAccess` grants `Get*`, `List*`, and `Describe*` across services, and plenty of those
return secret material. "Read-only" means "does not mutate", not "cannot see anything sensitive".
**Fix.** Attach an explicit Deny alongside it, and mirror the same table at the tool layer so the agent is
refused before the call is made and again if it somehow gets through.
**Where in this repo.** `policies/execution-role-deny.json` (statement `NoSecretMaterial`, plus `NoApiKeys`
and `NoReadingOwnTranscripts`); `tools/aws_call.py:DENY` and `:is_read_operation`;
`guard.ReadOnlyGuard.before_tool_call` calls the same predicate. Pinned by
`tests/test_agentcore_json.py::test_iam_deny_covers_tool_deny_table`, which maps the tool's
`execute_statement`/`batch_execute_statement` onto the IAM action `dynamodb:PartiQLSelect`.

### P31. A blanket `kms:Decrypt` deny stops the image pull, and the runtime never starts

**Symptom.** The runtime never reaches READY. Nothing in your application logs, because the application
never ran.
**Cause.** The image pull runs as the execution role, and a CMK-encrypted ECR repository needs
`kms:Decrypt` on the pull path. An explicit Deny beats the CDK's grant, so the platform cannot start your
container. This one was found by a real deploy, not by reading.
**Fix.** Scope the deny away from the service path: deny `kms:Decrypt` only when it is *not* being called
via ECR.
**Where in this repo.** `policies/execution-role-deny.json`, statement `NoDirectKmsDecrypt`, with
`Condition: {StringNotLike: {"kms:ViaService": ["ecr.*.amazonaws.com"]}}`. Pinned by
`tests/test_agentcore_json.py::test_kms_decrypt_deny_is_scoped_away_from_the_image_pull_path`.

### P32. Chowning `/app` hands the agent its own source code

**Symptom.** Nothing visible. That is the problem. An agent with a `write` tool and a writable `/app` can
rewrite its own code and dependencies, and the next process start runs whatever it wrote.
**Cause.** The obvious fix for P2 is `chown -R app_user /app`, and it quietly removes the only thing
stopping self-modification.
**Fix.** Leave `/app` and `.venv` root-owned. Pre-create and chown exactly the one directory the local
profile writes to. The cloud profile never touches it, because there the workspace is a tempdir.
**Where in this repo.** `app/ops_investigator/Dockerfile`:
`RUN mkdir -p /app/.agent && chown bedrock_agentcore:bedrock_agentcore /app/.agent`, then `USER
bedrock_agentcore`. The cloud profile's `write` and `edit` tools are off anyway (P21), which is the second
layer.

---

## Observability and operations

### P8. Transaction Search, ADOT, and the harness's own exporter

**Symptom.** Traces do not appear in CloudWatch GenAI Observability at all. Or they appear twice. Or the
spans are there but the GenAI views are empty.
**Cause.** Three separate requirements. CloudWatch Transaction Search must be enabled once per region.
ADOT must wrap the process. And if `OTEL_TRACES_EXPORTER` is set, the harness's telemetry module builds its
own exporter and competes with ADOT for ownership.
**Fix.** Enable Transaction Search, run under `opentelemetry-instrument`, and leave `OTEL_TRACES_EXPORTER`
**unset** in the container.
**Where in this repo.** `doctor.py:_transaction_search_check` calls `xray:GetTraceSegmentDestination` and
FAILs with the enable command; `app/ops_investigator/Dockerfile` has
`CMD ["opentelemetry-instrument", "python", "-m", "main"]`; `preflight.run_preflight` WARNs if
`OTEL_TRACES_EXPORTER` is set; `agentcore/agentcore.json` never sets it.

### P38. Naming a script after a stdlib module breaks every script beside it

**Symptom.** Every script in a directory dies on import, including ones that import nothing unusual:

```
ImportError: cannot import name 'EXACT_TOKEN_TYPES' from 'token' (.../scripts/token.py)
```

`uv run python scripts/auth_bootstrap.py --help` could not print its help text.
**Cause.** Running a script puts its own directory first on `sys.path`. `scripts/token.py` shadowed the
stdlib `token` module, which `tokenize` imports, which `linecache` imports, which every traceback needs.
**Fix.** Rename it, and add a test that checks no script name collides with `sys.stdlib_module_names`.
**Where in this repo.** `scripts/get_token.py` (the docstring explains why it is not `token.py`); pinned by
`tests/test_scripts.py::test_no_script_shadows_a_stdlib_module`. `make token` is still the target name.

### P49. `BedrockAgentCoreApp` installs no logging handler

**Symptom.** A completely empty container log. Your preflight lines, your per-turn lines, all of it gone,
while `WARNING` and `ERROR` escape only through `logging.lastResort` on stderr.
**Cause.** Neither `BedrockAgentCoreApp` nor uvicorn configures logging for your package, and `startup()`
runs before `app.run()`, so there is no window in which something else does it for you.
**Fix.** Call `logging.basicConfig` yourself at startup, at `WARNING`, and raise only your own package to
`INFO` so the SDKs stay quiet.
**Where in this repo.** `runtime_common.configure_logging`, called from `http_app.startup` and
`a2a_app.main`. Pinned by `tests/test_runtime_common.py::test_configure_logging_sends_this_package_to_info`.
