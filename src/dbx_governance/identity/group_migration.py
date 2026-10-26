"""Migrate workspace-local groups to account-level groups.

Workspace-local groups exist in one workspace only: Unity Catalog can't grant to them, and they can't be shared or
governed from the account. For each workspace-local group in dev, uat and prod, this module:

  1. creates an account group of its own, named by the naming template (which must contain {env}, so the same
     local group name in dev, uat and prod becomes three different account groups);
  2. gives it the same members, resolved to account identities (users by userName, service principals by
     applicationId, nested workspace-local groups by their own account group, migrated first);
  3. if the account group has the same name as the local group, renames the local group to <temp_prefix><name>
     first, because a workspace can't hold two groups with the same name;
  4. assigns the account group to the workspace and copies the local group's entitlements to it;
  5. moves the local group's permissions on workspace objects (clusters, jobs, notebooks, folders, warehouses, ...)
     to the account group with the permission migration API;
  6. deletes the local group, only when delete_local_groups is on (run it after the migrated permissions are
     checked).

An account group that already exists is only reused if this migration created it (it is in the registry) or it is
listed for adoption; anything else stops that group, so an existing account group is never taken over by accident.
Every step is idempotent, so a run can be repeated after a failure. Dry run ("plan") reads only and records what
apply would do.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from databricks.sdk.service import iam

# Built-in groups every workspace has; they are never migrated.
SYSTEM_GROUPS = {"admins", "users", "account users"}
WORKSPACE_GROUP = "WorkspaceGroup"  # meta.resourceType of a workspace-local group; account groups report "Group"
MIGRATION_BATCH = 1000  # permissions moved per permission migration call
SCIM_PATCH = [iam.PatchSchema.URN_IETF_PARAMS_SCIM_API_MESSAGES_2_0_PATCH_OP]


@dataclass(frozen=True)
class Workspace:
    env: str
    workspace_id: int
    host: str


@dataclass(frozen=True)
class Member:
    kind: str  # "user", "service_principal" or "group"
    key: str  # userName, applicationId, or the group's display name
    local_group: bool = False  # a nested group that is itself workspace-local


@dataclass(frozen=True)
class Action:
    step: str  # create_group, add_members, rename_local, assign, entitlements, migrate_permissions, delete_local
    detail: str


@dataclass
class GroupPlan:
    env: str
    workspace_id: int
    local_name: str
    local_id: str
    target_name: str
    members: list[Member] = field(default_factory=list)
    entitlements: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    account_group_id: str | None = None

    @property
    def needs_rename(self) -> bool:
        return self.target_name == self.local_name

    @property
    def nested_local_groups(self) -> list[str]:
        return [m.key for m in self.members if m.local_group]

    @property
    def blocked(self) -> bool:
        return bool(self.problems)


def validate_template(template: str) -> None:
    """The account group name must contain the environment, so each workspace gets its own account group."""
    for part in ("{group}", "{env}"):
        if part not in template:
            raise ValueError(f"naming.template {template!r} must contain {part}: each workspace's local group "
                             "becomes its own account group, never one shared across environments")


def is_workspace_local(group: iam.Group) -> bool:
    return bool(group.meta and group.meta.resource_type == WORKSPACE_GROUP)


def target_name(template: str, env: str, group: str) -> str:
    return template.format(env=env, group=group)


def _member(w, ref: iam.ComplexValue, local_names: set[str]) -> Member | None:
    """Resolve a workspace group member to something that identifies it at the account."""
    kind = (ref.ref or "").split("/")[0]
    if kind == "Users":
        return Member("user", w.users.get(ref.value).user_name)
    if kind == "ServicePrincipals":
        return Member("service_principal", w.service_principals.get(ref.value).application_id)
    if kind == "Groups":
        return Member("group", ref.display, local_group=ref.display in local_names)
    return None


def inventory(w, ws: Workspace, template: str, exclude: set[str], temp_prefix: str) -> list[GroupPlan]:
    """Workspace-local groups in one workspace, with members and entitlements resolved.

    A local group an earlier run already renamed to <temp_prefix><name> keeps <name> as the base of its target.
    """
    groups = list(w.groups.list(attributes="id,displayName,meta,members,entitlements"))
    local = [g for g in groups if is_workspace_local(g) and g.display_name not in SYSTEM_GROUPS | exclude]
    local_names = {g.display_name for g in local}
    plans = []
    for g in local:
        base = g.display_name.removeprefix(temp_prefix)
        plan = GroupPlan(ws.env, ws.workspace_id, g.display_name, g.id, target_name(template, ws.env, base),
                         entitlements=sorted(e.value for e in g.entitlements or []))
        for ref in g.members or []:
            m = _member(w, ref, local_names)
            if m is None:
                plan.problems.append(f"unknown member type {ref.ref!r} ({ref.display})")
            else:
                plan.members.append(m)
        plans.append(plan)
    return plans


def check_conflicts(plans: list[GroupPlan]) -> None:
    """Two local groups must never map to one account group (a safety net behind validate_template)."""
    by_target: dict[str, list[GroupPlan]] = {}
    for p in plans:
        by_target.setdefault(p.target_name, []).append(p)
    for name, same in by_target.items():
        if len(same) > 1:
            sources = ", ".join(sorted(f"{p.env}/{p.local_name}" for p in same))
            for p in same:
                p.problems.append(f"account group {name!r} would be shared by {sources}; each local group needs "
                                  "its own account group")


def dependency_order(plans: list[GroupPlan]) -> list[GroupPlan]:
    """Nested workspace-local groups first, so their account groups exist before a parent adds them.

    A cycle is reported as a problem. A parent whose nested local group is blocked is blocked too, so it never
    migrates with a member missing.
    """
    by_name = {p.local_name: p for p in plans}
    done, ordered = set(), []

    def visit(p: GroupPlan, path: tuple[str, ...]) -> None:
        if p.local_name in done:
            return
        if p.local_name in path:
            p.problems.append("nested group cycle: " + " > ".join(path + (p.local_name,)))
            return
        for child in p.nested_local_groups:
            if child in by_name:
                visit(by_name[child], path + (p.local_name,))
        done.add(p.local_name)
        ordered.append(p)

    for p in plans:
        visit(p, ())
    for p in ordered:  # children come first, so a block travels up in one pass
        for child in p.nested_local_groups:
            if child in by_name and by_name[child].blocked and not any(child in x for x in p.problems):
                p.problems.append(f"nested local group {child!r} is blocked; migrate it first")
    return ordered


class AccountIndex:
    """Look-ups at the account, cached for one run."""

    def __init__(self, a):
        self.a = a
        self._cache: dict[tuple[str, str], str | None] = {}

    def _one(self, kind: str, api, attr: str, value: str) -> str | None:
        key = (kind, value)
        if key not in self._cache:
            found = list(api.list(filter=f'{attr} eq "{value}"', attributes="id"))
            self._cache[key] = found[0].id if found else None
        return self._cache[key]

    def group_id(self, name: str) -> str | None:
        return self._one("group", self.a.groups, "displayName", name)

    def remember_group(self, name: str, gid: str) -> None:
        self._cache[("group", name)] = gid

    def member_id(self, m: Member, target_of: dict[str, str]) -> str | None:
        if m.kind == "user":
            return self._one("user", self.a.users, "userName", m.key)
        if m.kind == "service_principal":
            return self._one("service_principal", self.a.service_principals, "applicationId", m.key)
        return self.group_id(target_of.get(m.key, m.key) if m.local_group else m.key)


def apply(a, w, plan: GroupPlan, index: AccountIndex, target_of: dict[str, str], *, dry_run: bool,
          delete_local_groups: bool, temp_prefix: str, owned: set[str], adopt: set[str],
          on_created: Callable[[GroupPlan, str], None] = lambda plan, gid: None) -> None:
    """Carry out one group's migration. Records each step in plan.actions; dry_run only records.

    owned: account groups this migration created earlier (the registry); adopt: existing account groups the config
    explicitly allows to be reused. on_created is called right after an account group is created, so it can be
    registered before anything else can fail.
    """
    act = lambda step, detail: plan.actions.append(Action(step, detail))  # noqa: E731

    # 1. The account group: our own, never someone else's.
    gid = index.group_id(plan.target_name)
    if gid is not None and plan.target_name not in owned | adopt:
        plan.problems.append(f"account group {plan.target_name!r} already exists and was not created by this "
                             "migration; rename the target (naming.template) or list it in adopt_account_groups")
        return
    if gid is None:
        act("create_group", plan.target_name)
        if not dry_run:
            gid = a.groups.create(display_name=plan.target_name).id
            index.remember_group(plan.target_name, gid)
            on_created(plan, gid)
    plan.account_group_id = gid

    # 2. Its members.
    wanted = []
    for m in plan.members:
        mid = index.member_id(m, target_of)
        if mid is None and not (dry_run and m.local_group):
            plan.problems.append(f"{m.kind} {m.key!r} not found at the account; not added")
        elif mid is not None:
            wanted.append(mid)
    current = {v.value for v in (a.groups.get(gid).members or [])} if gid else set()
    missing = [mid for mid in wanted if mid not in current]
    if missing:
        act("add_members", f"{len(missing)} member(s)")
        if not dry_run:
            a.groups.patch(gid, schemas=SCIM_PATCH,
                           operations=[iam.Patch(op=iam.PatchOp.ADD, path="members",
                                                 value=[{"value": mid} for mid in missing])])

    # 3. Free the name in the workspace.
    from_name = plan.local_name
    if plan.needs_rename:
        from_name = temp_prefix + plan.local_name
        act("rename_local", f"{plan.local_name} -> {from_name}")
        if not dry_run:
            w.groups.patch(plan.local_id, schemas=SCIM_PATCH,
                           operations=[iam.Patch(op=iam.PatchOp.REPLACE, path="displayName", value=from_name)])

    # 4. Workspace assignment and entitlements.
    act("assign", f"{plan.target_name} to workspace {plan.workspace_id} (USER)")
    if not dry_run:
        a.workspace_assignment.update(plan.workspace_id, int(gid), permissions=[iam.WorkspacePermission.USER])
    if plan.entitlements:
        act("entitlements", ", ".join(plan.entitlements))
        if not dry_run:
            w.groups.patch(gid, schemas=SCIM_PATCH,
                           operations=[iam.Patch(op=iam.PatchOp.ADD, path="entitlements",
                                                 value=[{"value": e} for e in plan.entitlements])])

    # 5. Permissions on workspace objects.
    if dry_run:
        act("migrate_permissions", f"{from_name} -> {plan.target_name}")
    else:
        moved = 0
        while True:
            n = w.permission_migration.migrate_permissions(plan.workspace_id, from_name, plan.target_name,
                                                           size=MIGRATION_BATCH).permissions_migrated or 0
            moved += n
            if n == 0:
                break
        act("migrate_permissions", f"{from_name} -> {plan.target_name}: {moved} permission(s)")

    # 6. The local group, only when asked.
    if delete_local_groups:
        act("delete_local", from_name)
        if not dry_run:
            w.groups.delete(plan.local_id)
