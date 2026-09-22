from __future__ import annotations

from google.protobuf.json_format import MessageToDict
from ops_investigator import agent_card as ac

DISCOVERY = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_x/.well-known/openid-configuration"


def test_card_is_spec_1_0_shaped():
    card = ac.build_agent_card(public_url="https://example/run", discovery_url=DISCOVERY)
    d = MessageToDict(card)
    assert d["name"] == "AWS Ops Investigator"
    assert d["supportedInterfaces"][0] == {
        "url": "https://example/run",
        "protocolBinding": "JSONRPC",
        "protocolVersion": "1.0",
    }
    assert d["capabilities"]["streaming"] is True
    assert d["capabilities"].get("pushNotifications", False) is False
    assert d["capabilities"].get("extendedAgentCard", False) is False
    assert d["provider"] == {
        "organization": "strands-harness-on-aws",
        "url": "https://github.com/iracic82/strands-harness-on-aws",
    }
    assert d["version"] == "0.1.0"
    assert d["defaultInputModes"] == d["defaultOutputModes"] == ["text/plain"]
    assert {s["id"] for s in d["skills"]} == {"investigate-incident", "cost-review", "inventory"}


def test_card_has_no_0_3_fields():
    """P22: the 0.3 card carries these at the top level; a 1.0 card must not."""
    d = MessageToDict(ac.build_agent_card(public_url="http://localhost:9000", discovery_url=None))
    for stale in ("protocolVersion", "supportsAuthenticatedExtendedCard", "url", "preferredTransport"):
        assert stale not in d


def test_card_security_points_at_the_cognito_discovery_document():
    d = ac.card_json(ac.build_agent_card(public_url="https://example/run", discovery_url=DISCOVERY))
    assert d["securitySchemes"]["cognito"]["openIdConnectSecurityScheme"]["openIdConnectUrl"] == DISCOVERY
    assert d["securityRequirements"] == [{"schemes": {"cognito": {}}}]


def test_card_without_auth_has_no_security():
    d = ac.card_json(ac.build_agent_card(public_url="http://localhost:9000", discovery_url=None))
    assert "securitySchemes" not in d and "securityRequirements" not in d


def test_every_skill_is_text_in_text_out():
    d = ac.card_json(ac.build_agent_card(public_url="http://localhost:9000", discovery_url=None))
    for skill in d["skills"]:
        assert skill["inputModes"] == skill["outputModes"] == ["text/plain"]
        assert skill["description"] and skill["tags"] and skill["examples"]
