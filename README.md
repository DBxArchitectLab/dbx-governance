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
.github/workflows/ci.yml               Tests on every pull request; Run workflow: validate and deploy any branch
```

## Set up the governance workspace and CI/CD

The governance workspace is provisioned by `dbx-platform-infra-azure` (`live/governance`). These steps, done once,
let this repository's bundle deploy to it from GitHub Actions and let its jobs act on dev, uat and prod.

One service principal does both: it deploys the bundle and the jobs run as it. Using the same identity for both means
the deployer needs no extra role to set `run_as`.

### 1. Create the governance service principal

Databricks account console → **User management → Service principals → Add service principal** → `sp-dbx-governance`.
Then, on the service principal:

- **Roles** → turn on **Account admin** (the jobs change account groups and workspace assignments).
- **Credentials & secrets → Generate secret**. Copy the **Client ID** (its application ID) and the **Secret** now;
  the secret is shown once.

### 2. Make it a workspace admin in all four workspaces

Account console → **Workspaces** → for each of **governance, dev, uat and prod** → **Permissions → Add permissions** →
`sp-dbx-governance` → **Admin**. The bundle is deployed to the governance workspace; the jobs act inside dev, uat and
prod.

### 3. Store its credentials in a secret scope in the governance workspace

The jobs read the account ID and their own OAuth credentials from a secret scope. The governance workspace's
bootstrap already created the scope `sct_dbx_architect_lab`; either use it (and set `credentials.scope` in
`config/group_migration.yaml` to `sct_dbx_architect_lab`), or create a dedicated `governance` scope (the config's
default) with `databricks secrets create-scope governance` first. With the bootstrap's scope:

```bash
databricks auth login --host https://adb-<governance-workspace-id>.<n>.azuredatabricks.net

databricks secrets put-secret sct_dbx_architect_lab sp-client-id          --string-value "<client-id>"
databricks secrets put-secret sct_dbx_architect_lab sp-client-secret      --string-value "<secret>"
databricks secrets put-secret sct_dbx_architect_lab databricks-account-id --string-value "<account-id>"

# The jobs run as the service principal, so it must be able to read the scope.
databricks secrets put-acl sct_dbx_architect_lab <sp-application-id> READ
```

### 4. Let it write the inventory tables

In the governance workspace's SQL editor:

```sql
GRANT USE CATALOG, CREATE SCHEMA ON CATALOG dbxarchitectlab_governance TO `<sp-application-id>`;
```

The group migration creates the `identity` schema on its first run and owns it from then on.

### 5. Check serverless compute for jobs

The jobs run on serverless compute. It must be enabled for the account (account console → **Settings → Feature
enablement**); without it, the jobs need a job cluster instead.

### 6. Fill in the repository configuration

| File | Set |
| --- | --- |
| `databricks.yml` | `workspace.host`: the governance workspace URL. `run_as.service_principal_name`: `sp-dbx-governance`'s application ID |
| `config/group_migration.yaml` | `workspace_id` and `host` for dev, uat and prod (outputs `workspace_id` and `workspace_url` of the `dbx-platform-infra-azure` workspace stacks), and `credentials.scope` (step 3) |
| `resources/identity_group_migration.job.yml` | The address for the drift check's `on_failure` email |

### 7. Create the GitHub environment

GitHub → this repository → **Settings → Environments → New environment** `governance`, with three secrets:

| Secret | Value |
| --- | --- |
| `DATABRICKS_HOST` | The governance workspace URL |
| `DATABRICKS_CLIENT_ID` | `sp-dbx-governance`'s application ID |
| `DATABRICKS_CLIENT_SECRET` | Its OAuth secret (step 1) |

Add required reviewers to the environment if deployments should need an approval.

### 8. Deploy

1. **Merge to `main`.** A workflow that you start manually only appears under **Actions** once it is on the default
   branch. Pull requests run the unit tests automatically.
2. **Actions → CI and deploy → Run workflow**: pick the branch in **Use workflow from**; **Deploy the bundle** is
   ticked by default. The job **Deploy to governance workspace** runs `databricks bundle validate -t governance` and
   `databricks bundle deploy -t governance` (untick the box to validate only). The run is titled
   `Deploy <branch> → governance (@<you>)`. The unit tests aren't part of it: they run on the pull request.
3. In the governance workspace, **Workflows** now shows the bundle's jobs.

To deploy from your machine instead: `databricks auth login --host <governance-workspace-url>`, then
`databricks bundle deploy -t governance`. Your user then needs the **Service principal: User** role on
`sp-dbx-governance` (account console → the service principal → **Permissions**), because the bundle runs its jobs as
that service principal.

Later improvement: the deploy uses an OAuth secret. Databricks can also accept GitHub's OIDC token through a
federation policy on the service principal, which removes the secret, the same direction as the infrastructure
repositories.

## Identity: workspace-local groups to account groups

Workspace-local groups live in one workspace only. Unity Catalog can't grant to them, the account can't see or
audit them, and the same name can mean different people in dev and prod. Account groups fix all three: one
definition, assigned to the workspaces that need it, usable in Unity Catalog grants.

The `identity-workspace-group-migration` job moves every workspace-local group in dev, uat and prod to an account
group, one environment at a time:

1. **Account group:** creates one per local group per workspace, named by `naming.template` (default
   `{group}_{env}`). It only reuses an account group that it created itself in an earlier run (the registry table),
   or that the config lists in `adopt_account_groups`.
2. **Members:** the same users (by userName), service principals (by applicationId) and nested groups. Nested
   workspace-local groups are migrated first, and their account groups are added instead; if a nested group is
   blocked, every group that contains it is blocked too.
3. **Name clash:** if the account group has the local group's own name, the local group is renamed to
   `db-temp-<name>` first.
4. **Workspace access:** assigns the account group to the workspace (USER) and copies the local group's
   entitlements.
5. **Permissions:** moves the local group's permissions on workspace objects (clusters, jobs, notebooks, folders,
   SQL warehouses, ...) to the account group with the permission migration API.
6. **Clean-up:** deletes the local group, only in a run with `delete_local_groups = true`.

Every step is idempotent, so a failed run can be repeated. Problems (an identity missing at the account, a nested
group cycle or a blocked nested group, an account group that exists but wasn't created by the migration) are
reported, and that group is skipped in apply mode.

### One account group per workspace, never shared

Account groups are account-wide. If dev's, uat's and prod's `data_engineers` became one account group, everyone in
dev's group would gain prod's permissions. So the naming template **must** contain `{env}`: the same local group name
in three workspaces always becomes three account groups (`data_engineers_dev`, `data_engineers_uat`,
`data_engineers_prod`). The job refuses to start with a template that lacks `{group}` or `{env}`, and blocks any two
local groups that would still end up with one account group name.

### Inventory tables

Every run (plan and apply) appends to four Delta tables in `inventory.catalog`.`inventory.schema` (default
`dbxarchitectlab_governance.identity`, created on the first run):

| Table | One row per | Use it for |
| --- | --- | --- |
| `local_groups` | local group per run | The inventory: account group, member count, nested local groups, entitlements, status (`planned`, `migrated`, `local_group_deleted`, `blocked`), problems |
| `local_group_members` | member per local group per run | Who was in each local group when it was migrated |
| `migration_actions` | step per group per run | The audit trail: what was planned or done, in order |
| `account_group_registry` | account group the migration created | Which account groups the migration owns and may reuse |

Apply needs these tables (it reads the registry), so run it as the Databricks job.

**Step-by-step setup, run order, checks, recovery and troubleshooting:
[docs/identity-group-migration.md](docs/identity-group-migration.md).**

### Before the first run

- [Set up the governance workspace and CI/CD](#set-up-the-governance-workspace-and-cicd) is done: the service
  principal, its secrets, the catalog grant, the configuration, and the deployed bundle.
- Identity federation is on for dev, uat and prod (it is, because Unity Catalog is assigned).
- The drift check's schedule is paused until the migration is finished (it fails every day until then): **Workflows →
  identity-local-group-drift-check → Schedule → Pause**.

### Run it

Deploy the bundle (`databricks bundle deploy -t governance`, or the CI workflow), then run the job with parameters:

| Run | `mode` | `environments` | `delete_local_groups` |
| --- | --- | --- | --- |
| 1. Review what would change | `plan` | `dev,uat,prod` | `false` |
| 2. Migrate dev | `apply` | `dev` | `false` |
| 3. Check dev: users still reach their jobs, clusters, notebooks and warehouses | | | |
| 4. Migrate uat, then prod | `apply` | `uat`, then `prod` | `false` |
| 5. Remove the local groups, per environment, once checked | `apply` | `dev` (then `uat`, `prod`) | `true` |

Plan mode changes nothing in the workspaces or the account. The output lists every local group, its account group,
its nested local groups and each action (`WOULD ...`), plus any problem to fix first; the same goes into the
inventory tables.

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
