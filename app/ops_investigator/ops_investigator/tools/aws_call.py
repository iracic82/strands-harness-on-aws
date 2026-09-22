"""Read-only AWS API tool. Three layers keep it read-only: this allowlist, ReadOnlyGuard, and IAM (spec 4.6, P26)."""

from __future__ import annotations

import json
from typing import Any

import jmespath
from botocore.exceptions import BotoCoreError, ClientError
from strands import tool
from strands.types.tools import ToolContext

from ops_investigator.tools import _budget, _clients

OUTPUT_CAP = 40_000
CALL_BUDGET = 40
READ_PREFIXES = ("describe_", "list_", "get_", "lookup_", "search_", "query_")

# Operations that are "reads" by name but return secret material or transcripts (spec 4.6). Mirrors the IAM deny.
DENY: frozenset[tuple[str, str]] = frozenset(
    {
        ("secretsmanager", "get_secret_value"),
        ("ssm", "get_parameter"),
        ("ssm", "get_parameters"),
        ("ssm", "get_parameters_by_path"),
        ("ssm", "get_parameter_history"),
        ("kms", "decrypt"),
        ("kms", "generate_data_key"),
        ("kms", "generate_data_key_without_plaintext"),
        ("s3", "get_object"),
        ("s3", "get_object_torrent"),
        ("s3", "select_object_content"),
        ("dynamodb", "get_item"),
        ("dynamodb", "batch_get_item"),
        ("dynamodb", "scan"),
        ("dynamodb", "query"),
        ("dynamodb", "execute_statement"),
        ("dynamodb", "batch_execute_statement"),
        ("lambda", "get_function"),
        ("lambda", "get_function_configuration"),
        ("lambda", "get_layer_version"),
        ("ec2", "get_password_data"),
        ("ec2", "get_console_output"),
        ("ec2", "get_console_screenshot"),
        ("ec2", "describe_instance_attribute"),
        ("cloudformation", "get_template"),
        ("ecs", "describe_task_definition"),
        ("codebuild", "batch_get_projects"),
        ("apigateway", "get_api_keys"),
        ("apigateway", "get_api_key"),
        ("cognito-idp", "admin_get_user"),
        ("cognito-idp", "list_users"),
        ("sqs", "receive_message"),
        ("kinesis", "get_records"),
        ("kinesis", "get_shard_iterator"),
        ("sts", "assume_role"),
        ("sts", "get_federation_token"),
        ("sts", "get_session_token"),
        ("logs", "get_log_events"),
        ("logs", "filter_log_events"),
        ("logs", "start_query"),
        ("logs", "get_query_results"),
    }
)


def is_read_operation(service: str, operation: str) -> bool:
    op = operation.strip().lower()
    return op.startswith(READ_PREFIXES) and (service.strip().lower(), op) not in DENY


def _hint_for(code: str) -> str:
    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
        return (
            "The execution role lacks this permission or it is in the explicit deny list "
            "(policies/execution-role-deny.json)."
        )
    if code in ("Throttling", "ThrottlingException", "TooManyRequestsException"):
        return "Throttled by AWS; narrow the query or wait before retrying."
    return "Check the service/operation name (boto3 snake_case) and parameters."


def _cap(payload: Any) -> tuple[Any, bool]:
    text = json.dumps(payload, default=str)
    if len(text) <= OUTPUT_CAP:
        return payload, False
    return text[:OUTPUT_CAP] + "...", True


@tool(context=True)
def aws_call(
    service: str,
    operation: str,
    parameters: dict | None = None,
    region: str | None = None,
    query: str | None = None,
    max_items: int = 50,
    tool_context: ToolContext | None = None,
) -> str:
    """Call a read-only AWS API (boto3 snake_case operation, e.g. service="ec2", operation="describe_instances").

    Only Describe*/List*/Get*/Lookup*/Search*/Query* operations are allowed, minus operations that return secrets.
    Use `query` (JMESPath) to shrink large responses and `max_items` to bound pagination. Output is capped at 40k chars.
    """
    state = tool_context.invocation_state if tool_context is not None else None
    if not is_read_operation(service, operation):
        return json.dumps(
            {
                "error": f"{service}:{operation} is not permitted",
                "hint": "read-only investigator: Describe*/List*/Get*/Lookup*/Search*/Query* only, "
                "and never secret-bearing reads (S3 objects, SSM parameters, DynamoDB items, ...).",
            }
        )
    service = service.strip().lower()
    operation = operation.strip().lower()
    if not _budget.take(state, "aws_call", CALL_BUDGET):
        return json.dumps(
            {"error": "budget_exhausted", "hint": f"{CALL_BUDGET} AWS calls per turn; summarise what you have."}
        )
    ids = (state or {}).get("ids", {})
    try:
        client = _clients.make_client(
            service, region, session_id=ids.get("session_id", ""), actor_id=ids.get("actor_id", "")
        )
        params = dict(parameters or {})
        if client.can_paginate(operation):
            pages = client.get_paginator(operation).paginate(**params, PaginationConfig={"MaxItems": max_items})
            data: Any = pages.build_full_result()
        else:
            data = getattr(client, operation)(**params)
        if isinstance(data, dict):
            data.pop("ResponseMetadata", None)
        if query:
            data = jmespath.search(query, data)
        capped, truncated = _cap(data)
        return json.dumps(
            {
                "service": service,
                "operation": operation,
                "region": region,
                "data": capped,
                "truncated": truncated,
                "returned": len(data) if isinstance(data, (list, dict)) else (0 if data is None else 1),
            },
            default=str,
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "ClientError")
        return json.dumps(
            {"error": f"{code}: {e.response.get('Error', {}).get('Message', '')}"[:500], "hint": _hint_for(code)}
        )
    except (BotoCoreError, ValueError, TypeError, AttributeError, KeyError) as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"[:500], "hint": _hint_for("")})
