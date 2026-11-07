# Databricks notebook source
# MAGIC %md
# MAGIC # Workspace governance: validate centrally
# MAGIC
# MAGIC Runs in the **governance workspace** as `sp-dbx-governance`, started by the governance workflow
# MAGIC (`.github/workflows/governance.yml`) after its collectors, with the GitHub run ID.
# MAGIC
# MAGIC 1. Reads the run's snapshots from the volume (one JSON per workspace) and the standard they are checked
# MAGIC    against: the `governance.yml` uploaded with them, or the deployed copy when there is none.
# MAGIC 2. Builds one inventory and checks every workspace against the standard (`src/govern/rules.py`).
# MAGIC 3. Appends to `estate.inventory` (observed objects), `estate.results` (one row per rule evaluation) and
# MAGIC    `estate.compliance` (one scorecard per workspace and run).
# MAGIC 4. Writes `report/<environment>.md` and `report/summary.json` next to the snapshots: the workflow turns them
# MAGIC    into one issue per workspace.
# MAGIC
# MAGIC It never calls a dev or prod API: observation happened in the collectors; judgement happens here.
# MAGIC To check an earlier run again: **Run now with different parameters** › `run_id` = that run's GitHub run ID.

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

from govern import config, rules, validate

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
print(f"run {run_id}: {len(snapshots)} snapshots from {folder}; standard: "
      f"{'the run' if cfg is not deployed else 'deployed'} copy; {len(rules.RULES)} rules")

# COMMAND ----------

# MAGIC %md ## 2. Judge: every workspace against the standard

# COMMAND ----------

inventory, results, scores, reports = validate.build(cfg, run_id, datetime.now(timezone.utc), snapshots)
print(f"{len(inventory)} objects observed; {len(results)} checks")

# COMMAND ----------

# MAGIC %md ## 3. Record: inventory, results, compliance

# COMMAND ----------

if inventory:
    (spark.createDataFrame([tuple(r[c] for c in validate.INVENTORY_COLUMNS) for r in inventory],
                           schema=validate.INVENTORY_SCHEMA)
     .write.mode("append").saveAsTable(f"{schema}.inventory"))
if results:
    (spark.createDataFrame([tuple(r[c] for c in validate.RESULTS_COLUMNS) for r in results],
                           schema=validate.RESULTS_SCHEMA)
     .write.mode("append").saveAsTable(f"{schema}.results"))
spark.createDataFrame(scores).write.mode("append").option("mergeSchema", "true").saveAsTable(f"{schema}.compliance")

# COMMAND ----------

# MAGIC %md ## 4. Act: one report per workspace, for the workflow's issues

# COMMAND ----------

from govern import report

(folder / "report").mkdir(parents=True, exist_ok=True)
for env, md in reports.items():
    (folder / "report" / f"{env}.md").write_text(md, encoding="utf-8")
(folder / "report" / "summary.json").write_text(json.dumps(report.summary(scores), indent=2), encoding="utf-8")
for env in reports:
    print(reports[env])

# COMMAND ----------

# MAGIC %md ## This run at a glance

# COMMAND ----------

display(spark.table(f"{schema}.compliance").where(f"run_id = '{run_id}'")
        .select("environment", "compliant", "score", "passed", "failed", "exceptions", "errors", "worst", "owner"))

# COMMAND ----------

display(spark.table(f"{schema}.results").where(f"run_id = '{run_id}' AND status <> 'PASS'")
        .select("environment", "rule_id", "status", "severity", "object", "principal", "evidence", "remediation_sql")
        .orderBy("environment", "rule_id"))
