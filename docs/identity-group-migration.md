# Workspace-local groups to account groups: operator guide

How to set up, run, check and maintain the migration of workspace-local groups in the dev, uat and prod Azure
Databricks workspaces to account-level groups. For what the job does and why, see the
[README](../README.md#identity-workspace-local-groups-to-account-groups).

Contents

1. [Overview](#1-overview)
2. [Prerequisites](#2-prerequisites)
3. [One-time setup](#3-one-time-setup)
4. [Deploy the jobs](#4-deploy-the-jobs)
5. [Run the migration](#5-run-the-migration)
6. [Read the output](#6-read-the-output)
7. [Check each environment](#7-check-each-environment)
8. [Remove the local groups](#8-remove-the-local-groups)
9. [The daily drift check](#9-the-daily-drift-check)
10. [Recovery](#10-recovery)
11. [Troubleshooting](#11-troubleshooting)
12. [Reference](#12-reference)

---

## 1. Overview

| Item | Value |
| --- | --- |
| Runs in | The governance workspace (bundle target `governance`) |
| Acts on | dev, uat and prod workspaces, and the Databricks account |
| Runs as | The governance service principal (account admin; workspace admin in dev, uat, prod) |
| Jobs | `identity-workspace-group-migration` (on demand), `identity-local-group-drift-check` (daily, 06:00 UTC) |
| Default mode | `plan`: reads only, changes nothing |
| Account group name | `naming.template` in `config/group_migration.yaml`, default `{group}_{env}` |

The order is always: **plan everything → apply dev → check dev → apply uat → check → apply prod → check → delete
local groups, one environment at a time.**

## 2. Prerequisites

| Requirement | How to check |
| --- | --- |
| Unity Catalog metastore assigned to dev, uat and prod (this turns on identity federation) | Account console → Workspaces → each workspace shows the metastore |
| You are a Databricks **account admin** (for the setup in section 3) | Account console opens, and User management shows the Roles tab |
| Databricks CLI 0.230 or later | `databricks --version` |
| Workspace IDs and URLs of dev, uat, prod and the governance workspace | Outputs `workspace_id` and `workspace_url` of the `dbx-platform-infra-azure` workspace stacks, or the account console → Workspaces |
| A Unity Catalog schema for the run report (optional) | For example `governance.identity` in the governance workspace |

## 3. One-time setup

### 3.1 Create the governance service principal

The jobs need one identity that can change account groups and act in all three workspaces. Use a dedicated service
principal, not a person and not the deployment principal of the infrastructure repository.

1. Account console → **User management → Service principals → Add service principal**. Name it
   `sp-dbx-governance`. Note its **Application ID**.
2. Open it → **Roles** → turn on **Account admin**.
3. Open it → **Credentials & secrets** → **Generate secret**. Copy the **Client ID** and the **Secret** now; the secret
   is shown once.

### 3.2 Make it a workspace admin in dev, uat, prod and the governance workspace

For each of the four workspaces: account console → **Workspaces** → the workspace → **Permissions** → **Add
permissions** → `sp-dbx-governance` → **Admin**.

The permission migration API and the entitlement changes run inside each workspace, so **User** is not enough.

### 3.3 Store its credentials in the governance workspace

Authenticate the CLI to the governance workspace as yourself, then create the scope and the three secrets:

```bash
databricks auth login --host https://adb-<governance-workspace-id>.<n>.azuredatabricks.net

databricks secrets create-scope governance
databricks secrets put-secret governance sp-client-id          --string-value "<client-id>"
databricks secrets put-secret governance sp-client-secret      --string-value "<secret>"
databricks secrets put-secret governance databricks-account-id --string-value "<account-id>"

# The job runs as the service principal, so it must be able to read the scope.
databricks secrets put-acl governance <sp-application-id> READ
```

Clear the commands from your shell history afterwards if they contained the secret.

### 3.4 Fill in the configuration

`config/group_migration.yaml`:

```yaml
workspaces:
  dev:
    workspace_id: "1234567890123456"
    host: "https://adb-1234567890123456.7.azuredatabricks.net"
  uat:  { ... }
  prod: { ... }
```

Decide the naming policy before the first apply. It is hard to change once account groups exist:

| `naming.template` | Result for `data_engineers` in prod | Use when |
| --- | --- | --- |
| `{group}_{env}` (default) | `data_engineers_prod` | Members differ between environments (the usual case) |
| `{env}_{group}` | `prod_data_engineers` | Same, if you prefer the environment first |
| `{group}` | `data_engineers` | The group has the same members in every workspace and should be one account group |

With `{group}`, the job checks that the memberships really are identical and stops a group if they aren't.

Optionally, set `report_table: "governance.identity.group_migration_log"` to keep a history of every run, and list any
local group to leave untouched under `exclude_groups`.

`databricks.yml`: set the governance workspace `host` and `run_as.service_principal_name` (the application ID).

`resources/identity_group_migration.job.yml`: replace `<governance-team-email>` in the drift check.

Commit the changes on a branch and open a pull request: the CI workflow runs the unit tests.

## 4. Deploy the jobs

### From your machine

```bash
databricks auth login --host https://adb-<governance-workspace-id>.<n>.azuredatabricks.net
databricks bundle validate -t governance
databricks bundle deploy -t governance
```

### From GitHub Actions

1. Repository **Settings → Environments → New environment** `governance`, with three secrets: `DATABRICKS_HOST` (the
   governance workspace URL), `DATABRICKS_CLIENT_ID` and `DATABRICKS_CLIENT_SECRET` (a principal allowed to deploy
   to the governance workspace).
2. **Actions → CI and deploy → Run workflow**, tick **Deploy the bundle**.

Either way, the governance workspace now shows two jobs under **Workflows**: `identity-workspace-group-migration`
and `identity-local-group-drift-check`.

## 5. Run the migration

Each run takes three parameters. Start a run from **Workflows → identity-workspace-group-migration → Run now with
different parameters**, or from the CLI:

```bash
databricks bundle run -t governance identity_group_migration \
  --params mode=plan,environments=dev,delete_local_groups=false
```

Follow these runs in order. Don't start the next one until the previous one is clean.

| # | `mode` | `environments` | `delete_local_groups` | Purpose |
| --- | --- | --- | --- | --- |
| 1 | `plan` | `dev,uat,prod` | `false` | Inventory every local group and every problem, across all three workspaces |
| 2 | `plan` | `dev` | `false` | The exact actions for dev |
| 3 | `apply` | `dev` | `false` | Migrate dev |
| 4 | | | | Check dev (section 7) |
| 5 | `plan`, then `apply` | `uat` | `false` | Migrate uat, then check it |
| 6 | `plan`, then `apply` | `prod` | `false` | Migrate prod in an agreed change window, then check it |
| 7 | `apply` | one environment at a time | `true` | Remove the local groups (section 8) |

Before run 3, fix every `PROBLEM` from run 1: apply skips a group with a problem and fails the run, so nothing is
half-migrated silently.

**Tell users before apply in each environment.** Group names change (for example `data_engineers` →
`data_engineers_dev`). Access stays the same, but anything that refers to a group by name (scripts, job definitions
in code, documentation) needs the new name.

## 6. Read the output

The run's output (the task's **Output** tab) lists each local group, then a summary:

```
[dev] data_engineers -> data_engineers_dev  (12 members, entitlements: databricks-sql-access, workspace-access)
    WOULD create account group data_engineers_dev
    WOULD add 12 member(s)
    WOULD assign data_engineers_dev to workspace 1234567890123456 (USER)
    WOULD copy entitlements: databricks-sql-access, workspace-access
    WOULD migrate workspace object permissions data_engineers -> data_engineers_dev

[dev] legacy_analysts -> legacy_analysts_dev  (3 members, entitlements: none)
    ...
    PROBLEM user 'old.user@example.com' not found at the account; not added

2 workspace-local group(s) in dev; mode plan.
```

- `WOULD` appears in plan mode only. In apply mode the same lines are what was done, plus `N permission(s) migrated`.
- A group with a `PROBLEM` is skipped in apply mode, and the run ends as failed so you can't miss it.
- A second apply of the same environment shows no `create` or `add` lines: everything is already in place.

## 7. Check each environment

After each apply, in the migrated workspace:

| Check | Where | Expected |
| --- | --- | --- |
| The account groups exist with the right members | Account console → User management → Groups → `<group>_<env>` → Members | Same members as the local group |
| They are assigned to the workspace | Account console → Workspaces → the workspace → Permissions | Each `<group>_<env>` with **User** |
| Entitlements | Workspace → Settings → Identity and access → Groups → `<group>_<env>` | Same entitlements as the local group |
| Object permissions | Open a cluster, a job, a notebook folder and a SQL warehouse the group used → Permissions | `<group>_<env>` listed with the same level |
| Real access | Ask one member of each group to open a job, run a notebook, query a warehouse | Works as before |
| Local groups left | Workspace → Settings → Identity and access → Groups | Each local group is still there (or renamed `db-temp-<name>`) until section 8 |

If the run report is on, the same facts are in the table:

```sql
SELECT run_at, env, local_group, account_group, members, actions, problems
FROM governance.identity.group_migration_log
WHERE mode = 'apply'
ORDER BY run_at DESC, env, local_group;
```

Then grant the new account groups in Unity Catalog where needed. Unity Catalog never accepted workspace-local
groups, so there is nothing to migrate there, but the account groups are now usable in grants.

## 8. Remove the local groups

Only after section 7 passes for that environment, and preferably after a few days of normal use:

```bash
databricks bundle run -t governance identity_group_migration \
  --params mode=plan,environments=dev,delete_local_groups=true   # check the "delete local group" lines
databricks bundle run -t governance identity_group_migration \
  --params mode=apply,environments=dev,delete_local_groups=true
```

Repeat for uat, then prod. The migration steps run again first; they find everything in place and only the deletion
is new. Deleting a group can't be undone.

## 9. The daily drift check

`identity-local-group-drift-check` runs every day at 06:00 UTC in plan mode across dev, uat and prod, and fails when
any workspace-local group exists. Before the migration it fails every day; that is expected. After section 8 it
should pass every day.

When it fails later, someone created a local group. Open the failed run's output to see which one, then either
migrate it (runs 2-3 and section 8 for that environment) or delete it, and ask the creator to use account groups.

To pause it during the migration: **Workflows → identity-local-group-drift-check → Schedule → Pause**.

## 10. Recovery

| Situation | What to do |
| --- | --- |
| An apply run failed half-way | Fix the cause (section 11) and run the same apply again. Every step checks what exists first, so nothing is duplicated |
| Users lost access after apply, local group not yet deleted | Find the object in section 7's checks, add the missing permission to `<group>_<env>`, and note the object type so the other environments can be checked for it. The local group still exists with its members, so you can also grant it back temporarily |
| A local group was renamed to `db-temp-<name>` and you want the old name back | Workspace → Settings → Identity and access → Groups → rename it. A later run recognises either name |
| Wrong naming template used | Before deletion: change the template, run plan and apply again (new account groups are created and get the permissions), then delete the unwanted account groups in the account console |
| A local group was deleted too early | Recreate the access on the account group directly; the deleted group can't be restored |

## 11. Troubleshooting

| Message or symptom | Cause | Fix |
| --- | --- | --- |
| `Missing credential: secret governance/...` | Secret not created, or the job can't read the scope | Section 3.3, including `put-acl ... READ` |
| `PERMISSION_DENIED` on account calls (groups, workspace assignment) | The service principal is not an account admin | Section 3.1, step 2 |
| `PERMISSION_DENIED` on `permission_migration` or group patch | The service principal is not a workspace admin there | Section 3.2 |
| `PROBLEM ... not found at the account; not added` | The member exists only in the workspace, or was removed from the account / Entra ID | Add the user or service principal at the account (or in Entra ID if provisioned from there), or accept that they are left out, then apply again |
| `PROBLEM account group ... would merge different memberships` | The naming template has no `{env}` and the members differ | Use `{group}_{env}`, or align the memberships first |
| `PROBLEM nested group cycle` | Two local groups contain each other | Remove one membership in the workspace, then plan again |
| `PROBLEM unknown member type` | A member that is neither a user, a service principal nor a group | Check the group in the workspace UI |
| A group you expected is not listed | It is already an account group, it is `admins`/`users`/`account users`, or it is in `exclude_groups` | Nothing to do |
| `0 permission(s) migrated` | The local group held no permissions on workspace objects (only membership or entitlements) | Expected for such groups |
| The run report table is empty | `report_table` is null, or the schema doesn't exist / isn't writable by the service principal | Set it, create the schema, grant the service principal `USE CATALOG`, `USE SCHEMA`, `CREATE TABLE` |

## 12. Reference

### Job parameters

| Parameter | Values | Default | Meaning |
| --- | --- | --- | --- |
| `mode` | `plan`, `apply` | `plan` | `plan` reads only; `apply` makes the changes |
| `environments` | comma-separated keys from `workspaces` | `dev` | Which workspaces, in that order |
| `delete_local_groups` | `true`, `false` | `false` | Also delete the local groups |

The drift check passes `--fail-on-local-groups true` and is not meant to be run with other parameters.

### Configuration keys (`config/group_migration.yaml`)

| Key | Meaning |
| --- | --- |
| `account_host` | `https://accounts.azuredatabricks.net` |
| `workspaces.<env>.workspace_id`, `.host` | The governed workspaces |
| `naming.template` | Account group name; `{group}` and `{env}` are replaced |
| `naming.temp_prefix` | Prefix for a local group renamed to free its name (`db-temp-`) |
| `exclude_groups` | Local groups to skip |
| `credentials.*` | Secret scope and key names for the service principal and account ID |
| `report_table` | Delta table for the run report, or `null` |

### Run outside Databricks (read only)

```bash
pip install databricks-sdk pyyaml
export DATABRICKS_CLIENT_ID=... DATABRICKS_CLIENT_SECRET=... DATABRICKS_ACCOUNT_ID=...
python src/jobs/migrate_workspace_groups.py --config config/group_migration.yaml --mode plan --environments dev
```

### Files

| File | Purpose |
| --- | --- |
| `src/dbx_governance/identity/group_migration.py` | Inventory, conflict check, ordering and the six apply steps |
| `src/jobs/migrate_workspace_groups.py` | Job entry point: credentials, parameters, report |
| `resources/identity_group_migration.job.yml` | The two jobs |
| `config/group_migration.yaml` | Workspaces, naming, credentials, report table |
| `tests/test_group_migration.py` | Unit tests (`pytest -q tests`) |
