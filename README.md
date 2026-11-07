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

What runs here today: [Terraform drift detection and audit](#terraform-drift-detection-and-audit) for the dev and
prod workspaces. The workspace-local to account group migration that started here moved to `dbx-lakehouse-ai-bi`,
where each environment migrates its own workspace (serverless compute in the governance workspace can't call the dev
and prod APIs without cross-workspace network policies).

## Layout

```
databricks.yml                         Databricks Asset Bundle, target "governance" (the governance workspace)
config/drift.yml                       Drift detection: which workspaces and stacks, who the pipelines are, what to watch
resources/drift.yml                    The drift schema and volume, and the terraform-drift-audit job
src/drift/                             config, plan findings, audit query and remediation (plain Python, unit tested)
src/jobs/drift_audit.py                The job: plan findings + system.access.audit -> findings and health tables
tests/                                 pytest
.github/workflows/drift.yml            Daily: terraform plan on every stack, drift issues, then the audit job
.github/workflows/ci.yml               Pull requests: unit tests. Run workflow: validate and deploy any branch
```

## Terraform drift detection and audit

Terraform code in `dbx-platform-infra-azure` is the intent; the workspaces are the reality. Drift is the gap between
them: a setting clicked in the UI, a grant given by hand, an object created that no code knows about. Governance
defines the target state; validation proves it still holds, every day, and says what to do when it doesn't.

Two sides, because each sees what the other can't:

| Side | Tool | Answers | Blind spot |
| --- | --- | --- | --- |
| Plan | `terraform plan -detailed-exitcode` per stack, in GitHub Actions | What differs from code, and what an apply would do | Who did it; objects Terraform never created |
| Audit | `system.access.audit`, in the governance workspace | Who changed what, when; objects created outside Terraform | Whether it matters to the code |

```
06:00 UTC   .github/workflows/drift.yml
  stacks    read config/drift.yml -> one job per environment and stack
  plan      (x4) terragrunt plan -detailed-exitcode on live/<env>/<stack>, infra repo main, read with the deploy identity
              0 clean        -> close the stack's open drift issue
              2 changes      -> findings (JSON): changed / deleted outside Terraform, or code not applied
                                -> open or update the issue "Drift: <env> / <stack>" (labels drift, severity:<level>)
              1 error        -> the job fails
  audit     upload the findings to /Volumes/dbxarchitectlab_governance/drift/plan_findings/<run id>/
            databricks bundle run drift_audit   (governance workspace, as sp-dbx-governance)
              system.access.audit: watched changes in dev and prod by anyone but the pipelines
              -> dbxarchitectlab_governance.drift.findings   one row per finding, both sides, with the remediation
              -> dbxarchitectlab_governance.drift.health     one row per environment and run: in sync or not
```

Every finding ends in one of two places: **revert** (apply the code again: the manual change is undone) or **adopt**
(the change was right: put it in code, review, merge, apply). Either way, the next morning's plan proves it with exit
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
5. **Run it:** Actions › Terraform drift › Run workflow. After that it runs every day at 06:00 UTC.

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
