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


def _iam_actions(doc: dict) -> set[str]:
    actions: set[str] = set()
    for statement in doc["Statement"]:
        raw = statement["Action"]
        actions.update(a.lower() for a in (raw if isinstance(raw, list) else [raw]))
    return actions


def _covered(actions: set[str], service: str, operation: str) -> bool:
    action = IAM_ACTION_ALIASES.get((service, operation))
    if action is None:
        action = f"{service}:" + "".join(part.capitalize() for part in operation.split("_"))
    action = action.lower()
    return any(a == action or (a.endswith("*") and action.startswith(a[:-1])) for a in actions)


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
        assert env["HARNESS_MODEL"] == "bedrock/global.anthropic.claude-sonnet-4-6"
        assert env["HARNESS_EFFORT"] == "medium"
        assert env["HARNESS_MAX_SECONDS"] == "600"


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


def test_deny_policy_is_not_shipped_in_the_image():
    ignored = {line.strip() for line in (APP / ".dockerignore").read_text().splitlines()}
    assert "policies/" in ignored


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
