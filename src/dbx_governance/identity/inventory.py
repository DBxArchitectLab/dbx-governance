"""Unity Catalog tables that track the workspace group migration.

  local_groups             one row per workspace-local group per run: its account group, members, status
  local_group_members      one row per member of each local group per run
  migration_actions        one row per step per group per run (planned in plan mode, done in apply mode)
  account_group_registry   one row per account group this migration created; the source of truth for which
                           account groups the migration may reuse

All four live in <catalog>.<schema> from the config. Rows are only appended, so the tables are the history of every
run; filter on run_id, or take the latest run_at.
"""
from __future__ import annotations

from datetime import datetime

from .group_migration import GroupPlan

DDL = {
    "local_groups": """
        run_id STRING, run_at TIMESTAMP, mode STRING, env STRING, workspace_id BIGINT,
        local_group_id STRING, local_group STRING, account_group STRING, account_group_id STRING,
        member_count INT, nested_local_groups ARRAY<STRING>, entitlements ARRAY<STRING>,
        status STRING, problems ARRAY<STRING>""",
    "local_group_members": """
        run_id STRING, run_at TIMESTAMP, env STRING, workspace_id BIGINT, local_group STRING,
        member_kind STRING, member_key STRING, nested_local_group BOOLEAN""",
    "migration_actions": """
        run_id STRING, run_at TIMESTAMP, mode STRING, env STRING, workspace_id BIGINT, local_group STRING,
        account_group STRING, seq INT, step STRING, detail STRING""",
    "account_group_registry": """
        account_group STRING, account_group_id STRING, env STRING, workspace_id BIGINT,
        source_local_group STRING, created_by_run_id STRING, created_at TIMESTAMP""",
}


def status(plan: GroupPlan, mode: str) -> str:
    if plan.blocked:
        return "blocked"
    if mode == "plan":
        return "planned"
    return "local_group_deleted" if any(a.step == "delete_local" for a in plan.actions) else "migrated"


class Inventory:
    def __init__(self, spark, catalog: str, schema: str):
        self.spark = spark
        self.prefix = f"`{catalog}`.`{schema}`"

    def table(self, name: str) -> str:
        return f"{self.prefix}.`{name}`"

    def ensure_tables(self) -> None:
        self.spark.sql(f"CREATE SCHEMA IF NOT EXISTS {self.prefix}")
        for name, columns in DDL.items():
            self.spark.sql(f"CREATE TABLE IF NOT EXISTS {self.table(name)} ({columns}) USING DELTA")

    def owned_account_groups(self) -> set[str]:
        rows = self.spark.sql(f"SELECT DISTINCT account_group FROM {self.table('account_group_registry')}").collect()
        return {r.account_group for r in rows}

    def _append(self, name: str, rows: list[tuple]) -> None:
        if rows:
            columns = ", ".join(c.strip() for c in DDL[name].split(","))
            self.spark.createDataFrame(rows, schema=columns).write.mode("append").saveAsTable(self.table(name))

    def register(self, plan: GroupPlan, account_group_id: str, run_id: str, at: datetime) -> None:
        self._append("account_group_registry", [(plan.target_name, account_group_id, plan.env, plan.workspace_id,
                                                 plan.local_name, run_id, at)])

    def write_run(self, plans: list[GroupPlan], run_id: str, at: datetime, mode: str) -> None:
        self._append("local_groups", [
            (run_id, at, mode, p.env, p.workspace_id, p.local_id, p.local_name, p.target_name, p.account_group_id,
             len(p.members), p.nested_local_groups, p.entitlements, status(p, mode), p.problems)
            for p in plans])
        self._append("local_group_members", [
            (run_id, at, p.env, p.workspace_id, p.local_name, m.kind, m.key, m.local_group)
            for p in plans for m in p.members])
        self._append("migration_actions", [
            (run_id, at, mode, p.env, p.workspace_id, p.local_name, p.target_name, i, a.step, a.detail)
            for p in plans for i, a in enumerate(p.actions, 1)])
