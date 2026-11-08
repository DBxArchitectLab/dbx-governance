"""Workspace governance: config, the four collectors, rules and the build step, without a workspace."""
import copy
from datetime import date, datetime, timezone

import pytest
import yaml
from conftest import ROOT

from govern import account, collect, config, report, rules, systables, validate

CFG = config.load(ROOT / "config" / "governance.yml")
TODAY = date(2026, 11, 7)
NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)
ADMIN = "DBX_Architect_Lab_Admin"
INFRA_SP = "6eab1957-6c50-4f47-964b-c89be78ab1f1"
LAKEHOUSE_SP = "9a769c8e-a44b-4009-9378-719753df14c7"
PROD_ROOT = "abfss://dbx-architect-lab-prod@stdbxarchlabprod.dfs.core.windows.net/"
DEV_WS, PROD_WS, GOV_WS = 7405609089943919, 7405604784566671, 7405610301840934


# ---------------------------------------------------------------- config
def test_the_real_config_governs_dev_and_prod_and_the_account():
    assert [m["environment"] for m in config.matrix(CFG)["include"]] == ["dev", "prod"]
    assert config.matrix(CFG)["include"][1] == {"environment": "prod", "workspace_id": PROD_WS,
                                                "host": "https://adb-7405604784566671.11.azuredatabricks.net",
                                                "tier": "prod"}
    assert [m["environment"] for m in config.scopes(CFG)["include"]] == ["dev", "prod", "account"]


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
    (lambda c: c["estate"]["prod"].pop("sql_warehouse"), "sql_warehouse"),
    (lambda c: c["exceptions"][0].pop("expires"), "time-bound"),
    (lambda c: c["exceptions"][0].update(environment="qa"), "not in the estate"),
    (lambda c: c["standard"].update(schema_name_pattern="(["), "regular expression"),
    (lambda c: c["account"]["not_governed"].append({"workspace_id": PROD_WS, "reason": "x"}), "is in the estate"),
    (lambda c: c["system_tables"].update(lookback_days=0), "lookback_days"),
])
def test_a_typo_fails_with_the_field(change, message):
    cfg = copy.deepcopy(CFG)
    change(cfg)
    with pytest.raises(config.ConfigError, match=message):
        config.validate(cfg)


# ---------------------------------------------------------------- workspace API + information schema
TABLES_SQL_ROWS = [["sales", "orders", "MANAGED", LAKEHOUSE_SP], ["sales", "v_orders", "VIEW", "jane@contoso.com"]]
PRIVS_SQL_ROWS = [["jane@contoso.com", "sales", "orders", "SELECT"],        # inherited from the schema grant
                  [ADMIN, "sales", "orders", "ALL PRIVILEGES"],             # inherited from the catalog grant
                  ["analysts-adhoc", "sales", "orders", "MODIFY"]]          # granted on the table itself


def statement(body):
    cols, rows = ((["table_schema", "table_name", "table_type", "table_owner"], TABLES_SQL_ROWS)
                  if "information_schema.tables" in body["statement"]
                  else (["grantee", "table_schema", "table_name", "privilege_type"], PRIVS_SQL_ROWS))
    assert body["statement"].lstrip().upper().startswith("SELECT"), "collectors only read"
    assert body["warehouse_id"] == "w1"
    return {"statement_id": "s1", "status": {"state": "SUCCEEDED"},
            "manifest": {"schema": {"columns": [{"name": c} for c in cols]}}, "result": {"data_array": rows}}


def fake_api(responses):
    calls = []

    def call(method, path, query=None, body=None):
        calls.append((method, path, query))
        if method == "POST" and path == "/api/2.0/sql/statements":
            return statement(body)
        assert method == "GET", "collectors only read"
        r = responses.get(path, {})
        return r(query) if callable(r) else r
    return call, calls


PROD_API = {
    "/api/2.1/unity-catalog/catalogs": {"catalogs": [
        {"name": "dbxarchitectlab_prod", "owner": INFRA_SP, "isolation_mode": "ISOLATED",
         "storage_root": PROD_ROOT + "managed/dbxarchitectlab_prod/"},
        {"name": "dbxarchitectlab_dev", "owner": INFRA_SP, "isolation_mode": "ISOLATED"},
        {"name": "system", "owner": "System user"}]},
    "/api/2.1/unity-catalog/bindings/catalog/dbxarchitectlab_prod": {"bindings": [{"workspace_id": PROD_WS}]},
    "/api/2.1/unity-catalog/bindings/catalog/dbxarchitectlab_dev": {"bindings": [{"workspace_id": DEV_WS}]},
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
        {"workspace_id": DEV_WS}]},
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
    return collect.snapshot(call, CFG, "prod", now=NOW)


def test_the_collector_normalizes_one_workspace_from_two_sources(prod_snapshot):
    obs = prod_snapshot["observations"]
    assert prod_snapshot["errors"] == []
    by_source = {}
    for o in obs:
        by_source.setdefault(o["source"], set()).add(o["object_type"])
    assert by_source == {
        "workspace_api": {"catalog", "grant", "schema", "external_location", "cluster", "cluster_policy",
                          "warehouse", "job", "workspace_setting"},
        "information_schema": {"table", "grant"}}
    dev_cat = next(o for o in obs if o["name"] == "dbxarchitectlab_dev")
    assert dev_cat["attrs"]["accessible_here"] is False
    assert not any(o["name"].endswith("information_schema") for o in obs), "system schemas are skipped"
    assert not any(o["name"] == "ext_loc_bound_to_dev" for o in obs), "bound to another workspace: not usable here"
    assert [o["name"] for o in obs if o["object_type"] == "cluster"] == ["shared-analytics", "jane-quick-test"]
    assert [o["attrs"]["run_as"] for o in obs if o["object_type"] == "job"] == [LAKEHOUSE_SP, "jane@contoso.com"]


def test_table_grants_keep_only_what_was_granted_on_the_table(prod_snapshot):
    table_grants = [o["attrs"] for o in prod_snapshot["observations"]
                    if o["object_type"] == "grant" and o["attrs"]["securable_type"] == "table"]
    assert table_grants == [{"securable_type": "table", "securable": "dbxarchitectlab_prod.sales.orders",
                             "principal": "analysts-adhoc", "privileges": ["MODIFY"]}]


def test_a_statement_that_waits_for_the_warehouse_is_polled():
    states = iter(["RUNNING", "SUCCEEDED"])

    def call(method, path, query=None, body=None):
        if method == "POST":
            return {"statement_id": "s9", "status": {"state": "PENDING"}}
        state = next(states)
        return {"statement_id": "s9", "status": {"state": state},
                "manifest": {"schema": {"columns": [{"name": "a"}]}}, "result": {"data_array": [["1"]]}}
    slept = []
    assert collect.run_sql(call, "w1", "SELECT 1", sleep=slept.append) == [{"a": "1"}]
    assert slept == [5, 5]


def test_one_failing_domain_is_an_error_not_a_pass():
    good, _ = fake_api(PROD_API)

    def call(method, path, query=None, body=None):
        if path == "/api/2.0/workspace-conf":
            raise PermissionError("403: only admins")
        return good(method, path, query, body)
    snap = collect.snapshot(call, CFG, "prod")
    assert snap["errors"] == [{"domain": "settings", "source": "workspace_api",
                               "error": "PermissionError: 403: only admins"}]
    res = rules.evaluate(CFG, snap, TODAY)
    assert [(r["status"], r["source"]) for r in res if r["rule_id"] == "COL-00"] == [("ERROR", "workspace_api")]
    assert not rules.compliance("prod", res)["compliant"]


def test_a_missing_warehouse_is_an_information_schema_error():
    call, _ = fake_api({**PROD_API, "/api/2.0/sql/warehouses": {"warehouses": []}})
    snap = collect.snapshot(call, CFG, "prod")
    assert [(e["domain"], e["source"]) for e in snap["errors"]] == [("information_schema", "information_schema")]


# ---------------------------------------------------------------- account API
ACCOUNT_ID = "acc-1"
ACCOUNT_API = {
    f"/api/2.0/accounts/{ACCOUNT_ID}/workspaces": [{"workspace_id": DEV_WS, "workspace_name": "dbw-dev"},
                                                   {"workspace_id": 111, "workspace_name": "dbw-dbx-architect-lab-uat"}],
    f"/api/2.0/accounts/{ACCOUNT_ID}/metastores": {"metastores": [{"metastore_id": "m1", "name": "metastore-centralus"}]},
    f"/api/2.0/accounts/{ACCOUNT_ID}/metastores/m1/workspaces": {"workspace_ids": [DEV_WS, PROD_WS, GOV_WS, 111]},
    f"/api/2.0/accounts/{ACCOUNT_ID}/workspaces/{DEV_WS}/permissionassignments": {"permission_assignments": [
        {"principal": {"group_name": ADMIN}, "permissions": ["ADMIN"]}]},
    f"/api/2.0/accounts/{ACCOUNT_ID}/workspaces/{PROD_WS}/permissionassignments": {"permission_assignments": [
        {"principal": {"group_name": ADMIN}, "permissions": ["ADMIN"]},
        {"principal": {"service_principal_name": LAKEHOUSE_SP}, "permissions": ["ADMIN"]},
        {"principal": {"user_name": "jane@contoso.com"}, "permissions": ["ADMIN"]},
        {"principal": {"user_name": "bob@contoso.com"}, "permissions": ["USER"]}]},
}


@pytest.fixture(scope="module")
def account_snapshot():
    call, _ = fake_api(ACCOUNT_API)
    return account.snapshot(call, CFG, ACCOUNT_ID, now=NOW)


def test_the_account_collector(account_snapshot):
    assert account_snapshot["errors"] == []
    assigned = [o["attrs"] for o in account_snapshot["observations"] if o["object_type"] == "metastore_assignment"]
    assert [a["workspace_id"] for a in assigned] == [DEV_WS, PROD_WS, GOV_WS, 111]
    assert assigned[3]["workspace_name"] == "dbw-dbx-architect-lab-uat"
    prod = [o["attrs"] for o in account_snapshot["observations"]
            if o["object_type"] == "workspace_assignment" and o["attrs"]["workspace_id"] == PROD_WS]
    assert [(a["principal"], a["principal_type"]) for a in prod] == [
        (ADMIN, "group"), (LAKEHOUSE_SP, "service_principal"), ("jane@contoso.com", "user"),
        ("bob@contoso.com", "user")]


def test_the_estate_rules_find_an_ungoverned_workspace(account_snapshot):
    merged = validate.merge(CFG, NOW, [account_snapshot])
    res = rules.evaluate(CFG, merged["account"], TODAY)
    est1 = {r["object"]: r["status"] for r in res if r["rule_id"] == "EST-01"}
    assert est1 == {"dbw-dev": "PASS", "7405604784566671": "PASS", "7405610301840934": "PASS",
                    "dbw-dbx-architect-lab-uat": "FAIL"}
    assert {r["object"]: r["status"] for r in res if r["rule_id"] == "EST-02"} == {"dev": "PASS", "prod": "PASS"}
    assert {r["source"] for r in res} == {"account_api"} and {r["owner"] for r in res} == {ADMIN}


# ---------------------------------------------------------------- system tables
LINEAGE_ROWS = [{"source_table_catalog": "dbxarchitectlab_dev", "target_table_catalog": "dbxarchitectlab_prod",
                 "source_table_full_name": "dbxarchitectlab_dev.sales.raw", "target_table_full_name":
                 "dbxarchitectlab_prod.sales.orders", "created_by": "jane@contoso.com",
                 "last_seen": "2026-11-06 10:00:00", "events": 3}]
JOB_CLUSTER_ROWS = [{"workspace_id": str(PROD_WS), "job_id": "42", "runs": 5, "without_policy": 2,
                     "owned_by": "jane@contoso.com"}]


def test_the_system_table_queries_cover_the_estate_only():
    q = systables.queries(CFG)
    assert "system.access.table_lineage" in q["lineage"] and "'dbxarchitectlab_prod'" in q["lineage"]
    assert "system.compute.clusters" in q["job_clusters"] and f"'{PROD_WS}'" in q["job_clusters"]
    assert "date_sub(current_date(), 7)" in q["lineage"]


def test_the_system_tables_collector():
    rows = {"lineage": LINEAGE_ROWS, "job_clusters": JOB_CLUSTER_ROWS}
    out = systables.collect(lambda sql: rows["lineage" if "table_lineage" in sql else "job_clusters"], CFG)
    prod = out["prod"]["observations"]
    assert {o["object_type"] for o in prod} == {"coverage", "lineage_flow", "job_cluster"}
    assert [o["object_type"] for o in out["dev"]["observations"]] == ["coverage", "coverage"]
    res = rules.evaluate(CFG, {"environment": "prod", "workspace_id": PROD_WS, "observations": prod, "errors": []},
                         TODAY)
    iso = [r for r in res if r["rule_id"] == "ISO-02"]
    assert [(r["status"], r["severity"], r["source"]) for r in iso] == [("FAIL", "critical", "system_tables")]
    assert "dbxarchitectlab_dev.sales.raw (dev) → dbxarchitectlab_prod.sales.orders" in iso[0]["evidence"]
    assert [(r["object"], r["status"]) for r in res if r["rule_id"] == "CMP-04"] == [("job 42", "FAIL")]
    dev = rules.evaluate(CFG, {"environment": "dev", "workspace_id": DEV_WS,
                               "observations": out["dev"]["observations"], "errors": []}, TODAY)
    assert [(r["rule_id"], r["status"]) for r in dev if r["source"] == "system_tables"] == [
        ("ISO-02", "PASS"), ("CMP-04", "PASS")], "nothing found, and the query ran: a pass"


def test_no_coverage_means_no_pass():
    def fail(sql):
        raise PermissionError("no SELECT on the system schema")
    out = systables.collect(fail, CFG)
    res = rules.evaluate(CFG, {"environment": "prod", "workspace_id": PROD_WS, **out["prod"]}, TODAY)
    mine = [r for r in res if r["source"] == "system_tables"]
    assert {(r["rule_id"], r["status"]) for r in mine} == {("COL-00", "ERROR")}, "no PASS without coverage"
    assert [r["object"] for r in mine] == ["system_tables.lineage", "system_tables.job_clusters"]


# ---------------------------------------------------------------- rules on the merged workspace evidence
def by_rule(results, rule_id, status=None):
    return [r for r in results if r["rule_id"] == rule_id and (status is None or r["status"] == status)]


@pytest.fixture(scope="module")
def prod_results(prod_snapshot, account_snapshot):
    merged = validate.merge(CFG, NOW, [prod_snapshot, account_snapshot])
    return rules.evaluate(CFG, merged["prod"], TODAY)


def test_a_direct_user_grant_fails_with_the_sql_that_fixes_it(prod_results):
    jane = by_rule(prod_results, "IAM-01", "FAIL")
    assert [r["object"] for r in jane] == ["dbxarchitectlab_prod.sales"]
    assert jane[0]["severity"] == "high"
    assert jane[0]["remediation_sql"].splitlines()[1:] == [
        f"GRANT SELECT, USE SCHEMA ON SCHEMA `dbxarchitectlab_prod`.`sales` TO `{ADMIN}`;",
        "REVOKE SELECT, USE SCHEMA ON SCHEMA `dbxarchitectlab_prod`.`sales` FROM `jane@contoso.com`;"]


def test_the_information_schema_extends_the_grant_rules_to_tables(prod_results):
    t = [r for r in by_rule(prod_results, "IAM-02", "FAIL") if r["source"] == "information_schema"]
    assert [(r["object"], r["principal"]) for r in t] == [("dbxarchitectlab_prod.sales.orders", "analysts-adhoc")]
    assert t[0]["remediation_sql"] == \
        "REVOKE MODIFY ON TABLE `dbxarchitectlab_prod`.`sales`.`orders` FROM `analysts-adhoc`;"


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
    assert risky[1]["severity"] == "critical", "one exception covers all of the lab owner's rules"


def test_workspace_admins_come_from_the_account(prod_results):
    adm = {r["principal"]: r["status"] for r in by_rule(prod_results, "IAM-04")}
    assert adm == {ADMIN: "PASS", LAKEHOUSE_SP: "PASS", "jane@contoso.com": "FAIL"}, "USER assignments are not judged"
    assert {r["source"] for r in by_rule(prod_results, "IAM-04")} == {"account_api"}


def test_objects_owned_by_a_person_fail_and_are_handed_to_the_group(prod_results):
    owned = {r["object"]: r for r in by_rule(prod_results, "OWN-01", "FAIL")}
    assert set(owned) == {"dbxarchitectlab_prod.Adhoc-Sales", "dbxarchitectlab_prod.sales.v_orders"}
    assert owned["dbxarchitectlab_prod.Adhoc-Sales"]["remediation_sql"] == \
        f"ALTER SCHEMA `dbxarchitectlab_prod`.`Adhoc-Sales` OWNER TO `{ADMIN}`;"
    assert owned["dbxarchitectlab_prod.sales.v_orders"]["remediation_sql"] == \
        f"ALTER VIEW `dbxarchitectlab_prod`.`sales`.`v_orders` OWNER TO `{ADMIN}`;"
    assert owned["dbxarchitectlab_prod.sales.v_orders"]["source"] == "information_schema"
    assert [r["object"] for r in by_rule(prod_results, "NAM-01", "FAIL")] == ["dbxarchitectlab_prod.Adhoc-Sales"]


def test_isolation_and_storage(prod_results):
    assert [r["status"] for r in by_rule(prod_results, "ISO-01")] == ["PASS"], "dev's catalog is bound to dev only"
    sto = {r["object"]: r["status"] for r in by_rule(prod_results, "STO-02")}
    assert sto == {"ext_loc_dbx_architect_lab_prod": "PASS", "ext_loc_dbx_architect_lab_dev": "FAIL"}
    assert [r["status"] for r in by_rule(prod_results, "STO-01")] == ["PASS"]


def test_a_catalog_of_another_environment_reachable_here_fails():
    call, _ = fake_api({**PROD_API, "/api/2.1/unity-catalog/bindings/catalog/dbxarchitectlab_dev": {
        "bindings": [{"workspace_id": DEV_WS}, {"workspace_id": PROD_WS}]}})
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
    dev = rules.evaluate(CFG, {**prod_snapshot, "environment": "dev", "workspace_id": DEV_WS}, TODAY)
    assert not by_rule(dev, "JOB-01"), "prod only"
    assert by_rule(dev, "IAM-01", "FAIL")[0]["severity"] == "medium"


def test_every_rule_has_a_unique_id_valid_severities_a_source_and_a_scope():
    ids = [r.id for r in rules.RULES]
    assert len(ids) == len(set(ids)) == 18
    assert all(s in rules.SEVERITIES for r in rules.RULES for s in r.severity.values())
    assert {r.source for r in rules.RULES} == {"workspace_api", "account_api", "system_tables"}
    assert {r.scope for r in rules.RULES} == {"workspace", "account"}


# ---------------------------------------------------------------- the build step and the report
def test_build_merges_the_four_sources_and_scores_every_scope(prod_snapshot, account_snapshot):
    rows = {"lineage": LINEAGE_ROWS, "job_clusters": []}
    system = systables.collect(lambda sql: rows["lineage" if "table_lineage" in sql else "job_clusters"], CFG)
    inventory, results, scores, reports = validate.build(CFG, "42", NOW, [prod_snapshot, account_snapshot], system)
    by = {s["environment"]: s for s in scores}
    assert set(by) == {"dev", "prod", "account"}
    assert by["dev"]["errors"] == 1 and by["dev"]["checks"] == 1, "no snapshot: only the error"
    assert not by["account"]["compliant"], "uat is on the metastore but not in the estate"
    assert by["prod"]["worst"] == "critical" and by["prod"]["owner"] == ADMIN, "ISO-02: dev data into prod"
    assert by["prod"]["passed"] + by["prod"]["failed"] + by["prod"]["exceptions"] + by["prod"]["errors"] == \
        by["prod"]["checks"]
    assert {r["source"] for r in results if r["environment"] == "prod"} == set(rules.SOURCES)
    assert {i["source"] for i in inventory} == set(rules.SOURCES)
    assert all(set(r) >= set(validate.RESULTS_COLUMNS) for r in results)
    assert all(set(i) == set(validate.INVENTORY_COLUMNS) for i in inventory)
    md = reports["prod"]
    assert md.startswith("### prod: 🔴") and "REVOKE SELECT, USE SCHEMA" in md and "```sql" in md
    for label in ("workspace API", "information schema", "account API", "system tables"):
        assert f"| {label} |" in md
    assert md.index("| critical |") < md.index("| high |") < md.index("| medium |") < md.index("| low |")
    assert reports["account"].startswith("### account: 🔴")


def test_without_an_account_snapshot_the_admins_are_an_error(prod_snapshot):
    _, results, scores, _ = validate.build(CFG, "43", NOW, [prod_snapshot])
    prod_errors = [r["object"] for r in results if r["environment"] == "prod" and r["status"] == "ERROR"]
    assert prod_errors == ["account_api"]
    assert next(s for s in scores if s["environment"] == "account")["errors"] == 1


def test_a_clean_workspace_reports_compliant():
    snap = {"environment": "prod", "workspace_id": PROD_WS, "collected_at": "2026-11-07T06:30:00+00:00",
            "errors": [], "observations": [
                {"object_type": "catalog", "name": "dbxarchitectlab_prod", "owner": INFRA_SP, "source": "workspace_api",
                 "attrs": {"isolation_mode": "ISOLATED", "accessible_here": True, "storage_root": PROD_ROOT}},
                {"object_type": "workspace_setting", "name": "enableDbfsFileBrowser", "owner": None,
                 "source": "workspace_api", "attrs": {"value": "false"}}]}
    res = rules.evaluate(CFG, snap, TODAY)
    score = rules.compliance("prod", res)
    assert score["compliant"] and score["score"] == 100.0
    assert "Every check passed" in report.markdown({**score}, res)


def test_the_job_runs_the_notebook_and_the_notebook_uses_the_library():
    job = yaml.safe_load((ROOT / "resources" / "governance.yml").read_text(encoding="utf-8"))["resources"]["jobs"]
    task = job["workspace_governance"]["tasks"][0]["notebook_task"]
    text = (ROOT / "resources" / task["notebook_path"]).resolve().read_text(encoding="utf-8")
    assert text.startswith("# Databricks notebook source"), "deployed as a notebook, not a file"
    assert "validate.build(" in text and "systables.collect(" in text and 'dbutils.widgets.text("run_id"' in text
    assert [p["name"] for p in job["workspace_governance"]["parameters"]] == ["run_id"]


def test_the_workflow_collects_the_account_and_opens_an_account_issue():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "governance.yml").read_text(encoding="utf-8"))
    jobs = wf["jobs"]
    assert "collect-account" in jobs["validate"]["needs"]
    assert "collect-account" in jobs["collect-account"]["steps"][-2]["run"]
    assert jobs["remediate"]["strategy"]["matrix"] == "${{ fromJSON(needs.estate.outputs.scopes) }}"
