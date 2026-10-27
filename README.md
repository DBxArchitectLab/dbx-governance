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
and governs what happens in the dev, uat and prod workspaces: identities, access and audit.

| Repository | Owns |
| --- | --- |
| `dbx-platform-infra-azure` | Provisioning: metastore, workspaces, networking, bootstrap (catalog, storage, policies) |
| `dbx-lakehouse-ai-bi` | Data engineering and AI work, promoted dev → uat → prod through CI/CD |
| `dbx-governance` (this one) | Account-level governance across all workspaces: identity, access, audit |

Governance changes are not promoted from dev to prod like data pipelines: they act on all three workspaces from one
place, as an account admin, so they live here.

The bundle currently deploys no jobs. The workspace-local to account group migration that started here moved to
`dbx-lakehouse-ai-bi`, where each environment migrates its own workspace (serverless compute in the governance
workspace can't call the dev, uat and prod APIs without cross-workspace network policies).

## Layout

```
databricks.yml                         Databricks Asset Bundle, target "governance" (the governance workspace)
resources/                             Jobs and other resources the bundle deploys (one per *.yml; none yet)
.github/workflows/ci.yml               Run workflow: validate and deploy any branch to the governance workspace
```

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
