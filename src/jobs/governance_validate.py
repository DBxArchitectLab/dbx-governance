"""Job entry point: workspace governance, in the governance workspace, after the governance workflow's collectors.

  --config <file>     config/governance.yml (the bundle passes the deployed copy)
  --run-id <id>       the governance workflow's GitHub run ID: its snapshots are in the volume under this folder

1. Reads the snapshots the workflow uploaded (one JSON per workspace) and the standard they are checked against: the
   governance.yml uploaded with them (the run's own), or the deployed copy when there is none.
2. Normalizes them into one inventory and checks every workspace against the standard (src/govern/rules.py).
3. Appends to <catalog>.<schema>.inventory (what exists), .results (one row per check) and .compliance (one scorecard
   per workspace and run), so governance health can be measured over time.
4. Writes report/<environment>.md and report/summary.json next to the snapshots: the workflow turns them into one
   issue per workspace, for its owner.

It never calls a dev or prod API: observation happened in the collectors; judgement happens here, in one place.
Identity: sp-dbx-governance (run_as).
"""
import argparse
import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# src/ on the path, so govern imports. Not from __file__: a spark_python_task on serverless runs this file through
# exec(), which doesn't define it; the code object still carries the file's path.
sys.path.insert(0, str(Path(inspect.currentframe().f_code.co_filename).resolve().parents[1]))
from govern import config, report, rules  # noqa: E402

INVENTORY_SCHEMA = ("run_id string, collected_at timestamp, environment string, workspace_id long, object_type string, "
                    "name string, owner string, attributes string")
RESULTS_COLUMNS = ("run_id", "checked_at", "environment", "workspace_id", "owner", "rule_id", "domain", "title",
                   "object_type", "object", "principal", "status", "severity", "evidence", "remediation",
                   "remediation_sql", "exception")
RESULTS_SCHEMA = ", ".join(f"{c} {'timestamp' if c == 'checked_at' else 'long' if c == 'workspace_id' else 'string'}"
                           for c in RESULTS_COLUMNS)


def read_snapshots(folder):
    folder = Path(folder)
    snaps = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []
    return [s for s in snaps if "observations" in s]


def build(cfg, run_id, checked_at, snapshots):
    """Inventory, results, scorecards and reports, from the snapshots. Pure: no Spark here."""
    by_env = {s["environment"]: s for s in snapshots if s["environment"] in cfg["estate"]}
    inventory, results, scores, reports = [], [], [], {}
    for env, e in cfg["estate"].items():
        snap = by_env.get(env) or {"environment": env, "workspace_id": e["workspace_id"], "observations": [],
                                   "collected_at": checked_at.isoformat(), "errors": [
                                       {"domain": "snapshot", "error": "no snapshot from this run's collector"}]}
        for o in snap["observations"]:
            inventory.append(dict(run_id=str(run_id), collected_at=datetime.fromisoformat(snap["collected_at"]),
                                  environment=env, workspace_id=e["workspace_id"], object_type=o["object_type"],
                                  name=o["name"], owner=o.get("owner"),
                                  attributes=json.dumps(o["attrs"], sort_keys=True, default=str)))
        mine = rules.evaluate(cfg, snap, checked_at.date())
        for r in mine:
            r.update(run_id=str(run_id), checked_at=checked_at)
        score = {**rules.compliance(env, mine), "owner": e["owner"], "tier": e["tier"],
                 "worst": report.worst(mine), "run_id": str(run_id), "checked_at": checked_at}
        results += mine
        scores.append(score)
        reports[env] = report.markdown(score, mine)
    return inventory, results, scores, reports


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--run-id", required=True)
    a = p.parse_args(argv)
    deployed = config.load(a.config)
    out = deployed["output"]
    folder = Path(f"/Volumes/{out['catalog']}/{out['schema']}/{out['volume']}/{a.run_id}")
    # The standard this run is checked against travels with its snapshots, so the evidence says which one it was.
    cfg = config.load(folder / "governance.yml") if (folder / "governance.yml").is_file() else deployed
    schema = f"{out['catalog']}.{out['schema']}"

    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    snapshots = read_snapshots(folder)
    inventory, results, scores, reports = build(cfg, a.run_id, datetime.now(timezone.utc), snapshots)
    print(f"{len(snapshots)} snapshots from {folder}; {len(inventory)} objects; {len(results)} checks "
          f"against {len(rules.RULES)} rules")

    if inventory:
        spark.createDataFrame([tuple(r[c] for c in ("run_id", "collected_at", "environment", "workspace_id",
                                                    "object_type", "name", "owner", "attributes")) for r in inventory],
                              schema=INVENTORY_SCHEMA).write.mode("append").saveAsTable(f"{schema}.inventory")
    if results:
        spark.createDataFrame([tuple(r[c] for c in RESULTS_COLUMNS) for r in results],
                              schema=RESULTS_SCHEMA).write.mode("append").saveAsTable(f"{schema}.results")
    spark.createDataFrame(scores).write.mode("append").option("mergeSchema", "true").saveAsTable(f"{schema}.compliance")

    (folder / "report").mkdir(parents=True, exist_ok=True)
    for env, md in reports.items():
        (folder / "report" / f"{env}.md").write_text(md, encoding="utf-8")
    (folder / "report" / "summary.json").write_text(json.dumps(report.summary(scores), indent=2), encoding="utf-8")
    for env in reports:
        print(reports[env])


if __name__ == "__main__":
    main()
