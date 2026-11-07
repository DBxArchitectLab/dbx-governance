"""Both sides joined: the plan reports and the audit events in, the findings and health rows out. Pure: no Spark, no
dbutils, so the unit tests cover everything the terraform-drift-audit notebook computes; the notebook only reads the
volume and the audit log, writes the tables and shows the results.
"""
import json
from pathlib import Path

from drift import audit, config

FINDINGS_COLUMNS = ("run_id", "checked_at", "environment", "workspace_id", "source", "stack", "object", "category",
                    "action", "attributes", "who", "event_time", "severity", "remediation", "remediation_sql",
                    "likely_cause")
FINDINGS_SCHEMA = ("run_id string, checked_at timestamp, environment string, workspace_id long, source string, "
                   "stack string, object string, category string, action string, attributes string, who string, "
                   "event_time timestamp, severity string, remediation string, remediation_sql string, "
                   "likely_cause string")


def read_reports(folder):
    """The plan findings the drift workflow uploaded: one JSON per stack."""
    folder = Path(folder)
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []


def build(cfg, run_id, checked_at, reports, events):
    """The findings and health rows (dicts), from the plan reports and the audit events."""
    changes = []
    for e in events:
        env = config.environment_of(cfg, e["workspace_id"])
        category, remediation, sql = audit.classify(e)
        changes.append({**e, "environment": env, "category": category, "remediation": remediation, "sql": sql})

    sev = {env: e.get("severity", "medium") for env, e in cfg["environments"].items()}
    findings = []
    for r in reports:
        for f in r["findings"]:
            findings.append(dict(source="plan", environment=r["environment"], workspace_id=r["workspace_id"],
                                 stack=r["stack"], object=f["address"], category=f["category"],
                                 action=f["planned_action"], attributes=", ".join(f["attributes"]), who=None,
                                 event_time=None, severity=r["severity"], remediation=f["remediation"],
                                 remediation_sql="",
                                 likely_cause="; ".join(audit.likely_causes(f, r["environment"], changes))))
    for c in changes:
        findings.append(dict(source="audit", environment=c["environment"], workspace_id=int(c["workspace_id"]),
                             stack=None, object=audit.object_name(c.get("request_params") or {}),
                             category=c["category"], action=f"{c['service_name']}.{c['action_name']}", attributes="",
                             who=c["who"], event_time=c["event_time"], severity=sev.get(c["environment"], "medium"),
                             remediation=c["remediation"], remediation_sql=c["sql"], likely_cause=""))
    for f in findings:
        f.update(run_id=str(run_id), checked_at=checked_at)

    health = []
    for env in cfg["environments"]:
        mine = [r for r in reports if r["environment"] == env]
        expected = len(cfg["environments"][env]["stacks"])
        count = lambda s: sum(r["status"] == s for r in mine)  # noqa: E731
        env_changes = [c for c in changes if c["environment"] == env]
        health.append(dict(run_id=str(run_id), checked_at=checked_at, environment=env, stacks_expected=expected,
                           stacks_clean=count("clean"), stacks_drift=count("drift"),
                           stacks_not_applied=count("not_applied"), stacks_error=count("error"),
                           stacks_missing=expected - len(mine),
                           plan_findings=sum(len(r["findings"]) for r in mine), manual_changes=len(env_changes),
                           created_outside=sum(c["category"] == audit.CREATED_OUTSIDE for c in env_changes),
                           # In sync = every stack's plan is clean today. Manual changes stay visible for the whole
                           # lookback, even after they are reverted, so they are counted, not judged here.
                           in_sync=count("clean") == expected))
    return findings, health
