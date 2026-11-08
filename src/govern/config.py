"""config/governance.yml: load it, check it, and turn the estate into the governance workflow's job list."""
import re
from datetime import date
from pathlib import Path

import yaml

TIERS = ("nonprod", "prod")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class ConfigError(ValueError):
    pass


def load(path):
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    validate(cfg)
    return cfg


def validate(cfg):
    """Fail early, with the field that is wrong: a typo here would silently leave a workspace ungoverned."""
    for key in ("estate", "standard", "account", "system_tables", "output"):
        if not cfg.get(key):
            raise ConfigError(f"missing section: {key}")
    for env, e in cfg["estate"].items():
        where = f"estate.{env}"
        if not _NAME.match(env):
            raise ConfigError(f"{where}: lowercase letters, digits and hyphens only")
        if not isinstance(e.get("workspace_id"), int):
            raise ConfigError(f"{where}.workspace_id must be the numeric workspace ID")
        if not str(e.get("host", "")).startswith("https://"):
            raise ConfigError(f"{where}.host must be the workspace URL (https://...)")
        if not e.get("catalog"):
            raise ConfigError(f"{where}.catalog is missing: the environment's own catalog")
        if e.get("tier") not in TIERS:
            raise ConfigError(f"{where}.tier must be one of {', '.join(TIERS)}")
        if not e.get("owner"):
            raise ConfigError(f"{where}.owner is missing: the group that remediates")
        if not e.get("storage_roots") or not all(str(r).startswith("abfss://") for r in e["storage_roots"]):
            raise ConfigError(f"{where}.storage_roots: one or more abfss:// roots")
        if not e.get("sql_warehouse"):
            raise ConfigError(f"{where}.sql_warehouse is missing: the warehouse the information schema queries run on")
    ids = [e["workspace_id"] for e in cfg["estate"].values()]
    if len(ids) != len(set(ids)):
        raise ConfigError("estate: the same workspace_id appears twice")
    if "account" in cfg["estate"]:
        raise ConfigError("estate: 'account' is reserved for the account scope")

    acc = cfg["account"]
    if not str(acc.get("host", "")).startswith("https://"):
        raise ConfigError("account.host must be the account console URL (https://accounts.azuredatabricks.net)")
    if not acc.get("owner"):
        raise ConfigError("account.owner is missing: the group that remediates account findings")
    for i, x in enumerate(acc.get("not_governed") or []):
        if not isinstance(x.get("workspace_id"), int) or not x.get("reason"):
            raise ConfigError(f"account.not_governed[{i}]: a numeric workspace_id and a reason")
        if x["workspace_id"] in ids:
            raise ConfigError(f"account.not_governed[{i}]: workspace {x['workspace_id']} is in the estate")
    if not isinstance(cfg["system_tables"].get("lookback_days"), int) or cfg["system_tables"]["lookback_days"] < 1:
        raise ConfigError("system_tables.lookback_days must be a positive number of days")

    std = cfg["standard"]
    for key in ("approved_groups", "service_principals", "admin_groups", "high_risk_privileges", "shared_catalogs",
                "schema_name_pattern", "compute", "workspace_settings"):
        if key not in std:
            raise ConfigError(f"standard.{key} is missing")
    for sp in std["service_principals"]:
        if not _GUID.match(str(sp.get("application_id", ""))):
            raise ConfigError(f"standard.service_principals: {sp.get('name')!r} needs its application ID (a GUID)")
    try:
        re.compile(std["schema_name_pattern"])
    except re.error as exc:
        raise ConfigError(f"standard.schema_name_pattern is not a valid regular expression: {exc}") from None
    for key in ("required_tags", "max_autotermination_minutes", "max_warehouse_auto_stop_minutes"):
        if key not in std["compute"]:
            raise ConfigError(f"standard.compute.{key} is missing")
    for tier in std["workspace_settings"]:
        if tier not in TIERS:
            raise ConfigError(f"standard.workspace_settings.{tier}: tiers are {', '.join(TIERS)}")

    for i, x in enumerate(cfg.get("exceptions") or []):
        where = f"exceptions[{i}]"
        for key in ("rule", "environment", "object", "reason", "approved_by", "expires"):
            if not x.get(key):
                raise ConfigError(f"{where}.{key} is missing: every exception is explicit and time-bound")
        if not isinstance(x["rule"], (str, list)):
            raise ConfigError(f"{where}.rule: a rule ID, or a list of them")
        if not isinstance(x["expires"], date):
            raise ConfigError(f"{where}.expires must be a date (YYYY-MM-DD)")
        if x["environment"] not in ("*", "account") and x["environment"] not in cfg["estate"]:
            raise ConfigError(f"{where}.environment {x['environment']!r} is not in the estate")

    for key in ("catalog", "schema", "volume"):
        if not cfg["output"].get(key):
            raise ConfigError(f"output.{key} is missing")


def matrix(cfg):
    """The GitHub Actions matrix: {"include": [...]}, one collector job per workspace."""
    return {"include": [{"environment": env, "workspace_id": e["workspace_id"], "host": e["host"], "tier": e["tier"]}
                        for env, e in cfg["estate"].items()]}


def scopes(cfg):
    """The remediation matrix: every workspace in the estate, and the account. One issue each."""
    return {"include": [{"environment": env} for env in cfg["estate"]] + [{"environment": "account"}]}


def service_principal_ids(cfg):
    return {sp["application_id"] for sp in cfg["standard"]["service_principals"]}
