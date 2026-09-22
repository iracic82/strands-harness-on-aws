"""AWS-facing tools for the ops investigator agent."""

from __future__ import annotations

from .aws_call import aws_call
from .cost_explorer import cost_explorer
from .logs_insights import logs_insights

AWS_TOOLS = [aws_call, logs_insights, cost_explorer]
AWS_TOOL_NAMES = ["aws_call", "logs_insights", "cost_explorer"]
__all__ = ["AWS_TOOLS", "AWS_TOOL_NAMES", "aws_call", "cost_explorer", "logs_insights"]
