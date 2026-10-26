# dbx-governance

Part of **DBX Architect Lab**, an enterprise Databricks platform built in public:
[YouTube](https://www.youtube.com/@DBxArchitectLab) ·
[LinkedIn](https://www.linkedin.com/in/DBxArchitectLab) ·
[GitHub](https://github.com/DBxArchitectLab)

Platform-level governance and audit for the Azure Databricks platform. It runs from a dedicated governance workspace
and governs what happens in the dev, uat and prod workspaces: identities, access and audit.

| Repository | Owns |
| --- | --- |
| `dbx-platform-infra-azure` | Provisioning: metastore, workspaces, networking, bootstrap (catalog, storage, policies) |
| `dbx-lakehouse-ai-bi` | Data engineering and AI work, promoted dev → uat → prod through CI/CD |
| `dbx-governance` (this one) | Account-level governance across all workspaces: identity, access, audit |

Governance changes are not promoted from dev to prod like data pipelines: they act on all three workspaces from one
place, as an account admin, so they live here.

## Layout

```
databricks.yml                         Databricks Asset Bundle, target "governance" (the governance workspace)
resources/                             Jobs deployed by the bundle
config/                                What each job governs (workspaces, naming, credentials)
src/dbx_governance/                    Library code, one package per governance area
src/jobs/                              Job entry points
tests/                                 Unit tests (no Databricks connection needed)
.github/workflows/ci.yml               Tests on every pull request; bundle validate and deploy on demand
```

## Identity: workspace-local groups to account groups

Workspace-local groups live in one workspace only. Unity Catalog can't grant to them, the account can't see or
audit them, and the same name can mean different people in dev and prod. Account groups fix all three: one
definition, assigned to the workspaces that need it, usable in Unity Catalog grants.

The `identity-workspace-group-migration` job moves every workspace-local group in dev, uat and prod to an account
group, one environment at a time:

1. **Account group:** creates it (or reuses it) with the name from `naming.template`, default `{group}_{env}`.
2. **Members:** the same users (by userName), service principals (by applicationId) and nested groups. Nested
   workspace-local groups are migrated first, and their account groups are added instead.
3. **Name clash:** if the account group has the local group's own name, the local group is renamed to
   `db-temp-<name>` first.
4. **Workspace access:** assigns the account group to the workspace (USER) and copies the local group's
   entitlements.
5. **Permissions:** moves the local group's permissions on workspace objects (clusters, jobs, notebooks, folders,
   SQL warehouses, ...) to the account group with the permission migration API.
6. **Clean-up:** deletes the local group, only in a run with `delete_local_groups = true`.

Every step is idempotent, so a failed run can be repeated. Problems (an identity missing at the account, a nested
group cycle, two workspaces that would merge different members into one account group) are reported, and that group
is skipped in apply mode.

### Why `{env}` is in the default name

Account groups are account-wide. If dev's and prod's `data_engineers` both became one account group named
`data_engineers`, everyone in dev's group would gain prod's permissions. The default template keeps them apart
(`data_engineers_dev`, `data_engineers_prod`). Use `"{group}"` only for groups whose members are identical in every
workspace; the job refuses to merge different memberships.

**Step-by-step setup, run order, checks, recovery and troubleshooting:
[docs/identity-group-migration.md](docs/identity-group-migration.md).**

### Before the first run

- Identity federation is on for dev, uat and prod (it is, because Unity Catalog is assigned).
- A governance service principal is a **Databricks account admin** and a **workspace admin** in dev, uat and prod.
- A secret scope `governance` in the governance workspace holds `sp-client-id`, `sp-client-secret` and
  `databricks-account-id`.
- `config/group_migration.yaml` has the three workspace IDs and URLs (outputs of the `dbx-platform-infra-azure`
  workspace stacks), and `databricks.yml` has the governance workspace URL and the service principal's application ID.

### Run it

Deploy the bundle (`databricks bundle deploy -t governance`, or the CI workflow), then run the job with parameters:

| Run | `mode` | `environments` | `delete_local_groups` |
| --- | --- | --- | --- |
| 1. Review what would change | `plan` | `dev,uat,prod` | `false` |
| 2. Migrate dev | `apply` | `dev` | `false` |
| 3. Check dev: users still reach their jobs, clusters, notebooks and warehouses | | | |
| 4. Migrate uat, then prod | `apply` | `uat`, then `prod` | `false` |
| 5. Remove the local groups, per environment, once checked | `apply` | `dev` (then `uat`, `prod`) | `true` |

Plan mode only reads. The output lists every local group, its account group, and each action (`WOULD ...`), plus
any problem to fix first. Set `report_table` in the config to keep a Delta history of every run.

### What it doesn't cover

- **Unity Catalog grants:** none to migrate, since Unity Catalog never accepted workspace-local groups. Grant the new
  account groups where needed.
- **Legacy Hive metastore table ACLs:** check them separately if the workspaces used table access control.
- **Who should be in a group:** the job copies membership as it is. Review it before prod.

### Keeping it that way

The `identity-local-group-drift-check` job runs daily in plan mode across dev, uat and prod and fails (with an
email) when any workspace-local group exists. Longer term, make Microsoft Entra ID the source of account groups
(SCIM provisioning or automatic identity management), so groups are managed once, in Entra ID, and only assigned to
workspaces.

## Develop

```bash
pip install databricks-sdk pyyaml pytest
pytest -q tests
# against real workspaces, read only, from your machine:
DATABRICKS_CLIENT_ID=... DATABRICKS_CLIENT_SECRET=... DATABRICKS_ACCOUNT_ID=... \
  python src/jobs/migrate_workspace_groups.py --config config/group_migration.yaml --mode plan --environments dev
```
