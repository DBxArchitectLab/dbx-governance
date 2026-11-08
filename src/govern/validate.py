"""Layers 2 to 4 in one call: the four sources in; inventory, results, scorecards and reports out. Pure: no Spark, no
dbutils, so the unit tests cover everything the workspace-governance notebook computes; the notebook only reads the
volume, queries the system tables, writes the tables and shows the results.

Per workspace in the estate, the evidence is merged from:
  its snapshot          workspace API + information schema (collected in the workspace, by the workflow)
  the account snapshot  its admin assignments (account API, collected once, by the workflow)
  the system tables     its lineage and job clusters (queried here, for every workspace at once)
and the account itself is judged as one more scope, "account", from the account snapshot.
"""
import json
from datetime import datetime
from pathlib import Path

from govern import report, rules

INVENTORY_COLUMNS = ("run_id", "collected_at", "environment", "workspace_id", "source", "object_type", "name", "owner",
                     "attributes")
INVENTORY_SCHEMA = ("run_id string, collected_at timestamp, environment string, workspace_id long, source string, "
                    "object_type string, name string, owner string, attributes string")
RESULTS_COLUMNS = ("run_id", "checked_at", "environment", "workspace_id", "owner", "source", "rule_id", "domain",
                   "title", "object_type", "object", "principal", "status", "severity", "evidence", "remediation",
                   "remediation_sql", "exception")
RESULTS_SCHEMA = ", ".join(f"{c} {'timestamp' if c == 'checked_at' else 'long' if c == 'workspace_id' else 'string'}"
                           for c in RESULTS_COLUMNS)
ACCOUNT = "account"


def read_snapshots(folder):
    """Every snapshot the workflow uploaded to the run's folder: one per workspace, and the account's."""
    folder = Path(folder)
    snaps = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []
    return [s for s in snaps if "observations" in s]


def _missing(env, workspace_id, checked_at, source, what):
    return {"environment": env, "workspace_id": workspace_id, "observations": [], "collected_at": checked_at.isoformat(),
            "errors": [{"domain": "snapshot", "source": source, "error": what}]}


def merge(cfg, checked_at, snapshots, system=None):
    """One evidence set per scope: each workspace (all its sources) and the account."""
    by_env = {s["environment"]: s for s in snapshots if s.get("scope") != ACCOUNT and s["environment"] in cfg["estate"]}
    account = next((s for s in snapshots if s.get("scope") == ACCOUNT), None)
    merged = {}
    for env, e in cfg["estate"].items():
        ws = e["workspace_id"]
        snap = by_env.get(env) or _missing(env, ws, checked_at, rules.WORKSPACE_API,
                                           "no snapshot from this run's workspace collector")
        obs, errors = list(snap["observations"]), list(snap.get("errors") or [])
        if account is None:
            errors.append({"domain": "account_api", "source": rules.ACCOUNT_API,
                           "error": "no account snapshot in this run: workspace admins not read"})
        else:
            obs += [o for o in account["observations"]
                    if o["object_type"] == "workspace_assignment" and o["attrs"]["workspace_id"] == ws]
            errors += [x for x in account.get("errors") or [] if x.get("workspace_id") == ws]
        if system is not None:
            obs += system[env]["observations"]
            errors += system[env]["errors"]
        merged[env] = {**snap, "observations": obs, "errors": errors}
    if account is None:
        merged[ACCOUNT] = _missing(ACCOUNT, None, checked_at, rules.ACCOUNT_API,
                                   "no account snapshot from this run's account collector")
    else:
        merged[ACCOUNT] = {**account, "observations": [o for o in account["observations"]
                                                       if o["object_type"] != "workspace_assignment"],
                           "errors": [x for x in account.get("errors") or [] if not x.get("workspace_id")]}
    return merged


def build(cfg, run_id, checked_at, snapshots, system=None):
    """Inventory, results, scorecards and reports, for every workspace and the account."""
    inventory, results, scores, reports = [], [], [], {}
    for scope, snap in merge(cfg, checked_at, snapshots, system).items():
        env = rules.scope_env(cfg, scope)
        for o in snap["observations"]:
            if o["object_type"] == "coverage":
                continue
            inventory.append(dict(run_id=str(run_id), collected_at=datetime.fromisoformat(snap["collected_at"]),
                                  environment=scope, workspace_id=env.get("workspace_id"),
                                  source=o.get("source", rules.WORKSPACE_API), object_type=o["object_type"],
                                  name=o["name"], owner=o.get("owner"),
                                  attributes=json.dumps(o["attrs"], sort_keys=True, default=str)))
        mine = rules.evaluate(cfg, snap, checked_at.date())
        for r in mine:
            r.update(run_id=str(run_id), checked_at=checked_at)
        score = {**rules.compliance(scope, mine), "owner": env["owner"], "tier": env["tier"],
                 "worst": report.worst(mine), "run_id": str(run_id), "checked_at": checked_at}
        results += mine
        scores.append(score)
        reports[scope] = report.markdown(score, mine)
    return inventory, results, scores, reports
