# dbx-governance

> **📺 DBX Architect Lab on YouTube:
> [youtube.com/@DBxArchitectLab](https://www.youtube.com/@DBxArchitectLab)**
>
> One enterprise Databricks platform on Azure, AWS and Google Cloud, built in public: infrastructure, platform
> management, governance, data engineering and AI/BI. This repository is part of it, and the videos walk through the
> architecture and the deployment step by step.
>
> *With AI assistance, coding is no longer the hard part. A solid understanding of the architecture and the
> fundamentals is the key to success.*
>
> [YouTube](https://www.youtube.com/@DBxArchitectLab) ·
> [LinkedIn](https://www.linkedin.com/in/DBxArchitectLab) ·
> [GitHub](https://github.com/DBxArchitectLab)

Platform-level governance and audit for the Azure Databricks platform. It runs from a dedicated governance workspace
and governs what happens in the dev and prod workspaces: identities, access and audit.

| Repository | Owns |
| --- | --- |
| `dbx-platform-infra-azure` | Provisioning: metastore, workspaces, networking, bootstrap (catalog, storage, policies) |
| `dbx-lakehouse-ai-bi` | Data engineering and AI work, promoted dev → prod through CI/CD |
| `dbx-governance` (this one) | Account-level governance across all workspaces: identity, access, audit |

Governance changes are not promoted from dev to prod like data pipelines: they act on every workspace from one
place, as an account admin, so they live here.

What runs here today, for the dev and prod workspaces: [workspace governance](#workspace-governance) (is every
workspace compliant with the standard?) and [Terraform drift detection and audit](#terraform-drift-detection-and-audit)
(does every workspace still match its code?). The workspace-local to account group migration that started here moved to `dbx-lakehouse-ai-bi`,
where each environment migrates its own workspace (serverless compute in the governance workspace can't call the dev
and prod APIs without cross-workspace network policies).

## Layout

```
databricks.yml                         Databricks Asset Bundle, target "governance" (the governance workspace)
config/governance.yml                  Workspace governance: the estate, the standard, the exceptions
resources/governance.yml               The estate schema and snapshots volume, and the workspace-governance job
src/govern/                            collect, account, systables (observe), rules, validate (judge), report (act)
src/notebooks/workspace_governance.py  The job (a Databricks notebook): snapshots -> inventory, results, compliance, reports
.github/workflows/governance.yml       On demand: collect (4 sources), validate centrally, one issue per scope
config/drift.yml                       Drift detection: which workspaces and stacks, who the pipelines are, what to watch
resources/drift.yml                    The drift schema and volume, and the terraform-drift-audit job
src/drift/                             config, plan findings, audit query, remediation, findings (plain Python, unit tested)
src/notebooks/terraform_drift_audit.py The job (a Databricks notebook): plan findings + system.access.audit -> findings, health
tests/                                 pytest
.github/workflows/drift.yml            On demand (daily schedule off): plan on every stack, drift issues, then the audit job
.github/workflows/ci.yml               Pull requests: unit tests. Run workflow: validate and deploy any branch
```

## Workspace governance

Drift detection asks whether each workspace still matches its code. Governance asks a different question: does each
workspace meet the standard, whoever made it that way? A grant written in Terraform to a person is not drift (code and
reality agree), and it still breaks "groups, not people". At two workspaces you can look; at a hundred you need the
platform to tell you.

The pattern: **provision centrally, deploy consistently, validate centrally, remediate locally.** Governance code is not
deployed into every workspace. Metadata is brought to one governance workspace, checked there against one standard,
and every failure goes to its owner, with the evidence and the fix.

### Four metadata sources, one standard

| Source | Collected by | Sees | Feeds |
| --- | --- | --- | --- |
| Workspace API | the workflow, per workspace (`collect.py`) | catalogs and bindings, schemas, owners, grants, external locations, clusters, policies, warehouses, jobs, settings | IAM-01–03, OWN-01, NAM-01, ISO-01, STO-01–02, CMP-01–03, JOB-01, WSC-01 |
| Information schema | the workflow, per workspace, as SELECTs on the workspace's own SQL warehouse (`collect.py`) | tables and views of the environment's catalog, their owners and direct grants | IAM-01–03 and OWN-01, down to tables and views |
| Account API | the workflow, once (`account.py`) | the workspaces on each metastore; each workspace's admin assignments | EST-01, EST-02, IAM-04 |
| System tables | the notebook, in the governance workspace, for every workspace at once (`systables.py`) | `system.access.table_lineage`, `system.compute.clusters` | ISO-02, CMP-04 |

Each source is where that metadata can be read: the information schema only shows catalogs bound to the workspace you
query from, so it runs inside each workspace; only the account knows which workspaces exist and who administers them;
only system tables see every workspace in one query. Every observation and every result records its source, so the
results say which source found what. Terraform state and plans are a fifth source, used by
[drift detection](#terraform-drift-detection-and-audit), kept separate on purpose: it answers a different question
(does reality match the code?), needs the state and the deploy identity, and is fixed by an apply.

```
Run workflow  .github/workflows/governance.yml   (on demand; a daily schedule is two commented lines)
  estate           read config/governance.yml -> one collector per workspace, one remediation job per scope
  collect          (x2) per workspace, read-only, as the drift environment's service principal:
                     workspace API, and information schema on estate.<env>.sql_warehouse
                   -> snapshot <env>.json: observations in one shape, each with its source, and what could not be read
  collect-account  (x1) account API, read-only, same service principal (an account admin) -> snapshot account.json
  validate         upload the snapshots + governance.yml to /Volumes/dbxarchitectlab_governance/estate/snapshots/<run id>/
                   databricks bundle run workspace_governance   (governance workspace, as sp-dbx-governance)
                     the notebook src/notebooks/workspace_governance.py, one cell per step, on serverless:
                     queries the system tables, merges the four sources per workspace, judges every workspace and
                     the account with the run's own standard
                     -> dbxarchitectlab_governance.estate.inventory    what exists, per run and source (history)
                     -> dbxarchitectlab_governance.estate.results      one row per rule evaluation, with its source
                     -> dbxarchitectlab_governance.estate.compliance   one scorecard per scope (workspace or account)
                     -> report/<scope>.md, report/summary.json         next to the snapshots
  remediate        (x3) open or update the issue "Governance: <scope>" for dev, prod and the account (labels
                   governance, owner:<group>, severity:<worst>), or close it when the scope is compliant again
```

Why the workspace collectors run in GitHub Actions: the dev and prod catalogs are bound to their own workspace, so the
governance workspace can't see inside them, and its serverless compute can't call the dev and prod APIs without
cross-workspace network policies. Collection stays distributed; policy evaluation is centralized.

The collectors only read (GET requests and SELECT statements), but they sign in as the deployment identity
(`sp-dbx-platform-infra`), which can write. In production, give them a dedicated, least-privileged collector identity
(see Known limits).

### Three layers, kept apart

| Layer | Code | Knows | Doesn't know |
| --- | --- | --- | --- |
| Observe | `collect.py`, `account.py`, `systables.py` | Its source; one normalized shape | The standard |
| Judge | `rules.py` + `validate.py` + `standard` in the config | What is expected | Any API |
| Act | `report.py` + the workflow's issues | Who owns the fix | How it was found |

A new rule never touches a collector; a new source never touches a rule. The job is a Databricks notebook, so its run
page shows each step and ends with this run's scorecard, checks per source, and failures. Everything it computes is
in `src/govern/`, plain Python the unit tests cover; the notebook only reads the volume, runs the system-table
queries, writes the tables and displays them. The `src/govern` modules stay `.py` files, not notebooks, because the
collectors in GitHub Actions, the notebook and the tests import them.

### The rules

Eighteen rules in two scopes: sixteen run once per workspace, two once for the account.

| Rule | Source | Checks | prod | nonprod |
| --- | --- | --- | --- | --- |
| IAM-01 | workspace API, information schema | No direct grants to users: grant to groups (fix: the GRANT and REVOKE) | high | medium |
| IAM-02 | workspace API, information schema | Grants only to approved groups and service principals | high | medium |
| IAM-03 | workspace API, information schema | High-risk privileges (ALL PRIVILEGES, MANAGE) only for admin groups and pipelines | critical | high |
| IAM-04 | account API | Workspace admins are admin groups or pipelines, never people | high | medium |
| OWN-01 | workspace API, information schema | Catalog, schemas, external locations, tables and views owned by a group or a pipeline (fix: ALTER ... OWNER TO) | high | medium |
| NAM-01 | workspace API | Schema names follow the naming standard | low | low |
| ISO-01 | workspace API | The environment's catalog is isolated, and no other environment's catalog is reachable | critical | high |
| ISO-02 | system tables | No data flows into this environment from another one (table lineage, lookback) | critical | high |
| STO-01 | workspace API | Catalog and schema storage under the environment's storage roots | high | medium |
| STO-02 | workspace API | External locations usable in the workspace point at its own storage | high | high |
| CMP-01 | workspace API | All-purpose clusters use a cluster policy | high | medium |
| CMP-02 | workspace API | All-purpose clusters carry the required tags | medium | low |
| CMP-03 | workspace API | Clusters and SQL warehouses stop when idle | medium | medium |
| CMP-04 | system tables | Job clusters use a cluster policy (lookback) | high | medium |
| JOB-01 | workspace API | Jobs run as an approved service principal | high | - |
| WSC-01 | workspace API | Workspace settings match the standard | medium | low |
| EST-01 | account API (account scope) | Every workspace on the metastore is in the estate, or in `account.not_governed` with a reason | high | |
| EST-02 | account API (account scope) | Every estate workspace is on a metastore, and the estate shares one | high | |
| COL-00 | any | Every domain was collected: a check that could not run is an error, never a pass | high | high |

The system-table rules report PASS ("nothing found in the last 7 days") only when their query ran; a query that failed
is COL-00. Every result carries its severity, the evidence (object names and principals, never data), and the fix.
The SQL is for the owner to review and run; nothing is changed automatically.

**Exceptions are explicit and time-bound.** A failure that matches an entry under `exceptions` (rules, environment or
`account`, object and principal patterns) is reported as EXCEPTION with its reason and approver until its `expires`
date; the day after, it fails again. Changing an exception is a pull request, like changing the standard.

**The standard travels with the evidence.** The workflow uploads `config/governance.yml` next to the snapshots, and the
notebook checks against that copy, so every run records which standard it used, and a branch can be tried before
merging.

### Scale out: config/governance.yml

Everything that names a workspace is under `estate`. To add uat, one entry:

```yaml
estate:
  uat:
    workspace_id: <workspace ID>
    host: https://adb-<workspace ID>.<n>.azuredatabricks.net
    catalog: dbxarchitectlab_uat
    tier: nonprod                  # which rules apply, at which severity
    owner: <owning group>          # who gets the issue
    storage_roots: [abfss://dbx-architect-lab-uat@stdbxarchlabuat.dfs.core.windows.net/]
    sql_warehouse: Serverless Starter Warehouse    # the information schema queries run here (exact name)
```

Until then, EST-01 reports any workspace on the metastore that is neither in the estate nor in `account.not_governed`:
the account API is how a growing estate notices a workspace nobody registered. What runs today is the two-workspace
implementation; from two workspaces to a hundred, these are the targets (not built yet). What changes is how the list
is made and how the work is spread, not the rules:
- **The estate from the account, not by hand.** Generate the entries from the account API and the workspaces' tags
  (environment, owner, tier), in the same pipeline that vends the workspaces; EST-01 then guards the gap.
- **More from the system tables.** Where they cover a domain (compute, jobs, billing, audit), centralized queries
  replace per-workspace API calls, where supported and authorized. They have their own coverage, latency and
  permission requirements: they complement the API collectors, which stay for what only a workspace can answer
  (grants inside bound catalogs, workspace settings, the information schema).
- **Fan out in batches.** A GitHub Actions matrix runs up to 256 jobs; at more workspaces, give each collector a batch.
- **A collector identity of its own,** with read-only access in every workspace, instead of the deploy identity.
- **Owners from tags,** and findings routed to the owning team where it works (today: one issue per scope, labelled
  with its owner; in an enterprise: Jira, ServiceNow or Teams).

### Set up (once)

The collectors use the `drift` GitHub environment (the drift workflow's service principal, `sp-dbx-platform-infra`,
which can already read every workspace and the account, with `DATABRICKS_ACCOUNT_ID`), and the validate step uses the
`governance` environment. Nothing new to add in GitHub. Then:
1. **Information schema:** the queries run on `estate.<env>.sql_warehouse`, here the Serverless Starter Warehouse
   that Databricks creates in each workspace. `sp-dbx-platform-infra` needs CAN USE on it; as a workspace admin it
   already has it. If a workspace has no such warehouse, or it was renamed, set the name it has.
2. **System tables:** as a metastore admin, let `sp-dbx-governance` read the two tables:
   ```sql
   GRANT USE SCHEMA ON SCHEMA system.access TO `d81cb8b1-92e7-4ab2-ac47-0143a2a1d6fc`;   -- already there for drift
   GRANT SELECT ON TABLE system.access.table_lineage TO `d81cb8b1-92e7-4ab2-ac47-0143a2a1d6fc`;
   GRANT USE SCHEMA ON SCHEMA system.compute TO `d81cb8b1-92e7-4ab2-ac47-0143a2a1d6fc`;
   GRANT SELECT ON TABLE system.compute.clusters TO `d81cb8b1-92e7-4ab2-ac47-0143a2a1d6fc`;
   ```
3. **Deploy the bundle** (Actions › CI and deploy › Run workflow): it creates the `estate` schema, the `snapshots`
   volume and the `workspace-governance` job.
4. **Run it:** Actions › Workspace governance › Run workflow.

Known limits, to fix as the platform grows:
- **The collectors read as the deploy identity.** They only send GET requests and SELECT statements, but the
  credential could write. Target: a read-only collector identity in every workspace and the account.
- **The information schema needs a warehouse in each workspace.** A stopped warehouse starts for the two queries (and
  stops again on its auto-stop).
- **System tables lag** (minutes to hours), so ISO-02 and CMP-04 see recent activity late, and keep a finding for the
  lookback window after it is fixed.
- **Volumes and functions** are not collected yet: the next domain for the information schema.

## Terraform drift detection and audit

Terraform code in `dbx-platform-infra-azure` is the intent; the workspaces are the reality. Drift is the gap between
them: a setting clicked in the UI, a grant given by hand, an object created that no code knows about. Governance
defines the target state; validation proves it still holds, on every run, and says what to do when it doesn't.

Two sides, because each sees what the other can't:

| Side | Tool | Answers | Blind spot |
| --- | --- | --- | --- |
| Plan | `terraform plan -detailed-exitcode` per stack, in GitHub Actions | What differs from code, and what an apply would do | Who did it; objects Terraform never created |
| Audit | `system.access.audit`, in the governance workspace | Who changed what, when; objects created outside Terraform | Whether it matters to the code |

```
Run workflow  .github/workflows/drift.yml   (the daily 06:00 UTC schedule is off: two commented lines)
  stacks    read config/drift.yml -> one job per environment and stack
  plan      (x4) terragrunt plan -detailed-exitcode on live/<env>/<stack>, infra repo main, read with the deploy identity
              0 clean        -> close the stack's open drift issue
              2 changes      -> findings (JSON): changed / deleted outside Terraform, or code not applied
                                -> open or update the issue "Drift: <env> / <stack>" (labels drift, severity:<level>)
              1 error        -> the job fails
  audit     upload the findings to /Volumes/dbxarchitectlab_governance/drift/plan_findings/<run id>/
            databricks bundle run drift_audit   (governance workspace, as sp-dbx-governance)
              the notebook src/notebooks/terraform_drift_audit.py, one cell per step, on serverless
              system.access.audit: watched changes in dev and prod by anyone but the pipelines
              -> dbxarchitectlab_governance.drift.findings   one row per finding, both sides, with the remediation
              -> dbxarchitectlab_governance.drift.health     one row per environment and run: in sync or not
```

Every finding ends in one of two places: **revert** (apply the code again: the manual change is undone) or **adopt**
(the change was right: put it in code, review, merge, apply). Either way, the next run's plan proves it with exit
code 0, and the issue closes. For a grant given by hand, the finding includes the `REVOKE` (or `GRANT`) statements that
undo it, to review, never run automatically. For an object created outside Terraform, adopting means adding it to the
config with an `import` block.

Findings never contain values from the plan, only resource addresses and attribute names: a plan can hold secrets.

### Scale out: config/drift.yml

Everything that names a workspace is in `config/drift.yml`; the workflow builds its job list from it and the job reads
it. To add uat (or the governance workspace itself), add one entry:

```yaml
environments:
  uat:
    workspace_id: <workspace ID>
    stacks: [workspace, workspace-bootstrap]   # folders under live/uat/ in the infra repository
    severity: medium                           # low | medium | high: on its findings and issue labels
```

A new stack is one more name in `stacks`; a new kind of object to watch is one more `service_name: [action_name, ...]`
under `audit.watched`; a new pipeline identity goes in `pipeline_identities`, so its changes are not reported as manual.
A typo fails the first step with the field that is wrong (`src/drift/config.py`).

### Set up (once)

1. **The drift GitHub environment.** This repository › Settings › Environments › New environment `drift`, with the
   same secrets as the infra repository's deploy: `SP_CLIENT_ID`, `SP_CLIENT_SECRET`, `AZURE_TENANT_ID`,
   `AZURE_SUBSCRIPTION_ID`, `DATABRICKS_ACCOUNT_ID`, `DATABRICKS_METASTORE_ID` (`sp-dbx-platform-infra`).
2. **Its federated credential.** On `sp-dbx-platform-infra`'s app registration, add a federated credential (as in the
   infra README, step 6), so the Azure login works. This organisation's OIDC tokens use the ID-based subject, so the
   subject is `repo:DBxArchitectLab@336295900/dbx-governance@1424823529:environment:drift` (owner ID, repository ID),
   issuer `https://token.actions.githubusercontent.com`, audience `api://AzureADTokenExchange`. If `azure/login` fails
   with `AADSTS700213`, copy the subject from its log into the credential.
3. **The governance identity's data access.** `sp-dbx-governance` (`d81cb8b1-92e7-4ab2-ac47-0143a2a1d6fc`) needs
   `USE CATALOG` and `CREATE SCHEMA` on `dbxarchitectlab_governance`: add its application ID to `grant_principals` in
   `live/governance/workspace-bootstrap/catalog-config.yaml` (infra repository) and apply. And read access to the audit
   log, granted once by a metastore admin:
   ```sql
   GRANT USE CATALOG ON CATALOG system TO `d81cb8b1-92e7-4ab2-ac47-0143a2a1d6fc`;
   GRANT USE SCHEMA ON SCHEMA system.access TO `d81cb8b1-92e7-4ab2-ac47-0143a2a1d6fc`;
   GRANT SELECT ON TABLE system.access.audit TO `d81cb8b1-92e7-4ab2-ac47-0143a2a1d6fc`;
   ```
4. **Deploy the bundle** (Actions › CI and deploy › Run workflow): it creates the `drift` schema, the `plan_findings`
   volume and the `terraform-drift-audit` job.
5. **Run it:** Actions › Terraform drift › Run workflow. The daily schedule (06:00 UTC) is off; to turn it on,
   uncomment the `schedule` lines at the top of `.github/workflows/drift.yml`.

Known limits, to fix as the platform grows:
- **The plan identity is the deploy identity.** Planning needs to read every object the stacks manage, including
  account-level ones (only account admins can), and the stacks add the deploying identity to their grants; a different
  identity would plan its own grants as drift. The workflow only plans, but the credential could apply. Target: a
  dedicated plan identity with read-only Azure roles, and the infra stacks no longer granting the deployer by name.
- **The state firewall is opened per run**, like the deploy workflow: the runner's IP is added and removed. Target: a
  self-hosted runner inside the network, so the state account needs no public rule at all.
- **The audit log covers what is in `audit.watched`.** It is a list of kinds of objects, not an inventory; an object of
  an unwatched kind created by hand is not reported.
- **Azure changes have no "who" here.** The plan sees Azure drift too (a tag or a network rule changed in the portal),
  but `system.access.audit` only records Databricks; the Azure Activity Log has the rest.

`health.in_sync` means every stack's plan is clean that day. Manual changes stay in the audit for the whole lookback
(7 days), even once reverted, so `health` counts them next to it rather than folding them in.

## Set up the governance workspace and CI/CD

The governance workspace is provisioned by `dbx-platform-infra-azure` (`live/governance`). These steps, done once,
let this repository's bundle deploy to it from GitHub Actions.

One service principal does both: it deploys the bundle and the jobs run as it. Using the same identity for both means
the deployer needs no extra role to set `run_as`.

### 1. Create the governance service principal

Databricks account console → **User management → Service principals → Add service principal** → `sp-dbx-governance`.
Then, on the service principal, **Credentials & secrets → Generate secret**. Copy the **Client ID** (its application
ID) and the **Secret** now; the secret is shown once.

Give it an account role (for example **Account admin**) only when a job here needs account-level APIs.

### 2. Give it access to the governance workspace

Account console → **Workspaces** → **governance** → **Permissions → Add permissions** → `sp-dbx-governance` →
**Admin**.

### 3. Fill in the repository configuration

In `databricks.yml`: `workspace.host` is the governance workspace URL, and `run_as.service_principal_name` is
`sp-dbx-governance`'s application ID (not its display name).

### 4. Create the GitHub environment

GitHub → this repository → **Settings → Environments → New environment** `governance`, with three secrets:

| Secret | Value |
| --- | --- |
| `DATABRICKS_HOST` | The governance workspace URL |
| `DATABRICKS_CLIENT_ID` | `sp-dbx-governance`'s application ID |
| `DATABRICKS_CLIENT_SECRET` | Its OAuth secret (step 1) |

Add required reviewers to the environment if deployments should need an approval.

### 5. Deploy

1. **Merge to `main`.** A workflow that you start manually only appears under **Actions** once it is on the default
   branch.
2. **Actions → CI and deploy → Run workflow**: pick the branch in **Use workflow from**; **Deploy the bundle** is
   ticked by default. The job **Deploy to governance workspace** runs `databricks bundle validate -t governance` and
   `databricks bundle deploy -t governance` (untick the box to validate only). The run is titled
   `Deploy <branch> → governance (@<you>)`.
3. In the governance workspace, **Workflows** shows the bundle's jobs. A deployment removes the jobs that are no
   longer in the bundle.

To deploy from your machine instead: `databricks auth login --host <governance-workspace-url>`, then
`databricks bundle deploy -t governance`. Your user then needs the **Service principal: User** role on
`sp-dbx-governance` (account console → the service principal → **Permissions**), because the bundle runs its jobs as
that service principal.

Later improvement: the deploy uses an OAuth secret. Databricks can also accept GitHub's OIDC token through a
federation policy on the service principal, which removes the secret, the same direction as the infrastructure
repositories.

## Contributing and license

Issues and pull requests are welcome.
