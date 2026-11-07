"""Plan JSON to findings: drift told apart from code that was never applied, and no values leak."""
import json

from drift import cli, config, plan

STACK = config.Stack("prod", "workspace-bootstrap", "live/prod/workspace-bootstrap", 1, "high")

POLICY = "module.cluster_policy.databricks_cluster_policy.this[\"shared_compute_customize\"]"
GRANTS = "module.unity_catalog.databricks_grants.catalog[0]"
SCOPE = "module.secret_scope.databricks_secret_scope.this"
NEW = "module.sql_warehouse.databricks_sql_endpoint.this[\"reporting\"]"

PLAN = {
    "resource_drift": [
        {"address": POLICY, "type": "databricks_cluster_policy",
         "change": {"actions": ["update"], "before": {"definition": "{max 1}", "name": "p"},
                    "after": {"definition": "{max 8}", "name": "p"}}},
        {"address": GRANTS, "type": "databricks_grants",
         "change": {"actions": ["update"], "before": {"grant": [1]}, "after": {"grant": [1, 2]}}},
        {"address": SCOPE, "type": "databricks_secret_scope",
         "change": {"actions": ["delete"], "before": {"name": "s"}, "after": None}},
    ],
    "resource_changes": [
        {"address": POLICY, "type": "databricks_cluster_policy",
         "change": {"actions": ["update"], "before": {"definition": "{max 8}"}, "after": {"definition": "{max 1}"}}},
        {"address": GRANTS, "type": "databricks_grants",
         "change": {"actions": ["update"], "before": {"grant": [1, 2]}, "after": {"grant": [1]}}},
        {"address": SCOPE, "type": "databricks_secret_scope",
         "change": {"actions": ["create"], "before": None, "after": {"name": "s"}}},
        {"address": NEW, "type": "databricks_sql_endpoint",
         "change": {"actions": ["create"], "before": None, "after": {"name": "reporting", "cluster_size": "2X-Small"}}},
        {"address": "module.catalog.databricks_catalog.this", "type": "databricks_catalog",
         "change": {"actions": ["no-op"], "before": {}, "after": {}}},
    ],
}


def by_address(found):
    return {f["address"]: f for f in found}


def test_each_change_gets_its_story():
    found = by_address(plan.findings(PLAN))
    assert found[POLICY]["category"] == plan.CHANGED_OUTSIDE
    assert found[POLICY]["attributes"] == ["definition"]
    assert found[GRANTS]["category"] == plan.CHANGED_OUTSIDE
    assert found[SCOPE]["category"] == plan.DELETED_OUTSIDE
    assert found[SCOPE]["planned_action"] == "create"
    assert found[NEW]["category"] == plan.NOT_APPLIED
    assert "module.catalog.databricks_catalog.this" not in found


def test_status_from_exit_code_and_findings():
    assert plan.status(0, []) == "clean"
    assert plan.status(1, []) == "error"
    assert plan.status(2, plan.findings(PLAN)) == "drift"
    only_new = {"resource_changes": [PLAN["resource_changes"][3]]}
    assert plan.status(2, plan.findings(only_new)) == "not_applied"


def test_remediation_is_revert_or_adopt():
    found = by_address(plan.findings(PLAN))
    assert plan.remediation(STACK.path, found[POLICY]).startswith("Revert: apply live/prod/workspace-bootstrap")
    assert "Or adopt" in plan.remediation(STACK.path, found[POLICY])
    assert plan.remediation(STACK.path, found[NEW]).startswith("Deploy:")


def test_report_keeps_names_and_drops_values():
    rep = plan.report(STACK, 2, PLAN, {"github_run_id": "42"})
    assert rep["status"] == "drift" and rep["severity"] == "high" and rep["github_run_id"] == "42"
    text = json.dumps(rep)
    assert "{max 8}" not in text and "2X-Small" not in text


def test_cli_report_writes_json_and_a_summary(tmp_path, capsys):
    from conftest import ROOT
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(PLAN), encoding="utf-8")
    out = tmp_path / "findings" / "prod-workspace-bootstrap.json"
    cli.main(["report", "--config", str(ROOT / "config" / "drift.yml"), "--key", "prod-workspace-bootstrap",
              "--exit-code", "2", "--plan", str(plan_file), "--out", str(out)])
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "drift"
    assert "prod / workspace-bootstrap: drift (exit code 2)" in capsys.readouterr().out


def test_cli_report_for_a_clean_plan_has_no_plan_file(tmp_path, capsys):
    from conftest import ROOT
    out = tmp_path / "dev-workspace.json"
    cli.main(["report", "--config", str(ROOT / "config" / "drift.yml"), "--key", "dev-workspace",
              "--exit-code", "0", "--plan", str(tmp_path / "missing.json"), "--out", str(out)])
    assert json.loads(out.read_text(encoding="utf-8"))["findings"] == []
    assert "Code and reality agree." in capsys.readouterr().out
