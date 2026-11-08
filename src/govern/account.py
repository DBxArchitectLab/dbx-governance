"""Layer 1, observe, for the whole account: the account APIs, once per run, not once per workspace.

  metastore_assignment   every workspace assigned to each metastore in the account (and its name, when the account
                         lists workspaces): which workspaces exist, governed or not (EST-01, EST-02)
  workspace_assignment   who is assigned to each estate workspace, and with which permission, ADMIN or USER (IAM-04)

Only the account sees these: a workspace knows neither the other workspaces nor how its admins were assigned.
`call(method, path, query=None, body=None)` is the account's REST API (the CLI passes the Databricks SDK's
AccountClient api_client.do). Read-only: GET requests.
"""
from datetime import datetime, timezone

ACCOUNT_API = "account_api"


def _principal(p):
    for key, ptype in (("service_principal_name", "service_principal"), ("group_name", "group"),
                       ("user_name", "user")):
        if p.get(key):
            return p[key], ptype
    return p.get("display_name") or str(p.get("principal_id", "")), "unknown"


def snapshot(call, cfg, account_id, now=None):
    base = f"/api/2.0/accounts/{account_id}"
    observations, errors = [], []

    def failed(domain, exc, **extra):
        errors.append({"domain": domain, "source": ACCOUNT_API, "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                       **extra})

    names = {}
    try:    # names are a convenience: the evidence reads better with them, nothing depends on them
        names = {int(w["workspace_id"]): w.get("workspace_name", "") for w in call("GET", f"{base}/workspaces") or []}
    except Exception:  # noqa: BLE001
        pass
    try:
        for m in (call("GET", f"{base}/metastores") or {}).get("metastores") or []:
            ids = (call("GET", f"{base}/metastores/{m['metastore_id']}/workspaces") or {}).get("workspace_ids") or []
            for ws in ids:
                observations.append({"object_type": "metastore_assignment", "name": str(ws), "owner": None,
                                     "source": ACCOUNT_API,
                                     "attrs": {"workspace_id": int(ws), "workspace_name": names.get(int(ws), ""),
                                               "metastore_id": m["metastore_id"],
                                               "metastore_name": m.get("name", m["metastore_id"]),
                                               "region": m.get("region", "")}})
    except Exception as exc:  # noqa: BLE001
        failed("metastore_assignments", exc)

    for env, e in cfg["estate"].items():
        ws = e["workspace_id"]
        try:
            page = call("GET", f"{base}/workspaces/{ws}/permissionassignments") or {}
            for a in page.get("permission_assignments") or []:
                principal, ptype = _principal(a.get("principal") or {})
                observations.append({"object_type": "workspace_assignment", "name": f"{ws}:{principal}",
                                     "owner": None, "source": ACCOUNT_API,
                                     "attrs": {"workspace_id": ws, "principal": principal, "principal_type": ptype,
                                               "permissions": sorted(a.get("permissions") or [])}})
        except Exception as exc:  # noqa: BLE001
            failed("permission_assignments", exc, workspace_id=ws)

    return {"scope": "account", "environment": "account", "workspace_id": None,
            "collected_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            "observations": observations, "errors": errors}
