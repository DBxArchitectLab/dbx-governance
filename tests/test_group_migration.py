"""Unit tests for dbx_governance.identity.group_migration, with in-memory fakes of the account and workspace APIs."""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from databricks.sdk.service import iam

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dbx_governance.identity import group_migration as gm  # noqa: E402

WS = gm.Workspace("dev", 111, "https://adb-111.1.azuredatabricks.net")


def group(gid, name, local=True, members=(), entitlements=()):
    return iam.Group(id=gid, display_name=name,
                     meta=iam.ResourceMeta(resource_type="WorkspaceGroup" if local else "Group"),
                     members=[iam.ComplexValue(ref=r, value=v, display=d) for r, v, d in members],
                     entitlements=[iam.ComplexValue(value=e) for e in entitlements])


class FakeWorkspace:
    def __init__(self, groups):
        self.calls = []
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

        def finder(table, attr):
            def list_(filter, **_):
                value = filter.split('"')[1]
                return iter([NS(id=table[value])] if value in table else [])
            return list_

        self.groups = NS(list=finder(self.store, "displayName"), create=self._create,
                         get=lambda gid: NS(members=[]),
                         patch=lambda gid, **kw: self.calls.append(("acct.patch", gid,
                                                                   len(kw["operations"][0].value))))
        self.users = NS(list=finder(users, "userName"))
        self.service_principals = NS(list=finder(sps, "applicationId"))
        self.workspace_assignment = NS(update=lambda ws, pid, permissions: self.calls.append(("assign", ws, pid)))

    def _create(self, display_name):
        gid = str(900 + len(self.store))
        self.store[display_name] = gid
        self.calls.append(("create", display_name))
        return NS(id=gid)


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
    plans = {p.local_name: p for p in gm.inventory(w, WS, "{group}_{env}", set(), "db-temp-")}
    assert set(plans) == {"data_engineers", "analysts", "db-temp-legacy"}
    de = plans["data_engineers"]
    assert de.target_name == "data_engineers_dev"
    assert de.members == [gm.Member("user", "user7@example.com"), gm.Member("service_principal", "app-8"),
                          gm.Member("group", "analysts", local_group=True), gm.Member("group", "acct_group")]
    assert de.entitlements == ["databricks-sql-access", "workspace-access"]
    assert plans["db-temp-legacy"].target_name == "legacy_dev"  # renamed by an earlier run


def test_conflict_when_one_account_group_would_merge_different_members():
    dev = gm.GroupPlan("dev", 1, "eng", "1", "eng", members=[gm.Member("user", "a")])
    prod = gm.GroupPlan("prod", 2, "eng", "2", "eng", members=[gm.Member("user", "b")])
    gm.check_conflicts([dev, prod])
    assert dev.problems and prod.problems
    same_dev = gm.GroupPlan("dev", 1, "eng", "1", "eng", members=[gm.Member("user", "a")])
    same_prod = gm.GroupPlan("prod", 2, "eng", "2", "eng", members=[gm.Member("user", "a")])
    gm.check_conflicts([same_dev, same_prod])
    assert not same_dev.problems and not same_prod.problems


def test_nested_local_groups_come_first_and_cycles_are_reported():
    parent = gm.GroupPlan("dev", 1, "parent", "1", "parent_dev", members=[gm.Member("group", "child", True)])
    child = gm.GroupPlan("dev", 1, "child", "2", "child_dev")
    assert [p.local_name for p in gm.dependency_order([parent, child])] == ["child", "parent"]
    x = gm.GroupPlan("dev", 1, "x", "1", "x_dev", members=[gm.Member("group", "y", True)])
    y = gm.GroupPlan("dev", 1, "y", "2", "y_dev", members=[gm.Member("group", "x", True)])
    gm.dependency_order([x, y])
    assert any("cycle" in p for p in x.problems + y.problems)


def test_plan_mode_changes_nothing():
    w, a = FakeWorkspace([]), FakeAccount(users={"u@x.com": "50"})
    p = gm.GroupPlan("dev", 111, "eng", "3", "eng", members=[gm.Member("user", "u@x.com")],
                     entitlements=["workspace-access"])
    gm.apply(a, w, p, gm.AccountIndex(a), {}, dry_run=True, delete_local_groups=True, temp_prefix="db-temp-")
    assert a.calls == [] and w.calls == []
    assert p.actions[0] == "create account group eng"
    assert "rename local group eng to db-temp-eng" in p.actions
    assert p.actions[-1] == "delete local group db-temp-eng"


def test_apply_runs_the_steps_in_order():
    w = FakeWorkspace([])
    a = FakeAccount(groups={"child_dev": "70"}, users={"u@x.com": "50"}, sps={"app-1": "60"})
    p = gm.GroupPlan("dev", 111, "eng", "3", "eng_dev",
                     members=[gm.Member("user", "u@x.com"), gm.Member("service_principal", "app-1"),
                              gm.Member("group", "child", local_group=True)],
                     entitlements=["workspace-access"])
    gm.apply(a, w, p, gm.AccountIndex(a), {"child": "child_dev"}, dry_run=False, delete_local_groups=False,
             temp_prefix="db-temp-")
    gid = a.store["eng_dev"]
    assert a.calls == [("create", "eng_dev"), ("acct.patch", gid, 3), ("assign", 111, int(gid))]
    assert w.calls == [("ws.patch", gid, "entitlements"), ("migrate", "eng", "eng_dev"),
                       ("migrate", "eng", "eng_dev"), ("migrate", "eng", "eng_dev")]
    assert "  5 permission(s) migrated" in p.actions
    assert not any(c[0] == "ws.delete" for c in w.calls)


def test_missing_account_identity_is_reported_not_added():
    w, a = FakeWorkspace([]), FakeAccount(groups={"eng_dev": "80"})
    p = gm.GroupPlan("dev", 111, "eng", "3", "eng_dev", members=[gm.Member("user", "ghost@x.com")])
    gm.apply(a, w, p, gm.AccountIndex(a), {}, dry_run=False, delete_local_groups=False, temp_prefix="db-temp-")
    assert p.problems == ["user 'ghost@x.com' not found at the account; not added"]
    assert ("create", "eng_dev") not in a.calls


@pytest.mark.parametrize("template,expected", [("{group}_{env}", "eng_prod"), ("{group}", "eng"),
                                               ("{env}-{group}", "prod-eng")])
def test_target_name(template, expected):
    assert gm.target_name(template, "prod", "eng") == expected
