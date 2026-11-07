"""The audit side: who changed what by hand, from system.access.audit.

The plan shows what drifted, but not who changed it, and it cannot see an object Terraform never created. The audit
log covers both: every create, update or delete on a watched kind of object, by anyone who is not a pipeline identity.
For a manual grant change, this module also writes the SQL that undoes it (to review, never run automatically).
"""
import json
import re

from drift import config as cfgmod

CREATED_OUTSIDE = "created outside Terraform"
MANUAL_GRANT = "manual grant change"
DELETED_BY_HAND = "deleted by hand"
MANUAL_CHANGE = "manual change"

_WORD = re.compile(r"^[A-Za-z]+$")
# The request parameter that names the object, by the first one present.
_OBJECT_KEYS = ("securable_full_name", "name", "scope", "policy_id", "id", "warehouse_id", "settingKeyName",
                "workspaceConfKeys", "targetGroupName", "groupName", "targetUserName")
# Terraform resource types (the Databricks provider) and the audit service that records changes to them.
RESOURCE_SERVICE = {
    "databricks_catalog": "unityCatalog", "databricks_grants": "unityCatalog", "databricks_grant": "unityCatalog",
    "databricks_external_location": "unityCatalog", "databricks_storage_credential": "unityCatalog",
    "databricks_cluster_policy": "clusterPolicies", "databricks_permissions": "clusterPolicies",
    "databricks_secret_scope": "secrets", "databricks_secret_acl": "secrets",
    "databricks_sql_endpoint": "databrickssql", "databricks_workspace_conf": "workspace",
    "databricks_group_member": "accounts", "databricks_mws_permission_assignment": "accounts",
}
_SECURABLE_SQL = {"external_location": "EXTERNAL LOCATION", "storage_credential": "STORAGE CREDENTIAL"}


def _quote(name):
    return ".".join(f"`{part.replace('`', '``')}`" for part in name.split("."))


def _sql_list(values):
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)


def manual_changes_sql(cfg):
    """Watched, successful changes in the configured workspaces, by anyone but the pipeline identities."""
    watched = []
    for service, actions in cfg["audit"]["watched"].items():
        if not _WORD.match(service) or not all(_WORD.match(a) for a in actions):
            raise cfgmod.ConfigError(f"audit.watched.{service}: service and action names are letters only")
        watched.append(f"(service_name = '{service}' AND action_name IN ({_sql_list(actions)}))")
    workspaces = ", ".join(str(e["workspace_id"]) for e in cfg["environments"].values())
    return f"""SELECT event_time, workspace_id, user_identity.email AS who, service_name, action_name, request_params
FROM system.access.audit
WHERE event_date >= date_sub(current_date(), {int(cfg['audit']['lookback_days'])})
  AND workspace_id IN ({workspaces})
  AND response.status_code = 200
  AND user_identity.email NOT IN ({_sql_list(cfgmod.pipeline_ids(cfg))})
  AND ({' OR '.join(watched)})
ORDER BY event_time DESC"""


def object_name(params):
    return next((str(params[k]) for k in _OBJECT_KEYS if params.get(k)), "")


def revert_grants_sql(params):
    """The statements that undo an updatePermissions call: REVOKE what was added, GRANT back what was removed."""
    try:
        changes = json.loads(params.get("changes") or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(changes, dict):
        changes = changes.get("changes", [])
    kind = str(params.get("securable_type", "")).lower()
    on = "METASTORE" if kind == "metastore" else \
        f"{_SECURABLE_SQL.get(kind, kind.upper())} {_quote(params.get('securable_full_name', ''))}"
    out = []
    for ch in changes:
        who = "`" + ch.get("principal", "").replace("`", "``") + "`"   # one name, even with dots (an email)
        for priv in ch.get("add") or []:
            out.append(f"REVOKE {priv.replace('_', ' ')} ON {on} FROM {who};")
        for priv in ch.get("remove") or []:
            out.append(f"GRANT {priv.replace('_', ' ')} ON {on} TO {who};")
    return out


def classify(event):
    """(category, remediation, remediation_sql) for one manual change. event: a dict with the query's columns."""
    action, params = event["action_name"], event.get("request_params") or {}
    name = object_name(params) or "the object"
    if action == "updatePermissions":
        sql = revert_grants_sql(params)
        return (MANUAL_GRANT,
                f"Revert: if Terraform manages the grants on {name}, apply its stack; otherwise review and run the "
                "SQL. Or adopt: add the grant to the config.", "\n".join(sql))
    if action.startswith("create"):
        return (CREATED_OUTSIDE,
                f"Not in code, so the plan cannot see it. Adopt: add {name} to the config and import it "
                "(an import block). Or remove it.", "")
    if action.startswith(("delete", "remove")):
        return (DELETED_BY_HAND,
                f"If Terraform manages {name}, today's plan shows it and an apply recreates it.", "")
    return (MANUAL_CHANGE, f"Check today's plan for this workspace: revert {name} by applying, or adopt the change "
            "in code.", "")


def likely_causes(finding, environment, changes, limit=3):
    """Manual changes in the same workspace, on the same kind of object, that may explain a plan finding."""
    service = RESOURCE_SERVICE.get(finding["resource_type"])
    hits = [c for c in changes if c["environment"] == environment and c["service_name"] == service]
    return [f"{c['who']} {c['action_name']} {object_name(c.get('request_params') or {})} at {c['event_time']}"
            .replace("  ", " ") for c in hits[:limit]]
