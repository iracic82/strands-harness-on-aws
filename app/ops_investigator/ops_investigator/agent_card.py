"""A2A 1.0 agent card, hand-built on a2a-sdk 1.x protobuf types (spec 4.5, P22).

Strands' own `A2AServer` pins a2a-sdk 0.3 and emits a 0.3 card (top-level `url`, `protocolVersion`,
`supportsAuthenticatedExtendedCard`); this runtime speaks 1.0 end to end, so the card is built here.
In 1.0 the transport and the protocol version live on each `AgentInterface`, not on the card.
"""

from __future__ import annotations

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    OpenIdConnectSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
)
from google.protobuf.json_format import MessageToDict

NAME = "AWS Ops Investigator"
DESCRIPTION = (
    "Read-only diagnostician for one AWS account: failures, changes, and cost, with evidence. "
    "Built on the Strands harness and hosted on Amazon Bedrock AgentCore Runtime."
)
ORGANIZATION = "strands-harness-on-aws"
REPO_URL = "https://github.com/iracic82/strands-harness-on-aws"
PROTOCOL_VERSION = "1.0"
PROTOCOL_BINDING = "JSONRPC"
TEXT = ["text/plain"]
COGNITO_SCHEME = "cognito"
COGNITO_DESCRIPTION = "Cognito user pool JWT (AgentCore CUSTOM_JWT inbound auth)"

SKILLS = [
    AgentSkill(
        id="investigate-incident",
        name="Investigate an incident",
        description="Why is this Lambda/ECS service/API failing or slow? Evidence from config, CloudTrail, and logs.",
        tags=["aws", "ops", "incident"],
        examples=["Why did orders-api start returning 5XX an hour ago?"],
        input_modes=TEXT,
        output_modes=TEXT,
    ),
    AgentSkill(
        id="cost-review",
        name="Cost review",
        description="Where is spend going and what changed? Cost Explorer by service, region, or usage type.",
        tags=["aws", "cost"],
        examples=["What cost the most in the last 7 days and why?"],
        input_modes=TEXT,
        output_modes=TEXT,
    ),
    AgentSkill(
        id="inventory",
        name="Inventory a resource or region",
        description="List and summarise resources (read-only) with ids, ARNs, and state.",
        tags=["aws", "inventory"],
        examples=["What AgentCore runtimes exist in us-west-2 and are they READY?"],
        input_modes=TEXT,
        output_modes=TEXT,
    ),
]


def build_agent_card(*, public_url: str, discovery_url: str | None, version: str = "0.1.0") -> AgentCard:
    """The public card. Without a discovery URL the card advertises no security scheme at all, which is
    the honest answer for a local run: there is no authorizer in front of it."""
    card = AgentCard(
        name=NAME,
        description=DESCRIPTION,
        version=version,
        provider=AgentProvider(organization=ORGANIZATION, url=REPO_URL),
        documentation_url=f"{REPO_URL}#a2a",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False, extended_agent_card=False),
        default_input_modes=TEXT,
        default_output_modes=TEXT,
        supported_interfaces=[
            AgentInterface(url=public_url, protocol_binding=PROTOCOL_BINDING, protocol_version=PROTOCOL_VERSION)
        ],
        skills=SKILLS,
    )
    if discovery_url:
        card.security_schemes[COGNITO_SCHEME].CopyFrom(
            SecurityScheme(
                open_id_connect_security_scheme=OpenIdConnectSecurityScheme(
                    open_id_connect_url=discovery_url, description=COGNITO_DESCRIPTION
                )
            )
        )
        requirement = SecurityRequirement()
        # The map value is a StringList of scopes; `SetInParent` puts the (empty) entry in the map, which
        # a bare `requirement.schemes[...]` lookup would not — proto map reads do not create the key.
        requirement.schemes[COGNITO_SCHEME].SetInParent()
        card.security_requirements.append(requirement)
    return card


def card_json(card: AgentCard) -> dict:
    return MessageToDict(card)
