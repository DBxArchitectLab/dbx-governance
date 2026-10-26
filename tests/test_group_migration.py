"""Unit tests for the workspace group migration, with in-memory fakes of the account and workspace APIs and Spark."""
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from databricks.sdk.service import iam

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dbx_governance.identity import group_migration as gm  # noqa: E402
from dbx_governance.identity.inventory import Inventory, status  # noqa: E402

TEMPLATE, PREFIX = "{group}_{env}", "db-temp-"


def ws(env, wid):
    return gm.Workspace(env, wid, f"https://adb-{wid}.1.azuredatabricks.net")


def group(gid, name, local=True, members=(), entitlements=()):
    return iam.Group(id=gid, display_name=name,
                     meta=iam.ResourceMeta(resource_type="WorkspaceGroup" if local else "Group"),
                     members=[iam.ComplexValue(ref=r, value=v, display=d) for r, v, d in members],
                     entitlements=[iam.ComplexValue(value=e) for e in entitlements])


class FakeWorkspace:
    def __init__(self, groups=()):
        self.calls = []
        groups = list(groups)
        self.groups = NS(list=lambda **_: iter(groups),
                         patch=lambda gid, **kw: self.calls.append(("ws.patch", gid, kw["operations"][0].path)),
                         delete=lambda gid: self.calls.append(("ws.delete", gid)))
        self.users = NS(get=lambda uid: NS(user_name=f"user{uid}@example.com"))
        self.service_principals = NS(get=lambda sid: NS(application_id=f"app-{sid}"))
        self._batches = [3, 2, 0]
        self.permission_migration = NS(migrate_permissions=self._migrate)

    def _migrate(self, ws_id, frm, to, size):
        self.calls.append(("migrate", frm, to))
        return NS(permissions_migrated=self._batches.pop(0))


class FakeAccount:
    def __init__(self, groups=None, users=None, sps=None):
        self.calls = []
        self.store = dict(groups or {})  # name -> id
        users, sps = users or {}, sps or {}

        def finder(table):
            def list_(filter, **_):
                value = filter.split('"')[1]
                return iter([NS(id=table[value])] if value in table else [])
            return list_

        self.groups = NS(list=finder(self.store), create=self._create, get=lambda gid: NS(members=[]),
                         patch=lambda gid, **kw: self.calls.append(("acct.patch", gid,
                                                                   len(kw["operations"][0].value))))
        self.users = NS(list=finder(users))
        self.service_principals = NS(list=finder(sps))
        self.workspace_assignment = NS(update=lambda w, pid, permissions: self.calls.append(("assign", w, pid)))

    def _create(self, display_name):
        gid = str(900 + len(self.store))
        self.store[display_name] = gid
        self.calls.append(("create", display_name))
        return NS(id=gid)


def run(a, w, p, *, dry_run=False, owned=(), adopt=(), target_of=None, delete=False, on_created=None):
    gm.apply(a, w, p, gm.AccountIndex(a), target_of or {}, dry_run=dry_run, delete_local_groups=delete,
             temp_prefix=PREFIX, owned=set(owned), adopt=set(adopt),
             on_created=on_created or (lambda plan, gid: None))


def steps(p):
    return [x.step for x in p.actions]


# --- naming: one account group per workspace ----------------------------------------------------------------------

@pytest.mark.parametrize("template", ["{group}", "{env}", "data_engineers"])
def test_template_must_contain_group_and_env(template):
    with pytest.raises(ValueError):
        gm.validate_template(template)


def test_template_with_group_and_env_is_accepted():
    gm.validate_template("{group}_{env}")
    gm.validate_template("{env}-{group}")


def test_same_local_group_in_three_workspaces_becomes_three_account_groups():
    plans = []
    for env, wid in (("dev", 1), ("uat", 2), ("prod", 3)):
        w = FakeWorkspace([group("10", "data_engineers")])
        plans += gm.inventory(w, ws(env, wid), TEMPLATE, set(), PREFIX)
    gm.check_conflicts(plans)
    assert [p.target_name for p in plans] == ["data_engineers_dev", "data_engineers_uat", "data_engineers_prod"]
    assert not any(p.problems for p in plans)


def test_two_local_groups_mapping_to_one_account_group_are_blocked():
    a = gm.GroupPlan("dev", 1, "eng", "1", "eng_x")
    b = gm.GroupPlan("uat", 2, "eng", "2", "eng_x")
    gm.check_conflicts([a, b])
    assert a.blocked and b.blocked


# --- existing account groups are never taken over -----------------------------------------------------------------

def test_existing_account_group_not_created_by_the_migration_is_not_reused():
    w, a = FakeWorkspace(), FakeAccount(groups={"eng_dev": "80"})
    p = gm.GroupPlan("dev", 111, "eng", "3", "eng_dev", members=[gm.Member("user", "u@x.com")])
    run(a, w, p)
    assert p.blocked and "already exists and was not created by this migration" in p.problems[0]
    assert a.calls == [] and w.calls == []


@pytest.mark.parametrize("owned,adopt", [({"eng_dev"}, set()), (set(), {"eng_dev"})])
def test_registered_or_adopted_account_group_is_reused(owned, adopt):
    w, a = FakeWorkspace(), FakeAccount(groups={"eng_dev": "80"}, users={"u@x.com": "50"})
    p = gm.GroupPlan("dev", 111, "eng", "3", "eng_dev", members=[gm.Member("user", "u@x.com")])
    run(a, w, p, owned=owned, adopt=adopt)
    assert not p.blocked
    assert "create_group" not in steps(p) and ("assign", 111, 80) in a.calls


def test_a_new_account_group_is_registered_as_soon_as_it_is_created():
    w, a = FakeWorkspace(), FakeAccount()
    created = []
    p = gm.GroupPlan("dev", 111, "eng", "3", "eng_dev")
    run(a, w, p, on_created=lambda plan, gid: created.append((plan.target_name, gid)))
    assert created == [("eng_dev", a.store["eng_dev"])]
    assert p.account_group_id == a.store["eng_dev"]


# --- inventory of a workspace -------------------------------------------------------------------------------------

def test_inventory_keeps_only_local_groups_and_resolves_members():
    w = FakeWorkspace([
        group("1", "admins"),
        group("2", "acct_group", local=False),
        group("3", "data_engineers", members=[("Users/7", "7", "u"), ("ServicePrincipals/8", "8", "sp"),
                                              ("Groups/4", "4", "analysts"), ("Groups/2", "2", "acct_group")],
              entitlements=["workspace-access", "databricks-sql-access"]),
        group("4", "analysts"),
        group("5", "db-temp-legacy"),
    ])
    plans = {p.local_name: p for p in gm.inventory(w, ws("dev", 111), TEMPLATE, set(), PREFIX)}
    assert set(plans) == {"data_engineers", "analysts", "db-temp-legacy"}
    de = plans["data_engineers"]
    assert de.target_name == "data_engineers_dev"
    assert de.members == [gm.Member("user", "user7@example.com"), gm.Member("service_principal", "app-8"),
                          gm.Member("group", "analysts", local_group=True), gm.Member("group", "acct_group")]
    assert de.nested_local_groups == ["analysts"]
    assert de.entitlements == ["databricks-sql-access", "workspace-access"]
    assert plans["db-temp-legacy"].target_name == "legacy_dev"  # renamed by an earlier run


# --- nested local groups ------------------------------------------------------------------------------------------

def test_nested_local_groups_come_first():
    parent = gm.GroupPlan("dev", 1, "parent", "1", "parent_dev", members=[gm.Member("group", "child", True)])
    child = gm.GroupPlan("dev", 1, "child", "2", "child_dev")
    grandchild_parent = gm.GroupPlan("dev", 1, "top", "3", "top_dev", members=[gm.Member("group", "parent", True)])
    order = [p.local_name for p in gm.dependency_order([grandchild_parent, parent, child])]
    assert order == ["child", "parent", "top"]


def test_a_blocked_nested_group_blocks_its_parents():
    child = gm.GroupPlan("dev", 1, "child", "2", "child_dev", problems=["something"])
    parent = gm.GroupPlan("dev", 1, "parent", "1", "parent_dev", members=[gm.Member("group", "child", True)])
    top = gm.GroupPlan("dev", 1, "top", "3", "top_dev", members=[gm.Member("group", "parent", True)])
    gm.dependency_order([top, parent, child])
    assert parent.blocked and "'child' is blocked" in parent.problems[0]
    assert top.blocked and "'parent' is blocked" in top.problems[0]


def test_nested_group_cycle_is_reported():
    x = gm.GroupPlan("dev", 1, "x", "1", "x_dev", members=[gm.Member("group", "y", True)])
    y = gm.GroupPlan("dev", 1, "y", "2", "y_dev", members=[gm.Member("group", "x", True)])
    gm.dependency_order([x, y])
    assert any("cycle" in p for p in x.problems + y.problems)


def test_parent_adds_the_nested_groups_account_group():
    w, a = FakeWorkspace(), FakeAccount(groups={"child_dev": "70"})
    p = gm.GroupPlan("dev", 111, "parent", "3", "parent_dev", members=[gm.Member("group", "child", True)])
    run(a, w, p, owned={"child_dev"}, target_of={"child": "child_dev"})
    assert ("acct.patch", a.store["parent_dev"], 1) in a.calls


# --- the apply steps ----------------------------------------------------------------------------------------------

def test_plan_mode_changes_nothing():
    w, a = FakeWorkspace(), FakeAccount(users={"u@x.com": "50"})
    p = gm.GroupPlan("dev", 111, "eng_dev", "3", "eng_dev", members=[gm.Member("user", "u@x.com")],
                     entitlements=["workspace-access"])
    run(a, w, p, dry_run=True, delete=True)
    assert a.calls == [] and w.calls == []
    assert steps(p) == ["create_group", "add_members", "rename_local", "assign", "entitlements",
                        "migrate_permissions", "delete_local"]


def test_apply_runs_the_steps_in_order():
    w = FakeWorkspace()
    a = FakeAccount(groups={"child_dev": "70"}, users={"u@x.com": "50"}, sps={"app-1": "60"})
    p = gm.GroupPlan("dev", 111, "eng", "3", "eng_dev",
                     members=[gm.Member("user", "u@x.com"), gm.Member("service_principal", "app-1"),
                              gm.Member("group", "child", local_group=True)],
                     entitlements=["workspace-access"])
    run(a, w, p, owned={"child_dev"}, target_of={"child": "child_dev"})
    gid = a.store["eng_dev"]
    assert a.calls == [("create", "eng_dev"), ("acct.patch", gid, 3), ("assign", 111, int(gid))]
    assert w.calls == [("ws.patch", gid, "entitlements"), ("migrate", "eng", "eng_dev"),
                       ("migrate", "eng", "eng_dev"), ("migrate", "eng", "eng_dev")]
    assert p.actions[-1] == gm.Action("migrate_permissions", "eng -> eng_dev: 5 permission(s)")
    assert not any(c[0] == "ws.delete" for c in w.calls)


def test_missing_account_identity_is_reported_not_added():
    w, a = FakeWorkspace(), FakeAccount()
    p = gm.GroupPlan("dev", 111, "eng", "3", "eng_dev", members=[gm.Member("user", "ghost@x.com")])
    run(a, w, p)
    assert p.problems == ["user 'ghost@x.com' not found at the account; not added"]


# --- inventory tables ---------------------------------------------------------------------------------------------

class FakeSpark:
    def __init__(self, registry=()):
        self.sql_calls, self.writes, self.registry = [], {}, list(registry)

    def sql(self, statement):
        self.sql_calls.append(statement)
        return NS(collect=lambda: [NS(account_group=g) for g in self.registry])

    def createDataFrame(self, rows, schema):
        spark = self

        class Writer:
            def mode(self, _):
                return self

            def saveAsTable(self, table):
                spark.writes.setdefault(table.split(".")[-1].strip("`"), []).extend(rows)
        return NS(write=Writer())


def test_inventory_tables_are_created_and_the_registry_is_read():
    spark = FakeSpark(registry=["eng_dev"])
    inv = Inventory(spark, "dbxarchitectlab_governance", "identity")
    inv.ensure_tables()
    assert spark.sql_calls[0] == "CREATE SCHEMA IF NOT EXISTS `dbxarchitectlab_governance`.`identity`"
    assert sum("CREATE TABLE IF NOT EXISTS" in s for s in spark.sql_calls) == 4
    assert inv.owned_account_groups() == {"eng_dev"}


def test_a_run_writes_groups_members_and_actions():
    spark, at = FakeSpark(), datetime(2026, 10, 26, tzinfo=timezone.utc)
    inv = Inventory(spark, "c", "s")
    ok = gm.GroupPlan("dev", 1, "eng", "3", "eng_dev", members=[gm.Member("user", "u"), gm.Member("group", "c", True)],
                      actions=[gm.Action("create_group", "eng_dev"), gm.Action("assign", "eng_dev to 1")])
    bad = gm.GroupPlan("dev", 1, "ops", "4", "ops_dev", problems=["x"])
    inv.write_run([ok, bad], "run-1", at, "apply")
    groups = spark.writes["local_groups"]
    assert [(g[6], g[12]) for g in groups] == [("eng", "migrated"), ("ops", "blocked")]
    assert groups[0][10] == ["c"]  # nested_local_groups
    assert [(m[5], m[6], m[7]) for m in spark.writes["local_group_members"]] == [("user", "u", False),
                                                                                 ("group", "c", True)]
    assert [(r[7], r[8]) for r in spark.writes["migration_actions"]] == [(1, "create_group"), (2, "assign")]
    inv.register(ok, "900", "run-1", at)
    assert spark.writes["account_group_registry"] == [("eng_dev", "900", "dev", 1, "eng", "run-1", at)]


def test_status_values():
    p = gm.GroupPlan("dev", 1, "eng", "3", "eng_dev")
    assert status(p, "plan") == "planned" and status(p, "apply") == "migrated"
    p.actions.append(gm.Action("delete_local", "eng"))
    assert status(p, "apply") == "local_group_deleted"
    p.problems.append("x")
    assert status(p, "apply") == "blocked"
