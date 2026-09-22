"""The deployment config and the runtime deny table are one policy expressed twice. Keep them in sync."""

from __future__ import annotations

import json
from pathlib import Path

from ops_investigator.tools.aws_call import DENY

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app" / "ops_investigator"
DENY_POLICY = APP / "policies" / "execution-role-deny.json"

# boto3 operation -> IAM action, where the IAM action name is not the CamelCase of the operation.
# PartiQL reads against DynamoDB are authorized as dynamodb:PartiQLSelect; there is no
# dynamodb:ExecuteStatement or dynamodb:BatchExecuteStatement IAM action.
IAM_ACTION_ALIASES: dict[tuple[str, str], str] = {
    ("dynamodb", "execute_statement"): "dynamodb:PartiQLSelect",
    ("dynamodb", "batch_execute_statement"): "dynamodb:PartiQLSelect",
}

# Services the IAM deny covers by resource ARN rather than by a blanket action, so a bare
# service:Action lookup cannot find them: logs reads are denied only on the agent's own transcript
# log groups, and apigateway reads are denied only on /apikeys*.
RESOURCE_SCOPED_SERVICES = ("logs", "apigateway")


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _statement_actions(statement: dict) -> set[str]:
    raw = statement["Action"]
    return {a.lower() for a in (raw if isinstance(raw, list) else [raw])}


def _iam_actions(doc: dict) -> set[str]:
    return {action for statement in doc["Statement"] for action in _statement_actions(statement)}


def _covered(actions: set[str], service: str, operation: str) -> bool:
    action = IAM_ACTION_ALIASES.get((service, operation))
    if action is None:
        action = f"{service}:" + "".join(part.capitalize() for part in operation.split("_"))
    action = action.lower()
    # A bare "*" would match every operation and make this whole test vacuous, so it never counts
    # as coverage. Only a prefixed wildcard such as "s3:GetObject*" does.
    return any(a.strip("*") != "" and (a == action or (a.endswith("*") and action.startswith(a[:-1]))) for a in actions)


def test_agentcore_json_shape():
    cfg = _load(ROOT / "agentcore" / "agentcore.json")
    names = {r["name"]: r for r in cfg["runtimes"]}
    assert names["OpsInvestigator"]["protocol"] == "HTTP"
    assert names["OpsInvestigatorA2A"]["protocol"] == "A2A"
    assert names["OpsInvestigator"]["dockerfile"] == "Dockerfile"
    assert names["OpsInvestigatorA2A"]["dockerfile"] == "Dockerfile.a2a"
    assert names["OpsInvestigator"]["entrypoint"] == "main.py"
    assert names["OpsInvestigatorA2A"]["entrypoint"] == "a2a_main.py"
    for runtime in cfg["runtimes"]:
        assert runtime["runtimeVersion"] == "PYTHON_3_12"
        assert runtime["build"] == "Container"
        assert runtime["codeLocation"] == "app/ops_investigator/"
        assert "policies/execution-role-deny.json" in runtime["additionalPolicies"]
        assert "arn:aws:iam::aws:policy/ReadOnlyAccess" in runtime["additionalPolicies"]
        assert runtime["requestHeaderAllowlist"] == ["Authorization"]
        assert runtime["authorizerType"] == "CUSTOM_JWT"
    memory = cfg["memories"][0]
    assert memory["name"] == "OpsInvestigatorMemory"
    assert memory["eventExpiryDuration"] == 30
    assert {s["type"]: s["namespaceTemplates"] for s in memory["strategies"]} == {
        "USER_PREFERENCE": ["/preferences/{actorId}/"],
        "SEMANTIC": ["/facts/{actorId}/"],
    }


def test_runtimes_declare_the_model_and_budget_the_harness_reads():
    cfg = _load(ROOT / "agentcore" / "agentcore.json")
    for runtime in cfg["runtimes"]:
        env = {e["name"]: e["value"] for e in runtime["envVars"]}
        # platform.detect raises RuntimeConfigError in the cloud profile without a region, so every
        # runtime has to carry one. setup.py rewrites it per deployment target.
        assert env["AWS_REGION"] == "us-west-2", runtime["name"]
        # And AWS_DEFAULT_REGION beside it: botocore resolves the region from AWS_DEFAULT_REGION only,
        # so a client built without an explicit region_name (the harness's own Bedrock client) would
        # otherwise have no region at all inside the container.
        assert env["AWS_DEFAULT_REGION"] == env["AWS_REGION"], runtime["name"]
        assert env["HARNESS_MODEL"] == "bedrock/global.anthropic.claude-sonnet-4-6", runtime["name"]
        assert env["HARNESS_EFFORT"] == "medium", runtime["name"]
        assert env["HARNESS_MAX_SECONDS"] == "600", runtime["name"]
        assert env["OPS_LOG_GROUP_PREFIXES"] == "/aws/lambda/,/aws/ecs/,/aws/apigateway/,/aws/eks/", runtime["name"]


def test_jwt_authorizer_placeholders_are_valid_oidc_discovery_urls():
    # The CDK schema requires an https URL ending in the OIDC well-known path even before
    # auth_bootstrap fills in the real user pool, so the placeholder has to be URL-shaped.
    cfg = _load(ROOT / "agentcore" / "agentcore.json")
    for runtime in cfg["runtimes"]:
        jwt = runtime["authorizerConfiguration"]["customJwtAuthorizer"]
        assert jwt["discoveryUrl"].startswith("https://")
        assert jwt["discoveryUrl"].endswith("/.well-known/openid-configuration")
        assert "REPLACED_BY_auth_bootstrap" in jwt["discoveryUrl"]
        assert jwt["allowedClients"] == ["REPLACED_BY_auth_bootstrap"]


def test_additional_policy_files_resolve_against_code_location():
    # CDK reads non-ARN additionalPolicies entries off the local disk at synth time, relative to
    # codeLocation. A missing file fails the whole deploy, so assert the path really exists.
    cfg = _load(ROOT / "agentcore" / "agentcore.json")
    for runtime in cfg["runtimes"]:
        for entry in runtime["additionalPolicies"]:
            if entry.startswith("arn:"):
                continue
            assert (ROOT / runtime["codeLocation"] / entry).is_file(), entry


def test_dockerignore_excludes_build_only_inputs_but_keeps_runtime_files():
    ignored = {line.strip() for line in (APP / ".dockerignore").read_text().splitlines()}
    # policies/ is read by CDK off the local disk, never from the image; the rest is build or
    # local-run noise that has no business in a deployed container.
    assert {"policies/", "tests/", ".venv/", ".agent/"} <= ignored
    # AGENTS.md and skills/ are loaded from the working directory at runtime, so they must ship.
    assert not {"AGENTS.md", "skills/", "skills"} & ignored


def test_kms_decrypt_deny_is_scoped_away_from_the_image_pull_path():
    # Image pull runs as the execution role. A blanket Deny on kms:Decrypt beats the CDK's grant on
    # a CMK-encrypted ECR repository and the runtime can never start, so the deny must be
    # conditioned to exclude calls made via ECR.
    doc = _load(DENY_POLICY)
    holders = [s for s in doc["Statement"] if "kms:decrypt" in _statement_actions(s)]
    assert len(holders) == 1, holders
    statement = holders[0]
    assert statement["Condition"]["StringNotLike"]["kms:ViaService"] == ["ecr.*.amazonaws.com"]
    # The unconditioned bucket must not quietly regain it.
    unconditioned = [s for s in doc["Statement"] if "Condition" not in s]
    assert all("kms:decrypt" not in _statement_actions(s) for s in unconditioned)


def test_secret_material_deny_carries_the_credential_provider_caveat():
    # JSON has no comments, so the caveat rides on an annotation key that CDK's
    # PolicyDocument.fromJson drops before the policy reaches IAM.
    doc = _load(DENY_POLICY)
    by_sid = {s["Sid"]: s for s in doc["Statement"]}
    assert "bedrock-agentcore-identity" in by_sid["NoSecretMaterial"]["Comment"]
    assert "NotResource" in by_sid["NoSecretMaterial"]["Comment"]


def test_root_policy_copy_matches_the_deployed_one():
    assert _load(ROOT / "policies" / "execution-role-deny.json") == _load(DENY_POLICY)


def test_iam_deny_covers_tool_deny_table():
    actions = _iam_actions(_load(DENY_POLICY))
    missing = [
        (service, operation)
        for service, operation in DENY
        if service not in RESOURCE_SCOPED_SERVICES and not _covered(actions, service, operation)
    ]
    assert missing == [], missing


def test_resource_scoped_denies_target_the_right_arns():
    doc = _load(DENY_POLICY)
    by_sid = {s["Sid"]: s for s in doc["Statement"]}
    assert by_sid["NoApiKeys"]["Resource"] == "arn:aws:apigateway:*::/apikeys*"
    assert by_sid["NoReadingOwnTranscripts"]["Resource"] == [
        "arn:aws:logs:*:*:log-group:/aws/bedrock-agentcore/*",
        "arn:aws:logs:*:*:log-group:aws/spans*",
    ]
    assert all(s["Effect"] == "Deny" for s in doc["Statement"])
