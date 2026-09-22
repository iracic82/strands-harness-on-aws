# An exported AWS_REGION is honoured, so `make dev` never silently moves you to another region.
REGION ?= $(or $(AWS_REGION),us-west-2)
PROJECT ?= harnessaws
RUNTIME ?= OpsInvestigator
PROMPT ?= Which AWS account and region are you looking at? Use aws_call sts get_caller_identity.
TRANSPORT ?= https
# Both variables, always: botocore resolves the region from AWS_DEFAULT_REGION and the profile config
# only. A boto3 client built without an explicit region_name (the harness's own Bedrock client) does
# not see AWS_REGION at all, and would quietly use whatever region ~/.aws/config gives the profile.
REGION_ENV = AWS_REGION=$(REGION) AWS_DEFAULT_REGION=$(REGION)
# Always npx, always from the repo root: a deprecated Python `agentcore` binary shadows the npm CLI on
# some machines, and the CLI finds the project by walking into ./agentcore on its own.
AGENTCORE = npx -y @aws/agentcore@0.30.0

.PHONY: setup doctor auth-bootstrap token dev invoke test lint hygiene plan deploy status logs a2a-card memory-wipe smoke destroy

# The first command in a fresh clone: asks STS who you are and writes the two git-ignored files that
# carry your account id (agentcore/aws-targets.json and .env). Idempotent; --force overwrites edits.
setup:
	uv run python scripts/setup.py --region $(REGION)

# Only a FAIL should stop the quickstart chain, and make collapses any non-zero recipe status to its own
# exit 2. `--exit-on fail` does that inside doctor.py rather than with `|| [ $$? -eq 1 ]` here, which
# would also swallow an exit 1 doctor never produced: a uv resolution error or a traceback would read as
# a clean preflight. scripts/smoke.sh calls doctor.py directly and tests the status code itself.
doctor:
	uv run python doctor.py --region $(REGION) --exit-on fail

auth-bootstrap:
	uv run python scripts/auth_bootstrap.py --region $(REGION) --project $(PROJECT)

token:
	@uv run python scripts/get_token.py --region $(REGION) --client-id "$(CLIENT_ID)"

# The app chdirs into its own directory so AGENTS.md and skills/ are found; run it from the repo root.
# `$(AGENTCORE) dev --runtime $(RUNTIME)` is the alternative: it builds and runs the real container, but
# needs the CLI's project context and a deployed memory to attach to.
dev:
	$(REGION_ENV) uv run python app/ops_investigator/main.py

# make invoke PROMPT="..."                      -> the local dev server
# make invoke ARN=... TOKEN=... PROMPT="..."    -> the deployed runtime over HTTPS with a Cognito bearer
invoke:
	$(REGION_ENV) uv run python scripts/invoke.py --region $(REGION) --prompt "$(PROMPT)" \
		$(if $(ARN),--arn "$(ARN)" --transport $(TRANSPORT),--dev) $(if $(TOKEN),--token "$(TOKEN)",)

test:
	uv run pytest

# verify_docs.py comes last because it is the slow one: it runs every bash block in the docs that is not
# tagged manual, in a throwaway copy of the checkout, and renders every Mermaid diagram in a browser.
lint: hygiene
	uv run ruff check . && uv run ruff format --check .
	uv run python scripts/verify_docs.py

# The public-repo gate: no account id, ARN, Cognito id, /Users path, or the owner's own profile name in
# anything git would publish, and every file that would carry one is git-ignored.
#
# gitleaks runs twice on purpose. The default scan walks commits, so it sees history but not a file you
# have not committed yet; `--no-git` walks the working tree, so it sees the file you are about to add
# but no history at all. Neither alone covers the moment a secret is written down.
#
# Written as an `if` and not `cmd && gitleaks || echo`, because in that form a real leak exits non-zero,
# falls into the `||`, prints the "not installed" message, and the target passes. The two scans are
# joined with `&&` for the same reason: `a; b` in one recipe line reports only b's exit status.
hygiene:
	uv run pytest tests/test_repo_hygiene.py -q
	@if command -v gitleaks >/dev/null; then \
		gitleaks detect --no-banner --source . && \
		gitleaks detect --no-git --no-banner --source .; \
	else \
		echo "gitleaks not installed (brew install gitleaks); the secrets CI job runs it"; \
	fi

plan:
	$(AGENTCORE) deploy --dry-run

deploy:
	$(AGENTCORE) deploy -y

status:
	$(AGENTCORE) status --json

logs:
	$(AGENTCORE) logs --runtime $(RUNTIME)

a2a-card:
	$(REGION_ENV) uv run python scripts/a2a_client.py --url "$(A2A_URL)" $(if $(TOKEN),--token "$(TOKEN)",) \
		$(if $(A2A_PROMPT),--prompt "$(A2A_PROMPT)",)

memory-wipe:
	$(REGION_ENV) uv run python scripts/memory_wipe.py --region $(REGION) --memory-id "$(MEMORY_ID)" --actor "$(ACTOR)"

smoke:
	$(REGION_ENV) REGION=$(REGION) bash scripts/smoke.sh

destroy:
	$(AGENTCORE) remove all -y && $(AGENTCORE) deploy -y
