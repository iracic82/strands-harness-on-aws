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

.PHONY: setup doctor auth-bootstrap token dev invoke test lint plan deploy status logs a2a-card memory-wipe smoke destroy

setup:
	@echo "make setup is not implemented yet (Task 17): it will detect your account and region and write" >&2
	@echo "agentcore/aws-targets.json and .env for you. Until then, copy agentcore/aws-targets.example.json" >&2
	@echo "to agentcore/aws-targets.json and put your own account id in it." >&2
	@exit 1

doctor:
	uv run python doctor.py --region $(REGION)

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

lint:
	uv run ruff check . && uv run ruff format --check .

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
