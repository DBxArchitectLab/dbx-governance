"""Layers 2 and 3, judge: the standard as rules a machine can evaluate, against what the collectors observed.

A rule reads normalized observations and the expected state (config/governance.yml, standard); it never calls an API.
Every check returns PASS or FAIL with a severity, the evidence, and how to fix it; for Unity Catalog grants and owners,
also the SQL that fixes it (to review and run by the owner, never automatically). A failure that matches an explicit,
unexpired exception becomes EXCEPTION. A domain that could not be read becomes ERROR (COL-00).

Observations come from four sources (SOURCES); a rule names the one it was written for, and every check records the
source of the evidence it judged, so the results say which source found what. Two scopes: workspace rules run once
per workspace in the estate; account rules run once for the whole account.

Add a rule: write a function that yields check(...) results, decorate it with @rule, add a test. Nothing else changes.
"""
import fnmatch
import re
from dataclasses import dataclass

from govern import config as cfgmod

SEVERITIES = ("critical", "high", "medium", "low")
PASS, FAIL, EXCEPTION, ERROR = "PASS", "FAIL", "EXCEPTION", "ERROR"
WORKSPACE_API, INFORMATION_SCHEMA, ACCOUNT_API, SYSTEM_TABLES = (
    "workspace_api", "information_schema", "account_api", "system_tables")
SOURCES = (WORKSPACE_API, INFORMATION_SCHEMA, ACCOUNT_API, SYSTEM_TABLES)
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SQL_TYPE = {"catalog": "CATALOG", "schema": "SCHEMA", "external_location": "EXTERNAL LOCATION", "table": "TABLE"}


@dataclass(frozen=True)
class Rule:
    id: str
    domain: str
    title: str
    severity: dict          # tier -> severity
    fn: object
    source: str             # the source it was written for (a check can also judge evidence from another)
    scope: str              # workspace | account


RULES = []


def rule(rule_id, domain, title, prod, nonprod=None, source=WORKSPACE_API, scope="workspace"):
    """Register a rule. prod / nonprod: its severity in that tier; nonprod=None means the rule is prod only. Account
    rules use their prod severity."""
    def register(fn):
        sev = {"prod": prod, **({"nonprod": nonprod} if nonprod else {})}
        RULES.append(Rule(rule_id, domain, title, sev, fn, source, scope))
        return fn
    return register


def check(object_type, obj, ok, evidence, remediation="", sql="", principal="", source=None):
    return {"object_type": object_type, "object": obj, "ok": ok, "evidence": evidence,
            "remediation": "" if ok else remediation, "remediation_sql": "" if ok else sql, "principal": principal,
            "source": source}


# ---------------------------------------------------------------- helpers
def kind(principal, cfg):
    """user | service_principal | group, and whether the standard approves it."""
    if "@" in principal:
        return "user", False
    if _GUID.match(principal):
        return "service_principal", principal in cfgmod.service_principal_ids(cfg)
    return "group", principal in cfg["standard"]["approved_groups"]


def q(name):
    return ".".join(f"`{part.replace('`', '``')}`" for part in name.split("."))


def privs_sql(privileges):
    return ", ".join(p.replace("_", " ") for p in privileges)


def of(obs, object_type):
    return [o for o in obs if o["object_type"] == object_type]


def covered(obs, name):
    """Whether a source that reports 'nothing found' as a result actually ran (see systables.py)."""
    return any(o["object_type"] == "coverage" and o["name"] == name for o in obs)


def under(path, roots):
    return any(path.rstrip("/").lower().startswith(r.rstrip("/").lower()) for r in roots)


def grant_text(g):
    a = g["attrs"]
    return f"{', '.join(a['privileges'])} on {a['securable_type'].replace('_', ' ')} {a['securable']} to {a['principal']}"


def _on(a):
    return f"{_SQL_TYPE[a['securable_type']]} {q(a['securable'])}"


# ---------------------------------------------------------------- identity and access
@rule("IAM-01", "identity", "No direct grants to users: grant to groups", prod="high", nonprod="medium")
def no_user_grants(obs, env, cfg):
    for g in of(obs, "grant"):
        a = g["attrs"]
        is_user = kind(a["principal"], cfg)[0] == "user"
        sql = (f"-- Give access to the owning group, then remove the person\n"
               f"GRANT {privs_sql(a['privileges'])} ON {_on(a)} TO `{env['owner']}`;\n"
               f"REVOKE {privs_sql(a['privileges'])} ON {_on(a)} FROM `{a['principal']}`;")
        yield check("grant", a["securable"], not is_user, grant_text(g),
                    f"Grant to a group ({env['owner']} or an IdP group) and revoke the user", sql, a["principal"],
                    g.get("source"))


@rule("IAM-02", "identity", "Grants only to approved groups and service principals", prod="high", nonprod="medium")
def approved_principals(obs, env, cfg):
    for g in of(obs, "grant"):
        a = g["attrs"]
        k, approved = kind(a["principal"], cfg)
        if k == "user":
            continue    # IAM-01
        sql = f"REVOKE {privs_sql(a['privileges'])} ON {_on(a)} FROM `{a['principal']}`;"
        yield check("grant", a["securable"], approved, f"{grant_text(g)} ({k.replace('_', ' ')})",
                    "Add the principal to the standard (approved_groups or service_principals) through a pull "
                    "request, or revoke the grant", sql, a["principal"], g.get("source"))


@rule("IAM-03", "identity", "High-risk privileges only for admin groups and pipelines", prod="critical",
      nonprod="high")
def high_risk_privileges(obs, env, cfg):
    std = cfg["standard"]
    allowed = set(std["admin_groups"]) | cfgmod.service_principal_ids(cfg)
    for g in of(obs, "grant"):
        a = g["attrs"]
        risky = [p for p in a["privileges"] if p in std["high_risk_privileges"]]
        if not risky:
            continue
        sql = f"REVOKE {privs_sql(risky)} ON {_on(a)} FROM `{a['principal']}`;"
        yield check("grant", a["securable"], a["principal"] in allowed, grant_text(g),
                    f"Revoke {', '.join(risky)}: grant the least privilege the work needs", sql, a["principal"],
                    g.get("source"))


@rule("IAM-04", "identity", "Workspace admins are admin groups or pipelines, never people", prod="high",
      nonprod="medium", source=ACCOUNT_API)
def workspace_admins(obs, env, cfg):
    allowed_groups = set(cfg["standard"]["admin_groups"])
    sps = cfgmod.service_principal_ids(cfg)
    for a in of(obs, "workspace_assignment"):
        at = a["attrs"]
        if "ADMIN" not in at["permissions"]:
            continue
        ptype = at["principal_type"]
        ok = (ptype == "group" and at["principal"] in allowed_groups) or (ptype == "service_principal"
                                                                          and at["principal"] in sps)
        yield check("workspace_assignment", f"{env['name']} workspace", ok,
                    f"{at['principal']} ({ptype.replace('_', ' ')}) is a workspace admin of {env['name']}",
                    "Remove the direct ADMIN assignment (account console › Workspaces › Permissions); give admin "
                    f"through {', '.join(sorted(allowed_groups))}", "", at["principal"], a.get("source"))


# ---------------------------------------------------------------- ownership and naming
@rule("OWN-01", "ownership", "Owned by a group or a pipeline, never by a person", prod="high", nonprod="medium")
def owned_by_group(obs, env, cfg):
    owned = [o for o in of(obs, "catalog") if o["name"] == env["catalog"]]
    owned += of(obs, "schema") + of(obs, "external_location") + of(obs, "table")
    for o in owned:
        owner = o.get("owner") or ""
        k, approved = kind(owner, cfg) if owner else ("none", False)
        what = ("VIEW" if o["attrs"].get("table_type") == "VIEW" else "TABLE") if o["object_type"] == "table" \
            else _SQL_TYPE[o["object_type"]]
        sql = f"ALTER {what} {q(o['name'])} OWNER TO `{env['owner']}`;"
        yield check(o["object_type"], o["name"], approved, f"{what.lower()} {o['name']} is owned by "
                    f"{owner or 'nobody'} ({k.replace('_', ' ')})",
                    f"Hand ownership to the owning group ({env['owner']})", sql, owner, o.get("source"))


@rule("NAM-01", "naming", "Schema names follow the naming standard", prod="low", nonprod="low")
def schema_names(obs, env, cfg):
    pattern = re.compile(cfg["standard"]["schema_name_pattern"])
    for s in of(obs, "schema"):
        name = s["attrs"]["schema"]
        yield check("schema", s["name"], bool(pattern.match(name)),
                    f"schema name {name!r} against {pattern.pattern}",
                    "Create a schema with a compliant name, move the objects, drop the old one")


# ---------------------------------------------------------------- isolation and storage
@rule("ISO-01", "isolation", "Only the environment's own catalog is reachable", prod="critical", nonprod="high")
def catalog_isolation(obs, env, cfg):
    shared = set(cfg["standard"]["shared_catalogs"])
    others = {e["catalog"]: name for name, e in cfg["estate"].items() if e["catalog"] != env["catalog"]}
    cats = of(obs, "catalog")
    own = next((c for c in cats if c["name"] == env["catalog"]), None)
    if own is None:
        yield check("catalog", env["catalog"], False, f"catalog {env['catalog']} was not found in the workspace",
                    "Check the estate entry, or the collector identity's access to the catalog")
    else:
        yield check("catalog", own["name"], own["attrs"]["isolation_mode"] == "ISOLATED",
                    f"catalog {own['name']} is {own['attrs']['isolation_mode']}",
                    "Make the catalog ISOLATED and bind it to its own workspace (Terraform: isolation_mode, "
                    "databricks_workspace_binding)")
    for c in cats:
        if c["name"] == env["catalog"] or c["name"] in shared or not c["attrs"]["accessible_here"]:
            continue
        whose = f"{others[c['name']]}'s catalog" if c["name"] in others else "a catalog outside the estate"
        yield check("catalog", c["name"], False, f"catalog {c['name']} ({whose}) is reachable from {env['name']}",
                    "Bind the catalog to its own workspace only, or add it to standard.shared_catalogs")


@rule("ISO-02", "isolation", "No data flows into this environment from another one", prod="critical",
      nonprod="high", source=SYSTEM_TABLES)
def cross_environment_lineage(obs, env, cfg):
    flows = [f for f in of(obs, "lineage_flow") if f["attrs"]["target_catalog"] == env["catalog"]]
    for f in flows:
        a = f["attrs"]
        yield check("table", a["target_table"], False,
                    f"{a['source_table']} ({a['source_environment']}) → {a['target_table']}: {a['events']} "
                    f"lineage events, last {a['last_seen']}, by {a['created_by']}",
                    "Stop the flow; promote code, not data: rebuild the table from this environment's sources",
                    "", a["created_by"], f.get("source"))
    if not flows and covered(obs, "lineage"):
        days = cfg["system_tables"]["lookback_days"]
        yield check("catalog", env["catalog"], True, f"no table in {env['catalog']} read another environment's "
                    f"catalog in the last {days} days (system.access.table_lineage)", source=SYSTEM_TABLES)


@rule("STO-01", "storage", "Catalog and schema storage under the environment's roots", prod="high",
      nonprod="medium")
def managed_storage(obs, env, cfg):
    for o in [c for c in of(obs, "catalog") if c["name"] == env["catalog"]] + of(obs, "schema"):
        root = o["attrs"].get("storage_root") or ""
        if not root:
            continue    # a schema without its own root stores under the catalog's
        yield check(o["object_type"], o["name"], under(root, env["storage_roots"]),
                    f"{o['object_type']} {o['name']} stores at {root}",
                    f"Move the managed location under {', '.join(env['storage_roots'])}")


@rule("STO-02", "storage", "External locations usable here point at this environment's storage", prod="high",
      nonprod="high")
def external_location_storage(obs, env, cfg):
    for loc in of(obs, "external_location"):
        url = loc["attrs"]["url"]
        yield check("external_location", loc["name"], under(url, env["storage_roots"]),
                    f"external location {loc['name']} ({loc['attrs']['isolation_mode']}) is usable from "
                    f"{env['name']} and points at {url}",
                    "Bind the external location to the workspace whose storage it holds (isolation mode ISOLATED "
                    "+ workspace binding)")


# ---------------------------------------------------------------- compute and jobs
@rule("CMP-01", "compute", "All-purpose clusters use a cluster policy", prod="high", nonprod="medium")
def cluster_policy(obs, env, cfg):
    names = {p["attrs"]["policy_id"]: p["name"] for p in of(obs, "cluster_policy")}
    for c in of(obs, "cluster"):
        pid = c["attrs"]["policy_id"]
        yield check("cluster", c["name"], bool(pid), f"cluster {c['name']} (by {c.get('owner') or 'unknown'}) "
                    + (f"uses policy {names.get(pid, pid)}" if pid else "has no cluster policy"),
                    "Recreate the cluster from an approved policy; remove unrestricted cluster creation", "",
                    c.get("owner") or "")


@rule("CMP-02", "compute", "All-purpose clusters carry the required tags", prod="medium", nonprod="low")
def cluster_tags(obs, env, cfg):
    required = cfg["standard"]["compute"]["required_tags"]
    for c in of(obs, "cluster"):
        missing = [t for t in required if t not in c["attrs"]["tags"]]
        yield check("cluster", c["name"], not missing, f"cluster {c['name']} tags: "
                    f"{', '.join(c['attrs']['tags']) or 'none'}" + (f"; missing {', '.join(missing)}" if missing else ""),
                    f"Add the tags {', '.join(missing)} (best: fixed in the cluster policy)")


@rule("CMP-03", "compute", "Clusters and warehouses stop when idle", prod="medium", nonprod="medium")
def auto_stop(obs, env, cfg):
    c_max = cfg["standard"]["compute"]["max_autotermination_minutes"]
    w_max = cfg["standard"]["compute"]["max_warehouse_auto_stop_minutes"]
    for c in of(obs, "cluster"):
        m = c["attrs"]["autotermination_minutes"] or 0
        yield check("cluster", c["name"], 0 < m <= c_max, f"cluster {c['name']} "
                    + (f"auto-terminates after {m} minutes" if m else "never auto-terminates") + f" (max {c_max})",
                    f"Set auto-termination to at most {c_max} minutes")
    for w in of(obs, "warehouse"):
        m = w["attrs"]["auto_stop_mins"] or 0
        yield check("warehouse", w["name"], 0 < m <= w_max, f"warehouse {w['name']} "
                    + (f"auto-stops after {m} minutes" if m else "never auto-stops") + f" (max {w_max})",
                    f"Set auto-stop to at most {w_max} minutes")


@rule("CMP-04", "compute", "Job clusters use a cluster policy", prod="high", nonprod="medium", source=SYSTEM_TABLES)
def job_cluster_policy(obs, env, cfg):
    jobs = of(obs, "job_cluster")
    for j in jobs:
        a = j["attrs"]
        yield check("job_cluster", j["name"], a["without_policy"] == 0,
                    f"{j['name']}: {a['without_policy']} of {a['runs']} job clusters without a cluster policy "
                    f"(owner {j.get('owner') or 'unknown'})",
                    "Give the job's compute a policy (bundle: policy_id on the job cluster), or move it to "
                    "serverless", "", j.get("owner") or "", j.get("source"))
    if not jobs and covered(obs, "job_clusters"):
        days = cfg["system_tables"]["lookback_days"]
        yield check("workspace", env["name"], True, f"no classic job clusters in the last {days} days "
                    f"(system.compute.clusters)", source=SYSTEM_TABLES)


@rule("JOB-01", "jobs", "Production jobs run as a service principal", prod="high")
def jobs_run_as_sp(obs, env, cfg):
    for j in of(obs, "job"):
        run_as = j["attrs"]["run_as"]
        k, approved = kind(run_as, cfg) if run_as else ("none", False)
        yield check("job", j["name"], k == "service_principal" and approved,
                    f"job {j['name']} runs as {run_as or 'unknown'} ({k.replace('_', ' ')})",
                    "Deploy the job from a bundle with run_as set to the pipeline's service principal", "", run_as)


# ---------------------------------------------------------------- workspace configuration
@rule("WSC-01", "workspace", "Workspace settings match the standard", prod="medium", nonprod="low")
def workspace_settings(obs, env, cfg):
    expected = cfg["standard"]["workspace_settings"].get(env["tier"]) or {}
    seen = {s["name"]: s["attrs"]["value"] for s in of(obs, "workspace_setting")}
    for key, want in expected.items():
        got = seen.get(key)
        yield check("workspace_setting", key, str(got).lower() == str(want).lower(),
                    f"{key} is {got if got is not None else 'not set'} (standard: {want})",
                    f"Set {key} to {want} (Terraform: databricks_workspace_conf)")


# ---------------------------------------------------------------- the estate (account scope)
@rule("EST-01", "estate", "Every workspace on the metastore is governed, or excluded on purpose", prod="high",
      source=ACCOUNT_API, scope="account")
def estate_complete(obs, env, cfg):
    estate = {e["workspace_id"]: name for name, e in cfg["estate"].items()}
    excluded = {x["workspace_id"]: x["reason"] for x in cfg["account"].get("not_governed") or []}
    for a in of(obs, "metastore_assignment"):
        at = a["attrs"]
        ws, label = at["workspace_id"], at.get("workspace_name") or str(at["workspace_id"])
        if ws in estate:
            ok, why = True, f"in the estate as {estate[ws]}"
        elif ws in excluded:
            ok, why = True, f"not governed on purpose: {excluded[ws]}"
        else:
            ok, why = False, "not in the estate, and not excluded"
        yield check("workspace", label, ok, f"workspace {label} ({ws}) on metastore {at['metastore_name']}: {why}",
                    "Add it to estate in config/governance.yml, or to account.not_governed with a reason",
                    source=a.get("source"))


@rule("EST-02", "estate", "The estate shares one metastore", prod="high", source=ACCOUNT_API, scope="account")
def one_metastore(obs, env, cfg):
    on = {a["attrs"]["workspace_id"]: a["attrs"] for a in of(obs, "metastore_assignment")}
    if not on:
        return
    used = set()
    for name, e in cfg["estate"].items():
        at = on.get(e["workspace_id"])
        if at:
            used.add(at["metastore_name"])
        yield check("workspace", name, at is not None, f"{name} ({e['workspace_id']}) is "
                    + (f"on metastore {at['metastore_name']}" if at else "not assigned to any metastore"),
                    "Assign the workspace to the region's metastore (Terraform: metastore assignment)",
                    source=ACCOUNT_API)
    if len(used) > 1:
        yield check("estate", "metastores", False, f"the estate spans {len(used)} metastores: {', '.join(sorted(used))}",
                    "One metastore per region: move the workspaces to it", source=ACCOUNT_API)


# ---------------------------------------------------------------- evaluation
def _exception(cfg, rule_id, environment, obj, principal, today):
    for x in cfg.get("exceptions") or []:
        listed = [x["rule"]] if isinstance(x["rule"], str) else x["rule"]
        if rule_id in listed and x["environment"] in ("*", environment) and fnmatch.fnmatchcase(obj, x["object"]) \
                and fnmatch.fnmatchcase(principal or "", x.get("principal") or "*"):
            return x
    return None


def scope_env(cfg, environment):
    """The context a rule sees: a workspace in the estate, or the account."""
    if environment == "account":
        return {"name": "account", "tier": "prod", "owner": cfg["account"]["owner"], "catalog": None,
                "workspace_id": None}
    return {**cfg["estate"][environment], "name": environment}


def evaluate(cfg, snapshot, today):
    """Every rule of the snapshot's scope that applies to its tier, against its observations. Returns result rows."""
    environment = snapshot["environment"]
    scope = "account" if environment == "account" else "workspace"
    env = scope_env(cfg, environment)
    tier = env["tier"]
    results = []
    for err in snapshot.get("errors") or []:
        results.append(dict(rule_id="COL-00", domain="collection", title="Every domain was collected",
                            object_type="domain", object=err["domain"], principal="", status=ERROR, severity="high",
                            evidence=err["error"], remediation="Fix the collector identity's access and rerun",
                            remediation_sql="", exception="", source=err.get("source", WORKSPACE_API)))
    unread = any(err["domain"] == "snapshot" for err in snapshot.get("errors") or [])
    for r in [] if unread else RULES:    # no snapshot at all: nothing to judge, only the error
        if r.scope != scope or tier not in r.severity:
            continue
        for c in r.fn(snapshot["observations"], env, cfg):
            status, note = (PASS, "") if c["ok"] else (FAIL, "")
            if not c["ok"]:
                x = _exception(cfg, r.id, environment, c["object"], c["principal"], today)
                if x and x["expires"] >= today:
                    status = EXCEPTION
                    note = f"{x['reason']} Approved by {x['approved_by']}, until {x['expires']}."
                elif x:
                    note = f"The exception expired on {x['expires']}."
            results.append(dict(rule_id=r.id, domain=r.domain, title=r.title, object_type=c["object_type"],
                                object=c["object"], principal=c["principal"], status=status,
                                severity=r.severity[tier], evidence=c["evidence"], remediation=c["remediation"],
                                remediation_sql=c["remediation_sql"], exception=note,
                                source=c["source"] or r.source))
    for row in results:
        row.update(environment=environment, workspace_id=snapshot.get("workspace_id"), owner=env["owner"])
    return results


def compliance(environment, results):
    """One scope's scorecard. Compliant = no failures and nothing unread; exceptions count as handled."""
    n = lambda s: sum(r["status"] == s for r in results)  # noqa: E731
    failed = [r for r in results if r["status"] == FAIL]
    total = len(results)
    return dict(environment=environment, checks=total, passed=n(PASS), failed=n(FAIL), exceptions=n(EXCEPTION),
                errors=n(ERROR), failed_critical=sum(r["severity"] == "critical" for r in failed),
                failed_high=sum(r["severity"] == "high" for r in failed),
                score=round(100.0 * (n(PASS) + n(EXCEPTION)) / total, 1) if total else 0.0,
                compliant=n(FAIL) == 0 and n(ERROR) == 0)
