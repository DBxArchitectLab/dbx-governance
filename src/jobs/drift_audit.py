"""Job entry point: the drift audit, in the governance workspace, after the drift workflow's plans.

  --config <file>     config/drift.yml (the bundle passes the deployed copy)
  --run-id <id>       the drift workflow's GitHub run ID: its plan findings are in the volume under this folder

1. Reads the plan findings the drift workflow uploaded (one JSON per stack).
2. Queries system.access.audit for manual changes to watched objects in the configured workspaces.
3. Appends one row per finding to <catalog>.<schema>.findings (both sides, with the remediation), and one row per
   environment to <catalog>.<schema>.health, so drift can be measured over time.

The audit log covers every workspace in the account, so this runs in the governance workspace and never calls the
dev or prod APIs. Identity: sp-dbx-governance (run_as), with SELECT on system.access.audit.
"""
import argparse
import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# src/ on the path, so drift imports. Not from __file__: a spark_python_task on serverless runs this file through
# exec(), which doesn't define it; the code object still carries the file's path.
sys.path.insert(0, str(Path(inspect.currentframe().f_code.co_filename).resolve().parents[1]))
from drift import audit, config, plan  # noqa: E402

FINDINGS_COLUMNS = ("run_id", "checked_at", "environment", "workspace_id", "source", "stack", "object", "category",
                    "action", "attributes", "who", "event_time", "severity", "remediation", "remediation_sql",
                    "likely_cause")


def read_reports(folder):
    folder = Path(folder)
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []


def build(cfg, run_id, checked_at, reports, events):
    """The findings and health rows (dicts), from the plan reports and the audit events. Pure: no Spark here."""
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


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--run-id", required=True)
    a = p.parse_args(argv)
    cfg = config.load(a.config)
    out = cfg["output"]
    schema = f"{out['catalog']}.{out['schema']}"
    folder = f"/Volumes/{out['catalog']}/{out['schema']}/{out['volume']}/{a.run_id}"

    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    reports = read_reports(folder)
    events = [r.asDict(recursive=True) for r in spark.sql(audit.manual_changes_sql(cfg)).collect()]
    findings, health = build(cfg, a.run_id, datetime.now(timezone.utc), reports, events)
    print(f"{len(reports)} plan reports from {folder}; {len(events)} manual changes in the last "
          f"{cfg['audit']['lookback_days']} days")

    if findings:
        df = spark.createDataFrame([tuple(f[c] for c in FINDINGS_COLUMNS) for f in findings],
                                   schema="run_id string, checked_at timestamp, environment string, workspace_id long, "
                                          "source string, stack string, object string, category string, "
                                          "action string, attributes string, who string, event_time timestamp, "
                                          "severity string, remediation string, remediation_sql string, "
                                          "likely_cause string")
        df.write.mode("append").saveAsTable(f"{schema}.findings")
    spark.createDataFrame(health).write.mode("append").option("mergeSchema", "true").saveAsTable(f"{schema}.health")

    for h in health:
        print(f"{h['environment']}: {'in sync' if h['in_sync'] else 'NOT in sync'} | stacks clean "
              f"{h['stacks_clean']}/{h['stacks_expected']}, drift {h['stacks_drift']}, not applied "
              f"{h['stacks_not_applied']}, error {h['stacks_error']}, missing {h['stacks_missing']} | manual changes "
              f"{h['manual_changes']} (created outside Terraform {h['created_outside']})")
    for f in findings:
        print(f"- [{f['severity']}] {f['environment']} {f['source']}: {f['object']} | {f['category']}"
              + (f" | by {f['who']}" if f["who"] else "") + f" | {f['remediation']}")
        if f["remediation_sql"]:
            print("  " + f["remediation_sql"].replace("\n", "\n  "))


if __name__ == "__main__":
    main()
