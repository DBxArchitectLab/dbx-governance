# Databricks notebook source
# MAGIC %md
# MAGIC # Terraform drift: the audit side
# MAGIC
# MAGIC Runs in the **governance workspace** as `sp-dbx-governance`, started by the drift workflow
# MAGIC (`.github/workflows/drift.yml`) after its plans, with the GitHub run ID.
# MAGIC
# MAGIC 1. Reads the plan findings the workflow uploaded to the volume (one JSON per stack).
# MAGIC 2. Queries `system.access.audit` for manual changes to watched objects in the configured workspaces.
# MAGIC 3. Appends one row per finding to `drift.findings` (both sides, with the remediation), and one row per
# MAGIC    environment to `drift.health`, so drift can be measured over time.
# MAGIC
# MAGIC The audit log covers every workspace in the account, so this never calls the dev or prod APIs.
# MAGIC To re-read an earlier run's plans: **Run now with different parameters** › `run_id` = that run's GitHub run ID.

# COMMAND ----------

# MAGIC %pip install pyyaml --quiet

# COMMAND ----------

dbutils.widgets.text("run_id", "manual", "GitHub run ID")
dbutils.widgets.text("config", "", "Deployed config/drift.yml")

# COMMAND ----------

# The drift package is plain Python next to this notebook (src/drift), so it imports like any module.
import os
import sys

src = os.path.abspath(os.path.join(os.getcwd(), ".."))
if not os.path.isdir(os.path.join(src, "drift")):
    nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
    src = os.path.dirname(os.path.dirname(nb if nb.startswith("/Workspace") else "/Workspace" + nb))
sys.path.insert(0, src)

from datetime import datetime, timezone

from drift import audit, config, findings as findmod

# COMMAND ----------

# MAGIC %md ## 1. The plan side: this run's findings

# COMMAND ----------

run_id = dbutils.widgets.get("run_id")
cfg = config.load(dbutils.widgets.get("config") or os.path.join(os.path.dirname(src), "config", "drift.yml"))
out = cfg["output"]
schema = f"{out['catalog']}.{out['schema']}"
folder = f"/Volumes/{out['catalog']}/{out['schema']}/{out['volume']}/{run_id}"

reports = findmod.read_reports(folder)
print(f"{len(reports)} plan reports from {folder}")

# COMMAND ----------

# MAGIC %md ## 2. The audit side: manual changes in the last days

# COMMAND ----------

sql = audit.manual_changes_sql(cfg)
print(sql)
events = [r.asDict(recursive=True) for r in spark.sql(sql).collect()]
print(f"{len(events)} manual changes in the last {cfg['audit']['lookback_days']} days")

# COMMAND ----------

# MAGIC %md ## 3. Join both sides, and record

# COMMAND ----------

findings, health = findmod.build(cfg, run_id, datetime.now(timezone.utc), reports, events)
if findings:
    (spark.createDataFrame([tuple(f[c] for c in findmod.FINDINGS_COLUMNS) for f in findings],
                           schema=findmod.FINDINGS_SCHEMA)
     .write.mode("append").saveAsTable(f"{schema}.findings"))
spark.createDataFrame(health).write.mode("append").option("mergeSchema", "true").saveAsTable(f"{schema}.health")

for h in health:
    print(f"{h['environment']}: {'in sync' if h['in_sync'] else 'NOT in sync'} | stacks clean "
          f"{h['stacks_clean']}/{h['stacks_expected']}, drift {h['stacks_drift']}, not applied "
          f"{h['stacks_not_applied']}, error {h['stacks_error']}, missing {h['stacks_missing']} | manual changes "
          f"{h['manual_changes']} (created outside Terraform {h['created_outside']})")
for f in findings:
    print(f"- [{f['severity']}] {f['environment']} {f['source']}: {f['object']} | {f['category']}"
          + (f" | by {f['who']}" if f["who"] else "") + f" | {f['remediation']}")
    if f["remediation_sql"]:
        print("  " + f["remediation_sql"].replace("\n", "\n  "))

# COMMAND ----------

# MAGIC %md ## This run at a glance

# COMMAND ----------

display(spark.table(f"{schema}.health").where(f"run_id = '{run_id}'")
        .select("environment", "in_sync", "stacks_clean", "stacks_expected", "stacks_drift", "stacks_not_applied",
                "stacks_error", "stacks_missing", "manual_changes", "created_outside"))

# COMMAND ----------

display(spark.table(f"{schema}.findings").where(f"run_id = '{run_id}'")
        .select("environment", "source", "object", "category", "who", "likely_cause", "remediation",
                "remediation_sql")
        .orderBy("environment", "source"))
