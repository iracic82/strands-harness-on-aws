---
name: investigate-incident
description: Runbook for diagnosing a failing, slow, or expensive AWS resource with read-only evidence gathering.
---

# Investigate an AWS incident (read-only)

1. Identify: `aws_call sts get_caller_identity` for account and confirm the region the user means.
2. Locate the resource: `aws_call <service> describe_*/list_*` with a JMESPath `query` to keep output small.
3. Recent changes: `aws_call cloudtrail lookup_events` with `LookupAttributes` on ResourceName (last 24h).
4. Errors: `logs_insights` on the resource's log group, e.g.
   `fields @timestamp, @message | filter @message like /ERROR|Exception|Task timed out/ | sort @timestamp desc | limit 50`.
5. Health: CloudWatch metrics via `aws_call cloudwatch get_metric_statistics` (Errors, Throttles, Duration, 5XX).
6. Cost angle (if asked): `cost_explorer days=7 group_by=SERVICE`, then narrow with `USAGE_TYPE`.
7. For a wide scan across many resources, delegate ONE question to `reader`.
8. Report: Findings with evidence, likely cause, next checks, and what was not checked.
