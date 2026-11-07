"""The audit side: the query, the classification, the revert SQL, and both sides joined in the job."""
import importlib.util
import json
from datetime import datetime

import pytest
from conftest import ROOT

from drift import audit, config, plan

CFG = config.load(ROOT / "config" / "drift.yml")
PROD = 7405604784566671


def event(action, service="unityCatalog", who="hcui@example.com", ws=PROD, **params):
    return {"event_time": datetime(2026, 11, 20, 9, 30), "workspace_id": ws, "who": who, "service_name": service,
            "action_name": action, "request_params": params}


def test_query_watches_the_configured_workspaces_and_skips_the_pipelines():
    sql = audit.manual_changes_sql(CFG)
    assert "workspace_id IN (7405609089943919, 7405604784566671)" in sql
    assert "'6eab1957-6c50-4f47-964b-c89be78ab1f1'" in sql and "'9a769c8e-a44b-4009-9378-719753df14c7'" in sql
    assert "(service_name = 'clusterPolicies' AND action_name IN ('create', 'edit', 'delete'" in sql
    assert "date_sub(current_date(), 7)" in sql


def test_query_refuses_odd_names():
    cfg = json.loads(json.dumps(CFG))
    cfg["audit"]["watched"]["secrets"] = ["putAcl'; DROP TABLE x; --"]
    with pytest.raises(config.ConfigError):
        audit.manual_changes_sql(cfg)


def test_a_manual_grant_gets_the_sql_that_undoes_it():
    e = event("updatePermissions", securable_type="catalog", securable_full_name="dbxarchitectlab_prod",
              changes=json.dumps([{"principal": "data-team", "add": ["USE_CATALOG", "SELECT"]},
                                  {"principal": "hcui@example.com", "remove": ["MANAGE"]}]))
    category, remediation, sql = audit.classify(e)
    assert category == audit.MANUAL_GRANT
    assert sql.splitlines() == [
        "REVOKE USE CATALOG ON CATALOG `dbxarchitectlab_prod` FROM `data-team`;",
        "REVOKE SELECT ON CATALOG `dbxarchitectlab_prod` FROM `data-team`;",
        "GRANT MANAGE ON CATALOG `dbxarchitectlab_prod` TO `hcui@example.com`;"]
    assert "dbxarchitectlab_prod" in remediation


def test_external_location_and_wrapped_changes():
    sql = audit.revert_grants_sql({"securable_type": "external_location", "securable_full_name": "el_prod",
                                   "changes": json.dumps({"changes": [{"principal": "g", "add": ["READ_FILES"]}]})})
    assert sql == ["REVOKE READ FILES ON EXTERNAL LOCATION `el_prod` FROM `g`;"]


@pytest.mark.parametrize("action, service, category", [
    ("create", "clusterPolicies", audit.CREATED_OUTSIDE),
    ("createWarehouse", "databrickssql", audit.CREATED_OUTSIDE),
    ("deleteScope", "secrets", audit.DELETED_BY_HAND),
    ("edit", "clusterPolicies", audit.MANUAL_CHANGE),
])
def test_classification(action, service, category):
    assert audit.classify(event(action, service, name="x"))[0] == category


def load_job():
    spec = importlib.util.spec_from_file_location("drift_audit", ROOT / "src" / "jobs" / "drift_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_job_joins_both_sides():
    job = load_job()
    stack = config.Stack("prod", "workspace-bootstrap", "live/prod/workspace-bootstrap", PROD, "high")
    drifted = {"resource_drift": [{"address": "p", "type": "databricks_cluster_policy",
                                   "change": {"actions": ["update"], "before": {"d": 1}, "after": {"d": 8}}}],
               "resource_changes": [{"address": "p", "type": "databricks_cluster_policy",
                                     "change": {"actions": ["update"], "before": {"d": 8}, "after": {"d": 1}}}]}
    reports = [plan.report(stack, 2, drifted, {}),
               plan.report(config.Stack("prod", "workspace", "live/prod/workspace", PROD, "high"), 0, None, {}),
               plan.report(config.Stack("dev", "workspace", "live/dev/workspace", 7405609089943919, "low"), 0, None,
                           {})]
    events = [event("edit", "clusterPolicies", policy_id="ABC"), event("create", "clusterPolicies", name="adhoc")]
    findings, health = job.build(CFG, "99", datetime(2026, 11, 20), reports, events)

    from_plan = [f for f in findings if f["source"] == "plan"]
    assert len(from_plan) == 1 and from_plan[0]["likely_cause"].startswith("hcui@example.com edit ABC")
    created = [f for f in findings if f["category"] == audit.CREATED_OUTSIDE]
    assert created[0]["object"] == "adhoc" and created[0]["who"] == "hcui@example.com"
    assert all(set(f) == set(job.FINDINGS_COLUMNS) for f in findings)

    h = {x["environment"]: x for x in health}
    assert h["prod"]["stacks_drift"] == 1 and h["prod"]["stacks_clean"] == 1 and not h["prod"]["in_sync"]
    assert h["prod"]["created_outside"] == 1
    assert h["dev"]["stacks_clean"] == 1 and h["dev"]["stacks_missing"] == 1 and not h["dev"]["in_sync"]

    # Reverted the next day: both plans clean, so prod is in sync, while the audit still counts last week's changes.
    clean = [plan.report(config.Stack("prod", s, f"live/prod/{s}", PROD, "high"), 0, None, {})
             for s in ("workspace", "workspace-bootstrap")]
    _, health = job.build(CFG, "100", datetime(2026, 11, 21), clean, events)
    prod = next(x for x in health if x["environment"] == "prod")
    assert prod["in_sync"] and prod["manual_changes"] == 2
