# Databricks notebook source
# MAGIC %md
# MAGIC # Workspace governance: validate centrally
# MAGIC
# MAGIC Runs in the **governance workspace** as `sp-dbx-governance`, started by the governance workflow
# MAGIC (`.github/workflows/governance.yml`) after its collectors, with the GitHub run ID.
# MAGIC
# MAGIC Four metadata sources, one standard:
# MAGIC
# MAGIC | Source | Collected | Where |
# MAGIC | --- | --- | --- |
# MAGIC | Workspace API | per workspace, by the workflow | `<run id>/<env>.json` |
# MAGIC | Information schema | per workspace, on its own SQL warehouse, by the workflow | `<run id>/<env>.json` |
# MAGIC | Account API | once, by the workflow | `<run id>/account.json` |
# MAGIC | System tables | here, for every workspace at once | `system.access.table_lineage`, `system.compute.clusters` |
# MAGIC
# MAGIC 1. Reads the run's snapshots and the standard they are checked against (the `governance.yml` uploaded with them).
# MAGIC 2. Queries the system tables.
# MAGIC 3. Checks every workspace, and the account, against the standard (`src/govern/rules.py`, through `validate.py`).
# MAGIC 4. Appends to `estate.inventory`, `estate.results` and `estate.compliance`, and writes one report per scope
# MAGIC    for the workflow's issues.
# MAGIC
# MAGIC It never calls a dev or prod API. To check an earlier run again: **Run now with different parameters** ›
# MAGIC `run_id` = that run's GitHub run ID.

# COMMAND ----------

# MAGIC %pip install pyyaml --quiet

# COMMAND ----------

dbutils.widgets.text("run_id", "manual", "GitHub run ID")
dbutils.widgets.text("config", "", "Deployed config/governance.yml")

# COMMAND ----------

# The govern package is plain Python next to this notebook (src/govern), so it imports like any module.
import os
import sys

src = os.path.abspath(os.path.join(os.getcwd(), ".."))
if not os.path.isdir(os.path.join(src, "govern")):
    nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
    src = os.path.dirname(os.path.dirname(nb if nb.startswith("/Workspace") else "/Workspace" + nb))
sys.path.insert(0, src)

import json
from datetime import datetime, timezone
from pathlib import Path

from govern import config, report, rules, systables, validate

# COMMAND ----------

# MAGIC %md ## 1. The run's snapshots, and the standard they are checked against

# COMMAND ----------

run_id = dbutils.widgets.get("run_id")
deployed_path = dbutils.widgets.get("config") or os.path.join(os.path.dirname(src), "config", "governance.yml")
deployed = config.load(deployed_path)
out = deployed["output"]
folder = Path(f"/Volumes/{out['catalog']}/{out['schema']}/{out['volume']}/{run_id}")
# The standard travels with its snapshots, so the evidence says which one it was.
cfg = config.load(folder / "governance.yml") if (folder / "governance.yml").is_file() else deployed
schema = f"{out['catalog']}.{out['schema']}"

snapshots = validate.read_snapshots(folder)
for s in snapshots:
    print(f"{s['environment']}: {len(s['observations'])} observations, {len(s['errors'])} errors")
print(f"standard: {'the run' if cfg is not deployed else 'deployed'} copy; {len(rules.RULES)} rules")

# COMMAND ----------

# MAGIC %md ## 2. System tables: every workspace, one query each

# COMMAND ----------

for name, sql in systables.queries(cfg).items():
    print(f"-- {name}\n{sql}\n")
system = systables.collect(lambda sql: [r.asDict(recursive=True) for r in spark.sql(sql).collect()], cfg)
for env, part in system.items():
    print(f"{env}: {len(part['observations'])} observations, errors: {[e['domain'] for e in part['errors']] or 'none'}")

# COMMAND ----------

# MAGIC %md ## 3. Judge: every workspace, and the account, against the standard

# COMMAND ----------

inventory, results, scores, reports = validate.build(cfg, run_id, datetime.now(timezone.utc), snapshots, system)
print(f"{len(inventory)} objects observed; {len(results)} checks")

# COMMAND ----------

# MAGIC %md ## 4. Record: inventory, results, compliance

# COMMAND ----------

if inventory:
    (spark.createDataFrame([tuple(r[c] for c in validate.INVENTORY_COLUMNS) for r in inventory],
                           schema=validate.INVENTORY_SCHEMA)
     .write.mode("append").option("mergeSchema", "true").saveAsTable(f"{schema}.inventory"))
if results:
    (spark.createDataFrame([tuple(r[c] for c in validate.RESULTS_COLUMNS) for r in results],
                           schema=validate.RESULTS_SCHEMA)
     .write.mode("append").option("mergeSchema", "true").saveAsTable(f"{schema}.results"))
spark.createDataFrame(scores).write.mode("append").option("mergeSchema", "true").saveAsTable(f"{schema}.compliance")

# COMMAND ----------

# MAGIC %md ## 5. Act: one report per scope, for the workflow's issues

# COMMAND ----------

(folder / "report").mkdir(parents=True, exist_ok=True)
for scope, md in reports.items():
    (folder / "report" / f"{scope}.md").write_text(md, encoding="utf-8")
(folder / "report" / "summary.json").write_text(json.dumps(report.summary(scores), indent=2), encoding="utf-8")
for scope in reports:
    print(reports[scope])

# COMMAND ----------

# MAGIC %md ## This run at a glance

# COMMAND ----------

display(spark.table(f"{schema}.compliance").where(f"run_id = '{run_id}'")
        .select("environment", "compliant", "score", "passed", "failed", "exceptions", "errors", "worst", "owner"))

# COMMAND ----------

# Which source found what: checks per scope and source.
display(spark.sql(f"""
SELECT environment, source, count(*) AS checks, count_if(status = 'PASS') AS passed,
       count_if(status = 'FAIL') AS failed, count_if(status = 'EXCEPTION') AS exceptions,
       count_if(status = 'ERROR') AS errors
FROM {schema}.results WHERE run_id = '{run_id}' GROUP BY ALL ORDER BY environment, source"""))

# COMMAND ----------

display(spark.table(f"{schema}.results").where(f"run_id = '{run_id}' AND status <> 'PASS'")
        .select("environment", "source", "rule_id", "status", "severity", "object", "principal", "evidence",
                "remediation_sql")
        .orderBy("environment", "rule_id"))
