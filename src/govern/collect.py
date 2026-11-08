"""Layer 1, observe, per workspace: its actual state through two sources, normalized into one shape.

  workspace_api        the workspace's REST APIs: catalogs and bindings, the environment catalog's schemas, owners
                       and grants, external locations, clusters, cluster policies, SQL warehouses, jobs, settings
  information_schema   SELECT statements on the workspace's own SQL warehouse (estate.<env>.sql_warehouse), against
                       <catalog>.information_schema: tables and views, their owners and their direct grants. It runs
                       inside the workspace because the catalog is bound to it: no other workspace can see it.

A collector records what exists; it knows nothing about the standard. Every observation is
    {"object_type": ..., "name": ..., "owner": ... or None, "attrs": {...}, "source": ...}
and a snapshot is one workspace's observations plus anything that could not be read (errors). A check whose data
could not be read is reported as an error, never as a pass.

`call(method, path, query=None, body=None)` is the workspace's REST API (the CLI passes the Databricks SDK's
api_client.do), so this module needs no SDK and the tests feed it canned responses. Read-only: GET requests, and
SELECT statements through the SQL Statement Execution API.
"""
import time
from datetime import datetime, timezone
from urllib.parse import quote

UC = "/api/2.1/unity-catalog"
SKIP_SCHEMAS = {"information_schema"}
ALL_PURPOSE = {"UI", "API"}     # cluster_source of all-purpose clusters; job and pipeline clusters are ephemeral
WORKSPACE_API, INFORMATION_SCHEMA = "workspace_api", "information_schema"


def _pages(call, path, key, query=None, token_param="page_token"):
    query = dict(query or {})
    while True:
        page = call("GET", path, query) or {}
        yield from page.get(key) or []
        token = page.get("next_page_token")
        if not token:
            return
        query[token_param] = token


def _isolated(info):
    return "ISOLATED" in str(info.get("isolation_mode") or "")


def _bound_workspaces(call, securable_type, name):
    rows = _pages(call, f"{UC}/bindings/{securable_type}/{quote(name, safe='')}", "bindings")
    return sorted(int(b["workspace_id"]) for b in rows)


def _grant(securable_type, full_name, principal, privileges):
    return {"object_type": "grant", "name": f"{securable_type}:{full_name}", "owner": None,
            "attrs": {"securable_type": securable_type, "securable": full_name, "principal": principal,
                      "privileges": sorted(set(privileges))}}


def _grants(call, securable_type, full_name):
    page = call("GET", f"{UC}/permissions/{securable_type}/{quote(full_name, safe='')}", None) or {}
    return [_grant(securable_type, full_name, a["principal"], a.get("privileges") or [])
            for a in page.get("privilege_assignments") or []]


# ---------------------------------------------------------------- workspace API
def catalogs(call, env):
    out = []
    for c in _pages(call, f"{UC}/catalogs", "catalogs"):
        bound = _bound_workspaces(call, "catalog", c["name"]) if _isolated(c) else None
        out.append({"object_type": "catalog", "name": c["name"], "owner": c.get("owner"),
                    "attrs": {"isolation_mode": "ISOLATED" if _isolated(c) else "OPEN", "bound_workspaces": bound,
                              "accessible_here": bound is None or env["workspace_id"] in bound,
                              "storage_root": c.get("storage_root") or "", "catalog_type": c.get("catalog_type", "")}})
    return out


def own_catalog(call, env):
    """The environment's own catalog, walked: its grants, and every schema with its owner, storage and grants."""
    cat = env["catalog"]
    out = _grants(call, "catalog", cat)
    for s in _pages(call, f"{UC}/schemas", "schemas", {"catalog_name": cat}):
        if s["name"] in SKIP_SCHEMAS:
            continue
        full = s.get("full_name") or f"{cat}.{s['name']}"
        out.append({"object_type": "schema", "name": full, "owner": s.get("owner"),
                    "attrs": {"catalog": cat, "schema": s["name"], "storage_root": s.get("storage_root") or ""}})
        out += _grants(call, "schema", full)
    return out


def external_locations(call, env):
    out = []
    for loc in _pages(call, f"{UC}/external-locations", "external_locations"):
        bound = _bound_workspaces(call, "external_location", loc["name"]) if _isolated(loc) else None
        if bound is not None and env["workspace_id"] not in bound:
            continue    # bound elsewhere: not usable from this workspace
        out.append({"object_type": "external_location", "name": loc["name"], "owner": loc.get("owner"),
                    "attrs": {"url": loc.get("url", ""), "isolation_mode": "OPEN" if bound is None else "ISOLATED"}})
        out += _grants(call, "external_location", loc["name"])
    return out


def compute(call, env):
    out = []
    for c in _pages(call, "/api/2.1/clusters/list", "clusters", {"page_size": 100}):
        if c.get("cluster_source") not in ALL_PURPOSE:
            continue
        out.append({"object_type": "cluster", "name": c.get("cluster_name", c["cluster_id"]),
                    "owner": c.get("creator_user_name"),
                    "attrs": {"cluster_id": c["cluster_id"], "policy_id": c.get("policy_id") or "",
                              "tags": sorted((c.get("custom_tags") or {}).keys()),
                              "autotermination_minutes": c.get("autotermination_minutes", 0),
                              "data_security_mode": c.get("data_security_mode", ""), "state": c.get("state", "")}})
    for p in (call("GET", "/api/2.0/policies/clusters/list", None) or {}).get("policies") or []:
        out.append({"object_type": "cluster_policy", "name": p["name"], "owner": p.get("creator_user_name"),
                    "attrs": {"policy_id": p["policy_id"]}})
    for w in (call("GET", "/api/2.0/sql/warehouses", None) or {}).get("warehouses") or []:
        out.append({"object_type": "warehouse", "name": w["name"], "owner": w.get("creator_name"),
                    "attrs": {"warehouse_id": w["id"], "auto_stop_mins": w.get("auto_stop_mins", 0)}})
    return out


def jobs(call, env):
    out = []
    for j in _pages(call, "/api/2.2/jobs/list", "jobs", {"limit": 100}):
        settings = j.get("settings") or {}
        run_as = settings.get("run_as") or {}
        out.append({"object_type": "job", "name": settings.get("name", str(j["job_id"])),
                    "owner": j.get("creator_user_name"),
                    "attrs": {"job_id": j["job_id"], "run_as": run_as.get("service_principal_name")
                              or run_as.get("user_name") or j.get("creator_user_name") or ""}})
    return out


def settings(call, env, keys):
    if not keys:
        return []
    values = call("GET", "/api/2.0/workspace-conf", {"keys": ",".join(keys)}) or {}
    return [{"object_type": "workspace_setting", "name": k, "owner": None, "attrs": {"value": values.get(k)}}
            for k in keys]


# ---------------------------------------------------------------- information schema
def run_sql(call, warehouse_id, statement, timeout_s=600, sleep=time.sleep):
    """One SELECT through the SQL Statement Execution API; the rows as dicts. Waits for a stopped warehouse."""
    res = call("POST", "/api/2.0/sql/statements", None, {
        "warehouse_id": warehouse_id, "statement": statement, "wait_timeout": "50s", "on_wait_timeout": "CONTINUE",
        "format": "JSON_ARRAY", "disposition": "INLINE"})
    waited = 0
    while res["status"]["state"] in ("PENDING", "RUNNING"):
        if waited >= timeout_s:
            call("POST", f"/api/2.0/sql/statements/{res['statement_id']}/cancel", None, {})
            raise TimeoutError(f"statement still {res['status']['state']} after {timeout_s} s")
        sleep(5)
        waited += 5
        res = call("GET", f"/api/2.0/sql/statements/{res['statement_id']}", None)
    if res["status"]["state"] != "SUCCEEDED":
        raise RuntimeError(f"statement {res['status']['state']}: "
                           f"{(res['status'].get('error') or {}).get('message', '')}"[:300])
    columns = [c["name"] for c in res["manifest"]["schema"]["columns"]]
    result = res.get("result") or {}
    rows = list(result.get("data_array") or [])
    while result.get("next_chunk_index") is not None:
        result = call("GET", f"/api/2.0/sql/statements/{res['statement_id']}/result/chunks/"
                             f"{result['next_chunk_index']}", None) or {}
        rows += result.get("data_array") or []
    return [dict(zip(columns, r)) for r in rows]


def information_schema(call, env):
    """Tables and views of the environment's catalog, their owners and their direct grants, from its information
    schema, on the workspace's own warehouse."""
    name = env["sql_warehouse"]
    wh = next((w for w in (call("GET", "/api/2.0/sql/warehouses", None) or {}).get("warehouses") or []
               if w["name"] == name), None)
    if wh is None:
        raise LookupError(f"SQL warehouse {name!r} not found in the workspace (estate.{env['name']}.sql_warehouse)")
    cat = env["catalog"].replace("`", "``")
    tables = run_sql(call, wh["id"], f"SELECT table_schema, table_name, table_type, table_owner FROM `{cat}`."
                                     f"information_schema.tables WHERE table_schema <> 'information_schema'")
    privileges = run_sql(call, wh["id"], f"SELECT grantee, table_schema, table_name, privilege_type FROM `{cat}`."
                                         f"information_schema.table_privileges "
                                         f"WHERE table_schema <> 'information_schema'")
    out = [{"object_type": "table", "name": f"{env['catalog']}.{t['table_schema']}.{t['table_name']}",
            "owner": t["table_owner"], "attrs": {"schema": t["table_schema"], "table_type": t["table_type"]}}
           for t in tables]
    per = {}
    for p in privileges:
        key = (f"{env['catalog']}.{p['table_schema']}.{p['table_name']}", p["grantee"])
        per.setdefault(key, set()).add(p["privilege_type"].replace(" ", "_").upper())
    out += [_grant("table", table, grantee, privs) for (table, grantee), privs in sorted(per.items())]
    return out


def drop_inherited(observations):
    """A table grant the information schema reports may be inherited from its schema or catalog, which the
    workspace API already reports there. Keep only what was granted on the table itself."""
    parents = {}
    for o in observations:
        if o["object_type"] == "grant" and o["attrs"]["securable_type"] in ("catalog", "schema"):
            a = o["attrs"]
            parents.setdefault((a["securable"], a["principal"]), set()).update(a["privileges"])
    out = []
    for o in observations:
        a = o.get("attrs") or {}
        if o["object_type"] == "grant" and a.get("securable_type") == "table":
            cat, schema, _ = a["securable"].split(".", 2)
            inherited = parents.get((cat, a["principal"]), set()) | parents.get((f"{cat}.{schema}", a["principal"]),
                                                                                  set())
            if "ALL_PRIVILEGES" in inherited:
                continue
            own = [p for p in a["privileges"] if p not in inherited]
            if not own:
                continue
            o = {**o, "attrs": {**a, "privileges": own}}
        out.append(o)
    return out


# ---------------------------------------------------------------- one workspace
def snapshot(call, cfg, environment, now=None):
    """Everything one workspace exposes about the governed domains. One failing domain does not stop the others."""
    env = {**cfg["estate"][environment], "name": environment}
    keys = sorted((cfg["standard"]["workspace_settings"].get(env["tier"]) or {}).keys())
    domains = {"catalogs": (WORKSPACE_API, lambda: catalogs(call, env)),
               "own_catalog": (WORKSPACE_API, lambda: own_catalog(call, env)),
               "external_locations": (WORKSPACE_API, lambda: external_locations(call, env)),
               "compute": (WORKSPACE_API, lambda: compute(call, env)),
               "jobs": (WORKSPACE_API, lambda: jobs(call, env)),
               "settings": (WORKSPACE_API, lambda: settings(call, env, keys)),
               "information_schema": (INFORMATION_SCHEMA, lambda: information_schema(call, env))}
    observations, errors = [], []
    for domain, (source, read) in domains.items():
        try:
            observations += [{**o, "source": source} for o in read()]
        except Exception as exc:  # noqa: BLE001  report it; never let one domain hide the rest
            errors.append({"domain": domain, "source": source, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    return {"environment": environment, "workspace_id": env["workspace_id"], "tier": env["tier"],
            "collected_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            "observations": drop_inherited(observations), "errors": errors}
