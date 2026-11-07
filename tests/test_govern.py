"""Workspace governance: config, collector, rules and the job's build step, without a workspace."""
import copy
from datetime import date, datetime, timezone

import pytest
import yaml
from conftest import ROOT

from govern import collect, config, report, rules, validate

CFG = config.load(ROOT / "config" / "governance.yml")
TODAY = date(2026, 11, 7)
ADMIN = "DBX_Architect_Lab_Admin"
INFRA_SP = "6eab1957-6c50-4f47-964b-c89be78ab1f1"
LAKEHOUSE_SP = "9a769c8e-a44b-4009-9378-719753df14c7"
PROD_ROOT = "abfss://dbx-architect-lab-prod@stdbxarchlabprod.dfs.core.windows.net/"


# ---------------------------------------------------------------- config
def test_the_real_config_governs_dev_and_prod():
    assert [m["environment"] for m in config.matrix(CFG)["include"]] == ["dev", "prod"]
    assert config.matrix(CFG)["include"][1] == {"environment": "prod", "workspace_id": 7405604784566671,
                                                "host": "https://adb-7405604784566671.11.azuredatabricks.net",
                                                "tier": "prod"}


def test_the_estate_matches_the_drift_config():
    drift = yaml.safe_load((ROOT / "config" / "drift.yml").read_text(encoding="utf-8"))["environments"]
    for env, e in CFG["estate"].items():
        assert e["workspace_id"] == drift[env]["workspace_id"]


def test_the_bundle_resources_match_the_output_section():
    res = yaml.safe_load((ROOT / "resources" / "governance.yml").read_text(encoding="utf-8"))["resources"]
    assert res["schemas"]["estate"]["catalog_name"] == CFG["output"]["catalog"]
    assert res["schemas"]["estate"]["name"] == CFG["output"]["schema"]
    assert res["volumes"]["snapshots"]["name"] == CFG["output"]["volume"]


def test_scaling_out_is_one_entry():
    cfg = copy.deepcopy(CFG)
    cfg["estate"]["uat"] = {**cfg["estate"]["dev"], "workspace_id": 1, "catalog": "dbxarchitectlab_uat"}
    config.validate(cfg)
    assert [m["environment"] for m in config.matrix(cfg)["include"]] == ["dev", "prod", "uat"]


@pytest.mark.parametrize("change, message", [
    (lambda c: c["estate"]["prod"].update(tier="production"), "estate.prod.tier"),
    (lambda c: c["estate"]["prod"].update(host="adb-1.azuredatabricks.net"), "estate.prod.host"),
    (lambda c: c["estate"]["prod"].update(workspace_id=c["estate"]["dev"]["workspace_id"]), "appears twice"),
    (lambda c: c["exceptions"][0].pop("expires"), "time-bound"),
    (lambda c: c["exceptions"][0].update(environment="qa"), "not in the estate"),
    (lambda c: c["standard"].update(schema_name_pattern="(["), "regular expression"),
])
def test_a_typo_fails_with_the_field(change, message):
    cfg = copy.deepcopy(CFG)
    change(cfg)
    with pytest.raises(config.ConfigError, match=message):
        config.validate(cfg)


# ---------------------------------------------------------------- collector
def fake_api(responses):
    calls = []

    def call(method, path, query):
        assert method == "GET", "collectors only read"
        calls.append((path, query))
        r = responses.get(path, {})
        return r(query) if callable(r) else r
    return call, calls


PROD_API = {
    "/api/2.1/unity-catalog/catalogs": {"catalogs": [
        {"name": "dbxarchitectlab_prod", "owner": INFRA_SP, "isolation_mode": "ISOLATED",
         "storage_root": PROD_ROOT + "managed/dbxarchitectlab_prod/"},
        {"name": "dbxarchitectlab_dev", "owner": INFRA_SP, "isolation_mode": "ISOLATED"},
        {"name": "system", "owner": "System user"}]},
    "/api/2.1/unity-catalog/bindings/catalog/dbxarchitectlab_prod": {"bindings": [{"workspace_id": 7405604784566671}]},
    "/api/2.1/unity-catalog/bindings/catalog/dbxarchitectlab_dev": {"bindings": [{"workspace_id": 7405609089943919}]},
    "/api/2.1/unity-catalog/permissions/catalog/dbxarchitectlab_prod": {"privilege_assignments": [
        {"principal": ADMIN, "privileges": ["ALL_PRIVILEGES"]},
        {"principal": "dbxarchitectlab@gmail.com", "privileges": ["ALL_PRIVILEGES"]}]},
    "/api/2.1/unity-catalog/schemas": {"schemas": [
        {"name": "sales", "full_name": "dbxarchitectlab_prod.sales", "owner": LAKEHOUSE_SP},
        {"name": "Adhoc-Sales", "full_name": "dbxarchitectlab_prod.Adhoc-Sales", "owner": "jane@contoso.com"},
        {"name": "information_schema", "full_name": "dbxarchitectlab_prod.information_schema", "owner": "System"}]},
    "/api/2.1/unity-catalog/permissions/schema/dbxarchitectlab_prod.sales": {"privilege_assignments": [
        {"principal": "jane@contoso.com", "privileges": ["SELECT", "USE_SCHEMA"]}]},
    "/api/2.1/unity-catalog/permissions/schema/dbxarchitectlab_prod.Adhoc-Sales": {},
    "/api/2.1/unity-catalog/external-locations": {"external_locations": [
        {"name": "ext_loc_dbx_architect_lab_prod", "owner": ADMIN, "url": PROD_ROOT},
        {"name": "ext_loc_dbx_architect_lab_dev", "owner": ADMIN,
         "url": "abfss://dbx-architect-lab-dev@stdbxarchlabdev.dfs.core.windows.net/"},
        {"name": "ext_loc_bound_to_dev", "owner": ADMIN, "url": "abfss://x@y.dfs.core.windows.net/",
         "isolation_mode": "ISOLATION_MODE_ISOLATED"}]},
    "/api/2.1/unity-catalog/bindings/external_location/ext_loc_bound_to_dev": {"bindings": [
        {"workspace_id": 7405609089943919}]},
    "/api/2.1/clusters/list": {"clusters": [
        {"cluster_id": "1", "cluster_name": "shared-analytics", "cluster_source": "UI", "policy_id": "P1",
         "custom_tags": {"workload": "shared-analytics"}, "autotermination_minutes": 120, "creator_user_name": INFRA_SP},
        {"cluster_id": "2", "cluster_name": "jane-quick-test", "cluster_source": "UI",
         "autotermination_minutes": 0, "creator_user_name": "jane@contoso.com"},
        {"cluster_id": "3", "cluster_name": "job-123-run-1", "cluster_source": "JOB"}]},
    "/api/2.0/policies/clusters/list": {"policies": [{"policy_id": "P1", "name": "Shared Compute"}]},
    "/api/2.0/sql/warehouses": {"warehouses": [{"id": "w1", "name": "sql-wh-small", "auto_stop_mins": 20}]},
    "/api/2.2/jobs/list": lambda q: ({"jobs": [{"job_id": 1, "creator_user_name": "x",
                                                "settings": {"name": "silver", "run_as":
                                                             {"service_principal_name": LAKEHOUSE_SP}}}],
                                      "next_page_token": "p2"} if "page_token" not in q else
                                     {"jobs": [{"job_id": 2, "creator_user_name": "jane@contoso.com",
                                                "settings": {"name": "jane-adhoc"}}]}),
    "/api/2.0/workspace-conf": {"enableDbfsFileBrowser": "false"},
}


@pytest.fixture(scope="module")
def prod_snapshot():
    call, _ = fake_api(PROD_API)
    return collect.snapshot(call, CFG, "prod", now=datetime(2026, 11, 7, tzinfo=timezone.utc))


def test_the_collector_normalizes_one_workspace(prod_snapshot):
    obs = prod_snapshot["observations"]
    kinds = {o["object_type"] for o in obs}
    assert kinds == {"catalog", "grant", "schema", "external_location", "cluster", "cluster_policy", "warehouse",
                     "job", "workspace_setting"}
    assert prod_snapshot["errors"] == []
    dev_cat = next(o for o in obs if o["name"] == "dbxarchitectlab_dev")
    assert dev_cat["attrs"]["accessible_here"] is False
    assert not any(o["name"].endswith("information_schema") for o in obs), "system schemas are skipped"
    assert not any(o["name"] == "ext_loc_bound_to_dev" for o in obs), "bound to another workspace: not usable here"
    assert [o["name"] for o in obs if o["object_type"] == "cluster"] == ["shared-analytics", "jane-quick-test"]
    assert [o["attrs"]["run_as"] for o in obs if o["object_type"] == "job"] == [LAKEHOUSE_SP, "jane@contoso.com"]


def test_one_failing_domain_is_an_error_not_a_pass():
    def call(method, path, query):
        if path == "/api/2.0/workspace-conf":
            raise PermissionError("403: only admins")
        return PROD_API.get(path, {}) if not callable(PROD_API.get(path)) else PROD_API[path](query)
    snap = collect.snapshot(call, CFG, "prod")
    assert snap["errors"] == [{"domain": "settings", "error": "PermissionError: 403: only admins"}]
    res = rules.evaluate(CFG, snap, TODAY)
    assert [r["status"] for r in res if r["rule_id"] == "COL-00"] == ["ERROR"]
    assert not rules.compliance("prod", res)["compliant"]


# ---------------------------------------------------------------- rules
def by_rule(results, rule_id, status=None):
    return [r for r in results if r["rule_id"] == rule_id and (status is None or r["status"] == status)]


@pytest.fixture(scope="module")
def prod_results(prod_snapshot):
    return rules.evaluate(CFG, prod_snapshot, TODAY)


def test_a_direct_user_grant_fails_with_the_sql_that_fixes_it(prod_results):
    jane = by_rule(prod_results, "IAM-01", "FAIL")
    assert [r["object"] for r in jane] == ["dbxarchitectlab_prod.sales"]
    assert jane[0]["severity"] == "high"
    assert jane[0]["remediation_sql"].splitlines()[1:] == [
        f"GRANT SELECT, USE SCHEMA ON SCHEMA `dbxarchitectlab_prod`.`sales` TO `{ADMIN}`;",
        "REVOKE SELECT, USE SCHEMA ON SCHEMA `dbxarchitectlab_prod`.`sales` FROM `jane@contoso.com`;"]


def test_an_explicit_exception_holds_until_its_end_date(prod_snapshot, prod_results):
    owner = by_rule(prod_results, "IAM-01", "EXCEPTION")
    assert [r["principal"] for r in owner] == ["dbxarchitectlab@gmail.com"]
    assert "until 2027-03-31" in owner[0]["exception"]
    later = rules.evaluate(CFG, prod_snapshot, date(2027, 4, 1))
    expired = [r for r in by_rule(later, "IAM-01") if r["principal"] == "dbxarchitectlab@gmail.com"]
    assert expired[0]["status"] == "FAIL" and "expired on 2027-03-31" in expired[0]["exception"]


def test_high_risk_privileges_only_for_admins_and_pipelines(prod_results):
    risky = by_rule(prod_results, "IAM-03")
    assert {(r["principal"], r["status"]) for r in risky} == {(ADMIN, "PASS"),
                                                              ("dbxarchitectlab@gmail.com", "EXCEPTION")}
    assert risky[1]["severity"] == "critical", "one exception covers both of the lab owner's rules"


def test_a_schema_owned_by_a_person_fails_and_is_handed_to_the_group(prod_results):
    owned = by_rule(prod_results, "OWN-01", "FAIL")
    assert [r["object"] for r in owned] == ["dbxarchitectlab_prod.Adhoc-Sales"]
    assert owned[0]["remediation_sql"] == f"ALTER SCHEMA `dbxarchitectlab_prod`.`Adhoc-Sales` OWNER TO `{ADMIN}`;"
    assert [r["object"] for r in by_rule(prod_results, "NAM-01", "FAIL")] == ["dbxarchitectlab_prod.Adhoc-Sales"]


def test_isolation_and_storage(prod_results):
    assert [r["status"] for r in by_rule(prod_results, "ISO-01")] == ["PASS"], "dev's catalog is bound to dev only"
    sto = {r["object"]: r["status"] for r in by_rule(prod_results, "STO-02")}
    assert sto == {"ext_loc_dbx_architect_lab_prod": "PASS", "ext_loc_dbx_architect_lab_dev": "FAIL"}
    assert [r["status"] for r in by_rule(prod_results, "STO-01")] == ["PASS"]


def test_a_catalog_of_another_environment_reachable_here_fails():
    call, _ = fake_api({**PROD_API, "/api/2.1/unity-catalog/bindings/catalog/dbxarchitectlab_dev": {
        "bindings": [{"workspace_id": 7405609089943919}, {"workspace_id": 7405604784566671}]}})
    res = rules.evaluate(CFG, collect.snapshot(call, CFG, "prod"), TODAY)
    fail = by_rule(res, "ISO-01", "FAIL")
    assert [r["object"] for r in fail] == ["dbxarchitectlab_dev"] and "dev's catalog" in fail[0]["evidence"]


def test_compute_and_jobs(prod_results):
    assert {r["object"]: r["status"] for r in by_rule(prod_results, "CMP-01")} == {
        "shared-analytics": "PASS", "jane-quick-test": "FAIL"}
    assert {r["object"]: r["status"] for r in by_rule(prod_results, "CMP-02")} == {
        "shared-analytics": "PASS", "jane-quick-test": "FAIL"}
    assert {r["object"]: r["status"] for r in by_rule(prod_results, "CMP-03")} == {
        "shared-analytics": "PASS", "jane-quick-test": "FAIL", "sql-wh-small": "PASS"}
    assert {r["object"]: r["status"] for r in by_rule(prod_results, "JOB-01")} == {
        "silver": "PASS", "jane-adhoc": "FAIL"}
    assert [r["status"] for r in by_rule(prod_results, "WSC-01")] == ["PASS"]


def test_tiers_decide_which_rules_apply(prod_snapshot):
    dev = rules.evaluate(CFG, {**prod_snapshot, "environment": "dev", "workspace_id": 7405609089943919}, TODAY)
    assert not by_rule(dev, "JOB-01"), "prod only"
    assert by_rule(dev, "IAM-01", "FAIL")[0]["severity"] == "medium"


def test_every_rule_has_a_unique_id_and_valid_severities():
    ids = [r.id for r in rules.RULES]
    assert len(ids) == len(set(ids))
    assert all(s in rules.SEVERITIES for r in rules.RULES for s in r.severity.values())


# ---------------------------------------------------------------- the job's build step and the report
def test_build_scores_every_workspace_and_a_missing_snapshot_is_not_compliant(prod_snapshot):
    inventory, results, scores, reports = validate.build(CFG, "42", datetime(2026, 11, 7, tzinfo=timezone.utc),
                                                    [prod_snapshot])
    assert {s["environment"]: s["compliant"] for s in scores} == {"dev": False, "prod": False}
    dev = next(s for s in scores if s["environment"] == "dev")
    assert dev["errors"] == 1 and dev["checks"] == 1
    prod = next(s for s in scores if s["environment"] == "prod")
    assert prod["worst"] == "high" and prod["owner"] == ADMIN
    assert prod["passed"] + prod["failed"] + prod["exceptions"] + prod["errors"] == prod["checks"]
    assert len(inventory) == len(prod_snapshot["observations"])
    assert set(results[0]) >= set(validate.RESULTS_COLUMNS)
    assert set(inventory[0]) == set(validate.INVENTORY_COLUMNS)
    md = reports["prod"]
    assert md.startswith("### prod: 🔴") and "REVOKE SELECT, USE SCHEMA" in md and "```sql" in md
    assert md.index("| high |") < md.index("| medium |") < md.index("| low |"), "worst first"
    assert report.summary(scores)["environments"][1]["worst"] == "high"


def test_a_clean_workspace_reports_compliant():
    snap = {"environment": "prod", "workspace_id": 7405604784566671, "collected_at": "2026-11-07T06:30:00+00:00",
            "errors": [], "observations": [
                {"object_type": "catalog", "name": "dbxarchitectlab_prod", "owner": INFRA_SP,
                 "attrs": {"isolation_mode": "ISOLATED", "accessible_here": True, "storage_root": PROD_ROOT}},
                {"object_type": "workspace_setting", "name": "enableDbfsFileBrowser", "owner": None,
                 "attrs": {"value": "false"}}]}
    res = rules.evaluate(CFG, snap, TODAY)
    score = rules.compliance("prod", res)
    assert score["compliant"] and score["score"] == 100.0
    assert "Every check passed" in report.markdown({**score}, res)


def test_the_job_runs_the_notebook_and_the_notebook_uses_the_library():
    job = yaml.safe_load((ROOT / "resources" / "governance.yml").read_text(encoding="utf-8"))["resources"]["jobs"]
    task = job["workspace_governance"]["tasks"][0]["notebook_task"]
    nb = ROOT / "resources" / task["notebook_path"]
    text = nb.resolve().read_text(encoding="utf-8")
    assert text.startswith("# Databricks notebook source"), "deployed as a notebook, not a file"
    assert "validate.build(" in text and 'dbutils.widgets.text("run_id"' in text
    assert [p["name"] for p in job["workspace_governance"]["parameters"]] == ["run_id"]
