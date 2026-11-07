"""One stack's Terraform plan (terraform show -json) turned into findings.

terraform plan -detailed-exitcode answers yes or no: exit code 0 = code and reality agree, 2 = changes, 1 = error.
The JSON plan says which changes and why, and tells two different stories apart:
- resource_drift: what changed in reality since the last apply (someone clicked); the plan reverts it.
- resource_changes without drift: the code changed and was never applied (a merge without a deploy).

Only names leave this module: addresses and attribute names, never values. A plan can hold secrets.
"""
CHANGED_OUTSIDE = "changed outside Terraform"
DELETED_OUTSIDE = "deleted outside Terraform"
NOT_APPLIED = "code not applied"
DRIFT_CATEGORIES = (CHANGED_OUTSIDE, DELETED_OUTSIDE)


def _changed_attributes(before, after):
    before, after = before or {}, after or {}
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def _action(actions):
    if actions == ["no-op"] or actions == ["read"]:
        return None
    if set(actions) == {"delete", "create"}:
        return "replace"
    return actions[0]


def findings(plan):
    """Findings for every resource the plan would change, each with its category and the attributes involved."""
    drift = {d["address"]: d for d in plan.get("resource_drift", [])}
    out = []
    for rc in plan.get("resource_changes", []):
        action = _action(rc["change"]["actions"])
        if action is None:
            continue
        d = drift.get(rc["address"])
        if d is None:
            category, attrs = NOT_APPLIED, _changed_attributes(rc["change"].get("before"), rc["change"].get("after"))
        elif "delete" in d["change"]["actions"]:
            category, attrs = DELETED_OUTSIDE, []
        else:
            category, attrs = CHANGED_OUTSIDE, _changed_attributes(d["change"].get("before"), d["change"].get("after"))
        out.append({"address": rc["address"], "resource_type": rc["type"], "planned_action": action,
                    "category": category, "attributes": attrs})
    return out


def status(exit_code, found):
    """clean, drift, not_applied or error, from the plan's exit code and its findings."""
    if exit_code == 0:
        return "clean"
    if exit_code != 2:
        return "error"
    return "drift" if any(f["category"] in DRIFT_CATEGORIES for f in found) else "not_applied"


def remediation(stack_path, finding):
    """What to do about one plan finding: revert (apply the code) or adopt (change the code), never both."""
    if finding["category"] == NOT_APPLIED:
        return f"Deploy: apply {stack_path}; the code is ahead of the workspace."
    what = "recreates it" if finding["category"] == DELETED_OUTSIDE else "puts it back"
    return (f"Revert: apply {stack_path} ({what}). Or adopt: change the config in {stack_path}, merge, apply. "
            "Then the next plan is clean.")


def report(stack, exit_code, plan, run):
    """The document the drift workflow uploads for one stack. stack: config.Stack; run: GitHub run details."""
    found = findings(plan) if plan else []
    for f in found:
        f["remediation"] = remediation(stack.path, f)
    return {"environment": stack.environment, "stack": stack.stack, "path": stack.path,
            "workspace_id": stack.workspace_id, "severity": stack.severity, "exit_code": exit_code,
            "status": status(exit_code, found), "findings": found, **run}
