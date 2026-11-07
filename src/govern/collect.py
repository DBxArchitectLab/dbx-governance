"""Layer 1, observe: one workspace's actual state, read through its APIs and normalized into one shape.

A collector records what exists; it knows nothing about the standard. Every observation is
    {"object_type": ..., "name": ..., "owner": ... or None, "attrs": {...}}
and a snapshot is one workspace's observations plus anything that could not be read (errors). A check whose data
could not be read is reported as an error, never as a pass.

`call(method, path, query)` is the workspace's REST API (the CLI passes the Databricks SDK's api_client.do), so this
module needs no SDK and the tests feed it canned responses. Read-only: only GET requests.
"""
from datetime import datetime, timezone
from urllib.parse import quote

UC = "/api/2.1/unity-catalog"
SKIP_SCHEMAS = {"information_schema"}
ALL_PURPOSE = {"UI", "API"}     # cluster_source of all-purpose clusters; job and pipeline clusters are ephemeral


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


def _grants(call, securable_type, full_name):
    page = call("GET", f"{UC}/permissions/{securable_type}/{quote(full_name, safe='')}", None) or {}
    return [{"object_type": "grant", "name": f"{securable_type}:{full_name}", "owner": None,
             "attrs": {"securable_type": securable_type, "securable": full_name, "principal": a["principal"],
                       "privileges": sorted(a.get("privileges") or [])}}
            for a in page.get("privilege_assignments") or []]


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


def snapshot(call, cfg, environment, now=None):
    """Everything one workspace exposes about the governed domains. One failing domain does not stop the others."""
    env = cfg["estate"][environment]
    keys = sorted((cfg["standard"]["workspace_settings"].get(env["tier"]) or {}).keys())
    domains = {"catalogs": lambda: catalogs(call, env), "own_catalog": lambda: own_catalog(call, env),
               "external_locations": lambda: external_locations(call, env), "compute": lambda: compute(call, env),
               "jobs": lambda: jobs(call, env), "settings": lambda: settings(call, env, keys)}
    observations, errors = [], []
    for domain, read in domains.items():
        try:
            observations += read()
        except Exception as exc:  # noqa: BLE001  report it; never let one domain hide the rest
            errors.append({"domain": domain, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    return {"environment": environment, "workspace_id": env["workspace_id"], "tier": env["tier"],
            "collected_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            "observations": observations, "errors": errors}
