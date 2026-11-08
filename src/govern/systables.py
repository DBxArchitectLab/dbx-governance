"""Layer 1, observe, centrally: system tables, queried in the governance workspace, for every workspace at once.

  lineage        system.access.table_lineage: table-to-table flows between the estate's catalogs (ISO-02)
  job_clusters   system.compute.clusters: classic job clusters per job, and how many ran without a policy (CMP-04)

One query covers the whole account, so this source scales to any number of workspaces without a collector each; the
price is latency (minutes to hours) and SELECT grants on the system schemas. `run(sql)` executes a query and returns
the rows as dicts (the notebook passes Spark), so this module is plain Python and the tests feed it rows.

A query that ran adds a "coverage" observation, so a rule can say "nothing found" (PASS) only when it really looked.
"""
SYSTEM_TABLES = "system_tables"


def _list(values):
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)


def queries(cfg):
    days = int(cfg["system_tables"]["lookback_days"])
    catalogs = _list(e["catalog"] for e in cfg["estate"].values())
    workspaces = _list(e["workspace_id"] for e in cfg["estate"].values())
    return {
        "lineage": f"""SELECT source_table_catalog, target_table_catalog, source_table_full_name,
       target_table_full_name, created_by, max(event_time) AS last_seen, count(*) AS events
FROM system.access.table_lineage
WHERE event_date >= date_sub(current_date(), {days})
  AND source_table_catalog IN ({catalogs}) AND target_table_catalog IN ({catalogs})
  AND source_table_catalog <> target_table_catalog
GROUP BY ALL""",
        "job_clusters": f"""SELECT workspace_id, regexp_extract(cluster_name, '^job-([0-9]+)-', 1) AS job_id,
       count(DISTINCT cluster_id) AS runs,
       count(DISTINCT CASE WHEN policy_id IS NULL THEN cluster_id END) AS without_policy,
       max(owned_by) AS owned_by
FROM system.compute.clusters
WHERE workspace_id IN ({workspaces}) AND cluster_source = 'JOB'
  AND change_date >= date_sub(current_date(), {days})
GROUP BY ALL""",
    }


def collect(run, cfg):
    """Per environment: {"observations": [...], "errors": [...]}. A failed query is an error in every environment."""
    by_catalog = {e["catalog"]: env for env, e in cfg["estate"].items()}
    by_workspace = {int(e["workspace_id"]): env for env, e in cfg["estate"].items()}
    out = {env: {"observations": [], "errors": []} for env in cfg["estate"]}
    for name, sql in queries(cfg).items():
        try:
            rows = run(sql)
        except Exception as exc:  # noqa: BLE001
            for env in out:
                out[env]["errors"].append({"domain": f"system_tables.{name}", "source": SYSTEM_TABLES,
                                           "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            continue
        for env in out:
            out[env]["observations"].append({"object_type": "coverage", "name": name, "owner": None,
                                             "source": SYSTEM_TABLES, "attrs": {"rows": len(rows)}})
        for r in rows:
            if name == "lineage":
                env = by_catalog.get(r["target_table_catalog"])
                if env:
                    out[env]["observations"].append({
                        "object_type": "lineage_flow", "owner": r["created_by"], "source": SYSTEM_TABLES,
                        "name": f"{r['source_table_full_name']} -> {r['target_table_full_name']}",
                        "attrs": {"source_catalog": r["source_table_catalog"],
                                  "source_environment": by_catalog.get(r["source_table_catalog"], "?"),
                                  "target_catalog": r["target_table_catalog"],
                                  "source_table": r["source_table_full_name"],
                                  "target_table": r["target_table_full_name"], "created_by": r["created_by"],
                                  "last_seen": str(r["last_seen"]), "events": int(r["events"])}})
            else:
                env = by_workspace.get(int(r["workspace_id"]))
                if env:
                    out[env]["observations"].append({
                        "object_type": "job_cluster", "name": f"job {r['job_id'] or '?'}", "owner": r["owned_by"],
                        "source": SYSTEM_TABLES,
                        "attrs": {"job_id": r["job_id"], "runs": int(r["runs"]),
                                  "without_policy": int(r["without_policy"])}})
    return out
