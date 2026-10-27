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
.github/workflows/ci.yml               Run workflow: validate and deploy any branch to the governance workspace
.github/workflows/tests.yml            Unit tests on every pull request
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

The jobs read the account ID and their own OAuth credentials from the secret scope `sct_dbx_architect_lab`
(`credentials.scope` in `config/group_migration.yaml`). The governance workspace's bootstrap creates the scope, empty,
and only the infrastructure deployment principal can use it, so add the secrets and give `sp-dbx-governance` READ.
As a workspace admin of the governance workspace:

```bash
databricks auth login --host https://adb-7405610301840934.14.azuredatabricks.net

databricks secrets put-secret sct_dbx_architect_lab sp-client-id          --string-value "<client-id>"
databricks secrets put-secret sct_dbx_architect_lab sp-client-secret      --string-value "<secret>"
databricks secrets put-secret sct_dbx_architect_lab databricks-account-id --string-value "<account-id>"

# The jobs run as the service principal, so it must be able to read the scope.
databricks secrets put-acl sct_dbx_architect_lab d81cb8b1-92e7-4ab2-ac47-0143a2a1d6fc READ   # sp-dbx-governance

# Check: the three keys are listed, and sp-dbx-governance has READ.
databricks secrets list-secrets sct_dbx_architect_lab
databricks secrets list-acls sct_dbx_architect_lab
```

`<client-id>` and `<secret>` are `sp-dbx-governance`'s application ID and a Databricks OAuth secret (step 1);
`<account-id>` is in the account console, top right menu. If the job fails with `Missing credential: secret
sct_dbx_architect_lab/...`, one of the keys is missing or misspelled, or the READ grant is.

### 4. Let it write the inventory tables

The catalog grants are managed by the infrastructure repository and are authoritative (a grant made by hand is
removed on the next apply). In `dbx-platform-infra-azure`, add the application ID to `grant_principals` in
`live/governance/workspace-bootstrap/catalog-config.yaml`, then plan and apply
`governance-dbxarchitectlab-workspace-bootstrap`. The default privileges, `BROWSE`, `USE_CATALOG` and
`CREATE_SCHEMA`, are what the job needs.

The group migration creates the `identity` schema on its first run and owns it from then on.

### 5. Check serverless compute for jobs, and allow it to reach dev, uat and prod

The jobs run on serverless compute. It must be enabled for the account (account console → **Settings → Feature
enablement**); without it, the jobs need a job cluster instead.

The jobs run on serverless compute in the governance workspace and call the APIs of dev, uat and prod. Databricks
lets serverless compute reach **another** workspace only when the account's network policies allow it on both sides
([cross-workspace access](https://learn.microsoft.com/en-us/azure/databricks/security/network/front-end/cross-workspace-access),
Beta): the **egress** of the governance workspace and the **ingress** of each governed workspace. Without it, every
call to dev, uat or prod fails with `PermissionDenied: Cert validation failed. Workspace comparison, snp system
trusted, and snp trusted workload checks did not pass`.

As an account admin:

0. Account console → **Previews** → turn on **Cross Workspace Network Policy** (Beta). Without it, the **Ingress**
   tab shows only **Public Network Access** and has no cross-workspace rules.

Then account console → **Security → Networking → Context-based ingress & egress control**:

1. **Egress** (the policy of the governance workspace, `7405610301840934`): **Allow access to all destinations**
   already allows it. If it is **Restricted access to specific destinations**, add dev, uat and prod
   (`7405609089943919`, `7405605496889455`, `7405604784566671`) under **Allowed Databricks workspace destinations**.
2. **Ingress** (the policy of dev, uat and prod): on the **Ingress** tab, keep **Public Network Access → Allow access
   from all public IPs**; in the cross-workspace section, clear **Compatibility mode** if shown, choose **Restrict
   access to specific source workspaces**, and add an allow rule: access type **API**, identity `sp-dbx-governance`
   (or all service principals), network source **Selected workspaces** = the governance workspace
   (`7405610301840934`). **Save**, then wait 10-15 minutes before running the job. Denials are logged in
   `system.access.inbound_network` in the governed workspace.

All workspaces start on `default-policy`, so editing it changes every workspace. To change only dev, uat and prod,
create a policy (for example `governed-workspaces`) with the same egress settings and the ingress rule above, and
assign it to those three workspaces (account console → **Workspaces** → the workspace → **Network policy**). Same-
workspace traffic is always allowed, so nothing else changes. The job checks this first and stops with these steps
if a workspace refuses it.

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
   `Deploy <branch> → governance (@<you>)`. The unit tests aren't part of it: the **Tests** workflow runs them on every pull request.
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

The `identity-local-group-drift-check` job (schedule paused: `pause_status` in its job file) runs daily in plan mode across dev, uat and prod and fails (with an
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
