"""Layers 2 to 4 in one call: snapshots in, inventory, results, scorecards and reports out. Pure: no Spark, no
dbutils, so the unit tests cover everything the workspace-governance notebook computes; the notebook only reads the
volume, writes the tables and shows the results.
"""
import json
from datetime import datetime
from pathlib import Path

from govern import report, rules

INVENTORY_COLUMNS = ("run_id", "collected_at", "environment", "workspace_id", "object_type", "name", "owner",
                     "attributes")
INVENTORY_SCHEMA = ("run_id string, collected_at timestamp, environment string, workspace_id long, object_type string, "
                    "name string, owner string, attributes string")
RESULTS_COLUMNS = ("run_id", "checked_at", "environment", "workspace_id", "owner", "rule_id", "domain", "title",
                   "object_type", "object", "principal", "status", "severity", "evidence", "remediation",
                   "remediation_sql", "exception")
RESULTS_SCHEMA = ", ".join(f"{c} {'timestamp' if c == 'checked_at' else 'long' if c == 'workspace_id' else 'string'}"
                           for c in RESULTS_COLUMNS)


def read_snapshots(folder):
    """Every snapshot (one JSON per workspace) the workflow uploaded to the run's folder."""
    folder = Path(folder)
    snaps = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []
    return [s for s in snaps if "observations" in s]


def build(cfg, run_id, checked_at, snapshots):
    """Inventory, results, scorecards and reports, from the snapshots. A workspace without a snapshot is an error."""
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
